"""QR sharing of an assignment's student link: /teacher/assignment/<id>/qr.svg.

The QR code encodes the canonical ``<public base URL>/quiz/s/<public_token>`` and
nothing else. The endpoint is owner/admin only, takes no input that changes what is
encoded, writes nothing, and fails closed without a valid token or a trusted public
base URL. Everything runs on the class's temporary database and vault.
"""

import hashlib
import re
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import segno

from tests import test_assignment_public_tokens as token_tests
from tests.support import ROOT, web_app
from tests.support import NO_DEADLINE_FORM
from web import public_url
from web.services import qr

PUBLIC_BASE = "https://learnpilot.test"
SVG_NS = "{http://www.w3.org/2000/svg}"
SHARING_UNAVAILABLE_NOTE = "Линкът за ученици и QR кодът не са налични"


def svg_modules(svg: bytes) -> list[list[bool]]:
    """The dark-module grid (quiet zone included) drawn by a QR SVG.

    Reads the document itself: a white square covering the whole viewBox and one
    stroked path of horizontal runs (``M x y``, ``h length``, ``m dx dy``).
    """
    root = ET.fromstring(svg)
    _, _, width, height = (float(part) for part in root.attrib["viewBox"].split())
    group = root.find(f"{SVG_NS}g")
    scale = float(re.fullmatch(r"scale\(([\d.]+)\)", group.attrib["transform"]).group(1))
    size = int(width / scale)
    assert width == height and size * scale == width
    background, line = group.findall(f"{SVG_NS}path")
    assert background.attrib == {"fill": "#fff", "d": f"M0 0h{size}v{size}h-{size}z"}
    assert line.attrib["stroke"] == "#000"

    grid = [[False] * size for _ in range(size)]
    x = y = 0.0
    commands = re.findall(r"([Mmh])(-?[\d.]+)(?: (-?[\d.]+))?", line.attrib["d"])
    assert "".join(c + a + (" " + b if b else "") for c, a, b in commands) == line.attrib["d"]
    for command, first, second in commands:
        if command == "M":
            x, y = float(first), float(second)
        elif command == "m":
            x, y = x + float(first), y + float(second)
        else:
            for column in range(int(x), int(x + float(first))):
                assert not grid[int(y)][column]
                grid[int(y)][column] = True
            x += float(first)
    return grid


def expected_modules(text: str) -> list[list[bool]]:
    """The symbol segno builds for ``text``, padded with the 4-module quiet zone."""
    code = segno.make(text, error="m", boost_error=True, micro=False)
    return [[bool(cell) for cell in row] for row in code.matrix_iter(scale=1, border=4)]


class PublicBaseUrlFormatTest(unittest.TestCase):
    def test_accepted_values_are_normalized(self):
        for value, expected in (
            ("https://learnpilot.bg", "https://learnpilot.bg"),
            ("https://learnpilot.bg/", "https://learnpilot.bg"),
            ("  HTTPS://LearnPilot.BG  ", "https://learnpilot.bg"),
            ("http://127.0.0.1:5001", "http://127.0.0.1:5001"),
            ("https://abc-def.trycloudflare.com", "https://abc-def.trycloudflare.com"),
            ("http://[::1]:5001", "http://[::1]:5001"),
        ):
            with self.subTest(value=value):
                self.assertEqual(public_url.normalize_public_base_url(value), expected)

    def test_everything_else_is_refused(self):
        for value in (
            None, 5, b"https://learnpilot.bg", "", "   ", "learnpilot.bg", "//learnpilot.bg",
            "ftp://learnpilot.bg", "javascript:alert(1)", "https://", "https:///quiz",
            "https://learnpilot.bg/quiz", "https://learnpilot.bg/?x=1", "https://learnpilot.bg?",
            "https://learnpilot.bg#", "https://learnpilot.bg#f", "https://user@learnpilot.bg",
            "https://user:pw@learnpilot.bg", "https://learnpilot.bg:port", "https://learnpilot.bg:99999",
            "https://learn pilot.bg", "https://learnpilot.bg\nX", "https://learnpilot.bg\r\nX: y",
            "https://learnpilot.bg\\@evil.example", "https://-learnpilot.bg", "https://.learnpilot.bg",
            "https://учи.бг", "https://learnpilot.bg%2fevil", "https://[::1", "https://[zz::1]",
        ):
            with self.subTest(value=value):
                self.assertIsNone(public_url.normalize_public_base_url(value))

    def test_production_base_is_https_on_the_public_host(self):
        self.assertEqual(public_url.public_base_url_for_host(" LearnPilot.BG "), "https://learnpilot.bg")
        for value in (None, "", "localhost", "learnpilot.bg:443", "examp\u017fe.com", "127.1"):
            with self.subTest(value=value):
                self.assertIsNone(public_url.public_base_url_for_host(value))


VALID_PUBLIC_HOSTS = (
    ("learnpilot.bg", "learnpilot.bg"),
    ("learnpilot.example.com", "learnpilot.example.com"),
    ("  LearnPilot.Example.COM\n", "learnpilot.example.com"),
    ("abc-def.trycloudflare.com", "abc-def.trycloudflare.com"),
    ("a.b", "a.b"),
    ("1st.school-9.example", "1st.school-9.example"),
    ("xn--e1aybc.xn--90ae", "xn--e1aybc.xn--90ae"),  # тест.бг
    ("XN--Strae-OQA.de", "xn--strae-oqa.de"),  # straße.de
    (f'{"a" * 63}.example.com', f'{"a" * 63}.example.com'),
)
INVALID_PUBLIC_HOSTS = (
    None, 5, b"learnpilot.bg", "", "   ", "localhost", "learnpilot", "https://school.example",
    "*.school.example", "school.example:443", "school.example/evil", "school.example.", ".school.example",
    "a..b", "-a.example", "a-.example", "a.-b", "a.b-", "school_1.example", "user@school.example",
    "school.example?x", "school .example", "school.example\r\nX: y", "[::1]", "::1",
    f'{"a" * 64}.example.com', ".".join(["a" * 60] * 5),
    # Not ASCII, although Unicode case folding makes each of these match [a-z].
    "examp\u017fe.com", "ex\u0131mple.com", "\u0130stanbul.com", "\u212aelvin.example.com",
    "уча.бг", "learnpilot.b\uff47", "learnpilot\u3002bg", "learn\u00adpilot.bg", "learnpilot.bg\u200b",
    # Numeric-looking: a URL parser reads an IPv4 address, or nothing at all.
    "127.0.0.1", "1.2.3.4", "1.2", "127.1", "0.0", "08.09", "999.999.999.999", "1.2.3.4.5", "a.1",
    "0x7f.1", "school.0x1f", "school.0x", "2130706433.0",
    # Malformed IDNA A-labels.
    "xn--zz.example.com", "xn--a.com", "xn--999.example", "school.xn--0", "xn--a-.example.com",
)


class PublicHostContractTest(unittest.TestCase):
    """One definition of a usable ``DZI_PUBLIC_HOST`` for startup and for links."""

    def startup_accepts(self, value) -> bool:
        from web.production import production_config_errors

        environ = {} if value is None else {"DZI_PUBLIC_HOST": value}
        return not any("DZI_PUBLIC_HOST" in error for error in production_config_errors(environ))

    def test_valid_hosts_are_normalized_and_always_yield_the_https_base(self):
        for value, expected in VALID_PUBLIC_HOSTS:
            with self.subTest(value=value):
                self.assertEqual(public_url.normalize_public_host(value), expected)
                self.assertTrue(self.startup_accepts(value))
                base = public_url.public_base_url_for_host(value)
                self.assertEqual(base, f"https://{expected}")
                self.assertEqual(public_url.normalize_public_base_url(base), base)

    def test_invalid_hosts_are_refused_by_startup_and_by_link_building_alike(self):
        for value in INVALID_PUBLIC_HOSTS:
            with self.subTest(value=value):
                self.assertIsNone(public_url.normalize_public_host(value))
                self.assertIsNone(public_url.public_base_url_for_host(value))
                if value is None or isinstance(value, str):
                    self.assertFalse(self.startup_accepts(value))

    def test_startup_and_link_building_never_disagree(self):
        alphabet = ["a", "Z", "0", "9", "-", ".", "x", "n", "\u017f", "\u0131", "\u0130", "\u212a", "б", " ", ":", "/"]
        seen = accepted = 0
        for first in alphabet:
            for second in alphabet:
                for third in alphabet:
                    for value in (f"{first}{second}{third}", f"{first}{second}.{third}c", f"xn--{first}{second}{third}.bg",
                                  f"{first}.{second}{third}", f"lp.{first}{second}{third}"):
                        seen += 1
                        host = public_url.normalize_public_host(value)
                        self.assertEqual(self.startup_accepts(value), host is not None, ascii(value))
                        if host is None:
                            continue
                        accepted += 1
                        self.assertRegex(host, r"\A[a-z0-9.-]+\Z")
                        base = public_url.public_base_url_for_host(value)
                        self.assertEqual(public_url.normalize_public_base_url(base), f"https://{host}", ascii(value))
        self.assertGreater(accepted, 100)
        self.assertGreater(seen - accepted, 100)

    def test_production_startup_uses_the_shared_contract(self):
        source = (ROOT / "web" / "production.py").read_text(encoding="utf-8")
        self.assertIn("normalize_public_host(environ.get(PUBLIC_HOST_ENV))", source)
        self.assertIn("public_host = normalize_public_host(environ[PUBLIC_HOST_ENV])", source)
        self.assertNotIn("import re", source)  # no second hostname pattern beside the shared one


class QrSvgTest(unittest.TestCase):
    URL = f"{PUBLIC_BASE}/quiz/s/{'aZ0_-' + 'A' * 38}"

    def test_svg_draws_exactly_the_symbol_of_the_text(self):
        svg = qr.qr_svg(self.URL)
        self.assertEqual(svg_modules(svg), expected_modules(self.URL))
        self.assertNotEqual(svg_modules(svg), expected_modules(self.URL[:-1] + "B"))

    def test_symbol_is_a_full_qr_code_with_a_quiet_zone(self):
        grid = svg_modules(qr.qr_svg(self.URL))
        size, zone = len(grid), qr.QR_QUIET_ZONE_MODULES
        self.assertGreaterEqual(zone, 4)
        self.assertEqual((size - 2 * zone - 17) % 4, 0)  # a QR version, not a Micro QR
        for index in range(size):
            for offset in (*range(zone), *range(size - zone, size)):
                self.assertFalse(grid[index][offset] or grid[offset][index])
        finder = ["1111111", "1000001", "1011101", "1011101", "1011101", "1000001", "1111111"]
        for top, left in ((zone, zone), (zone, size - zone - 7), (size - zone - 7, zone)):
            rows = ["".join("1" if cell else "0" for cell in row[left:left + 7]) for row in grid[top:top + 7]]
            self.assertEqual(rows, finder)

    def test_error_correction_is_at_least_medium(self):
        with patch.object(qr.segno, "make", wraps=segno.make) as spy:
            qr.qr_svg(self.URL)
        spy.assert_called_once_with(self.URL, error="m", boost_error=True, micro=False)
        self.assertIn(segno.make(self.URL, error="m", boost_error=True, micro=False).error, ("M", "Q", "H"))

    def test_output_is_deterministic(self):
        self.assertEqual(qr.qr_svg(self.URL), qr.qr_svg(self.URL))

    def test_svg_is_self_contained_and_inert(self):
        svg = qr.qr_svg(self.URL)
        text = svg.decode("utf-8")
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="utf-8"?>\n<svg '))
        root = ET.fromstring(svg)
        self.assertEqual(root.tag, f"{SVG_NS}svg")
        self.assertEqual({element.tag for element in root.iter()}, {f"{SVG_NS}svg", f"{SVG_NS}g", f"{SVG_NS}path"})
        for element in root.iter():
            for name in element.attrib:
                self.assertIn(name, {"viewBox", "transform", "fill", "stroke", "d"})
        lowered = text.lower()
        for forbidden in ("<script", "<style", "<image", "<use", "<foreignobject", "<a ", "href", "xlink",
                          "onload", "onclick", "javascript:", "data:", "url(", "<!doctype", "<!entity",
                          "learnpilot", "/quiz/"):
            self.assertNotIn(forbidden, lowered)
        self.assertEqual(re.findall(r"https?://[^\s\"']+", text), ["http://www.w3.org/2000/svg"])

    def test_svg_needs_nothing_a_strict_content_security_policy_would_block(self):
        # "default-src 'none'" forbids inline styles, style sheets, fonts, images and
        # scripts. The symbol is drawn with presentation attributes alone, so the
        # header on the response cannot blank it, in an <img> or opened on its own.
        root = ET.fromstring(qr.qr_svg(self.URL))
        ns = "{http://www.w3.org/2000/svg}"
        self.assertEqual([element.tag for element in root.iter()], [f"{ns}svg", f"{ns}g", f"{ns}path", f"{ns}path"])
        attributes = {name for element in root.iter() for name in element.attrib}
        self.assertEqual(attributes, {"viewBox", "transform", "fill", "stroke", "d"})
        # Intrinsic square size from the viewBox: an <img> can lay it out without CSS.
        _, _, width, height = root.get("viewBox").split()
        self.assertEqual(width, height)


class QrSharingTestCase(token_tests.PublicTokenAppTestCase):
    def setUp(self):
        self.owner = self.account("teacher", display_name="Собственик")
        self.assignment_id = self.create(self.owner)
        self.public_token = self.token(self.assignment_id)
        self.path = f"/teacher/assignment/{self.assignment_id}/qr.svg"
        config = patch.dict(web_app.app.config, {public_url.PUBLIC_BASE_URL_CONFIG: PUBLIC_BASE})
        config.start()
        self.addCleanup(config.stop)

    @property
    def student_url(self):
        return f"{PUBLIC_BASE}/quiz/s/{self.public_token}"

    def encoded(self, client, path=None, **kwargs):
        """The response, and every text handed to the QR encoder while serving it."""
        with patch.object(qr.segno, "make", wraps=segno.make) as spy:
            response = client.get(path or self.path, **kwargs)
        return response, [call.args[0] for call in spy.call_args_list]

    def foreign_host_client(self, account):
        """A client whose session cookie is sent whatever ``Host`` a request names
        (the test client's cookie jar would otherwise keep it for localhost only)."""
        cookie = self.client(account).get_cookie("session").value
        client = web_app.app.test_client(use_cookies=False)
        client.environ_base["HTTP_COOKIE"] = f"session={cookie}"
        return client

    def with_token(self, value):
        assignment_id = self.legacy(self.owner)
        if value is not None:
            with web_app.quiz_db() as conn:
                conn.execute("UPDATE quiz_assignments SET public_token = ? WHERE id = ?", (value, assignment_id))
        return assignment_id


class QrAuthorizationTest(QrSharingTestCase):
    def test_owner_views_the_qr_inline(self):
        response, texts = self.encoded(self.client(self.owner))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(texts, [self.student_url])
        self.assertEqual(response.headers["Content-Type"], "image/svg+xml; charset=utf-8")
        self.assertEqual(response.headers["Content-Disposition"],
                         f'inline; filename="assignment_{self.assignment_id}_qr.svg"')
        self.assertEqual(svg_modules(response.data), expected_modules(self.student_url))

    def test_owner_downloads_the_same_qr(self):
        client = self.client(self.owner)
        inline = client.get(self.path)
        response, texts = self.encoded(client, self.path + "?download=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(texts, [self.student_url])
        self.assertEqual(response.headers["Content-Type"], "image/svg+xml; charset=utf-8")
        self.assertEqual(response.headers["Content-Disposition"],
                         f'attachment; filename="assignment_{self.assignment_id}_qr.svg"')
        self.assertEqual(response.data, inline.data)

    def test_admin_reaches_any_assignments_qr(self):
        response, texts = self.encoded(self.client(self.account("admin")))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(texts, [self.student_url])

    def assert_refused(self, client, status, location=None):
        for path in (self.path, self.path + "?download=1"):
            with self.subTest(path=path):
                response, texts = self.encoded(client, path)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers.get("Location"), location and f"/login?next={self.path}")
                self.assertEqual(texts, [])  # refused before anything is generated
                self.assertNotIn(b"<svg", response.data)
                self.assertNotIn(self.public_token.encode(), response.data)
                self.assertNotIn(self.public_token, str(response.headers))

    def test_another_teacher_gets_the_same_404_as_a_missing_assignment(self):
        other = self.client(self.account("teacher"))
        self.assert_refused(other, 404)
        missing = other.get("/teacher/assignment/987654/qr.svg")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.data, other.get(self.path).data)

    def test_student_is_forbidden(self):
        self.assert_refused(self.client(self.account("student")), 403)

    def test_anonymous_is_sent_to_sign_in(self):
        self.assert_refused(self.client(), 302, location=True)

    def test_pending_second_factor_is_anonymous(self):
        admin = self.account("admin")
        client = self.client()
        with client.session_transaction() as session:
            session.update(mfa_uid=admin["id"], mfa_sk=admin["session_key"], mfa_iat=int(time.time()))
        self.assert_refused(client, 302, location=True)

    def test_disabled_owner_and_stale_session_are_anonymous(self):
        disabled = self.account("teacher", is_active=False)
        assignment_id = self.create(disabled)
        self.assertEqual(self.client(disabled).get(f"/teacher/assignment/{assignment_id}/qr.svg").status_code, 302)
        stale = self.client()
        with stale.session_transaction() as session:
            session.update(uid=self.owner["id"], sk="not-the-session-key", iat=int(time.time()))
        self.assert_refused(stale, 302, location=True)

    def test_nonexistent_assignment_is_404_for_owner_and_admin(self):
        for account in (self.owner, self.account("admin")):
            response, texts = self.encoded(self.client(account), "/teacher/assignment/987654/qr.svg")
            self.assertEqual((response.status_code, texts), (404, []))

    def test_only_get_is_routed(self):
        client = self.client(self.owner)
        for method in ("post", "put", "delete", "patch"):
            with self.subTest(method=method):
                response = getattr(client, method)(self.path, headers=token_tests.SAME_ORIGIN)
                self.assertEqual(response.status_code, 405)


class QrTokenStateTest(QrSharingTestCase):
    def test_assignment_without_a_token_has_no_qr(self):
        assignment_id = self.with_token(None)
        response, texts = self.encoded(self.client(self.owner), f"/teacher/assignment/{assignment_id}/qr.svg")
        self.assertEqual((response.status_code, texts), (404, []))
        self.assertIsNone(self.token(assignment_id))  # and none is generated on the way

    def test_assignment_with_an_invalid_token_has_no_qr(self):
        for value in ("short", "A" * 42 + "/", "A" * 44, "../../teacher"):
            with self.subTest(value=value):
                assignment_id = self.with_token(value)
                for account in (self.owner, self.account("admin")):
                    response, texts = self.encoded(
                        self.client(account), f"/teacher/assignment/{assignment_id}/qr.svg?download=1")
                    self.assertEqual((response.status_code, texts), (404, []))
                    self.assertNotIn(b"<svg", response.data)
                self.assertEqual(self.token(assignment_id), value)

    def test_page_shows_qr_controls_only_with_a_valid_token(self):
        client = self.client(self.owner)
        html = client.get(f"/teacher/assignment/{self.assignment_id}").get_data(as_text=True)
        self.assertIn('id="share-qr"', html)
        self.assertIn(f'src="{self.path}"', html)
        self.assertIn(f'href="{self.path}?download=1"', html)
        self.assertIn(f'download="assignment_{self.assignment_id}_qr.svg"', html)
        self.assertIn("Изтегли QR кода (SVG)", html)
        self.assertIn('alt="QR код с линка за ученици към този тест"', html)
        # The existing sharing controls are intact and show the same canonical link.
        self.assertIn(f'id="share-link" class="share-input" value="{self.student_url}" readonly', html)
        self.assertIn('id="share-copy" data-target="share-link">Копирай</button>', html)
        self.assertNotIn(token_tests.NO_LINK_WARNING, html)
        self.assertNotIn(SHARING_UNAVAILABLE_NOTE, html)

        for value in (None, "not-a-token"):
            with self.subTest(value=value):
                assignment_id = self.with_token(value)
                html = client.get(f"/teacher/assignment/{assignment_id}").get_data(as_text=True)
                self.assertIn(token_tests.NO_LINK_WARNING, html)
                self.assertNotIn(SHARING_UNAVAILABLE_NOTE, html)  # no token: the backfill notice only
                for absent in ("share-qr", "qr.svg", "QR код", 'id="share-link"', "/quiz/s/"):
                    self.assertNotIn(absent, html)

    def test_page_is_not_served_to_those_who_cannot_get_the_qr(self):
        page = f"/teacher/assignment/{self.assignment_id}"
        for client, status in ((self.client(self.account("teacher")), 404),
                               (self.client(self.account("student")), 403), (self.client(), 302)):
            response = client.get(page)
            self.assertEqual(response.status_code, status)
            self.assertNotIn("qr.svg", response.get_data(as_text=True))

    def test_metadata_edit_error_page_keeps_the_qr_controls(self):
        response = self.client(self.owner).post(
            f"/teacher/assignment/{self.assignment_id}/edit", data={**NO_DEADLINE_FORM, "title_bg": ""}, headers=token_tests.SAME_ORIGIN)
        html = response.get_data(as_text=True)
        self.assertIn("Заглавието е задължително.", html)
        self.assertIn(f'src="{self.path}"', html)

    def test_listing_and_results_pages_have_no_qr(self):
        client = self.client(self.owner)
        for path in ("/teacher/assignments", f"/teacher/assignment/{self.assignment_id}/results"):
            response = client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("qr.svg", response.get_data(as_text=True))


class QrCanonicalUrlTest(QrSharingTestCase):
    def test_encoded_url_is_the_token_link_and_never_a_numeric_or_private_one(self):
        student = self.account("student")
        attempt_id = self.start(self.client(student), f"/quiz/s/{self.public_token}")
        response, texts = self.encoded(self.client(self.owner))
        self.assertEqual(texts, [self.student_url])
        encoded = texts[0]
        self.assertRegex(encoded, rf"^https://learnpilot\.test/quiz/s/{token_tests.TOKEN_SHAPE}$")
        for private in (f"/quiz/{self.assignment_id}", f"/quiz/attempt/{attempt_id}", "/result", "/teacher",
                        "/results", "/login", "?", "#", self.owner["session_key"], student["session_key"]):
            self.assertNotIn(private, encoded)
        grid = svg_modules(response.data)
        for other in (f"{PUBLIC_BASE}/quiz/{self.assignment_id}", f"{PUBLIC_BASE}/quiz/attempt/{attempt_id}",
                      f"{PUBLIC_BASE}/quiz/attempt/{attempt_id}/result",
                      f"{PUBLIC_BASE}/teacher/assignment/{self.assignment_id}/results"):
            self.assertNotEqual(grid, expected_modules(other))

    def test_request_cannot_change_what_is_encoded(self):
        client = self.client(self.owner)
        baseline = client.get(self.path).data
        evil = "https://evil.example/quiz/s/" + "E" * 43
        for query in (f"url={evil}", f"next={evil}", f"data={evil}", "token=" + "E" * 43, "host=evil.example",
                      "public_token=" + "E" * 43, "download=0", "download=true", "download=1&download=0",
                      "scale=999", "error=l", "border=0", "format=png"):
            with self.subTest(query=query):
                response, texts = self.encoded(client, f"{self.path}?{query}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(texts, [self.student_url])
                self.assertEqual(response.data, baseline)
                self.assertRegex(response.headers["Content-Disposition"],
                                 rf'^(inline|attachment); filename="assignment_{self.assignment_id}_qr\.svg"$')
                self.assertNotIn("evil", str(response.headers))
        self.assertTrue(client.get(f"{self.path}?download=1&x=y").headers["Content-Disposition"].startswith("attachment"))
        self.assertTrue(client.get(f"{self.path}?download=yes").headers["Content-Disposition"].startswith("inline"))

    def test_host_and_forwarding_headers_cannot_change_what_is_encoded(self):
        baseline = self.client(self.owner).get(self.path).data
        client = self.foreign_host_client(self.owner)
        for headers in ({"Host": "evil.example"}, {"Host": "evil.example:8443"},
                        {"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "http"},
                        {"Forwarded": "host=evil.example;proto=http"}, {"X-Original-Host": "evil.example"},
                        {"Referer": "https://evil.example/", "Origin": "https://evil.example"}):
            with self.subTest(headers=headers):
                response, texts = self.encoded(client, headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(texts, [self.student_url])
                self.assertEqual(response.data, baseline)
        html = client.get(f"/teacher/assignment/{self.assignment_id}", headers={"Host": "evil.example"})
        self.assertIn(f'value="{self.student_url}"', html.get_data(as_text=True))
        self.assertNotIn("evil.example/quiz", html.get_data(as_text=True))

    def test_production_like_setup_encodes_the_public_https_host_on_a_loopback_request(self):
        with patch.dict(web_app.app.config, {"TRUSTED_HOSTS": ["learnpilot.test", "localhost", "127.0.0.1"]}):
            client = self.client(self.owner)
            response, texts = self.encoded(client, base_url="http://localhost")
            self.assertEqual((response.status_code, texts), (200, [self.student_url]))
            refused, texts = self.encoded(client, headers={"Host": "evil.example"})
            self.assertEqual((refused.status_code, texts), (400, []))

    def test_malformed_configured_base_fails_closed(self):
        client = self.client(self.owner)
        for value in (None, "", "learnpilot.test", "https://learnpilot.test/app", "javascript:alert(1)",
                      "https://user@learnpilot.test", "https://learnpilot.test\r\nX: y"):
            with self.subTest(value=value), \
                    patch.dict(web_app.app.config, {public_url.PUBLIC_BASE_URL_CONFIG: value}):
                response, texts = self.encoded(client)
                self.assertEqual((response.status_code, texts), (503, []))
                self.assertNotIn(b"<svg", response.data)
                self.assertNotIn(self.public_token.encode(), response.data)
                html = client.get(f"/teacher/assignment/{self.assignment_id}").get_data(as_text=True)
                self.assert_sharing_unavailable(html)

    def assert_sharing_unavailable(self, html, *hosts):
        """The page offers no absolute student link and no QR code, only the notice."""
        self.assertIn('id="share-link-unavailable"', html)
        self.assertIn(SHARING_UNAVAILABLE_NOTE, html)
        self.assertNotIn(token_tests.NO_LINK_WARNING, html)
        for absent in ('id="share-qr"', "qr.svg", 'id="share-link"', 'id="share-copy"', *hosts):
            self.assertNotIn(absent, html)
        # Nothing absolute carries the token; the only link left is the relative one.
        self.assertEqual(re.findall(r"[^\s\"'<>]*/quiz/s/[^\s\"'<>]*", html), [f"/quiz/s/{self.public_token}"])

    def test_invalid_configuration_never_shows_a_link_built_from_the_request_host(self):
        # Trusted hosts are open here (development); production would answer 400 first.
        page = f"/teacher/assignment/{self.assignment_id}"
        client = self.foreign_host_client(self.owner)
        for value in (None, "", "evil-config", "https://learnpilot.test/app"):
            for headers in ({"Host": "evil.example"}, {"Host": "evil.example:8443"},
                            {"Host": "localhost", "X-Forwarded-Host": "evil.example"},
                            {"Host": "localhost"}):
                with self.subTest(value=value, headers=headers), \
                        patch.dict(web_app.app.config, {public_url.PUBLIC_BASE_URL_CONFIG: value}):
                    response = client.get(page, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    self.assert_sharing_unavailable(response.get_data(as_text=True), "evil.example", "localhost")
                    refused, texts = self.encoded(client, headers=headers)
                    self.assertEqual((refused.status_code, texts), (503, []))
                    self.assertNotIn(b"evil.example", refused.data)

    def test_edit_error_page_does_not_fall_back_to_the_request_host_either(self):
        client = self.foreign_host_client(self.owner)
        with patch.dict(web_app.app.config, {public_url.PUBLIC_BASE_URL_CONFIG: None}):
            response = client.post(
                f"/teacher/assignment/{self.assignment_id}/edit", data={**NO_DEADLINE_FORM, "title_bg": ""},
                headers={"Host": "evil.example", "Origin": "http://evil.example"})
        self.assertEqual(response.status_code, 400)
        html = response.get_data(as_text=True)
        self.assertIn("Заглавието е задължително.", html)
        self.assert_sharing_unavailable(html, "evil.example")

    def test_unconfigured_run_on_a_foreign_host_shows_no_link_and_loopback_still_does(self):
        page = f"/teacher/assignment/{self.assignment_id}"
        config = dict(web_app.app.config)
        del config[public_url.PUBLIC_BASE_URL_CONFIG]
        with patch.dict(web_app.app.config, config, clear=True):
            client = self.foreign_host_client(self.owner)
            for host in ("evil.example", "192.168.1.20:5001", "localhost.evil.example"):
                with self.subTest(host=host):
                    html = client.get(page, headers={"Host": host}).get_data(as_text=True)
                    self.assert_sharing_unavailable(html, host)
            html = client.get(page, base_url="http://127.0.0.1:5001").get_data(as_text=True)
            self.assertIn(f'value="http://127.0.0.1:5001/quiz/s/{self.public_token}"', html)
            self.assertIn('id="share-qr"', html)
            self.assertNotIn(SHARING_UNAVAILABLE_NOTE, html)

    def test_configured_tunnel_or_lan_base_is_shown_and_encoded_on_any_request_host(self):
        page = f"/teacher/assignment/{self.assignment_id}"
        client = self.foreign_host_client(self.owner)
        for base in ("https://abc-def.trycloudflare.com", "http://192.168.1.20:5001"):
            with self.subTest(base=base), patch.dict(web_app.app.config, {public_url.PUBLIC_BASE_URL_CONFIG: base}):
                link = f"{base}/quiz/s/{self.public_token}"
                for headers in ({"Host": "localhost:5001"}, {"Host": "evil.example"}):
                    html = client.get(page, headers=headers).get_data(as_text=True)
                    self.assertIn(f'id="share-link" class="share-input" value="{link}" readonly', html)
                    self.assertNotIn("evil.example", html)
                    response, texts = self.encoded(client, headers=headers)
                    self.assertEqual((response.status_code, texts), (200, [link]))

    def test_admin_gets_the_same_canonical_link_or_notice(self):
        admin = self.client(self.account("admin"))
        page = f"/teacher/assignment/{self.assignment_id}"
        self.assertIn(f'value="{self.student_url}"', admin.get(page).get_data(as_text=True))
        with patch.dict(web_app.app.config, {public_url.PUBLIC_BASE_URL_CONFIG: None}):
            self.assert_sharing_unavailable(admin.get(page).get_data(as_text=True))

    def test_without_configuration_only_a_loopback_request_gets_a_qr(self):
        config = dict(web_app.app.config)
        del config[public_url.PUBLIC_BASE_URL_CONFIG]
        with patch.dict(web_app.app.config, config, clear=True):
            client = self.foreign_host_client(self.owner)
            for base_url, host in (("http://localhost", "localhost"), ("http://127.0.0.1:5001", "127.0.0.1:5001"),
                                   ("http://[::1]:5001", "[::1]:5001")):
                with self.subTest(base_url=base_url):
                    response, texts = self.encoded(client, base_url=base_url)
                    self.assertEqual((response.status_code, texts),
                                     (200, [f"http://{host}/quiz/s/{self.public_token}"]))
            for host in ("evil.example", "localhost.evil.example", "evil.example:80", "localhost@evil.example",
                         "localhost/@evil.example", "localhost:80@evil.example", "127.0.0.1.evil.example",
                         "localhost:notaport", "192.168.1.20:5001"):
                with self.subTest(host=host):
                    response, texts = self.encoded(client, headers={"Host": host})
                    self.assertEqual(texts, [])
                    self.assertIn(response.status_code, (400, 503))
                    self.assertNotIn(b"<svg", response.data)


class QrResponseSafetyTest(QrSharingTestCase):
    def test_response_headers_keep_the_image_private_and_inert(self):
        for path in (self.path, self.path + "?download=1"):
            with self.subTest(path=path):
                response = self.client(self.owner).get(path)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(response.headers["Content-Security-Policy"], "default-src 'none'; sandbox")
                self.assertNotIn("public", response.headers["Cache-Control"])
                for header in ("ETag", "Last-Modified", "Expires", "Set-Cookie"):
                    self.assertNotIn(header, response.headers)

    def test_filename_comes_from_the_numeric_id_not_from_the_title(self):
        hostile = self.create(self.owner, title='../../etc/passwd"\r\nX-Injected: 1 <script>.svg')
        response = self.client(self.owner).get(f"/teacher/assignment/{hostile}/qr.svg?download=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Disposition"], f'attachment; filename="assignment_{hostile}_qr.svg"')
        self.assertNotIn("X-Injected", response.headers)
        self.assertNotIn(b"passwd", response.data)

    def test_served_svg_has_no_scripts_or_external_references(self):
        response = self.client(self.owner).get(self.path)
        text = response.get_data(as_text=True)
        root = ET.fromstring(response.data)
        self.assertEqual({element.tag for element in root.iter()}, {f"{SVG_NS}svg", f"{SVG_NS}g", f"{SVG_NS}path"})
        self.assertEqual(re.findall(r"https?://[^\s\"']+", text), ["http://www.w3.org/2000/svg"])
        for forbidden in ("<script", "href", "onload", "javascript:", self.public_token, "learnpilot.test"):
            self.assertNotIn(forbidden, text)

    def state(self):
        """Everything QR generation could conceivably write to: database, vault, uploads."""
        database = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted(Path(self.db_path).parent.glob(Path(self.db_path).name + "*"))}
        files = sorted(str(path) for path in Path(self.isolation_root).rglob("*") if "__pycache__" not in path.parts)
        return database, files, self.query("PRAGMA data_version")[0][0]

    def test_generating_a_qr_writes_nothing(self):
        owner, admin = self.client(self.owner), self.client(self.account("admin"))
        self.query("PRAGMA wal_checkpoint(TRUNCATE)")
        before = self.state()
        row = tuple(self.row(self.assignment_id))
        for client in (owner, admin):
            for path in (self.path, self.path + "?download=1"):
                self.assertEqual(client.get(path).status_code, 200)
        self.assertEqual(self.state(), before)
        self.assertEqual(tuple(self.row(self.assignment_id)), row)
        self.assertEqual(self.attempts_of(self.assignment_id), [])

    def test_no_external_qr_service_or_client_side_library_is_used(self):
        for relative in ("web/services/qr.py", "web/blueprints/teacher.py", "web/public_url.py",
                         "web/templates/teacher_assignment.html"):
            source = (ROOT / relative).read_text(encoding="utf-8").lower()
            for forbidden in ("api.qrserver", "chart.googleapis", "quickchart", "qrcode.js", "cdn.", "//cdn",
                              "urlopen", "requests.", "http.client"):
                with self.subTest(relative=relative, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)
        self.assertIn("segno>=1.6,<2", (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines())


class ExistingSharingUnchangedTest(QrSharingTestCase):
    def test_student_entry_and_teacher_link_still_work(self):
        self.assertEqual(self.client().get(f"/quiz/s/{self.public_token}").status_code, 200)
        self.assertEqual(self.client().get(f"/quiz/{self.assignment_id}").status_code, 404)
        self.assertEqual(self.client().get(f"/quiz/s/{self.public_token}/qr.svg").status_code, 404)
        self.assertEqual(self.client().get(f"/quiz/{self.assignment_id}/qr.svg").status_code, 404)
        html = self.client(self.owner).get(f"/teacher/assignment/{self.assignment_id}").get_data(as_text=True)
        self.assertIn(f'href="/quiz/s/{self.public_token}">Отвори теста</a>', html)
        self.assertEqual(self.token(self.assignment_id), self.public_token)

    def test_qr_route_is_the_only_new_route_and_it_is_teacher_management(self):
        rules = sorted(rule.rule for rule in web_app.app.url_map.iter_rules() if "qr" in rule.rule.lower())
        self.assertEqual(rules, ["/teacher/assignment/<int:assignment_id>/qr.svg"])


if __name__ == "__main__":
    unittest.main()

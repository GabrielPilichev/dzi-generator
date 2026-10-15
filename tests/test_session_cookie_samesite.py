"""Phase 2 — the session cookie is ``SameSite=Lax`` (prerequisite for OIDC sign-in).

One attribute of one cookie changed: a top-level GET that starts on another site
(a shared quiz link, later an identity provider's redirect) now arrives signed in.
Everything else has to hold with it, and is pinned here from real requests and the
real ``Set-Cookie`` headers:

* every response that writes the session cookie (the sign-in of each role, both
  steps of an admin's sign-in, a password change, a dropped stale identity, the
  sign-out) writes it ``SameSite=Lax; HttpOnly; Secure`` as a browser-session
  cookie, and plain-HTTP development drops ``Secure`` and nothing else;
* an anonymous request sets no cookie and a signed-in GET does not rewrite it;
* no GET changes state: every GET rule is requested by every kind of visitor the
  way a link on another site would arrive, and the database, the vault and the
  uploads are left as they were. The one write a GET does on purpose, the
  lifecycle observation stamp, is pinned for what it is;
* a cross-origin unsafe request is refused on every unsafe rule while the client
  still sends the session cookie, so nothing here relies on SameSite.

Accounts, codes and keys are synthetic; every database is a temporary one.
"""

import json
import logging
import re
import sqlite3
import time
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from tests.auth_helpers import create_account, set_account_session
from tests.google_oidc_support import GoogleAppTestCase, provider
from tests.lifecycle_support import LifecycleTestCase, eligible_section, ts
from tests.support import NO_DEADLINE_FORM, IsolatedAppTestCase, student_entry_path, web_app
from tests.test_access_policy import application_rules
from tests.test_account_login import DAY, NEW_PASSWORD, PASSWORD, SAME_ORIGIN, AccountAppTestCase
from web import oauth_state, public_url, request_integrity
from web.services import accounts

SESSION_COOKIE = "session"
ACCOUNT_KEYS = {"uid", "sk", "iat"}
PENDING_KEYS = {"mfa_uid", "mfa_sk", "mfa_iat"}
DZI_SOURCE = "may_2025_v2"

# What a browser sends with a top-level navigation that started on another site.
# The application reads none of it (a GET is not origin-checked). With a Lax cookie
# the browser also attaches the session, which the test client always does.
CROSS_SITE_NAVIGATION = {
    "Referer": "https://other.example/classroom",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-User": "?1",
}

# Unsafe requests that did not come from the application's own pages. The test
# client is http://localhost. SameSite never tells the hosts of one site apart;
# the origin check does.
FORGED_ORIGINS = {
    "another site": {"Origin": "https://evil.example"},
    "a subdomain of this host": {"Origin": "http://evil.localhost"},
    "this host on another port": {"Origin": "http://localhost:8443"},
    "this host over another scheme": {"Origin": "https://localhost"},
    "an opaque origin": {"Origin": "null"},
    "only a foreign Referer": {"Referer": "https://evil.example/form"},
    "a foreign Origin beside a same-origin Referer": {
        "Origin": "https://evil.example", "Referer": "http://localhost/login"},
}

VISITORS = ("anonymous", "student", "teacher", "admin")


def statuses(anonymous, student, teacher, admin) -> dict:
    """The status each kind of visitor gets; a tuple allows more than one."""
    return dict(zip(VISITORS, (anonymous, student, teacher, admin)))


EVERYONE = statuses(200, 200, 200, 200)
MOVED = statuses(301, 301, 301, 301)
ANY_ACCOUNT = statuses(302, 200, 200, 200)
THE_STUDENTS_OWN = statuses(404, 200, 404, 404)
TEACHER_PAGE = statuses(302, 403, 200, 200)
ADMIN_PAGE = statuses(302, 403, 403, 200)
DISABLED = statuses(404, 404, 404, 404)


def session_cookies(response) -> list[dict]:
    """The response's ``Set-Cookie`` headers, parsed: ``name``, ``value`` and every
    attribute by its lower-case name (a flag attribute maps to ``True``)."""
    cookies = []
    for header in response.headers.getlist("Set-Cookie"):
        pair, *attributes = [part.strip() for part in header.split(";")]
        name, _, value = pair.partition("=")
        cookie = {"name": name, "value": value}
        for attribute in attributes:
            key, has_value, attribute_value = attribute.partition("=")
            cookie[key.lower()] = attribute_value if has_value else True
        cookies.append(cookie)
    return cookies


def first_link(client, path: str, pattern: str) -> str:
    """The first link on ``path`` whose target matches ``pattern``: a real, rendered URL."""
    match = re.search(f'href="({pattern})"', client.get(path).get_data(as_text=True))
    if match is None:
        raise AssertionError(f"{path} has no link matching {pattern}")
    return match.group(1)


def created_id(response) -> int:
    """The id at the end of the redirect that follows a successful create or start."""
    if response.status_code != 302:
        raise AssertionError(f"expected a redirect, got {response.status_code}")
    return int(response.headers["Location"].rsplit("/", 1)[1])


class SessionCookieAssertions:
    def assert_session_cookie(self, response, *, secure=True, expired=False):
        """The response wrote the session cookie once, with exactly the pinned attributes."""
        cookies = session_cookies(response)
        self.assertEqual([cookie["name"] for cookie in cookies], [SESSION_COOKIE])
        cookie = cookies[0]
        expected = {"name", "value", "samesite", "httponly", "path"}
        if secure:
            expected.add("secure")
        if expired:
            expected |= {"expires", "max-age"}
        # Nothing else: no Domain, no Partitioned and, for a live cookie, no
        # Expires or Max-Age (it ends with the browser session; no remember-me).
        self.assertEqual(set(cookie), expected)
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertIs(cookie["httponly"], True)
        self.assertEqual(cookie["path"], "/")
        if secure:
            self.assertIs(cookie["secure"], True)
        if expired:
            self.assertEqual((cookie["value"], cookie["max-age"]), ("", "0"))
            self.assertIn("1970", cookie["expires"])
        else:
            self.assertTrue(cookie["value"])


# --------------------------------------------------------------------------- Set-Cookie


class SessionCookieAttributesTest(SessionCookieAssertions, AccountAppTestCase):
    """Every response that writes the session cookie, through the real forms."""

    def setUp(self):
        super().setUp()
        # What production always has (web.production sets it); a developer's shell
        # may run plain HTTP, which has its own test below.
        secure = patch.dict(self.app.config, {"SESSION_COOKIE_SECURE": True})
        secure.start()
        self.addCleanup(secure.stop)

    def assert_reads_only(self, client, path, status=200):
        """A page opened from another site: served, and the cookie is left alone."""
        response = client.get(path, headers=CROSS_SITE_NAVIGATION)
        self.assertEqual(response.status_code, status, path)
        self.assertEqual(response.headers.getlist("Set-Cookie"), [])
        return response

    def test_student_and_teacher_sign_in(self):
        cases = (("student", "/", "/account/attempts", "/teacher/new"),
                 ("teacher", "/teacher/new", "/teacher/assignments", "/teacher"))
        for role, home, own_page, forbidden in cases:
            with self.subTest(role=role):
                account, client = self.make(role), self.app.test_client()
                response = self.login(account["email"], client=client)
                self.assertEqual((response.status_code, response.headers["Location"]), (302, home))
                self.assert_session_cookie(response)
                state = self.session(client)
                self.assertEqual(set(state), ACCOUNT_KEYS)
                self.assertEqual((state["uid"], state["sk"]), (account["id"], account["session_key"]))
                # The role is the row's, never the cookie's: its pages open, the rest do not.
                for path in (home, own_page):
                    self.assert_reads_only(client, path)
                self.assert_reads_only(client, forbidden, 403)

    def test_admin_sign_in_at_both_steps(self):
        admin, client = self.make("admin"), self.app.test_client()
        self.now += timedelta(seconds=31)
        page = self.login(admin["email"], client=client)
        self.assertEqual(page.status_code, 200)
        self.assert_session_cookie(page)
        self.assertEqual(set(self.session(client)), PENDING_KEYS)
        # The password alone is nobody, also for a GET that arrives with the cookie.
        denied = self.assert_reads_only(client, "/teacher", 302)
        self.assertEqual(denied.headers["Location"], "/login?next=/teacher")

        done = self.submit_code(self.code(admin), client=client)
        self.assertEqual((done.status_code, done.headers["Location"]), (302, "/teacher"))
        self.assert_session_cookie(done)
        state = self.session(client)
        self.assertEqual(set(state), ACCOUNT_KEYS)
        self.assertEqual((state["uid"], state["sk"]), (admin["id"], admin["session_key"]))
        for path in ("/teacher", "/dzi", "/teacher/assignments"):
            self.assert_reads_only(client, path)

    def test_admin_sign_in_with_a_recovery_code(self):
        admin, client = self.make("admin"), self.app.test_client()
        self.now += timedelta(seconds=31)
        self.assert_session_cookie(self.login(admin["email"], client=client))
        with self.assertLogs("web.blueprints.account", logging.WARNING):
            done = self.submit_code(admin["recovery_codes"][0], client=client)
        self.assertEqual((done.status_code, done.headers["Location"]), (302, "/teacher"))
        self.assert_session_cookie(done)
        self.assertEqual(set(self.session(client)), ACCOUNT_KEYS)
        self.assert_reads_only(client, "/teacher")

    def test_sign_out_expires_the_cookie_and_only_a_post_signs_out(self):
        for role in ("student", "teacher", "admin"):
            with self.subTest(role=role):
                account, client = self.make(role), self.app.test_client()
                self.assertEqual(self.sign_in(account, client=client).status_code, 302)
                # A link cannot sign anyone out: GET is not a method of /logout.
                self.assert_reads_only(client, "/logout", 405)
                self.assertEqual(self.session(client)["uid"], account["id"])

                response = client.post("/logout", headers=SAME_ORIGIN)
                self.assertEqual((response.status_code, response.headers["Location"]), (302, "/"))
                self.assert_session_cookie(response, expired=True)
                self.assertEqual(self.session(client), {})
                denied = self.assert_reads_only(client, "/account/password", 302)
                self.assertEqual(denied.headers["Location"], "/login?next=/account/password")

    def test_password_change_reissues_the_cookie_and_ends_the_other_sessions(self):
        account = self.make("teacher")
        client, other_device = self.app.test_client(), self.app.test_client()
        for device in (client, other_device):
            self.login(account["email"], client=device)
        response = client.post("/account/password", headers=SAME_ORIGIN, data={
            "current_password": PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assert_session_cookie(response)
        self.assertEqual(self.session(client)["sk"], self.user(account["id"], "session_key"))
        self.assertNotEqual(self.session(client)["sk"], account["session_key"])
        self.assert_reads_only(client, "/teacher/new")
        # The other device carries the old key: anonymous, and its cookie is emptied.
        dropped = other_device.get("/teacher/new", headers=CROSS_SITE_NAVIGATION)
        self.assertEqual((dropped.status_code, dropped.headers["Location"]), (302, "/login?next=/teacher/new"))
        self.assert_session_cookie(dropped, expired=True)
        self.assertEqual(self.session(other_device), {})

    def test_an_expired_session_is_dropped_with_the_same_attributes(self):
        for role, age in (("student", 31 * DAY), ("teacher", 31 * DAY), ("admin", 13 * 3600)):
            with self.subTest(role=role):
                account, client = create_account(self.db_path, role), self.app.test_client()
                set_account_session(client, account["id"], account["session_key"], iat=int(time.time()) - age)
                response = client.get("/account/password", headers=CROSS_SITE_NAVIGATION)
                self.assertEqual((response.status_code, response.headers["Location"]),
                                 (302, "/login?next=/account/password"))
                self.assert_session_cookie(response, expired=True)
                self.assertEqual(self.session(client), {})

    def test_a_rekeyed_session_is_dropped_and_what_is_left_keeps_the_attributes(self):
        account, client = create_account(self.db_path, "teacher"), self.app.test_client()
        set_account_session(client, account["id"], account["session_key"])
        with client.session_transaction() as sess:
            sess["quiz_attempt_owner:7"] = True  # a historical attempt of this browser
        self.assert_reads_only(client, "/teacher/new")
        with closing(sqlite3.connect(self.db_path)) as conn:
            accounts.rotate_session_key(conn, account["id"])  # "sign out everywhere"
            conn.commit()
        response = client.get("/teacher/new", headers=CROSS_SITE_NAVIGATION)
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/login?next=/teacher/new"))
        # Rewritten, not expired: the identity is gone and the marker is still carried.
        self.assert_session_cookie(response)
        self.assertEqual(self.session(client), {"quiz_attempt_owner:7": True})

    def test_plain_http_development_drops_secure_and_nothing_else(self):
        with patch.dict(self.app.config, {"SESSION_COOKIE_SECURE": False}):
            account, client = self.make("teacher"), self.app.test_client()
            self.assert_session_cookie(self.login(account["email"], client=client), secure=False)
            self.assert_reads_only(client, "/teacher/new")
            self.assert_session_cookie(client.post("/logout", headers=SAME_ORIGIN), secure=False, expired=True)


class GoogleSessionCookieTest(SessionCookieAssertions, GoogleAppTestCase):
    """The responses of a Google sign-in and of a link that write the session cookie.

    The callback is the top-level GET from another site that the Lax cookie exists
    for: it arrives with the cookie of the start, and what it writes has the same
    attributes as every other session cookie.
    """

    def setUp(self):
        super().setUp()
        secure = patch.dict(self.app.config, {"SESSION_COOKIE_SECURE": True})
        secure.start()
        self.addCleanup(secure.stop)

    def test_the_start_writes_the_transaction_into_a_session_cookie(self):
        started = self.start()
        self.assertEqual(started.status_code, 303)
        self.assert_session_cookie(started)
        self.assertEqual(set(self.session()), {oauth_state.TX_SLOT})

    def test_the_callback_of_a_sign_in_issues_the_account_session(self):
        started = self.start()
        location = started.headers["Location"]
        code = self.google.authorize(location)
        # As the browser sends it: a navigation that started at Google.
        done = self.client.get("/auth/google/callback", headers=CROSS_SITE_NAVIGATION,
                               query_string={"code": code, "state": self.state_of(location)})
        self.assertEqual((done.status_code, done.headers["Location"]), (302, "/"))
        self.assert_session_cookie(done)
        self.assertEqual(set(self.session()), ACCOUNT_KEYS)
        self.assertEqual(self.client.get("/account/attempts", headers=CROSS_SITE_NAVIGATION).status_code, 200)

    def test_a_callback_that_fails_expires_or_rewrites_the_cookie_with_the_same_attributes(self):
        started = self.start()
        refused = self.callback(state=self.state_of(started.headers["Location"]), error="access_denied")
        self.assertEqual(refused.headers["Location"], "/login?google=cancelled")
        # The transaction was all the session held: the cookie is expired.
        self.assert_session_cookie(refused, expired=True)
        self.assertEqual(self.session(), {})

    def test_the_link_callback_and_the_confirmation(self):
        self.signed_in("teacher")
        link_started = self.start_link()
        self.assert_session_cookie(link_started)
        returned = self.return_from_google(link_started)
        self.assertEqual(returned.headers["Location"], "/auth/google/link/confirm")
        self.assert_session_cookie(returned)
        # The page reads only. A wrong password changes no cookie; the confirmation
        # rewrites it to drop the pending link, and issues no session.
        page = self.client.get("/auth/google/link/confirm", headers=CROSS_SITE_NAVIGATION)
        self.assertEqual((page.status_code, page.headers.getlist("Set-Cookie")), (200, []))
        wrong = self.confirm(password="not the password")
        self.assertEqual((wrong.status_code, wrong.headers.getlist("Set-Cookie")), (200, []))
        confirmed = self.confirm()
        self.assertEqual(confirmed.headers["Location"], "/account?google=linked")
        self.assert_session_cookie(confirmed)
        self.assertEqual(set(self.session()), ACCOUNT_KEYS)

    def test_a_cancellation_and_plain_http_development(self):
        self.signed_in("teacher")
        self.link_until_pending()
        self.assert_session_cookie(self.cancel())
        with patch.dict(self.app.config, {"SESSION_COOKIE_SECURE": False}):
            self.assert_session_cookie(self.start(self.app.test_client()), secure=False)


# --------------------------------------------------------------------------- shared state


class VisitorsTestCase(IsolatedAppTestCase):
    """State shared by the request sweeps: one visitor of every kind, two assignments
    the teacher made through the real form, and the student's one open and one
    submitted attempt."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        # The QR code of an assignment needs a configured public address (503 without).
        # Google sign-in is enabled, so that its routes are swept as they behave when
        # they do something (disabled, each of them is the generic 404).
        cls.app.config.update({"TESTING": True, public_url.PUBLIC_BASE_URL_CONFIG: "https://learnpilot.example",
                               oauth_state.PROVIDER_CONFIG_KEY: provider()})
        cls.db_path = cls.app.config["DB_PATH"]
        cls.teacher_email = "sweep-teacher@example.bg"
        cls.accounts = {
            "student": create_account(cls.db_path, "student"),
            "teacher": create_account(cls.db_path, "teacher", email=cls.teacher_email, password=PASSWORD),
            "admin": create_account(cls.db_path, "admin"),
        }
        cls.clients = {who: cls.client_for(who) for who in VISITORS}
        teacher, student = cls.clients["teacher"], cls.clients["student"]

        with closing(web_app.quiz_db()) as conn:
            section_id, _ = eligible_section(conn)
            cls.section_slug = conn.execute(
                "SELECT section_slug FROM curriculum_sections ORDER BY id LIMIT 1").fetchone()[0]
        cls.new_assignment_form = {**NO_DEADLINE_FORM, "section_id": section_id, "question_count": 3}
        cls.assignment_id, finished_assignment = (
            created_id(teacher.post("/teacher/new", data=cls.new_assignment_form, headers=SAME_ORIGIN))
            for _ in range(2))
        cls.entry = student_entry_path(cls.assignment_id)
        cls.open_attempt = created_id(student.post(cls.entry, headers=SAME_ORIGIN))
        cls.submitted_attempt = created_id(
            student.post(student_entry_path(finished_assignment), headers=SAME_ORIGIN))
        submitted = student.post(f"/quiz/attempt/{cls.submitted_attempt}", headers=SAME_ORIGIN,
                                 data=cls.one_answer(cls.submitted_attempt))
        if submitted.headers.get("Location") != f"/quiz/attempt/{cls.submitted_attempt}/result":
            raise AssertionError(f"the fixture attempt was not submitted ({submitted.status_code})")

    @classmethod
    def client_for(cls, who):
        client = cls.app.test_client()
        if who != "anonymous":
            set_account_session(client, cls.accounts[who]["id"], cls.accounts[who]["session_key"])
        return client

    @classmethod
    def one_answer(cls, attempt_id) -> dict:
        """A form that answers the first question of the attempt's own plan."""
        with closing(web_app.quiz_db()) as conn:
            plan = json.loads(conn.execute(
                "SELECT question_ids_json FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone()[0])
        question_ids = plan["question_ids"] if isinstance(plan, dict) else plan
        return {f"q_{int(question_ids[0])}": "A"}

    def setUp(self):
        self.monitor = sqlite3.connect(self.db_path)
        self.addCleanup(self.monitor.close)

    def data_version(self) -> int:
        """Differs between two readings exactly when another connection committed a
        change to the database in between."""
        return self.monitor.execute("PRAGMA data_version").fetchone()[0]

    def stored_files(self) -> dict:
        """Every file of the temporary state (database, vault notes, uploads)."""
        root = Path(self.isolation_root)
        return {str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
                for path in sorted(root.rglob("*")) if path.is_file()}

    def sessions(self) -> dict:
        state = {}
        for who, client in self.clients.items():
            with client.session_transaction() as sess:
                state[who] = dict(sess)
        return state

    def visitor(self, who):
        """The visitor's client; an anonymous visitor is a new browser every time."""
        return self.app.test_client() if who == "anonymous" else self.clients[who]


# --------------------------------------------------------------------------- GET


class CrossSiteNavigationTest(VisitorsTestCase):
    """No GET changes state, whoever follows a link to it from another site."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        anonymous = cls.app.test_client()
        segment = r'[^"/?#]+'
        subject = first_link(anonymous, "/", rf"/subjects/{segment}")
        grade = first_link(anonymous, subject, rf"/subjects/{segment}/\d+")
        topic_group = first_link(anonymous, grade, rf"/subjects/{segment}/\d+/{segment}")
        topic = first_link(anonymous, topic_group, rf"/subjects/{segment}/\d+/{segment}/{segment}")
        assignment = f"/teacher/assignment/{cls.assignment_id}"
        attempt = f"/quiz/attempt/{cls.open_attempt}"
        # Every GET rule of the application: the paths requested for it and the
        # status each kind of visitor gets.
        cls.get_routes = {
            "static": [("/static/css/tokens.css", EVERYONE)],
            "browsing.index": [("/", EVERYONE)],
            "browsing.grade_view": [("/grade/8", EVERYONE)],
            "browsing.section_view": [(f"/section/{cls.section_slug}", EVERYONE)],
            "browsing.subject_view": [(subject, EVERYONE)],
            "browsing.subject_grade_view": [(grade, EVERYONE)],
            "browsing.subject_topic_group_view": [(topic_group, EVERYONE)],
            "browsing.subject_topic_view": [(topic, EVERYONE)],
            "browsing.physics": [("/physics", MOVED)],
            "browsing.physics_grade": [("/physics/grade/8", MOVED)],
            "browsing.healthz": [("/healthz", EVERYONE)],
            "browsing.favicon": [("/favicon.ico", EVERYONE)],
            # Signed in already: sent on to the validated target, nothing is asked.
            "account.login": [("/login", statuses(200, 302, 302, 302)),
                              ("/login?next=/account/attempts", statuses(200, 302, 302, 302))],
            "account.password": [("/account/password", ANY_ACCOUNT)],
            # The profile gate is off in this configuration: a signed-in account gets the
            # generic 404, an anonymous visitor the sign-in like every account page.
            # tests/test_account_profile.py sweeps it with the gate on.
            "account.profile": [("/account/profile", statuses(302, 404, 404, 404)),
                                ("/account/profile?next=/account/attempts", statuses(302, 404, 404, 404))],
            # The account page reads only, whatever outcome code it is asked to show.
            "account.overview": [("/account", ANY_ACCOUNT), ("/account?google=linked", ANY_ACCOUNT),
                                 ("/account?google=link_cancelled", ANY_ACCOUNT)],
            # Without a pending link there is nothing to confirm: sent to the account page.
            "oidc.google_link_confirm": [("/auth/google/link/confirm", statuses(302, 302, 302, 302))],
            # The one GET that may change state, and only for the transaction of the
            # browser that makes it. A link from another site carries none that
            # matches: no cookie, no row, no outbound request.
            "oidc.google_callback": [
                (f"/auth/google/callback?code=forged&state={'s' * 43}", statuses(302, 302, 302, 302)),
                ("/auth/google/callback?error=access_denied", statuses(302, 302, 302, 302)),
                ("/auth/google/callback", statuses(302, 302, 302, 302))],
            # Registration (PR 4) is disabled in this configuration: every address is the
            # generic 404, for every visitor. tests/test_registration_routes.py sweeps them
            # with registration enabled and a registration in progress.
            "registration.choose": [("/register", DISABLED), ("/register?next=/account/attempts", DISABLED)],
            "registration.student": [("/register/student", DISABLED)],
            "registration.teacher": [("/register/teacher", DISABLED)],
            "registration.verify": [("/register/verify", DISABLED)],
            "quiz.quiz_start_public": [(cls.entry, EVERYONE)],
            "quiz.my_attempts": [("/account/attempts", ANY_ACCOUNT)],
            "quiz.quiz_attempt": [(attempt, THE_STUDENTS_OWN)],
            "quiz.quiz_result": [(f"/quiz/attempt/{cls.submitted_attempt}/result", THE_STUDENTS_OWN)],
            "practical.quiz_practical_attempt": [
                (f"{attempt}/practical/{DZI_SOURCE}", THE_STUDENTS_OWN),
                (f"{attempt}/practical/{DZI_SOURCE}?uploaded=1", THE_STUDENTS_OWN)],
            "teacher.teacher_new": [("/teacher/new", TEACHER_PAGE)],
            "teacher.teacher_assignments": [("/teacher/assignments", TEACHER_PAGE)],
            "teacher.teacher_assignment": [(assignment, TEACHER_PAGE)],
            "teacher.teacher_assignment_qr": [(f"{assignment}/qr.svg", TEACHER_PAGE),
                                              (f"{assignment}/qr.svg?download=1", TEACHER_PAGE)],
            "teacher.teacher_assignment_results": [(f"{assignment}/results", TEACHER_PAGE)],
            "teacher.teacher_assignment_results_export": [(f"{assignment}/results.csv", TEACHER_PAGE)],
            "practical.teacher_practical_submissions": [("/teacher/practical-submissions", TEACHER_PAGE)],
            # Nothing was uploaded in this state: no such file for those who may ask.
            "practical.teacher_practical_submission_file_download": [
                ("/teacher/practical/submission-file/1/download", statuses(302, 403, 404, 404))],
            "teacher.teacher_dashboard": [("/teacher", ADMIN_PAGE)],
            "teacher.teacher_dzi_training": [("/teacher/dzi-training", ADMIN_PAGE)],
            "dzi.dzi_index": [("/dzi", ADMIN_PAGE)],
            "dzi.dzi_source_view": [(f"/dzi/source/{DZI_SOURCE}", ADMIN_PAGE)],
            "dzi.dzi_practical_source": [(f"/dzi/source/{DZI_SOURCE}/practical", ADMIN_PAGE)],
            "dzi.admin_open_question_candidates": [("/admin/open-question-candidates", ADMIN_PAGE)],
            # The official resource files are local data a checkout may not have.
            "practical.dzi_practical_resource_download": [
                ("/dzi/practical/resource/1/download", statuses(302, 403, 403, (200, 404)))],
        }

    def test_every_get_rule_is_in_the_sweep(self):
        # A new GET route has to be added to the sweep below, and so be shown to read only.
        get_endpoints = {rule.endpoint for rule in application_rules(self.app) if "GET" in rule.methods}
        self.assertEqual(set(self.get_routes), get_endpoints)

    def test_no_get_changes_state_or_touches_the_cookie(self):
        sessions, files = self.sessions(), self.stored_files()
        for endpoint, cases in self.get_routes.items():
            for path, expected in cases:
                for who in VISITORS:
                    with self.subTest(endpoint=endpoint, path=path, visitor=who):
                        allowed = expected[who] if isinstance(expected[who], tuple) else (expected[who],)
                        version = self.data_version()
                        response = self.visitor(who).get(path, headers=CROSS_SITE_NAVIGATION)
                        response.close()  # a served file stays open until then
                        self.assertIn(response.status_code, allowed)
                        self.assertEqual(response.headers.getlist("Set-Cookie"), [])
                        self.assertEqual(self.data_version(), version, "the request committed a change")
        self.assertEqual(self.sessions(), sessions)
        self.assertEqual(self.stored_files(), files)

    def test_anonymous_unsafe_requests_set_no_cookie_either(self):
        requests = (
            ("a failed sign-in", "/login", {"email": self.teacher_email, "password": "not the password"},
             SAME_ORIGIN, 200),
            ("a start without an account", self.entry, {}, SAME_ORIGIN, 302),
            ("a forged sign-in", "/login", {"email": self.teacher_email, "password": PASSWORD},
             FORGED_ORIGINS["another site"], 403),
            ("a forged sign-out", "/logout", {}, FORGED_ORIGINS["another site"], 403),
        )
        for name, path, form, headers, status in requests:
            with self.subTest(request=name):
                response = self.app.test_client().post(path, data=form, headers=headers)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers.getlist("Set-Cookie"), [])


class GetObservationStampTest(LifecycleTestCase):
    """The one write a GET does on purpose (docs/ASSIGNMENT_LIFECYCLE_DESIGN.md, 6.3).

    The first request that finds a deadline passed records that it saw it, in a
    transaction that also keeps the lifecycle clock's row. The value is the
    server's clock, nothing in the request can choose it, it is written once, and
    a visitor without any session causes it just the same: the Lax cookie adds no
    way to change anything through it.
    """

    STAMPS = {"expiry_observed_at", "submissions_closed_at"}
    # Request input that must not end up anywhere.
    QUERY = ("?expiry_observed_at=2001-01-01+00:00:00&submissions_closed_at=2001-01-01+00:00:00"
             "&closed_at=2001-01-01+00:00:00&deadline_at=2099-01-01+00:00:00")

    def expired_assignment(self, teacher, student):
        assignment = self.assignment(teacher, deadline_at=self.T0 - timedelta(hours=2))
        attempt = self.attempt(assignment, student, started_at=self.T0 - timedelta(hours=3))
        return assignment, attempt

    def assert_only_stamped(self, client, path, assignment, attempt, status=200):
        row, attempt_row, counts = dict(self.row(assignment)), dict(self.attempt_row(attempt)), self.table_counts()
        seen_at = ts(self.clock.wall())
        response = client.get(path + self.QUERY, headers=CROSS_SITE_NAVIGATION)
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.headers.getlist("Set-Cookie"), [])
        stamped = dict(self.row(assignment))
        self.assertEqual({column for column in stamped if stamped[column] != row[column]}, self.STAMPS)
        self.assertEqual({stamped[column] for column in self.STAMPS}, {seen_at})
        self.assertEqual(dict(self.attempt_row(attempt)), attempt_row)
        self.assertEqual(self.table_counts(), counts)
        # The stamp's transaction also keeps the lifecycle clock's own row.
        clock = dict(self.clock_row())
        self.assertEqual((clock["state"], clock["high_water_at"]), ("ok", seen_at))
        # Seen again, later: nothing more is written, the clock row included.
        self.clock.advance(60)
        client.get(path + self.QUERY, headers=CROSS_SITE_NAVIGATION)
        self.assertEqual(dict(self.row(assignment)), stamped)
        self.assertEqual(dict(self.clock_row()), clock)

    def test_a_signed_in_get_records_only_that_the_deadline_had_passed(self):
        teacher, student = self.account("teacher"), self.account("student")
        pages = (
            ("teacher", "/teacher/assignment/{assignment}"),
            ("teacher", "/teacher/assignment/{assignment}/results"),
            ("student", "/quiz/attempt/{attempt}"),
        )
        for who, page in pages:
            with self.subTest(page=page):
                assignment, attempt = self.expired_assignment(teacher, student)
                client = self.client(teacher if who == "teacher" else student)
                self.assert_only_stamped(
                    client, page.format(assignment=assignment, attempt=attempt), assignment, attempt)

    def test_a_visitor_without_a_session_causes_the_same_stamp(self):
        assignment, attempt = self.expired_assignment(self.account("teacher"), self.account("student"))
        self.assert_only_stamped(self.client(), self.entry(assignment), assignment, attempt)

    def test_an_open_assignment_is_not_written_by_a_get(self):
        teacher, student = self.account("teacher"), self.account("student")
        assignment = self.assignment(teacher, deadline_at=self.T0 + timedelta(days=1))
        attempt = self.attempt(assignment, student, started_at=self.T0 - timedelta(minutes=5))
        row = dict(self.row(assignment))
        for client, path in ((self.client(teacher), f"/teacher/assignment/{assignment}"),
                             (self.client(teacher), f"/teacher/assignment/{assignment}/results"),
                             (self.client(student), f"/quiz/attempt/{attempt}"),
                             (self.client(), self.entry(assignment))):
            with self.subTest(path=path):
                self.assertEqual(client.get(path + self.QUERY, headers=CROSS_SITE_NAVIGATION).status_code, 200)
                self.assertEqual(dict(self.row(assignment)), row)


# --------------------------------------------------------------------------- unsafe methods


class CrossOriginUnsafeRequestTest(VisitorsTestCase):
    """The same-origin check refuses a forged unsafe request on its own.

    The test client sends the session cookie with every request, forged ones
    included: exactly what a browser without SameSite would do.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        assignment = f"/teacher/assignment/{cls.assignment_id}"
        attempt = f"/quiz/attempt/{cls.open_attempt}"
        # Every rule that accepts an unsafe method, with a body that would act.
        cls.unsafe_routes = {
            "account.login": ("/login", {"email": cls.teacher_email, "password": PASSWORD}),
            "account.login_totp": ("/login/totp", {"code": "123456"}),
            "account.logout": ("/logout", {}),
            "account.password": ("/account/password", {
                "current_password": PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD}),
            "account.profile": ("/account/profile", {
                "first_name": "Ана", "last_name": "Иванова", "grade": "9", "next": "/account/attempts"}),
            "oidc.google_start": ("/auth/google/start", {"next": "/account/attempts"}),
            "oidc.google_link": ("/auth/google/link", {}),
            "oidc.google_link_confirm": ("/auth/google/link/confirm", {"current_password": PASSWORD}),
            "oidc.google_link_cancel": ("/auth/google/link/cancel", {}),
            "registration.student": ("/register/student", {
                "first_name": "Ана", "last_name": "Иванова", "email": "ana.forged@example.bg", "grade": "9",
                "password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD}),
            "registration.teacher": ("/register/teacher", {}),
            "registration.verify": ("/register/verify", {"code": "123456"}),
            "registration.resend": ("/register/resend", {}),
            "registration.cancel": ("/register/cancel", {}),
            "quiz.quiz_start_public": (cls.entry, {}),
            "quiz.quiz_attempt": (attempt, cls.one_answer(cls.open_attempt)),
            "practical.quiz_practical_upload": (f"{attempt}/practical/{DZI_SOURCE}/task/1/upload", {}),
            "teacher.teacher_new": ("/teacher/new", cls.new_assignment_form),
            "teacher.teacher_dzi_training": ("/teacher/dzi-training", {"count": "1"}),
            "teacher.teacher_assignment_edit": (
                f"{assignment}/edit", {**NO_DEADLINE_FORM, "title_bg": "Променено заглавие"}),
            "teacher.teacher_assignment_duplicate": (f"{assignment}/duplicate", NO_DEADLINE_FORM),
            "teacher.teacher_assignment_close": (f"{assignment}/close", {}),
            "teacher.teacher_assignment_results": (
                f"{assignment}/results", {"text_answer_id": "1", "teacher_override": "1"}),
        }

    def test_every_unsafe_rule_is_in_the_sweep(self):
        unsafe_endpoints = {rule.endpoint for rule in application_rules(self.app)
                            if rule.methods & request_integrity.UNSAFE_METHODS}
        self.assertEqual(set(self.unsafe_routes), unsafe_endpoints)

    def test_a_forged_post_is_refused_for_every_visitor_and_changes_nothing(self):
        sessions, files, version = self.sessions(), self.stored_files(), self.data_version()
        for endpoint, (path, form) in self.unsafe_routes.items():
            for who in VISITORS:
                for origin, headers in FORGED_ORIGINS.items():
                    with self.subTest(endpoint=endpoint, visitor=who, origin=origin):
                        response = self.visitor(who).post(path, data=form, headers=headers)
                        self.assertEqual(response.status_code, 403)
                        self.assertEqual(response.headers.getlist("Set-Cookie"), [])
        self.assertEqual(self.data_version(), version, "a refused request committed a change")
        self.assertEqual(self.sessions(), sessions)
        self.assertEqual(self.stored_files(), files)
        # Nobody was signed out, re-keyed or left with a closed test.
        for who in ("student", "teacher", "admin"):
            with self.subTest(still_signed_in=who):
                self.assertEqual(self.clients[who].get("/account/password").status_code, 200)

    def test_a_post_without_origin_and_referer_is_refused_outside_testing(self):
        version = self.data_version()
        with patch.dict(self.app.config, {"TESTING": False}):
            for endpoint, (path, form) in self.unsafe_routes.items():
                with self.subTest(endpoint=endpoint):
                    response = self.clients["admin"].post(path, data=form)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.headers.getlist("Set-Cookie"), [])
        self.assertEqual(self.data_version(), version)

    def test_other_unsafe_methods_and_unknown_urls_are_checked_too(self):
        admin, forged = self.clients["admin"], FORGED_ORIGINS["another site"]
        for method in sorted(request_integrity.UNSAFE_METHODS - {"POST"}):
            with self.subTest(method=method):
                self.assertEqual(admin.open("/logout", method=method, headers=forged).status_code, 403)
                self.assertEqual(admin.open("/logout", method=method, headers=SAME_ORIGIN).status_code, 405)
        # A URL that matches no rule is no way around the check either.
        self.assertEqual(admin.post("/no/such/page", headers=forged).status_code, 403)
        self.assertEqual(admin.post("/no/such/page", headers=SAME_ORIGIN).status_code, 404)
        self.assertEqual(admin.get("/account/password").status_code, 200)

    def test_the_same_requests_from_the_application_itself_are_accepted(self):
        # The refusals above are about the origin and nothing else.
        client = self.app.test_client()
        path, form = self.unsafe_routes["account.login"]
        signed_in = client.post(path, data=form, headers=SAME_ORIGIN)
        self.assertEqual((signed_in.status_code, signed_in.headers["Location"]), (302, "/teacher/new"))
        self.assertEqual(client.get("/teacher/new").status_code, 200)
        signed_out = client.post("/logout", headers=SAME_ORIGIN)
        self.assertEqual((signed_out.status_code, signed_out.headers["Location"]), (302, "/"))
        self.assertEqual(client.get("/teacher/new").status_code, 302)


if __name__ == "__main__":
    unittest.main()

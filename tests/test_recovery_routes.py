"""Password recovery 8.3: the routes, the browser slot, texts, headers and the sign-in integration.

Test plan sections RB4, RB5, RW1 to RW10, RLog4 and the route-level halves of RV and RT
(docs/reviews/PASSWORD_RECOVERY_TEST_PLAN.md). Every test has its own temporary database built
from migrations and canonical data; none is ``data/questions.db``. Mail is ``InMemoryMailer``:
nothing is sent and no socket is opened. The clock is injected; nothing sleeps for a limit.
The wording compared here is the proposed wording of design 2.7 and 2.8: RD15 (the exact
Bulgarian texts) is open, and these tests change with it.
"""

import re
import unittest
from datetime import timedelta
from unittest.mock import patch

from markupsafe import escape

from tests.recovery_route_support import (
    CROSS_ORIGIN,
    EMAIL,
    NEW_PASSWORD,
    OLD_PASSWORD,
    ROUTES,
    SAME_ORIGIN,
    RecoveryRouteTestCase,
)
from web.recovery_settings import CONFIG_KEY
from web.services import accounts, recovery as recovery_service


WRONG = "00000000"
NO_RECOVERY = "Заявката за нова парола е изтекла или е започната от друг браузър. Започни отново."


def normalized(page):
    """A page with the request path of the sign-in link taken out (the 404 page links to /login?next=<path>)."""
    return re.sub(r"next=[^\"&]*", "next=", page)


def fields(markup):
    """``{name: attributes}`` of the form controls of a page."""
    return {name: attributes for attributes, name in
            ((m.group(0), m.group(1)) for m in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*>', markup))}


class DisabledTest(RecoveryRouteTestCase):
    """RW3: the generic 404 after the same-origin check, for every visitor, and no link."""

    def setUp(self):
        super().setUp()
        self.app.config[CONFIG_KEY] = None

    def test_every_route_is_the_generic_404_for_an_anonymous_visitor(self):
        generic = normalized(self.body(self.client.get("/definitely-not-a-page")))
        for method, path in (("get", "/recover"), ("post", "/recover"), ("get", "/recover/verify"),
                             ("post", "/recover/verify"), ("post", "/recover/resend"), ("post", "/recover/cancel")):
            with self.subTest(method=method, path=path):
                response = getattr(self.client, method)(path, headers=SAME_ORIGIN)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(normalized(self.body(response)), generic)

    def test_a_signed_in_visitor_gets_the_same_404_not_a_redirect(self):
        for role in ("student", "teacher", "admin"):
            client = self.app.test_client()
            account = self.make(role)
            self.sign_in(account, client=client)
            for path in ("/recover", "/recover/verify"):
                with self.subTest(role=role, path=path):
                    self.assertEqual(client.get(path).status_code, 404)
            for path in ROUTES:
                with self.subTest(role=role, post=path):
                    self.assertEqual(client.post(path, headers=SAME_ORIGIN).status_code, 404)

    def test_the_same_origin_check_comes_first(self):
        for path in ROUTES:
            with self.subTest(path=path):
                response = self.client.post(path, headers=CROSS_ORIGIN)
                self.assertEqual(response.status_code, 403)
                self.assertCookieless(response)

    def test_nothing_is_written_read_dispatched_or_set(self):
        before = self.digest()
        for path in ROUTES:
            response = self.client.post(path, data={"email": EMAIL, "code": "12345678"}, headers=SAME_ORIGIN)
            self.assertEqual(response.status_code, 404)
            self.assertCookieless(response)
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.mailer.messages, [])

    def test_login_has_no_link_and_no_notice(self):
        for url in ("/login", "/login?recovery=done"):
            page = self.body(self.client.get(url))
            self.assertNotIn("/recover", page)
            self.assertNotIn("Забравена парола", page)
            self.assertNotIn("Паролата е сменена", page)

    def test_a_missing_configuration_and_a_foreign_object_both_disable_it(self):
        for value in (None, object(), {"mailer": self.mailer}):
            self.app.config[CONFIG_KEY] = value
            with self.subTest(value=type(value).__name__):
                self.assertEqual(self.client.get("/recover").status_code, 404)
        del self.app.config[CONFIG_KEY]
        self.assertEqual(self.client.get("/recover").status_code, 404)


class GateTurnedOffTest(RecoveryRouteTestCase):
    def test_pending_rows_stay_unreachable_and_redeem_nothing_when_the_gate_is_turned_off(self):
        """RP4 at the routes: a recovery in progress, then the gate off: every route 404, a correct code
        redeems nothing, the row waits for the cleanup."""
        self.verified()
        self.recover()
        code = self.last_code()
        row = self.recoveries()[0]
        self.app.config[CONFIG_KEY] = None
        before = self.digest()
        for path in ("/recover", "/recover/verify"):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.redeem(code).status_code, 404)
        self.assertEqual(self.resend().status_code, 404)
        self.assertEqual(self.cancel().status_code, 404)
        self.assertEqual(self.digest(), before)
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))
        self.assertFalse(self.authenticates(EMAIL, NEW_PASSWORD))
        self.assertEqual(self.recoveries()[0]["state"], "pending")
        self.assertEqual(self.recoveries()[0]["id"], row["id"])


class MethodAndIntegrityTest(RecoveryRouteTestCase):
    """RW1: declared methods, access and integrity; cross-origin POSTs are 403 and write nothing."""

    def test_every_route_declares_public_access_and_same_origin_integrity(self):
        from web import access, request_integrity

        rules = {rule.endpoint: rule for rule in self.app.url_map.iter_rules() if rule.rule.startswith("/recover")}
        self.assertEqual({rule.rule for rule in rules.values()}, set(ROUTES))
        expected = {"recovery.start": {"GET", "POST"}, "recovery.verify": {"GET", "POST"},
                    "recovery.resend": {"POST"}, "recovery.cancel": {"POST"}}
        for endpoint, methods in expected.items():
            rule = rules[endpoint]
            self.assertEqual(rule.methods - {"HEAD", "OPTIONS"}, methods, endpoint)
            view = self.app.view_functions[endpoint]
            self.assertEqual(access.policy_for_view(view), access.PUBLIC, endpoint)
            self.assertEqual(request_integrity.integrity_policy_for_view(view), request_integrity.SAME_ORIGIN, endpoint)

    def test_other_methods_are_refused(self):
        for path in ("/recover/resend", "/recover/cancel"):
            with self.subTest(get=path):
                self.assertEqual(self.client.get(path).status_code, 405)
        for path in ROUTES:
            for method in ("put", "patch", "delete"):
                with self.subTest(method=method, path=path):
                    response = getattr(self.client, method)(path, headers=SAME_ORIGIN)
                    self.assertEqual(response.status_code, 405)

    def test_cross_origin_posts_are_403_and_write_nothing_for_every_visitor(self):
        account = self.verified()
        self.recover()
        code = self.last_code()
        signed_in = self.app.test_client()
        self.sign_in(self.make("student"), client=signed_in)
        before, sent = self.digest(), len(self.mailer.messages)
        for client in (self.client, self.app.test_client(), signed_in):
            for path, data in (("/recover", {"email": EMAIL}),
                               ("/recover/verify", {"code": code, "password": NEW_PASSWORD,
                                                    "confirm_password": NEW_PASSWORD}),
                               ("/recover/resend", {}), ("/recover/cancel", {})):
                for headers in (CROSS_ORIGIN, {"Referer": "http://evil.example/recover"},
                                {"Origin": "http://localhost.evil.example"}):
                    with self.subTest(path=path, headers=headers):
                        response = client.post(path, data=data, headers=headers)
                        self.assertEqual(response.status_code, 403)
                        self.assertCookieless(response)
        self.assertEqual(self.digest(), before)
        self.assertEqual(len(self.mailer.messages), sent)
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))
        self.assertEqual(account["email"], EMAIL)

    def test_a_post_with_no_origin_and_no_referer_is_refused_outside_testing(self):
        with patch.dict(self.app.config, {"TESTING": False}):
            for path in ROUTES:
                with self.subTest(path=path):
                    self.assertEqual(self.client.post(path, data={"email": EMAIL}).status_code, 403)
            self.assertEqual(self.client.post("/recover", data={"email": "bad"},
                                              headers={"Referer": "http://localhost/recover"}).status_code, 200)

    def test_head_and_options_are_reads_and_write_nothing(self):
        self.verified()
        before = self.digest()
        # A HEAD that carries a body is still a read: it must not submit anything.
        response = self.client.open("/recover", method="HEAD", data={"email": EMAIL}, headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.options("/recover").status_code, 200)
        self.recover()
        code = self.last_code()
        before_verify = self.digest()
        response = self.client.open("/recover/verify", method="HEAD", headers=SAME_ORIGIN, data={
            "code": code, "password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.digest(), before_verify)
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))
        self.assertEqual(len(self.mailer.messages), 1)
        self.assertNotEqual(before, before_verify)  # the one request in between did write


class SignedInTest(RecoveryRouteTestCase):
    """RW4: a signed-in visitor starts nothing and is sent home; the session is unchanged."""

    HOMES = {"student": "/", "teacher": "/teacher/new", "admin": "/teacher"}

    def test_gets_redirect_home_and_posts_start_nothing(self):
        for role, home in self.HOMES.items():
            client = self.app.test_client()
            account = self.make(role)
            self.sign_in(account, client=client)
            state, before = self.session(client), self.digest()
            for path in ROUTES:
                for method in ("get", "post"):
                    if method == "get" and path in ("/recover/resend", "/recover/cancel"):
                        continue
                    with self.subTest(role=role, method=method, path=path):
                        response = getattr(client, method)(
                            path, headers=SAME_ORIGIN, **({"data": {"email": EMAIL}} if method == "post" else {}))
                        self.assertEqual(response.status_code, 303)
                        self.assertEqual(response.headers["Location"], home)
                        self.assertCookieless(response)
            self.assertEqual(self.session(client), state)
            self.assertEqual(self.digest(), before)
        self.assertEqual(self.mailer.messages, [])

    def test_a_signed_in_browser_that_holds_a_slot_is_not_served_by_it(self):
        self.verified()
        self.recover()
        code = self.last_code()
        account = self.make("student")
        self.sign_in(account)                       # the sign-in itself drops the slot (RW6)
        self.assertIsNone(self.slot())
        self.set_slot({"v": 1, "t": "A" * 43, "next": ""})
        response = self.redeem(code)
        self.assertEqual((response.status_code, response.headers["Location"]), (303, "/"))
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))

    def test_a_stale_account_session_is_cleaned_as_before_and_recovery_proceeds_anonymously(self):
        self.verified()
        account = self.make("student")
        self.sign_in(account)
        self.execute("UPDATE users SET is_active = 0 WHERE id = ?", (account["id"],))
        response = self.client.get("/recover")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("uid", self.session())    # the application's own cleanup, unchanged


class RequestTest(RecoveryRouteTestCase):
    def test_the_form_is_accessible_and_sets_no_cookie(self):
        response = self.client.get("/recover?next=/account/attempts")
        page = self.body(response)
        self.assertEqual(response.status_code, 200)
        self.assertCookieless(response)
        self.assertIn("<h1>Нова парола</h1>", page)
        self.assertIn('<label for="email">Имейл</label>', page)
        self.assertRegex(page, r'<input id="email" name="email" type="email"[^>]*autocomplete="email"[^>]*required')
        self.assertIn('name="next" value="/account/attempts"', page)
        self.assertIn(">Изпрати код</button>", page)
        self.assertIn('method="post" action="/recover"', page)
        self.assertNotIn("aria-invalid", page)

    def test_a_request_for_a_verified_account_issues_one_code_and_redirects(self):
        account = self.verified()
        response = self.begin()
        self.assertEqual((response.status_code, response.headers["Location"]), (303, "/recover/verify"))
        self.assertEqual(len(self.mailer.messages), 1)
        message = self.mailer.messages[0]
        self.assertEqual(message.to, EMAIL)
        self.assertEqual(message.subject, "Код за нова парола в LearnPilot")
        self.assertRegex(message.text, r"^Кодът ти за нова парола е: \d{8}\n")
        row, = self.recoveries()
        self.assertEqual((row["state"], row["is_shadow"], row["user_id"], row["email"]), ("pending", 0, account["id"], EMAIL))
        send, = self.sends()
        self.assertEqual((send["kind"], send["outcome"], send["recovery_id"], send["pending_id"]),
                         ("code", "accepted", row["id"], None))
        self.assertIsNotNone(send["settled_at"])
        self.assertEqual(len(self.last_code()), 8)

    def test_a_request_for_anything_else_sends_one_neutral_notice_to_the_typed_address(self):
        self.cli_account("legacy@example.bg")
        for index, email in enumerate(("nobody@example.bg", "legacy@example.bg")):
            client = self.app.test_client()
            self.recover(email, client=client)
            message = self.mailer.messages[-1]
            self.assertEqual(message.to, email)
            self.assertEqual(message.subject, "Заявка за нова парола в LearnPilot")
            self.assertNotRegex(message.text, r"\d{6}")
            self.assertNotIn("http", message.text)
            self.assertEqual(self.recoveries()[index]["is_shadow"], 1)
            self.assertEqual(self.sends()[index]["kind"], "notice")

    def test_the_slot_holds_only_a_token_and_the_validated_next(self):
        self.verified()
        self.recover(next="/account/attempts")
        slot = self.slot()
        self.assertEqual(set(slot), {"v", "t", "next"})
        self.assertEqual((slot["v"], slot["next"]), (1, "/account/attempts"))
        self.assertRegex(slot["t"], r"^[A-Za-z0-9_-]{43}$")
        state = self.session()
        self.assertEqual(set(state), {"rec"})
        serialized = repr(state)
        for secret in (EMAIL, self.last_code(), "ana", "password"):
            self.assertNotIn(secret, serialized)

    def test_no_recovery_value_is_in_a_url_a_header_or_the_cookie_text(self):
        self.verified()
        response = self.recover()
        code = self.last_code()
        cookie = " ".join(response.headers.getlist("Set-Cookie"))
        page = self.body(self.client.get("/recover/verify"))
        for text in (response.headers["Location"], cookie):
            for secret in (EMAIL, code, "ana.ivanova", NEW_PASSWORD):
                self.assertNotIn(secret, text)
        self.assertNotIn(code, page)
        self.assertEqual(response.headers["Location"], "/recover/verify")

    def test_an_invalid_address_is_a_field_error_independent_of_any_account(self):
        self.verified()
        before = self.digest()
        for value in ("", "   ", "not an address", "ana@", "ана@example.bg", "a@b", "a b@example.bg", "ana@example.bg\r\nBcc: x@y.bg"):
            with self.subTest(value=value):
                response = self.begin(value)
                page = self.body(response)
                self.assertEqual(response.status_code, 200)
                self.assertIn('<p class="error field-error" id="email-error" role="alert">Невалиден имейл.</p>', page)
                self.assertIn('aria-invalid="true" aria-describedby="email-error"', page)
        response = self.client.post("/recover", data={}, headers=SAME_ORIGIN)   # the field is missing
        self.assertIn("Невалиден имейл.", self.body(response))
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.mailer.messages, [])
        self.assertIsNone(self.slot())

    def test_the_address_is_normalized_and_found_whatever_the_case(self):
        self.verified()
        self.recover("  ANA.Ivanova@EXAMPLE.bg ")
        self.assertEqual(self.mailer.messages[0].to, EMAIL)
        self.assertEqual(self.recoveries()[0]["is_shadow"], 0)

    def test_a_second_request_in_the_same_browser_voids_and_scrubs_the_first(self):
        self.verified()
        self.recover()
        first_code = self.last_code()
        first = self.recoveries()[0]["id"]
        self.advance(61)
        self.recover()
        rows = {row["id"]: row for row in self.recoveries()}
        self.assertEqual(rows[first]["state"], "void")
        self.assertIsNone(rows[first]["email"])
        self.assertIsNone(rows[first]["code_hash"])
        self.assertEqual(self.redeem(first_code).status_code, 200)    # the first code is dead
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))

    def test_the_address_cap_answers_429_with_the_notice_and_writes_nothing(self):
        self.verified()
        for _ in range(5):
            self.advance(1)
            self.recover(client=self.app.test_client())
        before = self.digest()
        response = self.begin(client=self.app.test_client())
        self.assertEqual(response.status_code, 429)
        self.assertIn("Твърде много имейли към този адрес. Опитай отново по-късно.", self.body(response))
        self.assertIn('value="ana.ivanova@example.bg"', self.body(response))
        self.assertEqual(self.digest(), before)
        self.assertEqual(len(self.mailer.messages), 5)

    def test_an_unavailable_service_answers_503_and_writes_nothing(self):
        self.verified()
        for reason, setup in (("hold", lambda: self.set_control(hold="restore")),
                              ("clock", lambda: self.set_control(high_water=self.now + timedelta(days=1)))):
            setup()
            before = self.digest()
            response = self.begin(client=self.app.test_client())
            with self.subTest(reason=reason):
                self.assertEqual(response.status_code, 503)
                self.assertIn("Възстановяването на парола временно не е достъпно. Опитай по-късно.", self.body(response))
                self.assertEqual(self.digest(), before)
        self.assertEqual(self.mailer.messages, [])
        self.assertIsNone(self.slot())

    def test_a_busy_sender_and_an_exhausted_total_budget_answer_503(self):
        self.verified()
        with patch("web.services.mail.sender_busy", return_value=True):
            self.assertEqual(self.begin().status_code, 503)
        self.assertEqual(self.mailer.messages, [])
        from web.recovery_settings import RecoverySettings
        self.app.config[CONFIG_KEY] = RecoverySettings(mailer=self.mailer, total_cap=1)
        self.recover(client=self.app.test_client())
        self.assertEqual(self.begin("other@example.bg", client=self.app.test_client()).status_code, 503)
        self.assertEqual(len(self.mailer.messages), 1)


class VerifyPageTest(RecoveryRouteTestCase):
    def test_the_page_shows_the_address_this_browser_typed_with_the_proposed_texts(self):
        self.verified()
        self.recover(next="/account/attempts")
        response = self.client.get("/recover/verify")
        page = self.body(response)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Ако за ana.ivanova@example.bg може да се зададе нова парола с код, изпратихме на този "
                      "адрес 8-цифрен код. Въведи го заедно с новата парола.", page)
        for text in ("Код", "Нова парола", "Повтори новата парола", "Запази новата парола", "Изпрати нов код",
                     "Грешен имейл? Започни отново"):
            self.assertIn(text, page)
        self.assertNotIn(self.last_code(), page)
        self.assertCookieless(response)

    def test_the_inputs_carry_the_autocomplete_and_accessibility_hints_of_design_2_6(self):
        self.verified()
        self.recover()
        page = self.body(self.client.get("/recover/verify"))
        self.assertRegex(page, r'<input id="code" name="code" type="text" inputmode="numeric" '
                               r'autocomplete="one-time-code"[^>]*required')
        for name in ("password", "confirm_password"):
            self.assertRegex(page, rf'<input id="{name}" name="{name}" type="password" autocomplete="new-password" '
                                   rf'maxlength="256" required')
            self.assertIn(f'<label for="{name}">', page)
        self.assertIn('<label for="code">', page)
        self.assertIn('autocomplete="off"', page)    # the form: nothing is remembered
        for attributes in fields(page).values():
            self.assertNotIn("value=", attributes.replace('type="hidden"', ""))
        # Three separate forms: the reset, the resend and the cancel, each a POST to its own route.
        self.assertEqual(re.findall(r'<form method="post" action="([^"]+)"', page),
                         ["/recover/verify", "/recover/resend", "/recover/cancel"])

    def test_resent_selects_a_fixed_notice_and_echoes_nothing(self):
        self.verified()
        self.recover()
        for value, shown in (("1", True), ("0", False), ("<b>x</b>", False), ("", False)):
            page = self.body(self.client.get("/recover/verify", query_string={"resent": value}))
            with self.subTest(value=value):
                self.assertEqual("Изпратихме нов код." in page, shown)
                self.assertNotIn("&lt;b&gt;", page)

    def test_without_a_slot_the_page_goes_back_to_the_form(self):
        response = self.client.get("/recover/verify")
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/recover"))
        self.assertCookieless(response)

    def test_a_slot_whose_row_is_gone_shows_the_no_usable_recovery_page_and_sets_no_cookie(self):
        self.verified()
        self.recover()
        self.execute("DELETE FROM password_recoveries")
        response = self.client.get("/recover/verify")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Заявката за нова парола е изтекла или е започната от друг браузър. Започни отново.",
                      self.body(response))
        self.assertCookieless(response)
        self.assertIsNotNone(self.slot())      # a GET does not touch the slot

    def test_html_in_a_kept_next_is_escaped_in_every_place_it_is_written(self):
        """accepted_next keeps a path with quotes and angle brackets; the pages must escape it."""
        hostile = '/x"><script>alert(1)</script>'
        self.verified()
        form = self.body(self.client.get("/recover", query_string={"next": hostile}))
        self.assertNotIn("<script>alert", form)
        self.assertIn(str(escape(hostile)), form)
        self.recover(next=hostile)
        for page in (self.body(self.client.get("/recover/verify")),
                     self.body(self.redeem("00000000", NEW_PASSWORD[:3]))):
            self.assertNotIn("<script>alert", page)


class RedeemTest(RecoveryRouteTestCase):
    def reset(self, **kwargs):
        account = self.verified(**kwargs)
        self.recover(kwargs.get("email", EMAIL))
        return account, self.last_code()

    def test_the_full_journey_ends_at_login_with_no_session_and_the_new_password_works(self):
        account, code = self.reset()
        before_throttle = self.throttle_rows()
        response = self.redeem(code)
        self.assertEqual((response.status_code, response.headers["Location"]), (303, "/login?recovery=done"))
        self.assertNoAccountSession()
        self.assertIsNone(self.slot())
        self.assertEqual(self.session(), {})
        row, = self.recoveries()
        self.assertEqual(row["state"], "redeemed")
        for column in ("email", "user_id", "session_key_digest", "code_hash", "code_expires_at"):
            self.assertIsNone(row[column])
        self.assertTrue(self.authenticates(EMAIL, NEW_PASSWORD))
        self.assertFalse(self.authenticates(EMAIL, OLD_PASSWORD))
        self.assertNotEqual(self.account(account["id"])["session_key"], account["session_key"])
        self.assertEqual(self.throttle_rows(), before_throttle)
        # No code is sent or asked at sign-in (D7): the next page is the ordinary one.
        page = self.body(self.client.get(response.headers["Location"]))
        self.assertIn("Паролата е сменена. Влез с новата парола.", page)
        signed = self.login(EMAIL, NEW_PASSWORD)
        self.assertEqual((signed.status_code, signed.headers["Location"]), (302, "/"))
        self.assertEqual(len(self.mailer.messages), 1)

    def test_the_validated_next_is_kept_through_the_whole_journey(self):
        account, _ = self.reset()
        self.advance(61)
        self.client = self.app.test_client()
        self.recover(next="/account/attempts")
        response = self.redeem(self.last_code())
        self.assertEqual(response.headers["Location"], "/login?next=/account/attempts&recovery=done")
        page = self.body(self.client.get(response.headers["Location"]))
        self.assertIn('name="next" value="/account/attempts"', page)
        signed = self.login(EMAIL, NEW_PASSWORD, next="/account/attempts")
        self.assertEqual(signed.headers["Location"], "/account/attempts")
        self.assertEqual(account["email"], EMAIL)

    def test_the_password_is_stored_exactly_as_typed(self):
        """RB3: spaces, a tab, a doubled space and a non-NFKC character are part of the password."""
        for index, typed in enumerate(("  lead and trail  ", "double  space  inside", "tab\tinside password",
                                       "ﬁne letters ﬁnish", "x" * 256)):
            email = f"exact{index}@example.bg"
            self.verified(email)
            client = self.app.test_client()
            self.recover(email, client=client)
            response = self.redeem(self.last_code(), typed, client=client)
            with self.subTest(password=typed[:12]):
                self.assertEqual(response.status_code, 303, self.body(response))
                self.assertTrue(self.authenticates(email, typed))
                for variant in (typed.strip(), " ".join(typed.split()), typed.replace("ﬁ", "fi"), typed + " "):
                    if variant != typed:
                        self.assertFalse(self.authenticates(email, variant), repr(variant))

    def test_password_errors_rerender_the_form_and_count_no_attempt_read_no_code_and_write_nothing(self):
        """RB4 and the validation order: nothing before the transaction writes."""
        account, code = self.reset(email="exact.mail@example.bg")
        before = self.digest()
        redeems = self.spy(recovery_service, "redeem")
        cases = {
            "too short": (code, "short", "short", "password", "Паролата трябва да е поне 10 символа."),
            "equals the email": (code, "exact.mail@example.bg", "exact.mail@example.bg", "password",
                                 "Паролата не може да съвпада с имейла."),
            "equals the email, folded": (code, "EXACT.mail@EXAMPLE.bg", "EXACT.mail@EXAMPLE.bg", "password",
                                         "Паролата не може да съвпада с имейла."),
            "mismatch": (code, NEW_PASSWORD, NEW_PASSWORD + "x", "confirm_password", "Паролите не съвпадат."),
            "too long": (code, "x" * 257, "x" * 257, "password", "Паролата трябва да е най-много 256 символа."),
        }
        for name, (typed_code, password, confirmation, field, message) in cases.items():
            response = self.redeem(typed_code, password, confirmation)
            page = self.body(response)
            with self.subTest(case=name):
                self.assertEqual(response.status_code, 200)
                self.assertIn(f'id="{field}-error" role="alert">{message}</p>', page)
                self.assertIn(f'aria-invalid="true" aria-describedby="{field}-error"', page)
                self.assertNotIn(code, page)
                # Not echoed: no control carries a value (the address itself is in the introduction).
                for name, attributes in fields(page).items():
                    self.assertNotIn("value=", attributes, name)
                self.assertEqual(self.digest(), before)
        self.assertEqual(redeems, [])
        self.assertEqual(self.recoveries()[0]["code_attempts"], 0)
        # The code is not looked at either: a wrong one with a bad password is a password error only.
        self.assertIn("Паролите не съвпадат.", self.body(self.redeem("00000000", NEW_PASSWORD, "different")))
        self.assertEqual(self.recoveries()[0]["code_attempts"], 0)
        self.assertEqual(account["email"], "exact.mail@example.bg")

    def test_both_password_errors_show_together_and_a_missing_field_is_a_password_error(self):
        _, code = self.reset()
        page = self.body(self.redeem(code, "short", "other"))
        self.assertIn('id="password-error"', page)
        self.assertIn('id="confirm_password-error"', page)
        page = self.body(self.client.post("/recover/verify", data={"code": code}, headers=SAME_ORIGIN))
        self.assertIn("Паролата трябва да е поне 10 символа.", page)
        self.assertEqual(self.recoveries()[0]["code_attempts"], 0)

    def test_a_wrong_code_is_counted_and_the_form_asks_again_without_echoing_anything(self):
        account, code = self.reset()
        wrong = "00000000" if code != "00000000" else "11111111"
        response = self.redeem(wrong)
        page = self.body(response)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="code-error" role="alert">Грешен или изтекъл код.</p>', page)
        self.assertIn('aria-invalid="true" aria-describedby="code-error"', page)
        self.assertNotIn(wrong, page)
        self.assertNotIn(NEW_PASSWORD.strip(), page)
        self.assertEqual(self.recoveries()[0]["code_attempts"], 1)
        self.assertIsNotNone(self.slot())
        self.assertNotRegex(fields(page)["code"], r'value="')
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))
        self.assertEqual(account["id"], self.recoveries()[0]["user_id"])
        # The correct code still works afterwards.
        self.assertEqual(self.redeem(code).status_code, 303)

    def test_malformed_codes_count_as_wrong_codes(self):
        _, code = self.reset()
        for index, typed in enumerate(("", "1234567", "123456789", "1234 5678", "١٢٣٤٥٦٧٨", "abcdefgh", "123456")):
            with self.subTest(code=typed):
                page = self.body(self.redeem(typed))
                self.assertIn("Грешен или изтекъл код.", page)
                self.assertEqual(self.recoveries()[0]["code_attempts"], min(index + 1, 5))
                if index == 4:
                    break
        self.assertIn("Твърде много грешни опити. Поискай нов код.", self.body(self.redeem("abcdefgh")))

    def test_five_wrong_codes_then_the_correct_one_is_refused_and_a_resend_makes_a_new_one_work(self):
        _, code = self.reset()
        wrong = "00000000" if code != "00000000" else "11111111"
        for _ in range(5):
            self.assertIn("Грешен или изтекъл код.", self.body(self.redeem(wrong)))
        page = self.body(self.redeem(code))
        self.assertIn("Твърде много грешни опити. Поискай нов код.", page)
        self.assertEqual(self.recoveries()[0]["code_attempts"], 5)
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))
        self.advance(61)
        self.assertEqual(self.resend().status_code, 303)
        self.assertEqual(self.recoveries()[0]["code_attempts"], 0)
        self.assertEqual(self.redeem(self.last_code()).status_code, 303)
        self.assertTrue(self.authenticates(EMAIL, NEW_PASSWORD))

    def test_a_code_past_ten_minutes_is_refused_without_counting_and_a_recovery_past_thirty_ends(self):
        _, code = self.reset()
        self.advance(601)
        page = self.body(self.redeem(code))
        self.assertIn("Грешен или изтекъл код.", page)
        self.assertEqual(self.recoveries()[0]["code_attempts"], 0)
        self.advance(1300)             # 1901 s since the request: past the 30 minutes
        before = self.digest()
        page = self.body(self.redeem(code))
        self.assertIn("Заявката за нова парола е изтекла или е започната от друг браузър. Започни отново.", page)
        # Past its time by the read (design 2.4, step 2): the same page as for no row, nothing
        # written; the row is expired and scrubbed by the next cleanup, not by this request.
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.recoveries()[0]["state"], "pending")
        self.assertIsNone(self.slot())
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))

    def test_another_browser_without_the_slot_reads_nothing_by_code_or_address(self):
        """RW5: the correct code from a browser that did not ask is the 'no usable recovery' page."""
        _, code = self.reset()
        other = self.app.test_client()
        views = self.spy(recovery_service, "pending_view")
        redeems = self.spy(recovery_service, "redeem")
        before = self.digest()
        page = self.body(self.redeem(code, client=other))
        self.assertIn("Заявката за нова парола е изтекла или е започната от друг браузър. Започни отново.", page)
        self.assertEqual((views, redeems), ([], []))
        self.assertEqual(self.digest(), before)
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))
        self.assertEqual(self.session(other), {})

    def test_a_repeat_after_a_success_whose_response_was_lost_writes_nothing(self):
        """RV5 at the routes: the row is redeemed and the browser still holds its slot. Design 2.4 reads
        the row first, and a final row is the 'no usable recovery' page there (step 2), so the
        ``NOT_PENDING (redeemed)`` redirect of the service is not reached by this order; nothing is
        written, nothing claims a second reset, and the password stays the new one (the design
        ambiguity is reported in PASSWORD_RECOVERY_8_3_STATUS.md)."""
        _, code = self.reset()
        slot = self.slot()
        first = self.redeem(code)
        self.assertEqual(first.status_code, 303)
        self.set_slot(slot)
        before = self.digest()
        repeat = self.redeem(code)
        self.assertEqual(repeat.status_code, 200)
        self.assertIn("Заявката за нова парола е изтекла или е започната от друг браузър. Започни отново.",
                      self.body(repeat))
        self.assertIsNone(self.slot())
        self.assertEqual(self.digest(), before)
        self.assertTrue(self.authenticates(EMAIL, NEW_PASSWORD))

    def test_success_redirects_to_login_never_to_a_next_directly(self):
        self.verified()
        for hostile in ("//evil.example/x", "https://evil.example", "/\\evil.example", "javascript:alert(1)"):
            client = self.app.test_client()
            self.recover(client=client, next=hostile)
            self.assertEqual(self.slot(client)["next"], "")
            response = self.redeem(self.last_code(), client=client)
            with self.subTest(next=hostile):
                self.assertEqual(response.headers["Location"], "/login?recovery=done")
                self.assertNotIn("evil", response.headers["Location"])
            self.execute("UPDATE users SET password_hash = ? WHERE email = ?",
                         (accounts.hash_new_password(OLD_PASSWORD, EMAIL), EMAIL))
            self.advance(2)


class ResendAndCancelTest(RecoveryRouteTestCase):
    def test_resend_replaces_the_code_and_goes_back_to_the_page_with_the_notice(self):
        self.verified()
        self.recover()
        first = self.last_code()
        self.advance(61)
        response = self.resend()
        self.assertEqual((response.status_code, response.headers["Location"]), (303, "/recover/verify?resent=1"))
        self.assertEqual(len(self.mailer.messages), 2)
        second = self.last_code()
        self.assertIn("Изпратихме нов код.", self.body(self.client.get(response.headers["Location"])))
        if first != second:
            self.assertIn("Грешен или изтекъл код.", self.body(self.redeem(first)))
        self.assertEqual([send["outcome"] for send in self.sends()], ["accepted", "accepted"])
        self.assertEqual(self.redeem(second).status_code, 303)

    def test_resend_inside_the_wait_after_four_sends_and_without_a_slot(self):
        self.verified()
        self.recover()
        before = self.digest()
        page = self.body(self.resend())
        self.assertIn("Можеш да поискаш нов код след малко.", page)
        self.assertEqual(self.digest(), before)
        for _ in range(3):
            self.advance(61)
            self.assertEqual(self.resend().status_code, 303)
        self.advance(61)
        response = self.resend()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Не можеш да поискаш повече кодове. Започни отначало.", self.body(response))
        self.assertEqual(len(self.mailer.messages), 4)
        other = self.app.test_client()
        page = self.body(self.resend(client=other))
        self.assertIn("Заявката за нова парола е изтекла или е започната от друг браузър. Започни отново.", page)

    def test_resend_answers_429_at_the_address_cap_and_503_when_unavailable(self):
        """The address caps are shared by every browser that asked for the address."""
        self.verified()
        self.recover()                                          # this browser's first send
        for _ in range(4):
            self.advance(1)
            self.recover(client=self.app.test_client())          # four more: five in the hour
        self.advance(61)
        before, sent = self.digest(), len(self.mailer.messages)
        capped = self.resend()
        self.assertEqual(capped.status_code, 429)
        self.assertIn("Твърде много имейли към този адрес. Опитай отново по-късно.", self.body(capped))
        self.assertEqual((self.digest(), len(self.mailer.messages)), (before, sent))
        self.assertIsNotNone(self.slot())
        # A hold, then a clock behind the mark: unavailable, nothing written, the slot kept.
        self.advance(3601)
        for label, setup in (("hold", lambda: self.set_control(hold="restore")),
                             ("clock", lambda: (self.execute("UPDATE registration_control SET hold_reason = NULL, "
                                                             "hold_set_at = NULL"),
                                                self.set_control(high_water=self.now + timedelta(days=1))))):
            setup()
            before = self.digest()
            response = self.resend()
            with self.subTest(reason=label):
                self.assertEqual(response.status_code, 503)
                self.assertEqual(self.digest(), before)
                self.assertIsNotNone(self.slot())

    def test_cancel_voids_and_scrubs_drops_the_slot_and_answers_the_same_303_for_every_state(self):
        """RW10: a pending row, a final row and an absent row all give the same redirect."""
        self.verified()
        self.recover(next="/account/attempts")
        row = self.recoveries()[0]["id"]
        expected = "/recover?next=/account/attempts"
        pending = self.cancel()
        self.assertEqual((pending.status_code, pending.headers["Location"]), (303, expected))
        self.assertIsNone(self.slot())
        stored = self.recoveries()[0]
        self.assertEqual((stored["id"], stored["state"]), (row, "void"))
        self.assertIsNone(stored["email"])
        self.assertIsNone(stored["code_hash"])
        # A final row (the slot is put back) and an absent one.
        slot = {"v": 1, "t": "B" * 43, "next": "/account/attempts"}
        self.set_slot({**slot})
        absent = self.cancel()
        self.assertEqual((absent.status_code, absent.headers["Location"]), (303, expected))
        self.recover(next="/account/attempts")
        again = self.slot()
        self.redeem(self.last_code())
        self.set_slot(again)
        final = self.cancel()
        self.assertEqual((final.status_code, final.headers["Location"]), (303, expected))
        self.assertEqual(self.recoveries()[1]["state"], "redeemed")
        # No slot at all.
        none = self.cancel()
        self.assertEqual((none.status_code, none.headers["Location"]), (303, "/recover"))

    def test_cancel_during_a_restore_hold_leaves_the_hold_and_the_mark_alone(self):
        self.verified()
        self.recover()
        self.set_control(high_water=self.now, hold="restore")
        control = self.control()
        response = self.cancel()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.recoveries()[0]["state"], "void")
        self.assertEqual(self.control(), control)

    def test_cancel_with_the_clock_behind_the_mark_voids_nothing_and_keeps_the_slot(self):
        self.verified()
        self.recover()
        self.set_control(high_water=self.now + timedelta(hours=2))
        before = self.digest()
        response = self.cancel()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.digest(), before)
        self.assertIsNotNone(self.slot())


class SlotShapeTest(RecoveryRouteTestCase):
    """RW5: a forged or malformed slot is ignored, as the registration slot is."""

    GOOD = "A" * 43

    def forged(self):
        return [
            None, "A" * 43, [], 5,
            {}, {"v": 1}, {"v": 1, "t": self.GOOD}, {"v": 1, "t": self.GOOD, "next": "", "extra": 1},
            {"v": 2, "t": self.GOOD, "next": ""}, {"v": "1", "t": self.GOOD, "next": ""},
            {"v": True, "t": self.GOOD, "next": ""}, {"v": 1.0, "t": self.GOOD, "next": ""},
            {"v": 1, "t": "A" * 42, "next": ""}, {"v": 1, "t": "A" * 44, "next": ""},
            {"v": 1, "t": "A" * 42 + "!", "next": ""}, {"v": 1, "t": "A" * 42 + " ", "next": ""},
            {"v": 1, "t": 12, "next": ""}, {"v": 1, "t": None, "next": ""},
            {"v": 1, "t": self.GOOD, "next": None}, {"v": 1, "t": self.GOOD, "next": 5},
            {"v": 1, "t": self.GOOD, "next": "//evil.example"}, {"v": 1, "t": self.GOOD, "next": "https://evil.example"},
            {"v": 1, "t": self.GOOD, "next": "/a b"}, {"v": 1, "t": self.GOOD, "next": "/x‮"},
            {"v": 1, "t": self.GOOD, "next": "/" + "a" * 600},
        ]

    def test_a_slot_of_any_other_shape_is_ignored_on_every_route(self):
        views = self.spy(recovery_service, "pending_view")
        writers = [self.spy(recovery_service, name) for name in ("redeem", "resend", "cancel")]
        before = self.digest()
        for value in self.forged():
            client = self.app.test_client()
            self.set_slot(value, client)
            with self.subTest(slot=repr(value)[:60]):
                get = client.get("/recover/verify")
                self.assertEqual((get.status_code, get.headers["Location"]), (302, "/recover"))
                self.assertIn("Заявката за нова парола е изтекла", self.body(self.redeem("12345678", client=client)))
                self.assertIn("Заявката за нова парола е изтекла", self.body(self.resend(client=client)))
                cancelled = self.cancel(client=client)
                self.assertEqual((cancelled.status_code, cancelled.headers["Location"]), (303, "/recover"))
        self.assertEqual(views, [])
        self.assertEqual(writers, [[], [], []])
        self.assertEqual(self.digest(), before)

    def test_a_well_formed_slot_that_matches_no_row_reads_one_row_and_is_dropped_by_a_post(self):
        self.set_slot({"v": 1, "t": self.GOOD, "next": "/account/attempts"})
        get = self.client.get("/recover/verify")
        self.assertIn("Заявката за нова парола е изтекла", self.body(get))
        self.assertIsNotNone(self.slot())
        self.assertIn("href=\"/recover?next=/account/attempts\"", self.body(get))
        before = self.digest()
        self.redeem("12345678")
        self.assertIsNone(self.slot())
        self.assertEqual(self.digest(), before)

    def test_a_copy_of_the_cookie_is_the_same_browser_and_needs_the_code_all_the_same(self):
        _ = self.verified()
        self.recover()
        copy = self.app.test_client()
        self.set_slot(self.slot(), copy)
        wrong = "00000000" if self.last_code() != "00000000" else "11111111"
        self.assertIn("Грешен или изтекъл код.", self.body(self.redeem(wrong, client=copy)))
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))
        self.assertEqual(self.redeem(self.last_code(), client=copy).status_code, 303)
        # Single use: the original browser's slot now points at a redeemed row.
        self.assertIn("Заявката за нова парола е изтекла", self.body(self.redeem(self.last_code())))

    def test_after_a_secret_key_rotation_every_recovery_in_progress_ends(self):
        """Design 2.5: bindings, code hashes, digests and address hashes all stop matching (and so does the
        cookie's signature: the browser holds no slot any more). Nothing is read or written by the old code."""
        self.verified()
        self.recover()
        code = self.last_code()
        rotated = self.app.config["SECRET_KEY"] + "-rotated"
        with patch.dict(self.app.config, {"SECRET_KEY": rotated}):
            before = self.digest()
            get = self.client.get("/recover/verify")
            self.assertEqual((get.status_code, get.headers["Location"]), (302, "/recover"))
            self.assertIn(NO_RECOVERY, self.body(self.redeem(code)))
            self.assertEqual(self.digest(), before)
            # A copy of the old slot under the new key: a well-formed slot that matches no row.
            self.set_slot({"v": 1, "t": "Z" * 43, "next": ""})
            self.assertIn(NO_RECOVERY, self.body(self.redeem(code)))
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))

    def test_a_slot_next_that_the_rule_refuses_is_never_used(self):
        self.verified()
        self.recover()
        slot = self.slot()
        self.set_slot({**slot, "next": "/ok path"})       # no longer accepted: the slot is ignored whole
        response = self.client.get("/recover/verify")
        self.assertEqual(response.headers["Location"], "/recover")


class HeadersTest(RecoveryRouteTestCase):
    """RW7, RW2: no-store and no-referrer on every recovery response; no GET writes."""

    def test_every_recovery_response_is_no_store_and_no_referrer(self):
        self.verified()
        responses = [
            self.client.get("/recover"), self.client.get("/recover/verify"), self.begin(), self.client.get("/recover/verify"),
            self.redeem("00000000"), self.resend(), self.cancel(),
            self.begin("bad address"), self.client.get("/recover/unknown"), self.client.get("/recover/resend"),
            self.client.post("/recover", headers=CROSS_ORIGIN), self.client.get("/recover/"),
        ]
        for response in responses:
            with self.subTest(status=response.status_code):
                self.assertEqual(response.headers.getlist("Cache-Control"), ["no-store"])
                self.assertEqual(response.headers.getlist("Referrer-Policy"), ["no-referrer"])
        outside = self.client.get("/login")
        self.assertEqual(outside.headers.getlist("Referrer-Policy"), [])
        self.assertEqual(self.client.get("/recovery").headers.getlist("Referrer-Policy"), [])
        self.assertEqual(self.client.get("/recover-x").headers.getlist("Referrer-Policy"), [])

    def test_the_disabled_404_and_a_signed_in_redirect_carry_them_too(self):
        self.app.config[CONFIG_KEY] = None
        response = self.client.get("/recover")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers.getlist("Referrer-Policy"), ["no-referrer"])
        self.assertEqual(response.headers.getlist("Cache-Control"), ["no-store"])

    def test_no_get_writes_the_database_or_sets_a_cookie_with_or_without_a_recovery_in_progress(self):
        self.verified()
        before = self.digest()
        for path in ("/recover", "/recover?next=/account/attempts", "/recover/verify", "/recover/verify?resent=1",
                     "/recover/nothing", "/recover/"):
            response = self.client.get(path)
            with self.subTest(path=path, recovery=False):
                self.assertCookieless(response)
        self.assertEqual(self.digest(), before)
        self.recover()
        during = self.digest()
        for path in ("/recover", "/recover/verify", "/recover/verify?resent=1", "/login?recovery=done"):
            response = self.client.get(path)
            with self.subTest(path=path, recovery=True):
                self.assertCookieless(response)
        self.assertEqual(self.digest(), during)
        # Even long after everything expired a GET only reads.
        self.advance(3 * 3600)
        late = self.digest()
        response = self.client.get("/recover/verify")
        self.assertIn("Заявката за нова парола е изтекла", self.body(response))
        self.assertCookieless(response)
        self.assertEqual(self.digest(), late)
        self.assertEqual(self.recoveries()[0]["state"], "pending")      # expiry is written by POSTs and the cleanup


class LoginIntegrationTest(RecoveryRouteTestCase):
    """RW9, RD14, RD16: the link and the URL-selected notice, only while enabled."""

    def test_the_link_is_shown_only_while_enabled_and_carries_the_validated_next(self):
        page = self.body(self.client.get("/login"))
        self.assertIn('<a href="/recover">Забравена парола?</a>', page)
        page = self.body(self.client.get("/login", query_string={"next": "/account/attempts"}))
        self.assertIn('<a href="/recover?next=/account/attempts">Забравена парола?</a>', page)
        page = self.body(self.client.get("/login", query_string={"next": "//evil.example"}))
        self.assertIn('<a href="/recover">Забравена парола?</a>', page)
        self.assertNotIn("evil", page)
        self.app.config[CONFIG_KEY] = None
        self.assertNotIn("Забравена парола", self.body(self.client.get("/login")))

    def test_the_notice_is_a_fixed_text_selected_by_the_url_and_proves_nothing(self):
        text = ("Паролата е сменена. Влез с новата парола. "
                "Ако видиш съобщението „Твърде много опити“, изчакай няколко минути.")
        before = self.digest()
        response = self.client.get("/login?recovery=done")
        page = self.body(response)
        self.assertIn(f'<div class="success login-message" role="status">{escape(text)}</div>', page)
        self.assertCookieless(response)
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.session(), {})
        for value in ("", "yes", "DONE", "done ", "<script>alert(1)</script>", "done&recovery=x"):
            page = self.body(self.client.get("/login", query_string={"recovery": value}))
            with self.subTest(value=value):
                self.assertNotIn("Паролата е сменена", page)
                self.assertNotIn("<script>alert", page)
        # Repeated parameter: the first value decides, nothing is echoed.
        page = self.body(self.client.get("/login?recovery=done&recovery=<b>"))
        self.assertIn("Паролата е сменена", page)
        self.assertNotIn("&lt;b&gt;", page)

    def test_a_failed_sign_in_after_the_notice_is_the_ordinary_answer(self):
        self.verified()
        response = self.login(EMAIL, "wrong password")
        self.assertIn("Грешен имейл или парола.", self.body(response))
        self.assertNotIn("Паролата е сменена", self.body(response))


class NextPreservationTest(RecoveryRouteTestCase):
    """RB5, RW8: a validated ``next`` is kept through every failure, unavailable and ended page, never
    repaired or echoed when refused, and validated again where it is used."""

    NEXT = "/account/attempts"

    def links(self, response):
        page = self.body(response)
        return re.findall(r'<a class="button(?: secondary)?" href="([^"]+)"', page)

    def assertCarriesNext(self, response):
        links = self.links(response)
        self.assertEqual(links, [f"/recover?next={self.NEXT}", f"/login?next={self.NEXT}"], self.body(response)[:200])

    def test_unavailable_failure_ended_and_invalidated_pages_carry_it(self):
        self.verified()
        # unavailable (a hold) at the request
        self.set_control(hold="restore")
        self.assertCarriesNext(self.begin(next=self.NEXT, client=self.app.test_client()))
        self.execute("UPDATE registration_control SET hold_reason = NULL, hold_set_at = NULL")
        self.advance(1)
        # a failure at the request
        with patch.object(recovery_service, "request", side_effect=RuntimeError("forced")):
            self.assertCarriesNext(self.begin(next=self.NEXT, client=self.app.test_client()))
        # a recovery in progress with the validated next
        client = self.app.test_client()
        self.recover(next=self.NEXT, client=client)
        code = self.last_code()
        self.assertEqual(self.slot(client)["next"], self.NEXT)
        # unavailable at the redemption, a failure of it, then invalidated
        self.set_control(high_water=self.now + timedelta(hours=1))
        self.assertCarriesNext(self.redeem(code, client=client))
        self.assertCarriesNext(self.cancel(client=client))
        self.execute("UPDATE registration_control SET high_water_at = ?", ("2026-10-21 00:00:00",))
        with patch.object(accounts, "set_password", side_effect=ValueError("forced")):
            self.assertCarriesNext(self.redeem(code, client=client))
        self.cli("sign-out", "--email", EMAIL)
        self.assertCarriesNext(self.redeem(code, client=client))         # invalidated
        self.assertIsNone(self.slot(client))

    def test_a_refused_next_is_dropped_never_repaired_and_never_echoed(self):
        for hostile in ("//evil.example", "https://evil.example/x", "/a b", "javascript:alert(1)", "/x\u202e",
                        "../etc", "/" + "a" * 600, "/a\\b", "evil"):
            page = self.body(self.client.get("/recover", query_string={"next": hostile}))
            with self.subTest(next=hostile[:20]):
                self.assertIn('name="next" value=""', page)
                self.assertNotIn("evil", page)
                self.assertNotIn("alert(1)", page)
            post = self.begin("bad address", next=hostile)
            self.assertIn('name="next" value=""', self.body(post))
            self.assertNotIn("evil", self.body(post))

    def test_the_kept_next_is_used_only_through_the_login_redirect_rule(self):
        self.verified()
        self.recover(next="/account/attempts?x=1&y=2")
        response = self.redeem(self.last_code())
        self.assertEqual(response.headers["Location"], "/login?next=/account/attempts?x%3D1%26y%3D2&recovery=done")
        page = self.body(self.client.get(response.headers["Location"]))
        self.assertIn('name="next" value="/account/attempts?x=1&amp;y=2"', page)

    def test_the_form_after_a_failed_validation_keeps_the_next_it_was_given(self):
        page = self.body(self.begin("bad", next=self.NEXT))
        self.assertIn(f'name="next" value="{self.NEXT}"', page)
        self.assertIn(f'href="/login?next={self.NEXT}"', page)


class AccessibilityTest(RecoveryRouteTestCase):
    """The forms are labelled, the errors are announced and tied to their fields, and every page says what it is."""

    def pages(self):
        self.verified()
        yield "request form", self.client.get("/recover")
        yield "request form, invalid", self.begin("bad")
        self.recover()
        yield "verify page", self.client.get("/recover/verify")
        yield "verify, wrong code", self.redeem(WRONG)
        yield "verify, password errors", self.redeem(self.last_code(), "short", "other")
        yield "verify, too early", self.resend()
        yield "verify, resent notice", self.client.get("/recover/verify?resent=1")
        self.execute("DELETE FROM password_recoveries")
        yield "no usable recovery", self.client.get("/recover/verify")
        self.set_control(hold="restore")
        yield "unavailable", self.begin(client=self.app.test_client())

    def test_every_page_has_a_language_a_title_one_heading_and_labelled_fields_with_resolvable_descriptions(self):
        seen = 0
        for name, response in self.pages():
            if response.status_code in (302, 303):
                response = self.client.get(response.headers["Location"])
            page = self.body(response)
            with self.subTest(page=name):
                seen += 1
                self.assertIn('<html lang="bg">', page)
                self.assertRegex(page, r"<title>[^<]+</title>")
                self.assertEqual(page.count("<h1>"), 1)
                ids = set(re.findall(r'\bid="([^"]+)"', page))
                for control in re.findall(r"<input[^>]*>", page):
                    if 'type="hidden"' in control:
                        continue
                    identifier = re.search(r'\bid="([^"]+)"', control).group(1)
                    self.assertIn(f'<label for="{identifier}">', page, control)
                    self.assertIn("name=", control)
                    for target in re.findall(r'aria-describedby="([^"]+)"', control):
                        self.assertIn(target, ids)
                self.assertEqual(len(re.findall(r'\bid="([^"]+)"', page)), len(set(re.findall(r'\bid="([^"]+)"', page))))
                for message in re.findall(r'<(?:div|p) class="(?:error|warning|success)[^"]*"[^>]*>', page):
                    self.assertRegex(message, r'role="(?:alert|status)"')
                for button in re.findall(r"<button[^>]*>", page):
                    self.assertIn('type="submit"', button)
        self.assertGreaterEqual(seen, 8)

    def test_a_field_with_an_error_is_marked_invalid_and_described_by_its_error(self):
        self.verified()
        self.recover()
        page = self.body(self.redeem(WRONG, "short", "other"))
        for field in ("password", "confirm_password"):
            self.assertRegex(page, rf'<input id="{field}"[^>]*aria-invalid="true" aria-describedby="{field}-error"')
            self.assertIn(f'<p class="error field-error" id="{field}-error" role="alert">', page)
        self.assertNotRegex(page, r'<input id="code"[^>]*aria-invalid')


class SessionTransitionTest(RecoveryRouteTestCase):
    """RW6: sign-in and the pending second factor drop ``rec`` and keep the attempt markers;
    sign-out clears everything; ``reg`` and ``rec`` coexist independently."""

    def start(self, client=None, email=EMAIL):
        self.recover(email, client=client)
        return client or self.client

    def test_sign_in_drops_the_slot_and_keeps_only_the_attempt_markers(self):
        self.verified()
        account = self.make("student")
        client = self.start()
        with client.session_transaction() as sess:
            sess["quiz_attempt_owner:7"] = True
        self.assertIsNotNone(self.slot(client))
        self.sign_in(account, client=client)
        self.assertEqual(set(self.session(client)), {"uid", "sk", "iat", "quiz_attempt_owner:7"})

    def test_the_pending_second_factor_drops_the_slot_too(self):
        admin = self.make("admin")
        client = self.start(email="nobody@example.bg")
        with client.session_transaction() as sess:
            sess["quiz_attempt_owner:9"] = True
        self.login(admin["email"], client=client)
        state = self.session(client)
        self.assertEqual(set(state), {"mfa_uid", "mfa_sk", "mfa_iat", "quiz_attempt_owner:9"})
        self.assertNotIn("rec", state)

    def test_sign_out_clears_everything(self):
        self.verified()
        account = self.make("student")
        client = self.start()
        self.sign_in(account, client=client)
        self.assertEqual(client.post("/logout", headers=SAME_ORIGIN).status_code, 302)
        self.assertEqual(self.session(client), {})

    def test_the_registration_slot_and_the_recovery_slot_are_independent(self):
        self.verified()
        self.recover()
        with self.client.session_transaction() as sess:
            sess["reg"] = {"v": 1, "t": "R" * 43, "next": ""}
        self.advance(61)
        self.assertEqual(self.resend().status_code, 303)
        state = self.session()
        self.assertEqual(state["reg"], {"v": 1, "t": "R" * 43, "next": ""})
        self.assertEqual(set(state["rec"]), {"v", "t", "next"})
        self.assertEqual(self.cancel().status_code, 303)
        self.assertEqual(self.session()["reg"], {"v": 1, "t": "R" * 43, "next": ""})
        self.assertNotIn("rec", self.session())

    def test_an_admin_sign_in_is_unchanged_by_a_reset_of_another_account(self):
        admin = self.make("admin")
        self.verified()
        self.recover()
        self.redeem(self.last_code())
        response = self.sign_in(admin)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.session()["uid"], admin["id"])


if __name__ == "__main__":
    unittest.main()

"""Registration PR 4: the student routes, the browser slot, transactions and dispatch.

Test plan sections F, G, H, I and K7 at route level (docs/reviews/EMAIL_REGISTRATION_TEST_PLAN.md).
Every test has its own temporary database built from migrations and canonical data;
none is ``data/questions.db``. Mail is ``InMemoryMailer``: nothing is sent and no socket
is opened. The registration clock is injected (``self.wall``); nothing sleeps for a
limit. Commit failures are real exceptions raised in place of ``COMMIT`` on the
request's own connection, which is then rolled back by the code under test.
"""

import hashlib
import html
import json
import logging
import re
import sqlite3
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from flask import g

from tests.registration_support import code_of
from tests.support import build_test_database, web_app
from tests.test_account_login import PASSWORD, SAME_ORIGIN, AccountAppTestCase
from web import account_session
from web.blueprints import registration as registration_blueprint
from web.registration_settings import CONFIG_KEY, RegistrationSettings
from web.services import accounts, mail, profiles
from web.services import registration as registration_service
from web.services.mail import InMemoryMailer, SendOutcome

T0 = datetime(2026, 10, 14, 9, 0, 0, tzinfo=timezone.utc)
EMAIL = "ana.ivanova@example.bg"
STUDENT_PASSWORD = "  pupil  passphrase  "  # exact: spaces are part of it
CROSS_ORIGIN = {"Origin": "http://evil.example"}
SENSITIVE = ("email", "first_name", "last_name", "grade", "school_name", "subjects_json", "password_hash",
             "code_hash", "code_expires_at")
REGISTRATION_TABLES = ("pending_registrations", "registration_sends", "registration_control", "users",
                       "user_profiles", "teacher_subjects")
ROUTES = ("/register", "/register/student", "/register/teacher", "/register/verify", "/register/resend",
          "/register/cancel")


def form(email=EMAIL, **overrides):
    values = {"first_name": "Ана", "last_name": "Иванова", "email": email, "grade": "9",
              "password": STUDENT_PASSWORD, "confirm_password": STUDENT_PASSWORD}
    values.update(overrides)
    return values


class FlakyCommits:
    """The request's real connection, except that the commits whose ordinal (1-based,
    counted over the whole test) is in ``fail`` raise instead of committing. The
    transaction is then still open, as after a real failed ``COMMIT``."""

    def __init__(self, conn, state):
        self._conn, self._state = conn, state

    def commit(self):
        self._state["commits"] += 1
        if self._state["commits"] in self._state["fail"]:
            raise sqlite3.OperationalError("disk I/O error (injected at COMMIT)")
        return self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class RegistrationRouteTestCase(AccountAppTestCase):
    """A fresh database per test, registration enabled with an in-memory mailer."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="registration-routes-")
        self.addCleanup(directory.cleanup)
        self.db_path = str(build_test_database(Path(directory.name) / "registration.db"))
        self.app.config["DB_PATH"] = self.db_path
        web_app.DB_PATH = web_app.QUIZ_DB_PATH = Path(self.db_path)  # restored by preserved_app_state
        super().setUp()
        self.mailer = InMemoryMailer()
        self.app.config[CONFIG_KEY] = RegistrationSettings(mailer=self.mailer)
        self.wall = T0
        clock = patch.object(registration_blueprint, "_utcnow", lambda: self.wall)
        clock.start()
        self.addCleanup(clock.stop)
        quiet = logging.NullHandler()
        logging.getLogger("learnpilot.registration").addHandler(quiet)
        self.addCleanup(logging.getLogger("learnpilot.registration").removeHandler, quiet)

    # ------------------------------------------------------------------ requests

    def submit(self, email=EMAIL, *, client=None, headers=SAME_ORIGIN, **overrides):
        return (client or self.client).post("/register/student", data=form(email, **overrides), headers=headers)

    def verify(self, code, *, client=None, headers=SAME_ORIGIN):
        return (client or self.client).post("/register/verify", data={"code": code}, headers=headers)

    def resend(self, *, client=None, headers=SAME_ORIGIN):
        return (client or self.client).post("/register/resend", headers=headers)

    def cancel(self, *, client=None, headers=SAME_ORIGIN):
        return (client or self.client).post("/register/cancel", headers=headers)

    def at(self, seconds):
        self.wall = T0 + timedelta(seconds=seconds)

    def last_code(self):
        """The six digits of the last message sent (a notice has none)."""
        found = re.findall(r"(?<!\d)\d{6}(?!\d)", self.mailer.messages[-1].text)
        self.assertEqual(len(found), 1, "the last message is a code")
        return found[0]

    def register(self, email=EMAIL, *, client=None, next_target=None):
        """Submit and verify; returns the verifying response."""
        extra = {"next": next_target} if next_target else {}
        submitted = self.submit(email, client=client, **extra)
        self.assertEqual(submitted.status_code, 303, self.body(submitted))
        return self.verify(self.last_code(), client=client)

    # ------------------------------------------------------------------ state

    def rows(self, sql, *params):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, params)]

    def pending(self):
        return self.rows("SELECT * FROM pending_registrations ORDER BY id")

    def sends(self):
        return self.rows("SELECT * FROM registration_sends ORDER BY id")

    def users_with(self, email):
        return self.rows("SELECT * FROM users WHERE email = ?", email)

    def digest(self):
        sha = hashlib.sha256()
        with closing(sqlite3.connect(self.db_path)) as conn:
            for table in REGISTRATION_TABLES:
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1"):
                    sha.update(repr((table, tuple(row))).encode("utf-8"))
        return sha.hexdigest()

    def set_control(self, *, high_water=None, hold=None):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO registration_control (id, high_water_at, hold_reason, hold_set_at) "
                "VALUES (1, ?, ?, ?)",
                (registration_service._stamp(high_water or self.wall), hold,
                 registration_service._stamp(self.wall) if hold else None))
            conn.commit()

    def control(self):
        found = self.rows("SELECT * FROM registration_control")
        return found[0] if found else None

    def slot(self, client=None):
        return self.session(client).get(registration_blueprint.SLOT)

    def set_slot(self, value, client=None):
        with (client or self.client).session_transaction() as sess:
            sess[registration_blueprint.SLOT] = value

    def flaky(self, *fail):
        """Fail the commits of these ordinals on the request connections of the blueprint."""
        state = {"commits": 0, "fail": set(fail)}
        real = registration_blueprint.get_request_db
        patcher = patch.object(registration_blueprint, "get_request_db", lambda: FlakyCommits(real(), state))
        patcher.start()
        self.addCleanup(patcher.stop)
        return state

    def spy(self, target, name):
        """Record every call of ``target.name`` and pass it on."""
        calls = []
        real = getattr(target, name)

        def recorded(*args, **kwargs):
            calls.append((args, kwargs))
            return real(*args, **kwargs)

        patcher = patch.object(target, name, recorded)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def assertNoAccountSession(self, client=None):
        state = self.session(client)
        for key in ("uid", "sk", "iat", "mfa_uid", "mfa_sk", "mfa_iat"):
            self.assertNotIn(key, state)


# --------------------------------------------------------------------------- the journey (F5, F5a, F6, F7)


class JourneyTest(RegistrationRouteTestCase):
    def test_full_student_journey_ends_at_login_without_a_session(self):
        chooser = self.client.get("/register?next=/account/attempts")
        self.assertEqual(chooser.status_code, 200)
        self.assertIn("/register/student?next=/account/attempts", html.unescape(self.body(chooser)))
        page = self.client.get("/register/student?next=/account/attempts")
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="next" value="/account/attempts"', self.body(page))

        submitted = self.submit(next="/account/attempts")
        self.assertEqual((submitted.status_code, submitted.headers["Location"]), (303, "/register/verify"))
        state = self.session()
        self.assertEqual(set(state), {"reg"})
        self.assertEqual(set(state["reg"]), {"v", "t", "next"})
        self.assertEqual((state["reg"]["v"], state["reg"]["next"]), (1, "/account/attempts"))
        self.assertEqual(len(self.mailer.messages), 1)
        message = self.mailer.messages[0]
        self.assertEqual((message.to, message.subject), (EMAIL, registration_service.CODE_SUBJECT))

        verify_page = self.client.get("/register/verify")
        self.assertEqual(verify_page.status_code, 200)
        self.assertIn(f"Изпратихме 6-цифрен код на {EMAIL}.", self.body(verify_page))
        self.assertNoAccountSession()

        # F7: before verification nothing is an account.
        self.assertEqual(self.client.get("/account").status_code, 302)
        refused = self.login(EMAIL, STUDENT_PASSWORD, client=self.app.test_client())
        self.assertIn("Грешен имейл или парола.", self.body(refused))
        self.assertEqual(self.users_with(EMAIL), [])

        verified = self.verify(self.last_code())
        self.assertEqual(verified.status_code, 303)
        self.assertEqual(verified.headers["Location"], "/login?next=/account/attempts&registration=activated")
        # F6: no slot, and no account key of any kind.
        self.assertEqual(self.session(), {})
        self.assertNoAccountSession()
        user = self.users_with(EMAIL)[0]
        self.assertEqual((user["role"], user["display_name"]), ("student", "Ана Иванова"))
        self.assertIsNotNone(user["email_verified_at"])
        profile = self.rows("SELECT * FROM user_profiles WHERE user_id = ?", user["id"])[0]
        self.assertEqual((profile["account_type"], profile["grade"]), ("student", 9))
        self.assertEqual(self.pending()[0]["state"], "activated")

        landing = self.client.get(verified.headers["Location"])
        self.assertIn("Имейлът е потвърден. Влез с имейл и парола.", self.body(landing))
        self.assertIn('name="next" value="/account/attempts"', self.body(landing))

        # F5a: sign-in is email and password; no code is sent or asked.
        sent = len(self.mailer.messages)
        signed_in = self.login(EMAIL, STUDENT_PASSWORD, next="/account/attempts")
        self.assertEqual((signed_in.status_code, signed_in.headers["Location"]), (302, "/account/attempts"))
        self.assertEqual(set(self.session()), {"uid", "sk", "iat"})
        self.assertEqual(self.client.get("/account/attempts").status_code, 200)
        self.client.post("/logout", headers=SAME_ORIGIN)
        again = self.login(EMAIL, STUDENT_PASSWORD)
        self.assertEqual((again.status_code, again.headers["Location"]), (302, "/"))
        self.assertEqual(len(self.mailer.messages), sent)

    def test_password_is_kept_exactly_and_variants_are_refused(self):
        self.register()
        for variant in ("pupil  passphrase", "pupil passphrase", STUDENT_PASSWORD.strip(), " pupil passphrase "):
            with self.subTest(variant=variant):
                self.assertIn("Грешен имейл или парола.", self.body(self.login(EMAIL, variant,
                                                                              client=self.app.test_client())))
        self.assertEqual(self.login(EMAIL, STUDENT_PASSWORD).status_code, 302)

    def test_admin_second_factor_and_cli_accounts_are_unchanged(self):
        admin = self.make("admin")
        first = self.login(admin["email"])
        self.assertEqual(first.status_code, 200)
        self.assertIn('name="code"', self.body(first))
        self.assertNotIn("uid", self.session())
        teacher = self.make("teacher")  # a CLI account: email_verified_at NULL, no profile
        self.assertIsNone(self.user(teacher["id"], "email_verified_at"))
        client = self.app.test_client()
        signed_in = self.login(teacher["email"], client=client)
        self.assertEqual((signed_in.status_code, signed_in.headers["Location"]), (302, "/teacher/new"))
        self.assertEqual(self.mailer.messages, [])


# --------------------------------------------------------------------------- forms and input


class FormTest(RegistrationRouteTestCase):
    def test_invalid_input_re_renders_without_passwords_and_writes_nothing(self):
        before = self.digest()
        response = self.submit(first_name="", email="not-an-email", grade="13",
                               confirm_password="something else entirely", next="//evil.example/x")
        self.assertEqual(response.status_code, 200)
        page = self.body(response)
        for text in ("Въведи име.", "Невалиден имейл.", "Избери клас.", "Паролите не съвпадат."):
            self.assertIn(text, page)
        self.assertNotIn(STUDENT_PASSWORD.strip(), page)
        self.assertNotIn("something else entirely", page)
        self.assertNotIn("evil.example", page)
        self.assertIn('value="Иванова"', page)
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.mailer.messages, [])
        self.assertNotIn("Set-Cookie", response.headers)

    def test_echoed_values_are_escaped(self):
        response = self.submit(first_name='"><script>alert(1)</script>', email='"><b>@x')
        page = self.body(response)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn('"><b>', page)
        self.assertIn("&lt;script&gt;", page)

    def test_tampered_role_fields_change_nothing(self):
        for field in ("role", "account_type", "is_admin", "email_verified_at", "profile_required_at"):
            self.submit(f"{field.replace('_', '')}@example.bg", **{field: "admin" if field != "account_type" else "teacher"})
            self.verify(self.last_code())
        roles = {row["role"] for row in self.rows("SELECT role FROM users WHERE email LIKE '%@example.bg'")}
        self.assertEqual(roles, {"student"})
        types = {row["account_type"] for row in self.rows("SELECT account_type FROM user_profiles")}
        self.assertEqual(types, {"student"})
        self.assertEqual(self.rows("SELECT * FROM users WHERE role = 'admin'"), [])

    def test_next_is_dropped_unless_accepted_at_render_and_at_use(self):
        for bad in ("https://evil.example/", "//evil.example", "/\\evil.example", "/a b", "/a‮b",
                    "/" + "a" * 600):
            with self.subTest(next=bad[:30]):
                page = self.body(self.client.get("/register/student", query_string={"next": bad}))
                self.assertIn('name="next" value=""', page)
        # A forged slot carrying an unsafe target is not a slot at all.
        self.set_slot({"v": 1, "t": "A" * 43, "next": "//evil.example"})
        self.assertEqual(self.client.get("/register/verify").headers["Location"], "/register")

    def test_the_code_field_accepts_surrounding_whitespace_only(self):
        self.submit()
        code = self.last_code()
        wrong = self.verify(code[:3] + " " + code[3:])
        self.assertIn("Грешен или изтекъл код.", self.body(wrong))
        done = self.verify(f"  {code}\n")
        self.assertEqual(done.status_code, 303)


# --------------------------------------------------------------------------- browser binding (F8 to F12, F15)


class BindingTest(RegistrationRouteTestCase):
    def test_another_browser_cannot_use_the_code(self):
        self.submit()
        code = self.last_code()
        other = self.app.test_client()
        attempts = self.pending()[0]["code_attempts"]
        answer = self.verify(code, client=other)
        self.assertEqual(answer.status_code, 200)
        self.assertIn("Регистрацията е изтекла или е започната от друг браузър.", self.body(answer))
        self.assertEqual(self.users_with(EMAIL), [])
        self.assertEqual((self.pending()[0]["state"], self.pending()[0]["code_attempts"]), ("pending", attempts))
        self.assertEqual(other.get("/register/verify").headers["Location"], "/register")
        # The browser that registered still can.
        self.assertEqual(self.verify(code).status_code, 303)

    def test_a_second_submission_voids_the_first(self):
        self.submit()
        first_code = self.last_code()
        self.submit("second.address@example.bg")
        first, second = self.pending()
        self.assertEqual((first["state"], second["state"]), ("void", "pending"))
        for column in SENSITIVE:
            self.assertIsNone(first[column])
        if first_code != self.last_code():
            self.assertIn("Грешен или изтекъл код.", self.body(self.verify(first_code)))
        self.assertEqual(self.users_with(EMAIL), [])

    def test_forged_slots_are_ignored(self):
        self.submit()
        token = self.slot()["t"]
        for forged in ({"v": 2, "t": token, "next": ""}, {"v": True, "t": token, "next": ""},
                       {"v": 1, "t": 12345, "next": ""}, {"v": 1, "t": token[:-1], "next": ""},
                       {"v": 1, "t": token, "next": "", "role": "admin"}, {"v": 1, "t": token},
                       [1, token, ""], token):
            with self.subTest(slot=repr(forged)[:40]):
                self.set_slot(forged)
                self.assertEqual(self.client.get("/register/verify").headers["Location"], "/register")
                self.assertIn("друг браузър", self.body(self.verify(self.last_code())))
        self.assertEqual(self.users_with(EMAIL), [])
        self.assertEqual(self.pending()[0]["code_attempts"], 0)

    def test_a_copied_cookie_gains_nothing_and_the_slot_after_success_is_spent(self):
        self.submit()
        copy = self.app.test_client()
        self.set_slot(dict(self.slot()), client=copy)
        code = self.last_code()
        self.assertEqual(self.verify(code).status_code, 303)
        repeat = self.verify(code, client=copy)
        self.assertEqual((repeat.status_code, repeat.headers["Location"]), (303, "/login"))
        self.assertIsNone(self.slot(copy))
        self.assertEqual(len(self.users_with(EMAIL)), 1)

    def test_cancel_voids_scrubs_and_drops_the_slot(self):
        self.submit(next="/account/attempts")
        cancelled = self.cancel()
        self.assertEqual((cancelled.status_code, cancelled.headers["Location"]),
                         (303, "/register/student?next=/account/attempts"))
        self.assertIsNone(self.slot())
        row = self.pending()[0]
        self.assertEqual(row["state"], "void")
        for column in SENSITIVE:
            self.assertIsNone(row[column])
        # Nothing is left to cancel or to verify.
        self.assertEqual(self.cancel().headers["Location"], "/register/student")
        self.assertEqual(self.client.get("/register/verify").headers["Location"], "/register")

    def test_signed_in_accounts_start_nothing_on_register_routes_only(self):
        for role, home in (("student", "/"), ("teacher", "/teacher/new"), ("admin", "/teacher")):
            with self.subTest(role=role):
                client = self.app.test_client()
                account = self.make(role)
                with client.session_transaction() as sess:
                    sess.update(uid=account["id"], sk=account["session_key"],
                                iat=int(datetime.now(timezone.utc).timestamp()))
                before_session, before_db = self.session(client), self.digest()
                for path in ("/register", "/register/student", "/register/verify"):
                    response = client.get(path)
                    self.assertEqual((response.status_code, response.headers["Location"]), (303, home), path)
                for path, data in (("/register/student", form(f"{role}.reg@example.bg")),
                                   ("/register/verify", {"code": "123456"}), ("/register/resend", {}),
                                   ("/register/cancel", {})):
                    response = client.post(path, data=data, headers=SAME_ORIGIN)
                    self.assertEqual((response.status_code, response.headers["Location"]), (303, home), path)
                self.assertEqual(self.session(client), before_session)
                self.assertEqual(self.digest(), before_db)
                # The account pages are not affected by that rule.
                self.assertEqual(client.get("/account").status_code, 200)
        self.assertEqual(self.mailer.messages, [])

    def test_sign_in_and_second_factor_drop_the_slot_and_keep_attempt_markers(self):
        student = self.make("student")
        self.submit()
        with self.client.session_transaction() as sess:
            sess["quiz_attempt_owner:7"] = True
        self.login(student["email"])
        state = self.session()
        self.assertNotIn("reg", state)
        self.assertTrue(state["quiz_attempt_owner:7"])
        # Sign-out clears everything, the markers included.
        self.client.post("/logout", headers=SAME_ORIGIN)
        self.assertEqual(self.session(), {})
        # An admin's password step (pending second factor) drops the slot too.
        admin = self.make("admin")
        client = self.app.test_client()
        self.submit("other.one@example.bg", client=client)
        with client.session_transaction() as sess:
            sess["quiz_attempt_owner:9"] = True
        self.login(admin["email"], client=client)
        state = self.session(client)
        self.assertNotIn("reg", state)
        self.assertIn("mfa_uid", state)
        self.assertTrue(state["quiz_attempt_owner:9"])

    def test_the_slot_holds_no_personal_data_code_or_password(self):
        self.submit()
        serialized = json.dumps(self.session(), ensure_ascii=False)
        for secret in (EMAIL, "Ана", "Иванова", STUDENT_PASSWORD, self.last_code(), self.sends()[0]["issuance_id"]):
            self.assertNotIn(secret, serialized)


# --------------------------------------------------------------------------- disabled, GETs, headers (F3, F4, F14, F16)


class DisabledAndGetTest(RegistrationRouteTestCase):
    def test_disabled_every_address_is_the_generic_404_and_login_has_no_link(self):
        self.app.config[CONFIG_KEY] = None
        for path in ROUTES:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404 if path not in (
                    "/register/resend", "/register/cancel") else 405)
                self.assertEqual(self.client.post(path, data=form(), headers=SAME_ORIGIN).status_code,
                                 404 if path != "/register" else 405)
                self.assertEqual(self.client.post(path, data=form(), headers=CROSS_ORIGIN).status_code, 403)
        login = self.body(self.client.get("/login?registration=activated"))
        self.assertNotIn("/register", login)
        self.assertNotIn("Имейлът е потвърден", login)
        self.assertEqual(self.rows("SELECT * FROM pending_registrations"), [])
        self.assertEqual(self.mailer.messages, [])

    def test_turned_off_with_a_registration_pending_nothing_activates(self):
        # K6: the slot and the code are both in hand; every route is 404 and writes nothing.
        self.submit()
        code = self.last_code()
        self.app.config[CONFIG_KEY] = None
        before = self.digest()
        self.assertEqual(self.verify(code).status_code, 404)
        self.assertEqual(self.resend().status_code, 404)
        self.assertEqual(self.cancel().status_code, 404)
        self.assertEqual(self.client.get("/register/verify").status_code, 404)
        self.assertEqual((self.digest(), self.users_with(EMAIL)), (before, []))
        self.assertEqual(len(self.mailer.messages), 1)

    def test_enabled_login_offers_the_link_and_only_known_notices(self):
        page = self.body(self.client.get("/login?next=/account/attempts&registration=%3Cb%3E"))
        self.assertIn("Нямаш профил?", page)
        self.assertIn('href="/register?next=/account/attempts"', page)
        self.assertNotIn("<b>", page)
        self.assertIn("Вече има профил с този имейл.", self.body(self.client.get("/login?registration=email_taken")))

    def test_teacher_route_is_404_even_with_its_gate_patched_on(self):
        for gate in (False, True):
            with self.subTest(gate=gate), patch.object(registration_service, "TEACHER_ACTIVATION", gate):
                self.assertEqual(self.client.get("/register/teacher").status_code, 404)
                self.assertEqual(self.client.post("/register/teacher", headers=SAME_ORIGIN).status_code, 404)
                self.assertNotIn("/register/teacher", self.body(self.client.get("/register")))

    def test_no_get_writes_or_sets_a_cookie_with_or_without_a_registration(self):
        def sweep(client):
            for path in ("/register", "/register/student", "/register/student?next=/account",
                         "/register/teacher", "/register/verify", "/register/verify?resent=1"):
                before = self.digest()
                response = client.get(path)
                self.assertEqual(response.headers.getlist("Set-Cookie"), [], path)
                self.assertEqual(self.digest(), before, path)

        sweep(self.app.test_client())
        self.submit()
        sweep(self.client)
        # Past its time the row is shown as gone, and the GET still writes nothing.
        self.at(31 * 60)
        before = self.digest()
        gone = self.client.get("/register/verify")
        self.assertIn("друг браузър", self.body(gone))
        self.assertEqual((self.digest(), gone.headers.getlist("Set-Cookie")), (before, []))
        self.assertEqual(self.pending()[0]["state"], "pending")

    def test_registration_responses_are_not_stored_and_send_no_referrer(self):
        self.submit()
        responses = [self.client.get("/register/verify"), self.verify("000000"), self.resend(), self.cancel(),
                     self.client.get("/register"), self.submit()]
        for response in responses:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
            self.assertNotIn(EMAIL, response.headers.get("Location", ""))

    def test_cross_origin_posts_are_refused_and_write_nothing(self):
        self.submit()
        before, state = self.digest(), self.session()
        for path, data in (("/register/student", form("x@example.bg")), ("/register/verify", {"code": self.last_code()}),
                           ("/register/resend", {}), ("/register/cancel", {})):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, data=data, headers=CROSS_ORIGIN).status_code, 403)
        self.assertEqual((self.digest(), self.session()), (before, state))
        self.assertEqual(len(self.mailer.messages), 1)


# --------------------------------------------------------------------------- codes, resend, expiry


class CodeTest(RegistrationRouteTestCase):
    def test_wrong_codes_count_and_the_sixth_is_refused(self):
        self.submit()
        code = self.last_code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.assertIn("Грешен или изтекъл код.", self.body(self.verify(wrong)))
        self.assertIn("Твърде много грешни опити. Поискай нов код.", self.body(self.verify(code)))
        self.assertEqual(self.users_with(EMAIL), [])
        self.at(61)
        self.assertEqual(self.resend().headers["Location"], "/register/verify?resent=1")
        self.assertIn("Изпратихме нов код.", self.body(self.client.get("/register/verify?resent=1")))
        self.assertEqual(self.verify(self.last_code()).status_code, 303)

    def test_resend_waits_replaces_the_code_and_ends_after_three(self):
        self.submit()
        first = self.last_code()
        self.assertIn("Можеш да поискаш нов код след малко.", self.body(self.resend()))
        self.assertEqual(len(self.mailer.messages), 1)
        for n in range(1, 4):
            self.at(61 * n)
            self.assertEqual(self.resend().status_code, 303)
        self.assertEqual(len(self.mailer.messages), 4)
        self.at(61 * 4)
        self.assertIn("Не можеш да поискаш повече кодове.", self.body(self.resend()))
        self.assertEqual(len(self.mailer.messages), 4)
        if first != self.last_code():
            self.assertIn("Грешен или изтекъл код.", self.body(self.verify(first)))
        self.assertEqual(self.verify(self.last_code()).status_code, 303)

    def test_an_expired_registration_ends_the_slot(self):
        self.submit()
        self.at(30 * 60)
        answer = self.verify(self.last_code())
        self.assertIn("Регистрацията е изтекла", self.body(answer))
        self.assertIsNone(self.slot())
        self.assertEqual(self.pending()[0]["state"], "expired")
        self.assertEqual(self.users_with(EMAIL), [])

    def test_code_errors_mark_the_field_invalid_and_other_pages_do_not(self):
        marked = 'aria-invalid="true" aria-describedby="code-error"'
        self.submit()
        clean = self.body(self.client.get("/register/verify"))
        self.assertNotIn("aria-invalid", clean)
        self.assertNotIn('id="code-error"', clean)
        wrong = self.body(self.verify("000000"))
        self.assertIn(marked, wrong)
        self.assertRegex(wrong, r'id="code-error" role="alert">Грешен или изтекъл код\.')
        for _ in range(5):
            too_many = self.body(self.verify("000000"))
        self.assertIn(marked, too_many)
        self.assertIn('id="code-error"', too_many)
        # An error about waiting is not about what was typed.
        self.at(1)
        early = self.body(self.resend())
        self.assertIn('id="code-error"', early)
        self.assertNotIn("aria-invalid", early)
        # Every displayed error sits in the element aria-describedby names, when it names one.
        self.assertRegex(early, r'id="code-error" role="alert">Можеш да поискаш нов код след малко\.')

    def assertUnmarkedError(self, page, text):
        self.assertRegex(page, r'id="code-error" role="alert">' + re.escape(html.escape(text, quote=False)))
        self.assertNotIn("aria-invalid", page)
        self.assertNotIn("aria-describedby", page)

    def test_resend_exhausted_address_capped_and_not_sent_do_not_mark_the_field(self):
        self.submit()
        for n in range(1, 4):
            self.at(61 * n)
            self.resend()
        self.at(61 * 4)
        self.assertUnmarkedError(self.body(self.resend()), "Не можеш да поискаш повече кодове.")

        # Three sends from other browsers, then this browser's own and its resend: the hour is full.
        for n in range(3):
            self.at(1000 + n)
            self.submit("capped@example.bg", client=self.app.test_client())
        capped_client = self.app.test_client()
        self.at(1002)
        self.submit("capped@example.bg", client=capped_client)
        self.at(1002 + 61)
        self.assertEqual(self.resend(client=capped_client).status_code, 303)
        self.at(1002 + 122)
        capped = self.resend(client=capped_client)
        self.assertEqual(capped.status_code, 429)
        self.assertUnmarkedError(self.body(capped), "Твърде много имейли към този адрес. Опитай отново по-късно.")

        # A code retired after NOT_SENT: the delivery explanation wins over a code error.
        self.mailer.outcome = SendOutcome.NOT_SENT
        retired = self.app.test_client()
        self.submit("retired@example.bg", client=retired)
        not_sent = "Не успяхме да изпратим имейла. Опитай отново след малко."
        self.assertUnmarkedError(self.body(retired.get("/register/verify")), not_sent)
        wrong = self.body(self.verify("000000", client=retired))
        self.assertUnmarkedError(wrong, not_sent)
        self.assertNotIn("Грешен или изтекъл код.", wrong)

    # ---- an ended registration is told apart from a finished one

    def assertStartAgain(self, response, *, next_target=None):
        """The "expired or started elsewhere" page: 200, no redirect, the slot spent, no account."""
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Location", response.headers)
        body = self.body(response)
        self.assertIn("Регистрацията е изтекла", body)
        self.assertIn("Започни регистрацията отначало", body)
        if next_target:
            self.assertIn(html.escape(f"/register/student?next={next_target}", quote=True), body)
        self.assertIsNone(self.slot())
        self.assertNoAccountSession()
        self.assertEqual(self.users_with(EMAIL), [])

    def test_a_row_expired_by_unrelated_traffic_is_not_a_silent_login_redirect(self):
        """Another browser's submission runs the inline cleanup, which expires and scrubs this
        browser's overdue row. This browser's code then meets a final row, not a missing one."""
        self.submit(next="/account/attempts")
        code = self.last_code()
        self.at(30 * 60 + 1)
        self.submit("someone.else@example.bg", client=self.app.test_client())
        self.assertEqual(self.pending()[0]["state"], "expired")
        before = self.digest()
        answer = self.verify(code)
        self.assertStartAgain(answer, next_target="/account/attempts")
        self.assertEqual(self.digest(), before)  # the final row was only read

    def test_a_row_expired_by_the_cleanup_service_is_not_a_silent_login_redirect(self):
        self.submit()
        code = self.last_code()
        self.at(30 * 60 + 1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cleaned = registration_service.cleanup(conn, clock=lambda: self.wall)
            conn.commit()
        self.assertEqual((cleaned.clock_behind, cleaned.scrubbed), (False, 1))
        self.assertStartAgain(self.verify(code))

    def test_a_registration_cancelled_in_another_tab_is_not_a_silent_login_redirect(self):
        self.submit(next="/account/attempts")
        code = self.last_code()
        other_tab = self.app.test_client()
        self.set_slot(dict(self.slot()), client=other_tab)
        self.assertEqual(self.cancel(client=other_tab).status_code, 303)
        self.assertEqual(self.pending()[0]["state"], "void")
        self.assertStartAgain(self.verify(code), next_target="/account/attempts")

    def test_a_registration_replaced_in_another_tab_is_not_a_silent_login_redirect(self):
        self.submit()
        code = self.last_code()
        other_tab = self.app.test_client()
        self.set_slot(dict(self.slot()), client=other_tab)
        self.assertEqual(self.submit("replacement@example.bg", client=other_tab).status_code, 303)
        self.assertEqual([row["state"] for row in self.pending()], ["void", "pending"])
        self.assertStartAgain(self.verify(code))
        # The replacement is the other tab's and is untouched.
        self.assertEqual(self.pending()[1]["state"], "pending")

    def test_a_repeat_after_activation_still_goes_to_login_silently(self):
        self.submit(next="/account/attempts")
        code = self.last_code()
        other_tab = self.app.test_client()
        self.set_slot(dict(self.slot()), client=other_tab)
        self.assertEqual(self.verify(code).status_code, 303)
        before = self.digest()
        repeat = self.verify(code, client=other_tab)
        self.assertEqual((repeat.status_code, repeat.headers["Location"]), (303, "/login?next=/account/attempts"))
        self.assertNoAccountSession(other_tab)
        self.assertIsNone(self.slot(other_tab))
        self.assertEqual(self.digest(), before)
        self.assertEqual(len(self.users_with(EMAIL)), 1)

    def test_a_binding_that_is_not_this_browsers_learns_nothing_of_another_final_state(self):
        """No slot, or a slot of another token: the "other browser" page, whatever any row holds."""
        self.submit()
        code = self.last_code()
        self.assertEqual(self.verify(code).status_code, 303)  # activated; this browser's slot is spent
        stranger = self.app.test_client()
        self.assertIn("друг браузър", self.body(self.verify(code, client=stranger)))
        self.set_slot({"v": 1, "t": "y" * 43, "next": ""}, client=stranger)
        answer = self.verify(code, client=stranger)
        self.assertEqual(answer.status_code, 200)
        self.assertNotIn("Location", answer.headers)

    def test_a_code_past_ten_minutes_is_refused_without_counting(self):
        self.submit()
        self.at(10 * 60)
        self.assertIn("Грешен или изтекъл код.", self.body(self.verify(self.last_code())))
        self.assertEqual(self.pending()[0]["code_attempts"], 0)


# --------------------------------------------------------------------------- delivery at route level (H1 to H7)


class DeliveryTest(RegistrationRouteTestCase):
    def test_not_sent_retires_the_code_and_a_resend_is_admitted_at_once(self):
        self.mailer.outcome = SendOutcome.NOT_SENT
        submitted = self.submit()
        self.assertEqual(submitted.headers["Location"], "/register/verify")
        page = self.body(self.client.get("/register/verify"))
        self.assertIn("Не успяхме да изпратим имейла. Опитай отново след малко.", page)
        self.assertIn("/register/resend", page)
        row, send = self.pending()[0], self.sends()[0]
        self.assertEqual((row["state"], row["code_hash"], row["resend_after"]), ("pending", None, None))
        self.assertEqual(send["outcome"], "not_sent")
        self.mailer.outcome = SendOutcome.ACCEPTED
        self.assertEqual(self.resend().headers["Location"], "/register/verify?resent=1")
        self.assertEqual(len(self.sends()), 2)  # two of the address's attempts, not one
        self.assertEqual(self.verify(self.last_code()).status_code, 303)

    def test_unknown_and_refused_keep_the_code_and_the_wait(self):
        for outcome in (SendOutcome.UNKNOWN, SendOutcome.REFUSED):
            with self.subTest(outcome=outcome):
                self.mailer.outcome = outcome
                client = self.app.test_client()
                self.submit(f"{outcome.value}@example.bg", client=client)
                page = self.body(client.get("/register/verify"))
                self.assertNotIn("Не успяхме", page)
                self.assertEqual(self.sends()[-1]["outcome"], outcome.value)
                self.assertIn("след малко", self.body(self.resend(client=client)))
                self.assertEqual(self.verify(self.last_code(), client=client).status_code, 303)

    def test_mail_is_sent_after_the_commit_with_no_transaction_open(self):
        seen = []

        def outcome(message):
            conn = g.get("db")
            committed = self.rows("SELECT state FROM pending_registrations")
            seen.append((conn is not None and conn.in_transaction, committed))
            return SendOutcome.ACCEPTED

        self.mailer.outcome = outcome
        clock_seen = []
        real_create = registration_service.create_pending

        def create_pending(conn, **kwargs):
            clock = kwargs["clock"]

            def instrumented():
                clock_seen.append(conn.in_transaction)
                return clock()

            return real_create(conn, **{**kwargs, "clock": instrumented})

        with patch.object(registration_service, "create_pending", create_pending):
            self.submit()
        self.assertEqual(seen, [(False, [{"state": "pending"}])])
        # The issuance's time was read inside the write transaction, after BEGIN IMMEDIATE.
        self.assertEqual(clock_seen, [True])
        self.assertEqual(self.sends()[0]["attempted_at"], "2026-10-14 09:00:00")

    def test_a_mailer_exception_is_unknown_and_shows_nothing(self):
        def boom(message):
            raise RuntimeError(f"cannot reach the relay for {message.to}")

        self.mailer.outcome = boom
        response = self.submit()
        self.assertEqual(response.status_code, 303)
        page = self.body(self.client.get("/register/verify"))
        self.assertNotIn("Traceback", page)
        self.assertNotIn("relay", page)
        self.assertEqual(self.sends()[0]["outcome"], "unknown")
        self.assertEqual(len(self.mailer.messages), 1)  # never a second send in the request

    def test_a_failed_settlement_leaves_the_row_started_and_the_page_normal(self):
        self.flaky(2)  # the issuance commits; the settlement's commit fails
        with self.assertLogs("learnpilot.registration", logging.WARNING) as logs:
            response = self.submit()
        self.assertEqual((response.status_code, response.headers["Location"]), (303, "/register/verify"))
        self.assertEqual(self.sends()[0]["outcome"], "started")
        self.assertIn("settlement_failed send=1 error=OperationalError", "\n".join(logs.output))
        self.assertEqual(len(self.mailer.messages), 1)
        self.assertEqual(self.verify(self.last_code()).status_code, 303)


# --------------------------------------------------------------------------- transactions and dispatch


class TransactionTest(RegistrationRouteTestCase):
    def test_failed_issuance_commit_sends_nothing_and_claims_nothing(self):
        dispatched = self.spy(registration_service, "dispatch")
        self.flaky(1)
        with self.assertLogs("learnpilot.registration", logging.INFO) as logs:
            response = self.submit()
        self.assertEqual(response.status_code, 500)
        self.assertIn("Нещо се обърка и нищо не беше записано.", self.body(response))
        self.assertEqual((dispatched, self.mailer.messages), ([], []))
        self.assertEqual((self.pending(), self.sends(), self.control()), ([], [], None))
        self.assertNotIn("reg", self.session())
        self.assertNotIn("Set-Cookie", response.headers)
        text = "\n".join(logs.output)
        self.assertIn("submit_failed error=OperationalError", text)
        self.assertNotRegex(text, r"\bsubmitted pending=")

    def test_a_retry_after_a_failed_issuance_is_one_new_issuance_sent_once(self):
        dispatched = self.spy(registration_service, "dispatch")
        settled = self.spy(registration_service, "settle_send")
        self.flaky(1)
        self.assertEqual(self.submit().status_code, 500)
        self.assertEqual(self.submit().status_code, 303)
        # SQLite gave the rolled-back numbers to the committed issuance; only the
        # committed one, with its own identity, was dispatched and settled.
        self.assertEqual(len(dispatched), 1)
        issued = dispatched[0][0][2]
        send = self.sends()
        self.assertEqual([row["id"] for row in send], [issued.send_id])
        self.assertEqual(send[0]["issuance_id"], issued.issuance_id)
        self.assertEqual([call[0][1:3] for call in settled], [(issued.send_id, issued.issuance_id)])
        self.assertEqual((send[0]["outcome"], len(self.mailer.messages)), ("accepted", 1))

    def test_failed_resend_commit_sends_nothing_and_the_old_code_still_works(self):
        self.submit()
        code = self.last_code()
        self.at(61)
        state = self.flaky(1)
        before = self.digest()
        response = self.resend()
        self.assertEqual(response.status_code, 500)
        self.assertEqual((self.digest(), len(self.mailer.messages)), (before, 1))
        state["fail"].clear()
        self.assertEqual(self.verify(code).status_code, 303)

    def test_failed_activation_commit_claims_nothing_keeps_the_slot_and_a_retry_activates(self):
        self.submit()
        code = self.last_code()
        state = self.flaky(1)
        with self.assertLogs("learnpilot.registration", logging.INFO) as logs:
            response = self.verify(code)
        self.assertEqual(response.status_code, 500)
        page = self.body(response)
        self.assertNotIn("потвърден", page)
        self.assertNotIn("Вече има профил", page)
        self.assertIsNotNone(self.slot())
        self.assertNoAccountSession()
        self.assertEqual(self.users_with(EMAIL), [])
        row = self.pending()[0]
        self.assertEqual((row["state"], row["code_attempts"]), ("pending", 0))
        self.assertIsNotNone(row["code_hash"])
        text = "\n".join(logs.output)
        self.assertIn("activation_failed error=OperationalError", text)
        self.assertNotRegex(text, r"\bactivated pending=")
        state["fail"].clear()
        retried = self.verify(code)
        self.assertEqual(retried.headers["Location"], "/login?registration=activated")
        self.assertEqual(len(self.users_with(EMAIL)), 1)

    def test_a_storage_failure_during_activation_is_the_generic_failure(self):
        self.submit()
        code = self.last_code()
        with patch.object(profiles, "create_profile", side_effect=sqlite3.IntegrityError("CHECK constraint failed")):
            response = self.verify(code)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("Вече има профил", self.body(response))
        self.assertEqual((self.users_with(EMAIL), self.rows("SELECT * FROM user_profiles")), ([], []))
        self.assertEqual(self.pending()[0]["state"], "pending")
        self.assertIsNotNone(self.slot())
        self.assertEqual(self.verify(code).status_code, 303)

    def test_a_failed_cancel_commit_keeps_everything(self):
        self.submit()
        self.flaky(1)
        response = self.cancel()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.pending()[0]["state"], "pending")
        self.assertIsNotNone(self.slot())

    def test_not_sendable_is_never_settled(self):
        settled = self.spy(registration_service, "settle_send")
        with patch.object(registration_service, "_is_committed_issuance", return_value=False), \
                self.assertLogs("learnpilot.registration", logging.WARNING) as logs:
            response = self.submit()
        self.assertEqual(response.status_code, 303)
        self.assertEqual((settled, self.mailer.messages), ([], []))
        self.assertEqual(self.sends()[0]["outcome"], "started")
        self.assertIn("dispatch_refused pending=1 send=1", "\n".join(logs.output))
        # The person can ask for a new code once the wait is over.
        self.at(61)
        self.assertEqual(self.resend().status_code, 303)
        self.assertEqual(len(self.mailer.messages), 1)

    def test_every_issuance_is_dispatched_exactly_once(self):
        dispatched = self.spy(registration_service, "dispatch")
        self.submit()
        self.submit()                      # a repeated POST: a new issuance, the first voided
        self.resend()                      # too early: no issuance
        self.at(61)
        self.resend()                      # a new generation
        self.resend()                      # too early again
        self.verify("000000" if self.last_code() != "000000" else "111111")
        identities = [call[0][2].issuance_id for call in dispatched]
        self.assertEqual(len(identities), 3)
        self.assertEqual(len(set(identities)), 3)
        self.assertEqual(sorted(identities), sorted(row["issuance_id"] for row in self.sends()))
        self.assertEqual(len(self.mailer.messages), 3)
        self.assertEqual({row["outcome"] for row in self.sends()}, {"accepted"})

    def test_concurrent_resends_from_copies_of_one_cookie_issue_and_send_once(self):
        self.submit()
        self.at(61)
        copy = self.app.test_client()
        self.set_slot(dict(self.slot()), client=copy)
        in_send, release = threading.Event(), threading.Event()

        def slow(message):
            in_send.set()
            release.wait(10)
            return SendOutcome.ACCEPTED

        self.mailer.outcome = slow
        results = {}
        first = threading.Thread(target=lambda: results.__setitem__("first", self.resend()))
        first.start()
        self.assertTrue(in_send.wait(10))
        # The first request is in its send with no transaction open; the second runs now.
        self.mailer.outcome = SendOutcome.ACCEPTED
        results["second"] = self.resend(client=copy)
        release.set()
        first.join(10)
        self.assertEqual(results["first"].status_code, 303)
        self.assertIn("след малко", self.body(results["second"]))
        self.assertEqual(len(self.mailer.messages), 2)  # the submission and one resend
        self.assertEqual([row["outcome"] for row in self.sends()], ["accepted", "accepted"])

    def test_concurrent_verifications_of_one_registration_create_one_account(self):
        self.submit()
        code = self.last_code()
        copy = self.app.test_client()
        self.set_slot(dict(self.slot()), client=copy)
        barrier = threading.Barrier(2)
        results = []

        def run(client):
            barrier.wait(10)
            results.append(self.verify(code, client=client))

        threads = [threading.Thread(target=run, args=(client,)) for client in (self.client, copy)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        locations = sorted(response.headers.get("Location") for response in results)
        self.assertEqual(locations, ["/login", "/login?registration=activated"])
        self.assertEqual(len(self.users_with(EMAIL)), 1)
        self.assertEqual(len(self.rows("SELECT * FROM user_profiles")), 1)


# --------------------------------------------------------------------------- holds, clock, budgets (K7)


class AvailabilityTest(RegistrationRouteTestCase):
    def assertUnavailable(self, response):
        self.assertEqual(response.status_code, 503)
        self.assertIn("Регистрацията временно не е достъпна. Опитай по-късно.", self.body(response))

    def check_refused_before_admission(self):
        """Submit and the same for an address with an account: 503, the same page, no
        cookie, no scrypt, no row, no ledger row, no mail."""
        existing = self.make("teacher")["email"]
        hashed = self.spy(accounts, "hash_new_password")
        before, sent = self.digest(), len(self.mailer.messages)
        answers = [self.submit(address, client=self.app.test_client()) for address in (EMAIL, existing)]
        for response in answers:
            self.assertUnavailable(response)
            self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(self.body(answers[0]), self.body(answers[1]))
        self.assertEqual((hashed, self.digest(), len(self.mailer.messages)), ([], before, sent))

    def test_a_restore_hold(self):
        self.set_control(hold="restore")
        self.check_refused_before_admission()

    def test_a_regressed_clock(self):
        self.set_control(high_water=T0 + timedelta(seconds=301))
        self.check_refused_before_admission()

    def test_an_unreaped_sender(self):
        patcher = patch.object(mail, "sender_busy", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.check_refused_before_admission()

    def test_the_total_budget(self):
        self.app.config[CONFIG_KEY] = RegistrationSettings(mailer=self.mailer, total_cap=1)
        self.submit("first.one@example.bg", client=self.app.test_client())
        self.check_refused_before_admission()

    def test_an_open_circuit(self):
        self.mailer.outcome = SendOutcome.NOT_SENT
        for n in range(3):
            self.submit(f"failing{n}@example.bg", client=self.app.test_client())
        self.assertEqual([row["outcome"] for row in self.sends()], ["not_sent"] * 3)
        self.check_refused_before_admission()

    def test_a_hold_refuses_submit_resend_and_verify_but_not_cancel(self):
        self.submit()
        code = self.last_code()
        self.at(61)
        self.set_control(hold="restore")
        control, before = self.control(), self.digest()
        self.assertUnavailable(self.resend())
        self.assertUnavailable(self.verify(code))
        self.assertUnavailable(self.submit("another@example.bg", client=self.app.test_client()))
        self.assertEqual(self.digest(), before)
        # Approved with PR 4: a hold does not block the cancellation of pending data.
        cancelled = self.cancel()
        self.assertEqual(cancelled.status_code, 303)
        self.assertIsNone(self.slot())
        self.assertEqual(self.pending()[0]["state"], "void")
        # The hold and the mark are untouched: registration stays closed.
        self.assertEqual(self.control(), control)
        self.assertUnavailable(self.submit())
        self.assertEqual(self.pending()[-1]["state"], "void")

    def test_a_regressed_clock_refuses_cancel_too(self):
        self.submit()
        self.set_control(high_water=T0 + timedelta(hours=1))
        before = self.digest()
        self.assertUnavailable(self.cancel())
        self.assertUnavailable(self.verify(self.last_code()))
        self.assertUnavailable(self.resend())
        self.assertEqual(self.digest(), before)
        self.assertIsNotNone(self.slot())
        # The page itself is shown; the POSTs decide.
        self.assertEqual(self.client.get("/register/verify").status_code, 200)

    def test_address_caps_answer_429_before_hashing(self):
        for n in range(5):
            self.at(n)
            self.submit(client=self.app.test_client())
        hashed = self.spy(accounts, "hash_new_password")
        self.at(10)
        capped = self.submit(client=self.app.test_client())
        self.assertEqual(capped.status_code, 429)
        self.assertIn("Твърде много имейли към този адрес.", self.body(capped))
        self.assertIn('value="Иванова"', self.body(capped))
        self.assertEqual(hashed, [])
        self.assertEqual(len(self.sends()), 5)

    def test_the_teacher_gate_voids_a_teacher_row_at_verify(self):
        # No route creates a teacher registration; one stored by the service is voided
        # at its browser's next request and never activated (D5).
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            teacher = registration_service.create_pending(
                conn, cleaned={"account_type": "teacher", "first_name": "Иван", "last_name": "Петров",
                               "email": "ivan@example.bg", "school_name": "СУ „Васил Левски“, Пловдив",
                               "subjects": [{"subject_id": None, "label": "Музика"}], "next": None},
                account_type="teacher", password_hash=accounts.hash_new_password(PASSWORD, "ivan@example.bg"),
                previous_binding=None, secret_key=self.app.config["SECRET_KEY"], clock=lambda: self.wall,
                sender_busy=False, teacher_enabled=True)
            conn.commit()
        self.set_slot({"v": 1, "t": teacher.token, "next": ""})
        code = code_of(teacher)
        response = self.verify(code)
        self.assertUnavailable(response)
        self.assertIsNone(self.slot())
        self.assertEqual(self.pending()[0]["state"], "void")
        self.assertEqual(self.users_with("ivan@example.bg"), [])


# --------------------------------------------------------------------------- enumeration (G)


class EnumerationTest(RegistrationRouteTestCase):
    def pair(self, action):
        """Run ``action(client, email)`` for a new address and for one with an account."""
        existing = "taken.address@example.bg"
        self.make("teacher", email=existing)
        fresh = "fresh.address@example.bg"  # the same length as the other
        self.assertEqual(len(fresh), len(existing))
        out = {}
        for email in (fresh, existing):
            client = self.app.test_client()
            out[email] = action(client, email)
        return out[fresh], out[existing], fresh, existing

    def comparable(self, response, email):
        body = response.get_data(as_text=True).replace(email, "ADDRESS")
        return (response.status_code, response.headers.get("Location"), body,
                sorted(name for name, _ in response.headers if name != "Set-Cookie"))

    def test_submit_verify_and_resend_answer_alike(self):
        hashed = self.spy(accounts, "hash_new_password")
        codes = patch.object(registration_service, "_new_code", return_value="123456")
        codes.start()
        self.addCleanup(codes.stop)

        def journey(client, email):
            submitted = self.submit(email, client=client)
            slot = self.slot(client)
            page = client.get("/register/verify")
            wrong = self.verify("999999", client=client)
            early = self.resend(client=client)
            self.at(61)
            resent = self.resend(client=client)
            self.at(0)
            return [submitted, page, wrong, early, resent], slot

        (fresh_answers, fresh_slot), (taken_answers, taken_slot), fresh, existing = self.pair(journey)
        for a, b in zip(fresh_answers, taken_answers):
            self.assertEqual(self.comparable(a, fresh), self.comparable(b, existing))
        self.assertEqual(set(fresh_slot), set(taken_slot))
        self.assertEqual(len(hashed), 2)
        rows = self.pending()
        self.assertEqual([(row["state"], row["send_count"], row["code_attempts"]) for row in rows],
                         [("pending", 2, 0), ("pending", 2, 0)])
        self.assertEqual(sorted(row["is_shadow"] for row in rows), [0, 1])
        self.assertEqual(len(self.sends()), 4)
        # G7: the owner gets the notice, which holds no name, role, link or code.
        notices = [m for m in self.mailer.messages if m.to == existing]
        self.assertEqual({m.subject for m in notices}, {registration_service.NOTICE_SUBJECT})
        for message in notices:
            self.assertNotRegex(message.text, r"\d{6}|https?://|Учител|teacher|Тест")

    def test_the_application_steps_of_a_submission_and_a_resend_do_not_depend_on_the_account(self):
        """G5, as far as a test can: the steps this application takes, in order, between
        the request and ``send`` and between ``send`` and the response are the same for a
        new address and an existing one, while the fake backend is told to take a
        different stated time for a code and for a notice.

        This compares the sequence of steps and their count, never elapsed time: a mocked
        backend cannot prove equal timing, and nothing here does. The application does
        not equalise the provider's own latency (design 3.7); that, and what a class
        behind one NAT costs, stay activation evidence (test plan section L).
        """
        trace, stated, slept = [], [], []

        def stated_delay(message):
            seconds = 7.0 if message.subject == registration_service.NOTICE_SUBJECT else 0.5
            stated.append(seconds)
            return seconds

        self.mailer.delay = stated_delay
        self.mailer.sleep = slept.append  # records the stated time; nothing waits
        real_send = self.mailer.send

        def step(owner, name, label=None):
            real = getattr(owner, name)

            def recorded(*args, **kwargs):
                trace.append(label or name)
                return real(*args, **kwargs)

            patcher = patch.object(owner, name, recorded)
            patcher.start()
            self.addCleanup(patcher.stop)

        for owner, name in ((registration_service, "availability"), (registration_service, "address_capped"),
                            (accounts, "hash_new_password"), (registration_service, "create_pending"),
                            (registration_service, "dispatch"), (registration_service, "settle_send"),
                            (registration_service, "resend"), (registration_blueprint, "_utcnow")):
            step(owner, name, "clock" if name == "_utcnow" else None)
        self.mailer.send = lambda message: (trace.append("send"), real_send(message))[1]
        app_sleeps = []
        sleeping = patch("time.sleep", lambda seconds: app_sleeps.append(seconds))
        sleeping.start()
        self.addCleanup(sleeping.stop)

        def requests(client, email):
            del trace[:]
            self.submit(email, client=client)
            submitted = list(trace)
            self.at(61)
            del trace[:]
            self.resend(client=client)
            resent = list(trace)
            self.at(0)
            return submitted, resent

        (fresh_submit, fresh_resend), (taken_submit, taken_resend), _, _ = self.pair(requests)

        # Each kind took its own stated time at the backend, and that is the only difference.
        self.assertEqual(sorted(stated), [0.5, 0.5, 7.0, 7.0])
        self.assertEqual(sorted(slept), [0.5, 0.5, 7.0, 7.0])
        self.assertEqual(app_sleeps, [], "the application never waits on its own")
        self.assertEqual(fresh_submit, taken_submit)
        self.assertEqual(fresh_resend, taken_resend)
        for steps in (fresh_submit, fresh_resend):
            self.assertEqual(steps.count("send"), 1)
            self.assertEqual(steps.count("dispatch"), 1)
            self.assertEqual(steps.count("settle_send"), 1)
            before, after = steps[:steps.index("send")], steps[steps.index("send") + 1:]
            self.assertLess(before.index("dispatch"), steps.index("send"))
            # After the backend returns only the settlement follows: its clock read and itself.
            self.assertEqual([name for name in after if name != "clock"], ["settle_send"])
        self.assertEqual(fresh_submit.count("hash_new_password"), 1)

    def test_a_one_sided_delivery_failure_is_not_visible(self):
        def by_kind(message):
            return SendOutcome.REFUSED if message.subject == registration_service.NOTICE_SUBJECT else SendOutcome.ACCEPTED

        self.mailer.outcome = by_kind
        fresh, taken, fresh_email, existing = self.pair(
            lambda client, email: (self.submit(email, client=client), client.get("/register/verify")))
        for a, b in zip(fresh, taken):
            self.assertEqual(self.comparable(a, fresh_email), self.comparable(b, existing))

    def test_not_sent_gives_the_same_visible_failure_for_both_kinds(self):
        self.mailer.outcome = SendOutcome.NOT_SENT
        fresh, taken, fresh_email, existing = self.pair(
            lambda client, email: (self.submit(email, client=client), client.get("/register/verify")))
        self.assertIn("Не успяхме да изпратим имейла", self.body(fresh[1]))
        self.assertEqual(self.comparable(fresh[1], fresh_email), self.comparable(taken[1], existing))
        self.assertEqual({row["code_hash"] for row in self.pending()}, {None})

    def test_the_shadow_code_never_activates_and_login_stays_uniform(self):
        existing = self.make("student", email="owner@example.bg")
        before = self.rows("SELECT * FROM users WHERE id = ?", existing["id"])
        self.submit("owner@example.bg")
        for guess in ("000000", "123456"):
            self.verify(guess)
        self.assertEqual(self.rows("SELECT * FROM users WHERE id = ?", existing["id"]), before)
        # G8: an unverified registration's credentials, an unknown address, a wrong password.
        self.submit("never.verified@example.bg", client=self.app.test_client())
        for email, password in (("never.verified@example.bg", STUDENT_PASSWORD), ("nobody@example.bg", PASSWORD),
                                ("owner@example.bg", "a wrong password")):
            page = self.body(self.login(email, password, client=self.app.test_client()))
            self.assertIn("Грешен имейл или парола.", page)


# --------------------------------------------------------------------------- roles (I1, I2, I4)


class RoleTest(RegistrationRouteTestCase):
    def test_a_registered_student_has_exactly_the_student_matrix(self):
        self.register()
        self.login(EMAIL, STUDENT_PASSWORD)
        for path, status in (("/account", 200), ("/account/attempts", 200), ("/teacher/new", 403),
                             ("/teacher", 403), ("/dzi", 403), ("/teacher/dzi-training", 403)):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, status)

    def test_no_request_produces_an_admin_or_teacher(self):
        self.submit("x@example.bg", role="admin", is_admin="1", account_type="teacher", school_name="СУ",
                    subject="it")
        self.verify(self.last_code())
        self.assertEqual({row["role"] for row in self.rows("SELECT role FROM users")}, {"student"})


# --------------------------------------------------------------------------- logging (3.10)


class LoggingTest(RegistrationRouteTestCase):
    def test_lines_are_fixed_events_with_ids_and_never_a_value(self):
        with self.assertLogs("learnpilot.registration", logging.DEBUG) as logs:
            self.submit(next="/account/attempts")
            first = self.last_code()
            self.verify("000000" if first != "000000" else "111111")
            self.at(61)
            self.resend()
            self.verify(self.last_code())
        text = "\n".join(logs.output)
        for event in ("submitted pending=1 send=1", "send_settled send=1 outcome=accepted", "wrong_code pending=1",
                      "resent pending=1 send=2", "activated pending=1 user="):
            self.assertIn(event, text)
        token_values = [row["issuance_id"] for row in self.sends()]
        for secret in (EMAIL, "Ана", "Иванова", STUDENT_PASSWORD.strip(), first, self.last_code(),
                       self.app.config["SECRET_KEY"], *token_values, "/account/attempts"):
            self.assertNotIn(secret, text)

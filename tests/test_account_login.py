"""Accounts PR 4 — account login, logout, password change, session lifetime and throttle.

``/login`` issues exactly ``uid``, ``sk`` and ``iat``; every credential failure
looks the same; an identifier is throttled whether or not an account exists; an
account session has an absolute lifetime decided by the role in the database;
``POST /logout`` clears this browser only; a password change re-keys the account.
Throttle time is injected, so nothing here sleeps.
"""

import ast
import html
import itertools
import re
import sqlite3
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pyotp
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

from tests.auth_helpers import (
    HISTORICAL_SHARED_LOGIN_SESSION,
    create_account,
    enroll_totp,
    set_account_session,
)
from tests.support import ROOT, IsolatedAppTestCase, web_app
from web import access, account_session, oauth_state, request_integrity
from web import db as web_db
from web.access import ANONYMOUS
from web.blueprints import account as account_blueprint
from web.redirects import safe_redirect_target
from web.services import accounts, google_oidc, login_throttle

PASSWORD = "correct horse battery"
NEW_PASSWORD = "an entirely new passphrase"
INVALID = "Грешен имейл или парола."
THROTTLED = "Твърде много опити. Опитай отново след няколко минути."
DAY = 24 * 3600
T0 = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)
SAME_ORIGIN = {"Origin": "http://localhost"}
CROSS_ORIGIN = {"Origin": "http://evil.example"}
_emails = itertools.count(1)
# Synthetic, generated per test run; admins made below get a TOTP under it.
TOTP_KEY = Fernet.generate_key()


class AccountAppTestCase(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        cls.app.config.update(TESTING=True)
        cls.db_path = cls.app.config["DB_PATH"]

    def setUp(self):
        self.client = self.app.test_client()
        self.now = T0
        clock = patch.object(account_blueprint, "_utcnow", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        config = patch.dict(self.app.config, {"TOTP_ENCRYPTION_KEY": TOTP_KEY})
        config.start()
        self.addCleanup(config.stop)

    def make(self, role="teacher", *, password=PASSWORD, totp=True, **options):
        """A real account row with a unique email; returns id, session_key and email.

        An admin also gets a confirmed TOTP (``totp_secret``, ``recovery_codes``)
        unless ``totp=False``.
        """
        email = options.pop("email", None) or f"user{next(_emails)}@example.bg"
        account = create_account(self.db_path, role, email=email, password=password, **options)
        account = {**account, "email": email, "role": role}
        if role == "admin" and totp:
            enrolled = enroll_totp(self.db_path, account["id"], TOTP_KEY, self.now - timedelta(minutes=5))
            account.update(session_key=enrolled["session_key"], totp_secret=enrolled["secret"],
                           recovery_codes=enrolled["recovery_codes"])
        return account

    def code(self, account, at=None):
        return pyotp.TOTP(account["totp_secret"]).at(at or self.now)

    def submit_code(self, code, *, client=None, headers=SAME_ORIGIN, **form):
        return (client or self.client).post("/login/totp", data={"code": code, **form}, headers=headers)

    def sign_in(self, account, *, client=None, **form):
        """Sign in through the real forms; an admin also answers the TOTP step.

        The admin step moves the clock to a fresh TOTP step first, so each sign-in uses
        a new code, and submits the ``next`` the TOTP page carries.
        """
        if account["role"] != "admin":
            return self.login(account["email"], client=client, **form)
        self.now += timedelta(seconds=31)
        page = self.login(account["email"], client=client, **form)
        self.assertEqual(page.status_code, 200, self.body(page))
        next_value = html.unescape(re.search(r'name="next" value="([^"]*)"', self.body(page)).group(1))
        return self.submit_code(self.code(account), client=client, next=next_value)

    def query(self, sql, params=()):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(sql, params).fetchall()

    def user(self, user_id, column):
        return self.query(f"SELECT {column} FROM users WHERE id = ?", (user_id,))[0][0]

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(sql, params)
            conn.commit()

    def throttle_rows(self):
        return self.query("SELECT identifier_hash, failure_count, blocked_until FROM login_throttle")

    def throttle_key(self, identifier):
        return login_throttle.identifier_hash(self.app.config["SECRET_KEY"], identifier)

    def failures(self, identifier):
        rows = self.query("SELECT failure_count FROM login_throttle WHERE identifier_hash = ?",
                          (self.throttle_key(identifier),))
        return rows[0][0] if rows else 0

    def login(self, email, password=PASSWORD, *, client=None, headers=SAME_ORIGIN, **form):
        return (client or self.client).post(
            "/login", data={"email": email, "password": password, **form}, headers=headers)

    def session(self, client=None):
        with (client or self.client).session_transaction() as sess:
            return dict(sess)

    def body(self, response):
        return response.get_data(as_text=True)

    def fail_login(self, email, times):
        """Record ``times`` failures for ``email`` without paying for scrypt each time."""
        with patch.object(accounts, "check_password_hash", return_value=False):
            for _ in range(times):
                self.login(email, "wrong password")

    def principal_for(self, session_state, path="/"):
        with self.app.test_request_context(path):
            from flask import session

            session.update(session_state)
            self.assertIsNone(self.app.preprocess_request())
            return access.current_principal(), dict(session)


# --------------------------------------------------------------------------- login


class LoginTest(AccountAppTestCase):
    def test_get_renders_the_form_without_a_cookie(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        html = self.body(response)
        for needle in ('name="email"', 'name="password"', 'method="post"', 'autocomplete="current-password"'):
            self.assertIn(needle, html)
        self.assertNotIn("Set-Cookie", response.headers)

    def test_each_role_signs_in_and_lands_on_its_home(self):
        for role, home in (("student", "/"), ("teacher", "/teacher/new"), ("admin", "/teacher")):
            with self.subTest(role=role):
                account = self.make(role)
                client = self.app.test_client()
                before = int(time.time())
                response = self.sign_in(account, client=client)
                self.assertEqual((response.status_code, response.headers["Location"]), (302, home))
                state = self.session(client)
                self.assertEqual(set(state), {"uid", "sk", "iat"})
                self.assertEqual((state["uid"], state["sk"]), (account["id"], account["session_key"]))
                self.assertIs(type(state["iat"]), int)
                self.assertTrue(before <= state["iat"] <= int(time.time()))
                self.assertEqual(client.get(home).status_code, 200)

    def test_role_and_name_are_not_copied_into_the_session(self):
        account = self.make("admin", display_name="Ана Админова")
        self.login(account["email"])
        serialized = repr(self.session())
        for leaked in ("admin", "Ана", "role", "display_name", "is_admin", "is_teacher", "is_active",
                       account["email"]):
            self.assertNotIn(leaked, serialized)

    def test_email_is_normalized_before_lookup(self):
        account = self.make("teacher")
        response = self.login(f"  {account['email'].upper()} ")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.session()["uid"], account["id"])

    def test_last_login_at_is_recorded_only_on_success(self):
        account = self.make("teacher")
        self.login(account["email"], "wrong password")
        self.assertIsNone(self.user(account["id"], "last_login_at"))
        self.login(account["email"])
        self.assertRegex(self.user(account["id"], "last_login_at"), r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_pre_login_state_is_cleared_except_attempt_ownership(self):
        account = self.make("student")
        with self.client.session_transaction() as sess:
            sess.update({**HISTORICAL_SHARED_LOGIN_SESSION,
                         "quiz_attempt_owner:7": True, "quiz_attempt_owner:12": True,
                         "something_else": "x"})
        self.login(account["email"])
        state = self.session()
        self.assertEqual(set(state), {"uid", "sk", "iat", "quiz_attempt_owner:7", "quiz_attempt_owner:12"})
        self.assertIs(state["quiz_attempt_owner:7"], True)

    def test_historical_shared_login_keys_grant_nothing_before_or_after_an_account_login(self):
        account = self.make("student")
        with self.client.session_transaction() as sess:
            sess.update(HISTORICAL_SHARED_LOGIN_SESSION)
        for path in ("/teacher", "/teacher/new", "/dzi", "/teacher/assignments", "/account/password"):
            with self.subTest(path=path, when="before"):
                response = self.client.get(path)
                self.assertEqual((response.status_code, response.headers["Location"]),
                                 (302, f"/login?next={path}"))
        self.login(account["email"])
        self.assertEqual(set(self.session()), {"uid", "sk", "iat"})
        # Put back next to the student account, they still grant nothing.
        with self.client.session_transaction() as sess:
            sess.update(HISTORICAL_SHARED_LOGIN_SESSION)
        for path in ("/teacher", "/teacher/new", "/dzi", "/teacher/assignments"):
            with self.subTest(path=path, when="after"):
                self.assertEqual(self.client.get(path).status_code, 403)

    def test_signing_in_replaces_another_account(self):
        first, second = self.make("admin"), self.make("student")
        self.login(first["email"])
        self.login(second["email"])
        self.assertEqual(self.session()["uid"], second["id"])
        self.assertEqual(self.client.get("/teacher").status_code, 403)

    def test_login_response_never_echoes_the_password(self):
        account = self.make("teacher")
        for password in (PASSWORD, "wrong-password-value"):
            with self.subTest(password=password):
                response = self.login(account["email"], password, client=self.app.test_client())
                self.assertNotIn(password, self.body(response))
                self.assertNotIn(password, response.headers.get("Location", ""))


class LoginFailureTest(AccountAppTestCase):
    def failure_cases(self):
        active = self.make("teacher")
        disabled = self.make("teacher", is_active=False)
        passwordless = self.make("teacher", password=None)
        return {
            "wrong password": (active["email"], "not the password"),
            "unknown email": ("nobody-here@example.bg", PASSWORD),
            "disabled account": (disabled["email"], PASSWORD),
            "account without password": (passwordless["email"], PASSWORD),
            "malformed identifier": ("not an email", PASSWORD),
            "empty identifier": ("", PASSWORD),
            "empty password": (active["email"], ""),
            "oversized password": (active["email"], "x" * 5000),
        }

    def test_every_failure_cause_gives_the_same_public_response(self):
        seen = set()
        for name, (email, password) in self.failure_cases().items():
            with self.subTest(case=name):
                client = self.app.test_client()
                response = self.login(email, password, client=client)
                html = self.body(response)
                self.assertEqual(response.status_code, 200)
                self.assertIn(INVALID, html)
                self.assertNotIn("uid", self.session(client))
                self.assertNotIn("Set-Cookie", response.headers)
                # Identical apart from the email echoed back into the form.
                seen.add((response.status_code, html.replace(self._escaped(email), "")))
        self.assertEqual(len(seen), 1)

    @staticmethod
    def _escaped(value):
        from markupsafe import escape

        return f'value="{escape(value)}" autocomplete="username"'

    def test_every_failure_cause_costs_exactly_one_password_verification(self):
        for name, (email, password) in self.failure_cases().items():
            with self.subTest(case=name):
                with patch.object(accounts, "check_password_hash",
                                  wraps=accounts.check_password_hash) as verify:
                    self.login(email, password, client=self.app.test_client())
                self.assertEqual(verify.call_count, 1)

    def test_unknown_and_passwordless_accounts_verify_against_the_dummy_hash(self):
        passwordless = self.make("teacher", password=None)
        for email in ("nobody-here@example.bg", passwordless["email"], "not an email"):
            with self.subTest(email=email):
                with patch.object(accounts, "check_password_hash",
                                  wraps=accounts.check_password_hash) as verify:
                    self.login(email, client=self.app.test_client())
                verify.assert_called_once_with(accounts._DUMMY_PASSWORD_HASH, PASSWORD)

    def test_dummy_hash_is_a_module_level_scrypt_hash_that_is_never_stored(self):
        dummy = accounts._DUMMY_PASSWORD_HASH
        self.assertTrue(dummy.startswith("scrypt:"))
        self.assertEqual(dummy.split("$", 1)[0], accounts._CURRENT_HASH_METHOD)
        self.login("nobody-here@example.bg")
        self.assertIs(accounts._DUMMY_PASSWORD_HASH, dummy)  # not regenerated per request
        self.assertEqual(self.query("SELECT COUNT(*) FROM users WHERE password_hash = ?", (dummy,)), [(0,)])

    def test_malformed_identifiers_never_raise(self):
        for value in ("a@@b.bg", "@", " ", "a b@c.bg", "x" * 5000, "a@b.bg\x00", "ａ＠ｂ", "'; DROP TABLE users;--"):
            with self.subTest(value=value[:20]):
                response = self.login(value, client=self.app.test_client())
                self.assertEqual(response.status_code, 200)
                self.assertIn(INVALID, self.body(response))
        self.assertGreater(self.query("SELECT COUNT(*) FROM users")[0][0], -1)

    def test_identifier_is_escaped_when_echoed(self):
        response = self.login('"><script>alert(1)</script>')
        self.assertNotIn("<script>alert(1)</script>", self.body(response))
        self.assertLess(len(self.body(self.login("x" * 100_000))), 60_000)  # echo is bounded

    def test_credentials_are_never_read_from_the_query_string(self):
        account = self.make("teacher")
        response = self.client.post(
            f"/login?email={account['email']}&password={PASSWORD}", data={}, headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("uid", self.session())
        self.assertNotIn(account["email"], self.body(self.client.get(f"/login?email={account['email']}")))


class PasswordRehashTest(AccountAppTestCase):
    OLD_METHOD = "pbkdf2:sha256:1000"

    def with_old_hash(self):
        account = self.make("teacher")
        self.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (generate_password_hash(PASSWORD, method=self.OLD_METHOD), account["id"]))
        return account

    def test_successful_login_upgrades_an_outdated_hash_and_keeps_the_session_key(self):
        account = self.with_old_hash()
        changed_at = self.user(account["id"], "password_changed_at")
        self.assertEqual(self.login(account["email"]).status_code, 302)
        stored = self.user(account["id"], "password_hash")
        self.assertEqual(stored.split("$", 1)[0], accounts._CURRENT_HASH_METHOD)
        self.assertEqual(self.user(account["id"], "session_key"), account["session_key"])
        self.assertEqual(self.user(account["id"], "password_changed_at"), changed_at)
        self.assertEqual(self.session()["sk"], account["session_key"])
        # The upgraded hash still verifies the same password.
        self.assertEqual(self.login(account["email"], client=self.app.test_client()).status_code, 302)

    def test_failed_login_never_rehashes(self):
        account = self.with_old_hash()
        before = self.user(account["id"], "password_hash")
        self.login(account["email"], "wrong password")
        self.assertEqual(self.user(account["id"], "password_hash"), before)

    def test_current_hash_is_not_rewritten(self):
        account = self.make("teacher")
        before = self.user(account["id"], "password_hash"), self.user(account["id"], "updated_at")
        self.login(account["email"])
        self.assertEqual((self.user(account["id"], "password_hash"), self.user(account["id"], "updated_at")),
                         before)


class LoginRedirectTest(AccountAppTestCase):
    UNSAFE = ("https://evil.example/", "//evil.example/", "/\\evil.example", "/\\/evil.example",
              "\\\\evil.example", "javascript:alert(1)", "teacher", "http:/evil.example",
              "/teacher\r\nSet-Cookie: x=1", "/teacher\tx", " /teacher")

    def test_safe_next_is_followed(self):
        account = self.make("admin")
        response = self.sign_in(account, next="/teacher/assignments?page=2")
        self.assertEqual(response.headers["Location"], "/teacher/assignments?page=2")

    def test_next_from_the_query_string_reaches_the_form_and_is_followed(self):
        account = self.make("admin")
        self.assertIn('name="next" value="/teacher/assignments"',
                      self.body(self.client.get("/login?next=/teacher/assignments")))
        page = self.client.post("/login?next=/teacher/assignments",
                                data={"email": account["email"], "password": PASSWORD}, headers=SAME_ORIGIN)
        self.assertIn('name="next" value="/teacher/assignments"', self.body(page))
        response = self.submit_code(self.code(account), next="/teacher/assignments")
        self.assertEqual(response.headers["Location"], "/teacher/assignments")

    def test_unsafe_next_falls_back_to_the_role_home(self):
        homes = {"admin": "/teacher", "teacher": "/teacher/new", "student": "/"}
        accounts_by_role = {role: self.make(role) for role in homes}
        for target in self.UNSAFE:
            for role, home in homes.items():
                with self.subTest(next=target, role=role):
                    response = self.sign_in(accounts_by_role[role], client=self.app.test_client(),
                                            next=target)
                    self.assertEqual((response.status_code, response.headers["Location"]), (302, home))

    def test_unsafe_next_is_not_rendered_into_the_form(self):
        for target in self.UNSAFE:
            with self.subTest(next=target):
                response = self.client.get("/login", query_string={"next": target})
                self.assertEqual(response.status_code, 200)
                self.assertIn('name="next" value=""', self.body(response))

    def test_unsafe_next_survives_a_failed_attempt_as_empty(self):
        response = self.login("nobody-here@example.bg", next="//evil.example/")
        self.assertIn('name="next" value=""', self.body(response))

    def test_validator_rejects_backslashes_and_control_characters(self):
        with self.app.test_request_context("/"):
            for target in self.UNSAFE:
                with self.subTest(target=target):
                    self.assertEqual(safe_redirect_target(target, "/fallback"), "/fallback")
            for target in ("/", "/teacher", "/teacher/new?subject=x&section=y", "/quiz/5#top", "/%5cx"):
                with self.subTest(target=target):
                    self.assertEqual(safe_redirect_target(target, "/fallback"), target)



# --------------------------------------------------------------------------- throttle


class LoginThrottleTest(AccountAppTestCase):
    def test_failures_are_recorded_for_existing_and_unknown_identifiers(self):
        account = self.make("teacher")
        self.login(account["email"], "wrong password")
        self.login(account["email"], "wrong password")
        self.login("ghost@example.bg", "wrong password")
        self.assertEqual(self.failures(account["email"]), 2)
        self.assertEqual(self.failures("ghost@example.bg"), 1)

    def test_identifier_variants_share_one_counter(self):
        self.login("Ghost2@Example.BG ", "wrong password")
        self.login(" ghost2@example.bg", "wrong password")
        self.assertEqual(self.failures("ghost2@example.bg"), 2)

    def test_no_plaintext_identifier_is_stored(self):
        account = self.make("teacher")
        attempts = (account["email"], "secret-ghost@example.bg", "not an email at all")
        for email in attempts:
            self.login(email, "wrong password")
        rows = self.throttle_rows()
        for email in attempts:
            self.assertEqual(self.failures(accounts.throttle_identifier(email)), 1)
        dump = repr(self.query("SELECT * FROM login_throttle"))
        for email in attempts:
            self.assertNotIn(email, dump)
            self.assertNotIn(email.split("@")[0], dump)
        for key, _, _ in rows:
            self.assertRegex(key, r"^[0-9a-f]{64}$")

    def test_malformed_identifiers_are_throttled_under_a_bounded_key(self):
        huge = "x" * 5000
        self.fail_login(huge, 10)
        self.assertEqual(self.login(huge).status_code, 429)
        self.assertTrue(all(len(key) == 64 for key, _, _ in self.throttle_rows()))
        identifier = accounts.throttle_identifier(huge)
        self.assertLess(len(identifier), 300)
        with self.assertRaises(ValueError):
            accounts.normalize_email(identifier)  # can never collide with a real email

    def test_tenth_failure_blocks_and_the_block_looks_the_same_for_everyone(self):
        account = self.make("teacher")
        responses = {}
        for label, email in (("existing", account["email"]), ("unknown", "ghost3@example.bg")):
            self.fail_login(email, 9)
            self.assertEqual(self.login(email, "wrong password").status_code, 200)  # the tenth
            blocked = self.login(email, "wrong password")
            self.assertEqual(blocked.status_code, 429)
            html = self.body(blocked)
            self.assertIn(THROTTLED, html)
            self.assertNotIn(INVALID, html)
            responses[label] = html.replace(email, "")
        self.assertEqual(responses["existing"], responses["unknown"])

    def test_blocked_identifier_is_refused_even_with_the_right_password(self):
        account = self.make("teacher")
        self.fail_login(account["email"], 10)
        response = self.login(account["email"])
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("uid", self.session())
        self.assertIsNone(self.user(account["id"], "last_login_at"))

    def test_blocked_request_never_reaches_password_hashing_and_writes_nothing(self):
        account = self.make("teacher")
        self.fail_login(account["email"], 10)
        before = self.throttle_rows()
        with patch.object(accounts, "check_password_hash") as verify, \
                patch.object(accounts, "generate_password_hash") as hashing:
            for email in (account["email"], account["email"].upper()):
                self.assertEqual(self.login(email).status_code, 429)
        verify.assert_not_called()
        hashing.assert_not_called()
        self.assertEqual(self.throttle_rows(), before)

    def test_block_expires_after_fifteen_minutes(self):
        account = self.make("teacher")
        self.fail_login(account["email"], 10)
        self.now = T0 + timedelta(minutes=15) - timedelta(seconds=1)
        self.assertEqual(self.login(account["email"]).status_code, 429)
        self.now = T0 + timedelta(minutes=15)
        self.assertEqual(self.login(account["email"]).status_code, 302)
        self.assertEqual(self.failures(account["email"]), 0)

    def test_success_clears_the_counter(self):
        account = self.make("teacher")
        self.fail_login(account["email"], 9)
        self.assertEqual(self.login(account["email"]).status_code, 302)
        self.assertEqual(self.failures(account["email"]), 0)
        self.fail_login(account["email"], 9)
        self.assertEqual(self.login(account["email"], client=self.app.test_client()).status_code, 302)

    def test_stale_rows_are_cleaned_on_the_next_write(self):
        self.fail_login("old-ghost@example.bg", 3)
        self.now = T0 + timedelta(minutes=16)
        self.assertEqual(self.failures("old-ghost@example.bg"), 3)  # reads do not clean
        self.client.get("/login")
        self.assertEqual(self.failures("old-ghost@example.bg"), 3)
        self.fail_login("new-ghost@example.bg", 1)
        self.assertEqual(self.failures("old-ghost@example.bg"), 0)
        self.assertEqual(self.failures("new-ghost@example.bg"), 1)

    def test_one_identifier_does_not_block_another(self):
        account = self.make("teacher")
        self.fail_login("ghost4@example.bg", 10)
        self.assertEqual(self.login(account["email"]).status_code, 302)

    def test_cross_origin_and_headerless_posts_cannot_touch_the_throttle_or_the_session(self):
        account = self.make("teacher")
        before = self.throttle_rows()
        response = self.login(account["email"], "wrong password", headers=CROSS_ORIGIN)
        self.assertEqual(response.status_code, 403)
        response = self.login(account["email"], headers=CROSS_ORIGIN)
        self.assertEqual(response.status_code, 403)
        self.app.config["TESTING"] = False
        try:
            self.assertEqual(self.login(account["email"], headers={}).status_code, 403)
        finally:
            self.app.config["TESTING"] = True
        self.assertEqual(self.throttle_rows(), before)
        self.assertEqual(self.session(), {})
        self.assertIsNone(self.user(account["id"], "last_login_at"))


# --------------------------------------------------------------------------- lifetime


class SessionLifetimeRuleTest(unittest.TestCase):
    NOW = 1_800_000_000

    def test_lifetimes(self):
        self.assertEqual(accounts.SESSION_LIFETIME_SECONDS,
                         {"student": 30 * DAY, "teacher": 30 * DAY, "admin": 12 * 3600})

    def test_boundaries(self):
        for role, lifetime in (("student", 30 * DAY), ("teacher", 30 * DAY), ("admin", 12 * 3600)):
            with self.subTest(role=role):
                self.assertFalse(accounts.session_expired(role, self.NOW, self.NOW))
                self.assertFalse(accounts.session_expired(role, self.NOW - lifetime + 1, self.NOW))
                self.assertTrue(accounts.session_expired(role, self.NOW - lifetime, self.NOW))
                self.assertTrue(accounts.session_expired(role, self.NOW - lifetime - 1, self.NOW))

    def test_admin_lifetime_is_shorter_than_a_teacher_session_of_the_same_age(self):
        issued = self.NOW - 13 * 3600
        self.assertTrue(accounts.session_expired("admin", issued, self.NOW))
        self.assertFalse(accounts.session_expired("teacher", issued, self.NOW))

    def test_missing_malformed_and_future_iat_fail_closed(self):
        for iat in (None, "", "1800000000", 1.8e9, True, False, 0, -5, [self.NOW], {"t": 1},
                    self.NOW + accounts.SESSION_CLOCK_SKEW_SECONDS + 1, 10**15):
            with self.subTest(iat=iat):
                self.assertTrue(accounts.session_expired("teacher", iat, self.NOW))
        self.assertFalse(accounts.session_expired("teacher", self.NOW + 5, self.NOW))  # small clock skew

    def test_unknown_role_is_expired(self):
        for role in (None, "", "superuser"):
            self.assertTrue(accounts.session_expired(role, self.NOW, self.NOW))


class SessionLifetimeEnforcementTest(AccountAppTestCase):
    MARGIN = 120  # seconds away from the boundary; the exact boundary is pinned above

    def state(self, account, age, **extra):
        return {"uid": account["id"], "sk": account["session_key"], "iat": int(time.time()) - age, **extra}

    def test_sessions_inside_the_lifetime_are_valid(self):
        for role, age in (("student", 30 * DAY - self.MARGIN), ("teacher", 30 * DAY - self.MARGIN),
                          ("admin", 12 * 3600 - self.MARGIN)):
            with self.subTest(role=role):
                account = create_account(self.db_path, role)
                principal, state = self.principal_for(self.state(account, age))
                self.assertEqual((principal.user_id, principal.role), (account["id"], role))
                self.assertIn("uid", state)

    def test_sessions_past_the_lifetime_are_anonymous_and_dropped(self):
        for role, age in (("student", 30 * DAY + self.MARGIN), ("teacher", 30 * DAY + self.MARGIN),
                          ("admin", 12 * 3600 + self.MARGIN)):
            with self.subTest(role=role):
                account = create_account(self.db_path, role)
                principal, state = self.principal_for(
                    self.state(account, age, **{"quiz_attempt_owner:3": True}))
                self.assertEqual(principal, ANONYMOUS)
                for key in ("uid", "sk", "iat"):
                    self.assertNotIn(key, state)
                self.assertIs(state["quiz_attempt_owner:3"], True)

    def test_lifetime_follows_the_database_role_not_the_cookie(self):
        account = create_account(self.db_path, "admin")
        principal, _ = self.principal_for(self.state(account, 13 * 3600, role="teacher"))
        self.assertEqual(principal, ANONYMOUS)
        # Demoted to teacher, the same 13-hour-old session is inside the teacher lifetime.
        self.execute("UPDATE users SET role = 'teacher' WHERE id = ?", (account["id"],))
        principal, _ = self.principal_for(self.state(account, 13 * 3600))
        self.assertEqual(principal.role, "teacher")

    def test_missing_and_malformed_iat_fail_closed(self):
        account = create_account(self.db_path, "admin")
        base = {"uid": account["id"], "sk": account["session_key"]}
        for name, extra in {"missing": {}, "none": {"iat": None}, "string": {"iat": "1800000000"},
                            "float": {"iat": time.time()}, "bool": {"iat": True}, "zero": {"iat": 0},
                            "list": {"iat": [1]}, "far future": {"iat": int(time.time()) + DAY}}.items():
            with self.subTest(iat=name):
                principal, state = self.principal_for({**base, **extra})
                self.assertEqual(principal, ANONYMOUS)
                self.assertNotIn("uid", state)

    def test_expired_account_session_never_falls_back_to_historical_keys(self):
        account = create_account(self.db_path, "student")
        for historical in ({"admin_authenticated": True}, {"tester_authenticated": True},
                           HISTORICAL_SHARED_LOGIN_SESSION):
            with self.subTest(historical=historical):
                principal, state = self.principal_for(self.state(account, 31 * DAY, **historical))
                self.assertEqual(principal, ANONYMOUS)
                for key in ("uid", "sk", "iat"):
                    self.assertNotIn(key, state)
                # Whatever is left in the cookie is not an identity on the next request either.
                self.assertEqual(self.principal_for(state)[0], ANONYMOUS)

    def test_expired_admin_is_redirected_and_stays_anonymous_on_the_next_request(self):
        account = create_account(self.db_path, "admin")
        with self.client.session_transaction() as sess:
            sess.update(self.state(account, 12 * 3600 + self.MARGIN, admin_authenticated=True))
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                response = self.client.get("/teacher")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "/login?next=/teacher")
        self.assertEqual({key for key in self.session() if key in ("uid", "sk", "iat")}, set())

    def test_cookie_is_a_browser_session_cookie_with_its_security_attributes(self):
        account = self.make("teacher")
        cookie = self.login(account["email"]).headers["Set-Cookie"]
        for attribute in ("Secure", "HttpOnly", "SameSite=Lax"):
            self.assertIn(attribute, cookie)
        self.assertNotIn("Expires=", cookie)
        self.assertNotIn("Max-Age=", cookie)
        self.assertNotIn("_permanent", self.session())

    def test_anonymous_request_still_sets_no_cookie(self):
        for path in ("/", "/login", "/account/password", "/teacher"):
            with self.subTest(path=path):
                self.assertNotIn("Set-Cookie", self.app.test_client().get(path).headers)


# --------------------------------------------------------------------------- logout


class LogoutTest(AccountAppTestCase):
    def test_post_clears_the_whole_session_and_redirects_home(self):
        account = self.make("teacher")
        self.login(account["email"])
        with self.client.session_transaction() as sess:
            sess["quiz_attempt_owner:9"] = True
        response = self.client.post("/logout", headers=SAME_ORIGIN)
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/"))
        self.assertEqual(self.session(), {})
        self.assertEqual(self.client.get("/teacher/new").status_code, 302)

    def test_logout_ignores_a_next_parameter(self):
        response = self.client.post("/logout?next=//evil.example/", data={"next": "https://evil.example/"},
                                    headers=SAME_ORIGIN)
        self.assertEqual(response.headers["Location"], "/")

    def test_get_is_not_a_logout(self):
        account = self.make("teacher")
        self.login(account["email"])
        self.assertEqual(self.client.get("/logout").status_code, 405)
        rule = next(r for r in self.app.url_map.iter_rules() if r.rule == "/logout")
        self.assertEqual(rule.methods - {"OPTIONS"}, {"POST"})
        self.assertEqual(self.session()["uid"], account["id"])
        self.assertEqual(self.client.get("/teacher/new").status_code, 200)

    def test_cross_origin_post_is_rejected_before_the_session_changes(self):
        account = self.make("teacher")
        self.login(account["email"])
        self.assertEqual(self.client.post("/logout", headers=CROSS_ORIGIN).status_code, 403)
        self.assertEqual(self.session()["uid"], account["id"])

    def test_logout_does_not_rotate_the_key_so_other_devices_stay_signed_in(self):
        account = self.make("teacher")
        other_device = self.app.test_client()
        self.login(account["email"])
        self.login(account["email"], client=other_device)
        self.client.post("/logout", headers=SAME_ORIGIN)
        self.assertEqual(self.user(account["id"], "session_key"), account["session_key"])
        self.assertEqual(other_device.get("/teacher/new").status_code, 200)

    def test_anonymous_sessions_may_post_logout_and_everything_is_cleared(self):
        self.assertEqual(self.client.post("/logout", headers=SAME_ORIGIN).status_code, 302)
        with self.client.session_transaction() as sess:
            sess.update(HISTORICAL_SHARED_LOGIN_SESSION)
        self.client.post("/logout", headers=SAME_ORIGIN)
        self.assertEqual(self.session(), {})

    def test_every_role_lands_on_the_home_page(self):
        # Decision P3 of the Google sign-in design: the destination is unchanged, with
        # the provider enabled as without it, and no message is shown about Google.
        provider = google_oidc.provider_config(
            "tests.apps.googleusercontent.com", "synthetic-client-secret", "http://localhost")
        for enabled in (False, True):
            for role in ("student", "teacher", "admin"):
                with self.subTest(role=role, google_enabled=enabled), \
                        patch.dict(self.app.config, {oauth_state.PROVIDER_CONFIG_KEY: provider if enabled else None}):
                    client = self.app.test_client()
                    account = create_account(self.db_path, role)
                    set_account_session(client, account["id"], account["session_key"])
                    response = client.post("/logout?next=/teacher", headers=SAME_ORIGIN)
                    self.assertEqual((response.status_code, response.headers["Location"]), (302, "/"))
                    self.assertEqual(self.session(client), {})

    def test_the_session_is_cleared_in_one_place(self):
        # Sign-out goes through web.account_session.end_session, like the two
        # functions that replace a session: no controller clears the session itself.
        with patch.object(account_blueprint, "end_session", wraps=account_session.end_session) as ended:
            self.client.post("/logout", headers=SAME_ORIGIN)
        ended.assert_called_once_with()
        for relative in ("web/blueprints/account.py", "web/blueprints/oidc.py"):
            self.assertNotIn("session.clear()", Path(ROOT, relative).read_text(encoding="utf-8"))

    def test_logout_is_the_only_sign_out_route(self):
        logout_rules = [(rule.rule, sorted(rule.methods - {"HEAD", "OPTIONS"}))
                        for rule in self.app.url_map.iter_rules() if "logout" in rule.rule]
        self.assertEqual(logout_rules, [("/logout", ["POST"])])
        account = self.make("teacher")
        self.sign_in(account)
        for path in ("/tester/logout", "/admin/logout"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        # The removed GET logouts changed nothing: the account is still signed in.
        self.assertEqual(self.client.get("/teacher/new").status_code, 200)


# --------------------------------------------------------------------------- password change


class PasswordChangeTest(AccountAppTestCase):
    def signed_in(self, role="teacher", **options):
        account = self.make(role, **options)
        set_account_session(self.client, account["id"], account["session_key"])
        return account

    def change(self, current=PASSWORD, new=NEW_PASSWORD, confirm=None, *, client=None, headers=SAME_ORIGIN):
        return (client or self.client).post(
            "/account/password",
            data={"current_password": current, "new_password": new,
                  "confirm_password": new if confirm is None else confirm},
            headers=headers)

    def snapshot(self, account):
        return self.query("SELECT password_hash, session_key, password_changed_at FROM users WHERE id = ?",
                          (account["id"],))[0]

    def test_form_renders_for_an_account(self):
        self.signed_in("student")
        response = self.client.get("/account/password")
        self.assertEqual(response.status_code, 200)
        html = self.body(response)
        for field in ("current_password", "new_password", "confirm_password"):
            self.assertIn(f'name="{field}"', html)

    def test_anonymous_is_sent_to_the_account_login(self):
        for method in ("get", "post"):
            with self.subTest(method=method):
                response = getattr(self.app.test_client(), method)("/account/password", headers=SAME_ORIGIN)
                self.assertEqual((response.status_code, response.headers["Location"]),
                                 (302, "/login?next=/account/password"))

    def test_historical_shared_login_keys_are_not_accounts(self):
        for historical in ({"tester_authenticated": True}, {"admin_authenticated": True, "ui_profile": "admin"}):
            with self.subTest(historical=historical):
                client = self.app.test_client()
                with client.session_transaction() as sess:
                    sess.update(historical)
                self.assertEqual(client.get("/account/password").headers["Location"],
                                 "/login?next=/account/password")
                response = self.change(client=client)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response.headers["Location"])

    def test_change_rekeys_the_account_and_keeps_this_browser_signed_in(self):
        account = self.signed_in("teacher")
        issued_at = self.session()["iat"] - 3600
        with self.client.session_transaction() as sess:
            sess.update({"iat": issued_at, "quiz_attempt_owner:4": True, "ui_profile": "tester"})
        other_device = self.app.test_client()
        set_account_session(other_device, account["id"], account["session_key"])
        self.execute("UPDATE users SET password_changed_at = '2020-01-01 00:00:00' WHERE id = ?", (account["id"],))
        old_hash = self.user(account["id"], "password_hash")

        response = self.change()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Паролата е сменена", self.body(response))

        new_key = self.user(account["id"], "session_key")
        self.assertNotEqual(new_key, account["session_key"])
        self.assertNotEqual(self.user(account["id"], "password_hash"), old_hash)
        self.assertGreater(self.user(account["id"], "password_changed_at"), "2020-01-01 00:00:00")
        state = self.session()
        self.assertEqual(set(state), {"uid", "sk", "iat", "quiz_attempt_owner:4"})
        self.assertEqual((state["uid"], state["sk"]), (account["id"], new_key))
        self.assertEqual(state["iat"], issued_at)  # re-keying does not extend the lifetime
        self.assertEqual(self.client.get("/teacher/new").status_code, 200)
        # Every other session is signed out.
        self.assertEqual(other_device.get("/teacher/new").status_code, 302)
        # Only the new password signs in.
        self.assertEqual(self.login(account["email"], PASSWORD, client=self.app.test_client()).status_code, 200)
        self.assertEqual(self.login(account["email"], NEW_PASSWORD, client=self.app.test_client()).status_code, 302)

    def test_rejections_change_nothing(self):
        account = self.signed_in("teacher")
        before = self.snapshot(account)
        cases = {
            "wrong current": (dict(current="not my password"), "Грешна текуща парола."),
            "empty current": (dict(current=""), "Грешна текуща парола."),
            "mismatch": (dict(confirm=NEW_PASSWORD + "x"), "Новите пароли не съвпадат."),
            "too short": (dict(new="short"), "поне 10 символа"),
            "too long": (dict(new="x" * 257), "най-много 256"),
            "equals email": (dict(new=account["email"].upper()), "не може да съвпада с имейла"),
        }
        for name, (form, message) in cases.items():
            with self.subTest(case=name):
                response = self.change(**form)
                self.assertEqual(response.status_code, 200)
                self.assertIn(message, self.body(response))
                self.assertNotIn("Паролата е сменена", self.body(response))
                self.assertEqual(self.snapshot(account), before)
                self.assertEqual(self.session()["sk"], account["session_key"])

    def test_admin_needs_fourteen_characters_and_others_ten(self):
        admin = self.signed_in("admin")
        before = self.snapshot(admin)
        response = self.change(new="thirteen-char")
        self.assertIn("администратор трябва да е поне 14", self.body(response))
        self.assertEqual(self.snapshot(admin), before)
        self.assertIn("Паролата е сменена", self.body(self.change(new="fourteen-chars")))

        self.client = self.app.test_client()
        self.signed_in("teacher")
        self.assertIn("Паролата е сменена", self.body(self.change(new="thirteen-char")))

    def test_passwords_are_never_echoed(self):
        self.signed_in("teacher")
        for form in (dict(current="wrong-current-value"), dict(confirm="mismatching-confirm-value"), {}):
            html = self.body(self.change(**form))
            for secret in (PASSWORD, NEW_PASSWORD, "wrong-current-value", "mismatching-confirm-value"):
                self.assertNotIn(secret, html)

    def test_wrong_current_password_counts_against_the_login_throttle(self):
        account = self.signed_in("teacher")
        self.change(current="not my password")
        self.change(current="not my password")
        self.assertEqual(self.failures(account["email"]), 2)
        self.change(confirm="mismatch")  # not a credential failure
        self.assertEqual(self.failures(account["email"]), 2)
        # The same counter as the login form.
        self.login(account["email"], "wrong password", client=self.app.test_client())
        self.assertEqual(self.failures(account["email"]), 3)

    def test_blocked_identifier_cannot_change_the_password_and_is_not_hashed(self):
        account = self.signed_in("teacher")
        self.fail_login(account["email"], 10)
        before = self.snapshot(account)
        with patch.object(accounts, "check_password_hash") as verify:
            response = self.change()
        verify.assert_not_called()
        self.assertEqual(response.status_code, 429)
        self.assertIn(THROTTLED, self.body(response))
        self.assertEqual(self.snapshot(account), before)

    def test_successful_change_clears_the_throttle(self):
        account = self.signed_in("teacher")
        self.change(current="not my password")
        self.change()
        self.assertEqual(self.failures(account["email"]), 0)

    def test_account_without_a_password_gets_a_notice_instead_of_the_form(self):
        # An account that signs in only with Google has no password to change. It is
        # not told "wrong current password", and no throttle failure is counted
        # against it for an answer it cannot give.
        account = create_account(self.db_path, "teacher")
        set_account_session(self.client, account["id"], account["session_key"])
        for response in (self.client.get("/account/password"), self.change()):
            self.assertEqual(response.status_code, 200)
            page = self.body(response)
            self.assertIn("Този профил влиза само с Google и няма парола за смяна.", page)
            self.assertNotIn("Грешна текуща парола.", page)
            self.assertNotIn('name="current_password"', page)
        self.assertIsNone(self.user(account["id"], "password_hash"))
        self.assertEqual(self.throttle_rows(), [])
        self.assertEqual(self.session()["sk"], account["session_key"])

    def test_failure_after_the_update_rolls_everything_back(self):
        account = self.signed_in("teacher")
        self.change(current="not my password")  # a committed throttle row to clear
        before = self.snapshot(account)
        with patch.object(login_throttle, "clear", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.change()
        self.assertEqual(self.snapshot(account), before)
        self.assertEqual(self.failures(account["email"]), 1)
        self.assertEqual(self.session()["sk"], account["session_key"])
        self.assertEqual(self.client.get("/teacher/new").status_code, 200)

    def test_cross_origin_post_cannot_change_the_password(self):
        account = self.signed_in("teacher")
        before = self.snapshot(account)
        self.assertEqual(self.change(headers=CROSS_ORIGIN).status_code, 403)
        self.assertEqual(self.change(current="wrong", headers=CROSS_ORIGIN).status_code, 403)
        self.assertEqual(self.snapshot(account), before)
        self.assertEqual(self.failures(account["email"]), 0)
        self.assertEqual(self.session()["sk"], account["session_key"])

    def test_role_cannot_be_changed_through_the_form(self):
        account = self.signed_in("student")
        self.client.post("/account/password", headers=SAME_ORIGIN, data={
            "current_password": PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD,
            "role": "admin", "is_active": "1", "email": "other@example.bg", "display_name": "Друг"})
        self.assertEqual(self.query("SELECT role, email FROM users WHERE id = ?", (account["id"],)),
                         [("student", account["email"])])


# --------------------------------------------------------------------------- templates, cache, queries


class NavigationTest(AccountAppTestCase):
    def context(self, session_state):
        """The template context the app's processors build for a request."""
        with self.app.test_request_context("/"):
            from flask import session

            session.update(session_state)
            self.assertIsNone(self.app.preprocess_request())
            context = {}
            self.app.update_template_context(context)
            return context

    def account_state(self, role, **extra):
        account = create_account(self.db_path, role, display_name=f"Име {role}")
        return {"uid": account["id"], "sk": account["session_key"], "iat": int(time.time()), **extra}

    # Template names of the removed shared-login/profile state. Nothing may inject them.
    REMOVED_CONTEXT_NAMES = (
        "tester_authenticated", "admin_authenticated", "can_generate_tests", "ui_profile",
        "ui_profile_label", "tester_password_configured", "admin_password_configured",
        "legacy_shared_login_enabled",
    )

    def flags(self, context):
        """(is_teacher, is_admin, may list assignments) as templates see them."""
        account = context["current_account"]
        return (bool(account and account.is_teacher), bool(account and account.is_admin),
                context["can_list_assignments"])

    def test_account_roles_drive_the_template_state(self):
        for role, flags, label in (("student", (False, False, False), "Ученик"),
                                   ("teacher", (True, False, True), "Учител"),
                                   ("admin", (True, True, True), "Админ")):
            with self.subTest(role=role):
                context = self.context(self.account_state(role))
                self.assertEqual(self.flags(context), flags)
                self.assertEqual(context["current_account"].role, role)
                self.assertEqual(context["current_account"].display_name, f"Име {role}")
                self.assertEqual(context["current_account_role_label"], label)
                for removed in self.REMOVED_CONTEXT_NAMES:
                    self.assertNotIn(removed, context)

    def test_historical_session_keys_produce_no_template_state(self):
        for state in (
            {},
            {"ui_profile": "admin"},
            {"tester_authenticated": True, "ui_profile": "tester"},
            {"admin_authenticated": True, "ui_profile": "admin"},
            HISTORICAL_SHARED_LOGIN_SESSION,
        ):
            with self.subTest(state=state):
                context = self.context(state)
                self.assertEqual(self.flags(context), (False, False, False))
                self.assertIsNone(context["current_account"])
                self.assertIsNone(context["current_account_role_label"])
                for removed in self.REMOVED_CONTEXT_NAMES:
                    self.assertNotIn(removed, context)

    def test_account_identity_is_unaffected_by_historical_session_keys(self):
        context = self.context(self.account_state("student", **HISTORICAL_SHARED_LOGIN_SESSION))
        self.assertEqual(self.flags(context), (False, False, False))
        self.assertEqual(context["current_account_role_label"], "Ученик")
        context = self.context(self.account_state("teacher", **HISTORICAL_SHARED_LOGIN_SESSION))
        self.assertEqual(self.flags(context), (True, False, True))

    def test_stale_account_identity_renders_as_anonymous_despite_historical_keys(self):
        context = self.context({"uid": 987654, "sk": "stale", "iat": int(time.time()),
                                "admin_authenticated": True})
        self.assertEqual(self.flags(context), (False, False, False))
        self.assertIsNone(context["current_account"])

    def test_account_pages_show_the_name_a_post_logout_and_no_removed_links(self):
        for role, label in (("student", "Ученик"), ("teacher", "Учител"), ("admin", "Админ")):
            with self.subTest(role=role):
                client = self.app.test_client()
                account = create_account(self.db_path, role, display_name=f"Име {role}")
                set_account_session(client, account["id"], account["session_key"])
                html = self.body(client.get("/"))
                self.assertIn(f"Име {role}", html)
                self.assertIn(f'<span class="profile-link active">{label}</span>', html)
                self.assertIn('<form class="profile-logout-form" method="post" action="/logout">', html)
                self.assertIn('href="/account/password"', html)
                self.assertIn('class="nav-links"', html)
                for removed in ("/tester/logout", "/admin/logout", "/tester/login", "/admin/login",
                                "/profile/"):
                    self.assertNotIn(removed, html)
                # The footer names the account's role, never a profile.
                footer = html[html.index('<footer class="site-footer">'):]
                self.assertIn(f"<span>{label}</span>", footer)

    def test_anonymous_and_historical_sessions_get_only_the_account_login_link(self):
        anonymous = self.body(self.client.get("/"))
        with self.client.session_transaction() as sess:
            sess.update(HISTORICAL_SHARED_LOGIN_SESSION)
        historical = self.body(self.client.get("/"))
        # The removed keys change nothing a visitor sees.
        self.assertEqual(historical, anonymous)
        for page in (anonymous, historical):
            self.assertEqual(page.count('href="/login?next=/"'), 2)  # desktop and mobile menus
            self.assertIn('aria-label="Вход"', page)
            for absent in ('action="/logout"', 'class="nav-links"', 'href="/account/password"',
                           "/tester/login", "/admin/login", "/tester/logout", "/admin/logout",
                           "/profile/", "Тестер", "Вход за админ", ">Админ<",
                           'href="/teacher/assignments"', "админ изглед"):
                self.assertNotIn(absent, page)

    def test_display_name_is_escaped(self):
        account = create_account(self.db_path, "student", display_name="<b>Иван</b>")
        set_account_session(self.client, account["id"], account["session_key"])
        html = self.body(self.client.get("/"))
        self.assertNotIn("<b>Иван</b>", html)
        self.assertIn("&lt;b&gt;Иван&lt;/b&gt;", html)


class CacheControlTest(AccountAppTestCase):
    def test_account_responses_are_not_stored(self):
        for role, paths in (("student", ("/", "/account/password", "/no/such/page")),
                            ("teacher", ("/teacher/new", "/teacher")),  # a page and a 403 refusal
                            ("admin", ("/teacher", "/teacher/assignments"))):
            client = self.app.test_client()
            account = create_account(self.db_path, role)
            set_account_session(client, account["id"], account["session_key"])
            for path in paths:
                with self.subTest(role=role, path=path):
                    self.assertEqual(client.get(path).headers.get("Cache-Control"), "no-store")

    def test_logout_response_of_an_account_is_not_stored(self):
        account = self.make("teacher")
        set_account_session(self.client, account["id"], account["session_key"])
        self.assertEqual(self.client.post("/logout", headers=SAME_ORIGIN).headers.get("Cache-Control"),
                         "no-store")

    def test_anonymous_static_and_health_responses_are_unaffected(self):
        static_file = next(Path(ROOT, "web", "static").rglob("*.css")).relative_to(Path(ROOT, "web", "static"))
        anonymous = self.app.test_client()
        for path in ("/", "/login", "/teacher"):
            with self.subTest(path=path):
                self.assertNotIn("Cache-Control", anonymous.get(path).headers)
        historical = self.app.test_client()
        with historical.session_transaction() as sess:
            sess.update(HISTORICAL_SHARED_LOGIN_SESSION)
        self.assertNotIn("Cache-Control", historical.get("/teacher").headers)

        account = create_account(self.db_path, "admin")
        set_account_session(self.client, account["id"], account["session_key"])
        static = self.client.get(f"/static/{static_file.as_posix()}")
        self.assertNotEqual(static.headers.get("Cache-Control"), "no-store")
        static.close()
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_stale_account_session_is_anonymous_for_caching_too(self):
        set_account_session(self.client, 987654, "stale")
        self.assertNotIn("Cache-Control", self.client.get("/").headers)


class AccountQueryBudgetTest(AccountAppTestCase):
    def traced(self):
        statements = []
        connect = web_db.connect_db

        def traced_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        return patch.object(web_db, "connect_db", traced_connect), statements

    @staticmethod
    def users_reads(statements):
        return [sql for sql in statements if "FROM users" in sql]

    def test_anonymous_pages_run_no_users_or_throttle_query(self):
        patcher, statements = self.traced()
        with patcher:
            for path in ("/", "/login"):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual([sql for sql in statements if "users" in sql or "login_throttle" in sql], [])

    def test_account_pages_still_run_exactly_one_users_lookup(self):
        for role, path in (("student", "/"), ("teacher", "/teacher/new"), ("admin", "/teacher"),
                           ("student", "/account/password")):
            with self.subTest(role=role, path=path):
                client = self.app.test_client()
                account = create_account(self.db_path, role)
                set_account_session(client, account["id"], account["session_key"])
                patcher, statements = self.traced()
                with patcher:
                    self.assertEqual(client.get(path).status_code, 200)
                reads = self.users_reads(statements)
                self.assertEqual(len(reads), 1, reads)
                self.assertRegex(reads[0], r"FROM users WHERE id = \d+$")

    def test_login_runs_one_email_lookup_and_bounded_throttle_statements(self):
        account = self.make("teacher")
        patcher, statements = self.traced()
        with patcher:
            self.assertEqual(self.login(account["email"]).status_code, 302)
        self.assertEqual(len(self.users_reads(statements)), 1)
        self.assertEqual(len([sql for sql in statements if "login_throttle" in sql]), 2)  # check + clear

    def test_retiring_takes_the_connection_only_for_a_live_linking_object(self):
        # A sign-in, a password change and a sign-out try to retire linking in
        # progress first. Without a linking object in the session that attempt opens
        # nothing and runs no statement, with Google sign-in enabled as without it.
        provider = google_oidc.provider_config(
            "tests.apps.googleusercontent.com", "synthetic-client-secret", "http://localhost")
        change = {"current_password": PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD}
        for enabled in (False, True):
            with self.subTest(google_enabled=enabled), \
                    patch.dict(self.app.config, {oauth_state.PROVIDER_CONFIG_KEY: provider if enabled else None}), \
                    patch.object(oauth_state, "get_request_db", wraps=oauth_state.get_request_db) as accessor:
                account, client = self.make("teacher"), self.app.test_client()
                patcher, statements = self.traced()
                with patcher:
                    self.assertEqual(self.login(account["email"], client=client).status_code, 302)
                    self.assertEqual(client.post("/account/password", data=change, headers=SAME_ORIGIN).status_code, 200)
                    self.assertEqual(client.post("/logout", headers=SAME_ORIGIN).status_code, 302)
                accessor.assert_not_called()
                self.assertEqual([sql for sql in statements
                                  if "oidc_consumed" in sql or sql.upper().startswith("BEGIN IMMEDIATE")], [])

    def test_failed_login_runs_one_email_lookup(self):
        patcher, statements = self.traced()
        with patcher:
            self.login("ghost9@example.bg", "wrong password")
        self.assertEqual(len(self.users_reads(statements)), 1)
        self.assertNotIn("ghost9", " ".join(sql for sql in statements if "login_throttle" in sql))


# --------------------------------------------------------------------------- structure


def imported_modules(relative):
    tree = ast.parse(Path(ROOT, relative).read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


class AccountBlueprintBoundariesTest(unittest.TestCase):
    ROUTES = {
        "/login": ("account.login", {"GET", "POST"}, access.PUBLIC),
        "/logout": ("account.logout", {"POST"}, access.PUBLIC),
        "/login/totp": ("account.login_totp", {"POST"}, access.PUBLIC),
        "/account": ("account.overview", {"GET"}, access.SIGNED_IN),
        "/account/password": ("account.password", {"GET", "POST"}, access.SIGNED_IN),
        "/account/profile": ("account.profile", {"GET", "POST"}, access.PUBLIC),
    }
    # The routes of Google sign-in (PR #331), named exactly: nothing else may appear.
    GOOGLE_ROUTES = {
        "/auth/google/start": {"POST"},
        "/auth/google/link": {"POST"},
        "/auth/google/callback": {"GET"},
        "/auth/google/link/confirm": {"GET", "POST"},
        "/auth/google/link/cancel": {"POST"},
    }

    def test_blueprint_owns_exactly_the_account_routes(self):
        rules = {r.rule: r for r in web_app.app.url_map.iter_rules() if r.endpoint.startswith("account.")}
        self.assertEqual(set(rules), set(self.ROUTES))
        for url, (endpoint, methods, policy) in self.ROUTES.items():
            with self.subTest(url=url):
                view = web_app.app.view_functions[endpoint]
                self.assertEqual(rules[url].endpoint, endpoint)
                self.assertEqual(rules[url].methods - {"HEAD", "OPTIONS"}, methods)
                self.assertEqual(view.__module__, "web.blueprints.account")
                self.assertEqual(access.policy_for_view(view), policy)
                # A view that accepts an unsafe method declares the same-origin
                # mechanism; the account page is a GET and declares none.
                unsafe = bool(methods & request_integrity.UNSAFE_METHODS)
                self.assertEqual(request_integrity.integrity_policy_for_view(view),
                                 request_integrity.SAME_ORIGIN if unsafe else None)

    def test_google_sign_in_has_exactly_its_five_routes_and_nothing_registers_or_resets(self):
        rules = {rule.rule: rule for rule in web_app.app.url_map.iter_rules()}
        google = {url: rule for url, rule in rules.items() if "google" in url or "oauth" in url or "oidc" in url
                  or "callback" in url or url.startswith("/auth")}
        self.assertEqual(set(google), set(self.GOOGLE_ROUTES))
        for url, methods in self.GOOGLE_ROUTES.items():
            with self.subTest(url=url):
                self.assertEqual(google[url].methods - {"HEAD", "OPTIONS"}, methods)
                self.assertTrue(google[url].endpoint.startswith("oidc."))
        # Registration (PR 4) is the registration blueprint's, under /register only.
        # Elsewhere still no registration, no password reset and no email verification.
        registration = {url for url in rules if url == "/register" or url.startswith("/register/")}
        self.assertEqual({rules[url].endpoint.split(".")[0] for url in registration}, {"registration"})
        # Password recovery (scope 8.3) is its own blueprint, under /recover only, and signs no one in.
        recovery = {url for url in rules if url == "/recover" or url.startswith("/recover/")}
        self.assertEqual({rules[url].endpoint.split(".")[0] for url in recovery}, {"recovery"})
        paths = " ".join(url for url in rules if url not in registration | recovery)
        for fragment in ("register", "signup", "sign-up", "reset", "forgot", "verify", "unlink", "microsoft", "apple"):
            self.assertNotIn(fragment, paths)
        # The admin second factor has exactly one route, and no web enrolment exists.
        self.assertEqual([rule.rule for rule in web_app.app.url_map.iter_rules() if "totp" in rule.rule],
                         ["/login/totp"])

    def test_blueprint_imports_neither_web_app_nor_another_controller(self):
        imported = imported_modules("web/blueprints/account.py")
        self.assertNotIn("web.app", imported)
        self.assertFalse({name for name in imported if name.startswith("web.blueprints")})

    def test_request_integrity_stays_account_free(self):
        relative = "web/request_integrity.py"
        source = Path(ROOT, relative).read_text(encoding="utf-8")
        self.assertFalse({name for name in imported_modules(relative)
                          if "accounts" in name or "login_throttle" in name})
        for needle in ("sqlite3", "get_request_db", "login_throttle"):
            self.assertNotIn(needle, source)

    def test_password_sign_in_keeps_its_routes_beside_google_sign_in(self):
        self.assertFalse(Path(ROOT, "web/blueprints/auth.py").exists())
        self.assertNotIn("auth", web_app.app.blueprints)
        login_rules = sorted(rule.rule for rule in web_app.app.url_map.iter_rules() if "login" in rule.rule)
        self.assertEqual(login_rules, ["/login", "/login/totp"])

    def test_services_stay_independent_of_flask(self):
        for relative in ("web/services/accounts.py", "web/services/login_throttle.py"):
            with self.subTest(module=relative):
                self.assertFalse({name for name in imported_modules(relative)
                                  if name.split(".")[0] == "flask" or name.startswith("web.")})

    def test_hook_order_is_integrity_then_identity_then_authorization(self):
        hooks = [func.__name__ for func in web_app.app.before_request_funcs[None]]
        self.assertLess(hooks.index("protect_same_origin_unsafe_methods"), hooks.index("load_current_user"))
        self.assertLess(hooks.index("load_current_user"), hooks.index("enforce_access_policy"))
        # The OAuth callback's own integrity check is the one addition, between the two.
        self.assertEqual(hooks, ["protect_same_origin_unsafe_methods", "verify_oauth_state", "load_current_user",
                                 "enforce_access_policy"])

    def test_there_is_no_remember_me(self):
        for relative in ("web/blueprints/account.py", "web/app.py", "web/templates/login.html"):
            with self.subTest(module=relative):
                source = Path(ROOT, relative).read_text(encoding="utf-8")
                self.assertNotIn("session.permanent", source)
                self.assertNotIn("remember", source.lower())


if __name__ == "__main__":
    unittest.main()

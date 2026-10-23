"""A HEAD to the account forms and to the Google-link confirmation is a read, whatever it carries.

Flask routes HEAD to the GET/POST handlers of ``/login``, ``/account/profile``,
``/account/password`` and ``/auth/google/link/confirm``. They once branched on
``request.method == "GET"``, so a HEAD with a form body reached the POST action: a
cross-origin HEAD to ``/login`` recorded throttle failures and signed a browser in with
valid credentials. Same-origin protection and the proxy's rate-limit keys cover unsafe
methods only, so nothing else stood in the way (the fix for the registration forms is
``tests/test_registration_head.py``).

These tests send HEAD requests with real bodies and require the contents of every table
unchanged, the session unchanged, no password verified or hashed, no throttle write and no
form action reached; that the HEAD answers exactly as the GET of the same request does, with
an empty body; that a cross-origin POST is still refused; and that the legitimate POST that
follows still works. A stale account identity is dropped from the cookie by
``load_current_user`` on any request; that cleanup is not a form action and is compared with
the GET's, not claimed absent.

Every database is temporary (migrations plus canonical data); Google is the in-process fake.
"""

import hashlib
import sqlite3
import tempfile
from contextlib import ExitStack, closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from tests.auth_helpers import create_account, set_account_session
from tests.google_oidc_support import provider
from tests.support import build_test_database, web_app
from tests.test_account_login import (
    CROSS_ORIGIN, NEW_PASSWORD, PASSWORD, SAME_ORIGIN, AccountAppTestCase)
from tests.test_account_profile import STUDENT_FORM, TEACHER_FORM, ProfileRouteTestCase
from tests.test_google_oidc_routes import RoutesTestCase
from web import account_session, oauth_state
from web.blueprints import account as account_blueprint
from web.services import accounts, login_throttle, oidc_accounts, profiles
from web.services import registration_forms as forms

WRONG_ORIGIN_REFERER = {"Referer": "http://evil.example/login"}
COMPARED_HEADERS = ("Location", "Content-Type", "Content-Length", "Cache-Control", "Referrer-Policy")


def database_digest(db_path) -> str:
    """A digest of the contents of every table of the database."""
    sha = hashlib.sha256()
    with closing(sqlite3.connect(db_path)) as conn:
        tables = [name for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for table in tables:
            for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY 1'):
                sha.update(repr((table, tuple(row))).encode("utf-8"))
    return sha.hexdigest()


class HeadAssertions:
    """Shared by the four route families; ``guarded`` names what a HEAD must never call."""

    guarded = ()

    def head(self, path, data=None, *, client=None, headers=SAME_ORIGIN):
        return (client or self.client).open(path, method="HEAD", data=data, headers=headers)

    def guard(self):
        """Spies that record and pass through; every one must stay uncalled across a HEAD."""
        stack = ExitStack()
        self.addCleanup(stack.close)
        return {f"{owner.__name__}.{name}": stack.enter_context(
            patch.object(owner, name, wraps=getattr(owner, name))) for owner, name in self.guarded}

    def state(self, client=None):
        return database_digest(self.db_path), self.session(client)

    def assertInert(self, response, calls, before, client=None):
        """Nothing was called, written or put into the session, and no cookie was set."""
        for name, spy in calls.items():
            self.assertEqual(spy.call_count, 0, name)
        self.assertEqual(self.state(client), before)
        self.assertEqual(response.headers.getlist("Set-Cookie"), [])

    def assertAsGet(self, response, path, data=None, *, client=None, headers=SAME_ORIGIN):
        """The HEAD answered what the GET of the same request answers, without a body."""
        got = (client or self.client).open(path, method="GET", data=data, headers=headers)
        self.assertEqual(response.get_data(), b"")
        self.assertEqual(response.status_code, got.status_code)
        for name in COMPARED_HEADERS:
            self.assertEqual(response.headers.get(name), got.headers.get(name), name)
        self.assertEqual(response.headers.getlist("Set-Cookie"), got.headers.getlist("Set-Cookie"))
        return got


class FreshDatabaseCase(HeadAssertions, AccountAppTestCase):
    """A database of this test's own, restored with the application state afterwards."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="account-head-")
        self.addCleanup(directory.cleanup)
        self.db_path = str(build_test_database(Path(directory.name) / "head.db"))
        self.app.config["DB_PATH"] = self.db_path
        web_app.DB_PATH = web_app.QUIZ_DB_PATH = Path(self.db_path)  # restored by preserved_app_state
        super().setUp()


# --------------------------------------------------------------------------- /login


class LoginHeadTest(FreshDatabaseCase):
    guarded = (
        (accounts, "authenticate"), (accounts, "check_password_hash"), (accounts, "generate_password_hash"),
        (accounts, "record_login"), (login_throttle, "is_blocked"), (login_throttle, "record_failure"),
        (login_throttle, "clear"), (account_blueprint, "issue_account_session"),
        (account_blueprint, "issue_pending_second_factor"),
    )

    def credentials(self, account, password=PASSWORD):
        return {"email": account["email"], "password": password}

    def test_valid_and_invalid_credentials_sign_nobody_in_and_count_nothing(self):
        for role in ("student", "teacher", "admin"):
            account = self.make(role)
            for label, password in (("valid", PASSWORD), ("invalid", "not the password")):
                for headers in (SAME_ORIGIN, CROSS_ORIGIN, WRONG_ORIGIN_REFERER):
                    with self.subTest(role=role, credentials=label, headers=headers):
                        client = self.app.test_client()
                        calls, before = self.guard(), self.state(client)
                        body = self.credentials(account, password)
                        response = self.head("/login", body, client=client, headers=headers)
                        self.assertEqual(response.status_code, 200)
                        self.assertInert(response, calls, before, client)
                        self.assertEqual(self.session(client), {})
                        self.assertAsGet(response, "/login", body, client=client, headers=headers)
            self.assertEqual(self.throttle_rows(), [])
            self.assertIsNone(self.user(account["id"], "last_login_at"))

    def test_existing_throttle_state_is_neither_counted_nor_cleared(self):
        account = self.make("teacher")
        for failures in (3, 10):  # counting, then blocked
            with self.subTest(failures=failures):
                self.execute("DELETE FROM login_throttle")
                self.fail_login(account["email"], failures)
                rows = self.throttle_rows()
                for password in (PASSWORD, "not the password"):
                    client = self.app.test_client()
                    calls, before = self.guard(), self.state(client)
                    body = self.credentials(account, password)
                    response = self.head("/login", body, client=client)
                    # The read answers the form, as the GET does; only the POST is refused with 429.
                    self.assertEqual(response.status_code, 200)
                    self.assertInert(response, calls, before, client)
                    self.assertAsGet(response, "/login", body, client=client)
                self.assertEqual(self.throttle_rows(), rows)
                self.assertEqual(self.failures(account["email"]), failures)
        self.assertEqual(self.login(account["email"]).status_code, 429)

    def test_a_signed_in_account_is_sent_on_and_is_not_replaced(self):
        signed_in, other = self.make("teacher"), self.make("student")
        set_account_session(self.client, signed_in["id"], signed_in["session_key"])
        for body in (self.credentials(other), self.credentials(other, "wrong"), {}):
            with self.subTest(body=sorted(body)):
                calls, before = self.guard(), self.state()
                response = self.head("/login?next=/account", body)
                self.assertEqual((response.status_code, response.headers["Location"]), (302, "/account"))
                self.assertInert(response, calls, before)
                self.assertEqual(self.session()["uid"], signed_in["id"])
                self.assertAsGet(response, "/login?next=/account", body)
        self.assertEqual(self.throttle_rows(), [])

    def test_a_pending_second_factor_is_neither_reissued_nor_replaced(self):
        admin, teacher = self.make("admin"), self.make("teacher")
        self.assertEqual(self.login(admin["email"]).status_code, 200)   # the password step
        pending = self.session()
        self.assertEqual(set(pending) & set(account_blueprint.PENDING_SECOND_FACTOR_KEYS),
                         set(account_blueprint.PENDING_SECOND_FACTOR_KEYS))
        self.now += timedelta(seconds=40)   # a reissue would carry a new mfa_iat
        for body in (self.credentials(admin), self.credentials(teacher), self.credentials(admin, "wrong")):
            with self.subTest(email=body["email"], password=body["password"] == PASSWORD):
                calls, before = self.guard(), self.state()
                response = self.head("/login", body)
                self.assertEqual(response.status_code, 200)
                self.assertInert(response, calls, before)
                self.assertEqual(self.session(), pending)
                self.assertAsGet(response, "/login", body)
        # And a fresh browser's HEAD with the admin's password starts no second factor.
        client = self.app.test_client()
        calls, before = self.guard(), self.state(client)
        self.assertInert(self.head("/login", self.credentials(admin), client=client), calls, before, client)
        self.assertEqual(self.session(client), {})
        # The pending sign-in is still the one the password step issued, and still completes.
        self.assertEqual(self.submit_code(self.code(admin)).status_code, 302)
        self.assertEqual(self.session()["uid"], admin["id"])

    def test_an_unrelated_stale_identity_is_cleaned_as_by_the_get_and_nothing_is_issued(self):
        account = self.make("teacher")
        for method in ("HEAD", "GET"):
            with self.subTest(method=method):
                client = self.app.test_client()
                with client.session_transaction() as sess:
                    sess.update({"uid": 987654, "sk": "stale", "iat": 1})
                calls, before = self.guard(), database_digest(self.db_path)
                response = client.open("/login", method=method, data=self.credentials(account),
                                       headers=SAME_ORIGIN)
                self.assertEqual(response.status_code, 200)
                for name, spy in calls.items():
                    self.assertEqual(spy.call_count, 0, name)
                self.assertEqual(database_digest(self.db_path), before)
                # The stale keys are dropped by the account loader; no account session is issued.
                self.assertEqual(self.session(client), {})

    def test_cross_origin_post_is_refused_and_same_origin_post_signs_in(self):
        account = self.make("teacher")
        for headers in (CROSS_ORIGIN, WRONG_ORIGIN_REFERER):
            with self.subTest(headers=headers):
                self.assertEqual(self.login(account["email"], headers=headers).status_code, 403)
        self.assertEqual((self.session(), self.throttle_rows()), ({}, []))
        self.assertEqual(self.login(account["email"], "wrong").status_code, 200)
        self.assertEqual(self.failures(account["email"]), 1)
        response = self.login(account["email"])
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/teacher/new"))
        self.assertEqual(self.session()["uid"], account["id"])
        self.assertEqual(self.failures(account["email"]), 0)
        self.assertIsNotNone(self.user(account["id"], "last_login_at"))


# --------------------------------------------------------------------------- /account/password


class PasswordHeadTest(FreshDatabaseCase):
    guarded = (
        (accounts, "verify_password"), (accounts, "check_password_hash"), (accounts, "generate_password_hash"),
        (accounts, "set_password"), (accounts, "password_problem"), (login_throttle, "is_blocked"),
        (login_throttle, "record_failure"), (login_throttle, "clear"),
        (account_blueprint, "issue_account_session"), (account_session, "issue_account_session"),
    )

    def signed_in(self, role="teacher", client=None, **options):
        account = self.make(role, **options)
        set_account_session(client or self.client, account["id"], account["session_key"])
        return account

    def form(self, current=PASSWORD, new=NEW_PASSWORD):
        return {"current_password": current, "new_password": new, "confirm_password": new}

    def credentials_of(self, account):
        return self.query("SELECT password_hash, session_key, password_changed_at FROM users WHERE id = ?",
                          (account["id"],))[0]

    def test_correct_and_incorrect_current_passwords_change_nothing(self):
        for role in ("student", "teacher", "admin"):
            new = NEW_PASSWORD + " and more" if role == "admin" else NEW_PASSWORD
            client = self.app.test_client()
            account = self.signed_in(role, client)
            other_device = self.app.test_client()
            set_account_session(other_device, account["id"], account["session_key"])
            stored = self.credentials_of(account)
            for label, current in (("correct", PASSWORD), ("incorrect", "not the password")):
                for headers in (SAME_ORIGIN, CROSS_ORIGIN):
                    with self.subTest(role=role, current=label, headers=headers):
                        calls, before = self.guard(), self.state(client)
                        body = self.form(current, new)
                        response = self.head("/account/password", body, client=client, headers=headers)
                        self.assertEqual(response.status_code, 200)
                        self.assertInert(response, calls, before, client)
                        self.assertAsGet(response, "/account/password", body, client=client, headers=headers)
            # Not re-keyed, not replaced: this browser and every other device stay signed in.
            self.assertEqual(self.credentials_of(account), stored)
            self.assertEqual(self.session(client)["sk"], account["session_key"])
            self.assertEqual(other_device.get("/account").status_code, 200)
        self.assertEqual(self.throttle_rows(), [])

    def test_existing_throttle_state_is_neither_counted_nor_cleared(self):
        account = self.signed_in("teacher")
        for failures in (3, 10):
            with self.subTest(failures=failures):
                self.execute("DELETE FROM login_throttle")
                self.fail_login(account["email"], failures)
                rows = self.throttle_rows()
                for current in (PASSWORD, "not the password"):
                    calls, before = self.guard(), self.state()
                    response = self.head("/account/password", self.form(current))
                    self.assertEqual(response.status_code, 200)
                    self.assertInert(response, calls, before)
                self.assertEqual(self.throttle_rows(), rows)
        self.assertEqual(self.client.post("/account/password", data=self.form(), headers=SAME_ORIGIN).status_code,
                         429)

    def test_a_passwordless_account_gets_the_same_notice_and_nothing_else(self):
        account = create_account(self.db_path, "teacher")
        set_account_session(self.client, account["id"], account["session_key"])
        calls, before = self.guard(), self.state()
        response = self.head("/account/password", self.form())
        self.assertEqual(response.status_code, 200)
        self.assertInert(response, calls, before)
        self.assertAsGet(response, "/account/password", self.form())
        self.assertIsNone(self.user(account["id"], "password_hash"))
        self.assertEqual(self.throttle_rows(), [])

    def test_anonymous_head_is_sent_to_the_login_as_the_get_is(self):
        client = self.app.test_client()
        calls, before = self.guard(), self.state(client)
        response = self.head("/account/password", self.form(), client=client)
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/login?next=/account/password"))
        self.assertInert(response, calls, before, client)
        self.assertAsGet(response, "/account/password", self.form(), client=client)

    def test_cross_origin_post_is_refused_and_same_origin_post_changes_the_password(self):
        account = self.signed_in("teacher")
        stored = self.credentials_of(account)
        for headers in (CROSS_ORIGIN, WRONG_ORIGIN_REFERER):
            with self.subTest(headers=headers):
                response = self.client.post("/account/password", data=self.form(), headers=headers)
                self.assertEqual(response.status_code, 403)
        self.assertEqual((self.credentials_of(account), self.throttle_rows()), (stored, []))
        self.assertEqual(self.client.post("/account/password", data=self.form("wrong"),
                                          headers=SAME_ORIGIN).status_code, 200)
        self.assertEqual(self.failures(account["email"]), 1)
        changed = self.client.post("/account/password", data=self.form(), headers=SAME_ORIGIN)
        self.assertIn("Паролата е сменена", self.body(changed))
        new_key = self.user(account["id"], "session_key")
        self.assertNotEqual(new_key, account["session_key"])
        self.assertEqual(self.session()["sk"], new_key)
        self.assertEqual(self.failures(account["email"]), 0)


# --------------------------------------------------------------------------- /account/profile


class ProfileHeadTest(HeadAssertions, ProfileRouteTestCase):
    guarded = (
        (forms, "validate_profile"), (profiles, "complete_student_profile"), (profiles, "save_profile"),
        (profiles, "create_profile"),
    )

    def profile_state(self, account):
        return (self.user(account["id"], "display_name"), self.user(account["id"], "profile_required_at"),
                self.profile_rows())

    def test_valid_bodies_write_nothing_for_every_role_and_profile_state(self):
        cases = (
            ("student", None, False, STUDENT_FORM),
            ("student", None, True, STUDENT_FORM),
            ("student", "student", False, STUDENT_FORM),
            ("teacher", "teacher", False, TEACHER_FORM),
            ("teacher", None, False, STUDENT_FORM),     # a POST is 403 here
            ("admin", None, False, TEACHER_FORM),       # a POST is 403 here
        )
        for role, stored, marked, body in cases:
            with self.subTest(role=role, profile=stored, marked=marked):
                account, client = self.signed_in(role, marked=marked, profile=stored)
                before_profile = self.profile_state(account)
                for headers in (SAME_ORIGIN, CROSS_ORIGIN):
                    path = "/account/profile?next=/account"
                    calls, before = self.guard(), self.state(client)
                    response = self.head(path, {**body, "next": "/account"}, client=client, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    self.assertInert(response, calls, before, client)
                    self.assertAsGet(response, path, {**body, "next": "/account"}, client=client, headers=headers)
                self.assertEqual(self.profile_state(account), before_profile)
                with closing(sqlite3.connect(self.db_path)) as conn:
                    self.assertEqual(profiles.needs_profile(conn, {"id": account["id"]}, enabled=True),
                                     marked and stored is None)

    def test_the_profile_requirement_is_still_completed_by_the_post(self):
        account, client = self.signed_in("student", marked=True)
        calls, before = self.guard(), self.state(client)
        self.assertInert(self.head("/account/profile", STUDENT_FORM, client=client), calls, before, client)
        self.assertEqual(self.query("SELECT COUNT(*) FROM user_profiles"), [(0,)])
        response = self.post(client, STUDENT_FORM)
        self.assertEqual((response.status_code, response.headers["Location"]), (303, "/account/profile?saved=1"))
        self.assertEqual(self.user(account["id"], "display_name"), "Мария Петрова")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertFalse(profiles.needs_profile(conn, {"id": account["id"]}, enabled=True))

    def test_invalid_bodies_are_the_same_read(self):
        _, client = self.signed_in("student", marked=True)
        for body in ({"first_name": "", "grade": "99"}, {"role": "admin", "account_type": "teacher"}, {}):
            with self.subTest(body=sorted(body)):
                calls, before = self.guard(), self.state(client)
                response = self.head("/account/profile", body, client=client)
                self.assertEqual(response.status_code, 200)
                self.assertInert(response, calls, before, client)
                self.assertAsGet(response, "/account/profile", body, client=client)

    def test_the_saved_notice_is_read_as_by_the_get(self):
        # The saved flag is display only; HEAD now takes the GET's read, so its headers match.
        _, client = self.signed_in("student", profile="student")
        calls, before = self.guard(), self.state(client)
        response = self.head("/account/profile?saved=1", STUDENT_FORM, client=client)
        self.assertInert(response, calls, before, client)
        got = self.assertAsGet(response, "/account/profile?saved=1", STUDENT_FORM, client=client)
        self.assertIn("Профилът е запазен.", self.text(got))

    def test_gate_and_access_states_answer_as_the_get(self):
        anonymous = self.app.test_client()
        calls, before = self.guard(), self.state(anonymous)
        response = self.head("/account/profile", STUDENT_FORM, client=anonymous)
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/login?next=/account/profile"))
        self.assertInert(response, calls, before, anonymous)
        self.assertAsGet(response, "/account/profile", STUDENT_FORM, client=anonymous)

        account, client = self.signed_in("student", marked=True)
        self.execute("UPDATE users SET is_active = 0 WHERE id = ?", (account["id"],))
        before = database_digest(self.db_path)
        response = self.head("/account/profile", STUDENT_FORM, client=client)
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/login?next=/account/profile"))
        self.assertEqual(database_digest(self.db_path), before)
        self.assertEqual(self.session(client), {})     # the account loader drops the disabled identity
        self.execute("UPDATE users SET is_active = 1 WHERE id = ?", (account["id"],))

        _, client = self.signed_in("student", marked=True)
        with patch.dict(self.app.config, {profiles.CONFIG_KEY: False}):
            for headers in (SAME_ORIGIN, CROSS_ORIGIN):
                calls, before = self.guard(), self.state(client)
                response = self.head("/account/profile", STUDENT_FORM, client=client, headers=headers)
                self.assertEqual(response.status_code, 404)
                self.assertInert(response, calls, before, client)
                self.assertAsGet(response, "/account/profile", STUDENT_FORM, client=client, headers=headers)
            self.assertEqual(self.post(client, STUDENT_FORM, headers=CROSS_ORIGIN).status_code, 403)
        self.assertEqual(self.query("SELECT COUNT(*) FROM user_profiles"), [(0,)])

    def test_cross_origin_post_is_refused_and_the_same_origin_post_saves(self):
        account, client = self.signed_in("student", profile="student")
        before = self.profile_state(account)
        for headers in (CROSS_ORIGIN, WRONG_ORIGIN_REFERER):
            with self.subTest(headers=headers):
                self.assertEqual(self.post(client, STUDENT_FORM, headers=headers).status_code, 403)
        self.assertEqual(self.profile_state(account), before)
        self.assertEqual(self.post(client, STUDENT_FORM).status_code, 303)
        self.assertEqual(self.query("SELECT grade FROM user_profiles WHERE user_id = ?", (account["id"],)), [(10,)])


# --------------------------------------------------------------------------- /auth/google/link/confirm


class LinkConfirmHeadTest(HeadAssertions, RoutesTestCase):
    guarded = (
        (accounts, "verify_password"), (accounts, "check_password_hash"), (login_throttle, "is_blocked"),
        (login_throttle, "record_failure"), (login_throttle, "clear"), (oidc_accounts, "confirm_link"),
        (oauth_state, "begin_write"), (oauth_state, "drop_pending_link"),
    )
    PATH = "/auth/google/link/confirm"

    def state(self, client=None):
        # The pending link lives in the session; the cookie itself must not be rewritten either.
        return super().state(client) + (self.cookie(client),)

    def pending(self, role="teacher"):
        account = self.signed_in(role)
        self.link_until_pending(subject=f"{role}s-google", email=f"{role}.g@example.bg")
        return account

    def test_a_valid_pending_link_is_shown_and_kept_whatever_the_password(self):
        for role in ("student", "teacher"):
            with self.subTest(role=role):
                self.client = self.app.test_client()
                account = self.pending(role)
                self.confirm(password="a first wrong one")      # an existing throttle failure
                self.assertEqual(self.failures(account["email"]), 1)
                slot, rows = self.session()[oauth_state.LINK_SLOT], self.consumption_rows()
                for password in (PASSWORD, "not the password", "x" * 300):
                    for headers in (SAME_ORIGIN, CROSS_ORIGIN, WRONG_ORIGIN_REFERER):
                        calls, before = self.guard(), self.state()
                        body = {"current_password": password}
                        response = self.head(self.PATH, body, headers=headers)
                        self.assertEqual(response.status_code, 200)
                        self.assertInert(response, calls, before)
                        self.assertAsGet(response, self.PATH, body, headers=headers)
                self.assertEqual(self.session()[oauth_state.LINK_SLOT], slot)
                self.assertEqual((self.identity_rows(account["id"]), self.consumption_rows()), ([], rows))
                self.assertEqual(self.failures(account["email"]), 1)
                # The legitimate POST still links with the same pending link.
                self.assert_redirect(self.confirm(), "/account?google=linked")
                self.assertEqual(self.identity_rows(account["id"]),
                                 [(account["id"], "https://accounts.google.com", f"{role}s-google", "google",
                                   f"{role}.g@example.bg")])
                self.assertEqual(self.failures(account["email"]), 0)
                self.assertNotIn(oauth_state.LINK_SLOT, self.session())

    def test_a_blocked_counter_is_a_read_too(self):
        account = self.pending()
        self.fail_login(account["email"], 10)
        rows = self.throttle_rows()
        calls, before = self.guard(), self.state()
        response = self.head(self.PATH, {"current_password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertInert(response, calls, before)
        self.assertEqual(self.throttle_rows(), rows)
        self.assertEqual(self.confirm().status_code, 429)

    def test_a_missing_binding_redirects_and_none_is_issued(self):
        self.signed_in("teacher")
        calls, before = self.guard(), self.state()
        response = self.head(self.PATH, {"current_password": PASSWORD})
        self.assert_redirect(response, "/account?google=expired")
        self.assertInert(response, calls, before)
        self.assertNotIn(oauth_state.LINK_SLOT, self.session())     # absent before and after
        self.assertAsGet(response, self.PATH, {"current_password": PASSWORD})

    def test_a_malformed_foreign_or_expired_binding_is_left_exactly_as_it_was(self):
        # The GET leaves an unusable slot alone and only the POST drops it; the HEAD must not drop it.
        account = self.pending()
        live, start = self.session()[oauth_state.LINK_SLOT], self.clock
        # Another run's link: well-formed, bound to this session and unexpired, so its
        # instance is the only reason it is refused.
        foreign = {**live, "inst": ("1" if live["inst"][0] == "0" else "0") + live["inst"][1:]}
        self.assertRegex(foreign["inst"], r"^[0-9a-f]{32}\Z")
        self.assertNotEqual(foreign["inst"], oauth_state.INSTANCE)
        self.assertEqual({**foreign, "inst": oauth_state.INSTANCE}, live)
        self.assertTrue(oauth_state.is_pending_link(foreign))
        self.assertLess(start, foreign["exp"])
        cases = (
            ("malformed", {**live, "extra": 1}, start),
            ("other instance", foreign, start),
            ("expired", live, live["exp"]),
        )
        for label, slot, clock in cases:
            with self.subTest(binding=label):
                with self.client.session_transaction() as sess:
                    sess[oauth_state.LINK_SLOT] = slot
                self.clock = clock
                calls, before = self.guard(), self.state()
                response = self.head(self.PATH, {"current_password": PASSWORD})
                self.assert_redirect(response, "/account?google=expired")
                self.assertInert(response, calls, before)
                self.assertEqual(self.session()[oauth_state.LINK_SLOT], slot)    # unchanged, not dropped
                self.assertAsGet(response, self.PATH, {"current_password": PASSWORD})
                self.assertEqual(self.session()[oauth_state.LINK_SLOT], slot)    # nor by the GET
                # The POST is what drops an unusable slot.
                self.assert_redirect(self.confirm(), "/account?google=expired")
                self.assertNotIn(oauth_state.LINK_SLOT, self.session())
            self.clock = start
        self.assertEqual((self.identity_rows(), self.throttle_rows()), ([], []))
        self.assertEqual(self.failures(account["email"]), 0)

    def test_role_and_provider_states_answer_as_the_get_and_keep_the_link(self):
        teacher = self.pending()
        slot = self.session()[oauth_state.LINK_SLOT]
        # Promoted to admin while the link is pending: the GET redirects; only a POST is refused (403).
        self.execute("UPDATE users SET role = 'admin' WHERE id = ?", (teacher["id"],))
        calls, before = self.guard(), self.state()
        response = self.head(self.PATH, {"current_password": PASSWORD})
        self.assert_redirect(response, "/account?google=expired")
        self.assertInert(response, calls, before)
        self.assertAsGet(response, self.PATH, {"current_password": PASSWORD})
        self.assertEqual(self.confirm().status_code, 403)
        self.assertEqual(self.session()[oauth_state.LINK_SLOT], slot)
        self.execute("UPDATE users SET role = 'teacher' WHERE id = ?", (teacher["id"],))
        # Provider disabled: the generic 404, the link stays for when it is back.
        with patch.dict(self.app.config, {oauth_state.PROVIDER_CONFIG_KEY: None}):
            calls, before = self.guard(), self.state()
            response = self.head(self.PATH, {"current_password": PASSWORD})
            self.assertEqual(response.status_code, 404)
            self.assertInert(response, calls, before)
            self.assertAsGet(response, self.PATH, {"current_password": PASSWORD})
        self.assertEqual(self.app.config[oauth_state.PROVIDER_CONFIG_KEY], provider())
        # Anonymous: the login redirect, as the GET.
        anonymous = self.app.test_client()
        calls, before = self.guard(), self.state(anonymous)
        response = self.head(self.PATH, {"current_password": PASSWORD}, client=anonymous)
        self.assert_redirect(response, "/login?next=/auth/google/link/confirm")
        self.assertInert(response, calls, before, anonymous)
        self.assertEqual(self.identity_rows(), [])
        self.assert_redirect(self.confirm(), "/account?google=linked")

    def test_cross_origin_post_is_refused_and_keeps_the_link(self):
        account = self.pending()
        slot = self.session()[oauth_state.LINK_SLOT]
        for headers in (CROSS_ORIGIN, WRONG_ORIGIN_REFERER):
            with self.subTest(headers=headers):
                self.assertEqual(self.confirm(headers=headers).status_code, 403)
        self.assertEqual((self.session()[oauth_state.LINK_SLOT], self.identity_rows()), (slot, []))
        self.assertEqual(self.failures(account["email"]), 0)
        self.assert_redirect(self.confirm(), "/account?google=linked")

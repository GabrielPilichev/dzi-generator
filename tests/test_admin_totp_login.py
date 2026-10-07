"""Admin TOTP (Accounts PR T) — the two-step admin sign-in.

A correct admin password issues only the pending second factor (``mfa_uid``,
``mfa_sk``, ``mfa_iat``), which no principal loader reads; ``POST /login/totp`` turns it
into the normal ``uid``/``sk``/``iat`` session. Teacher and student sign-in are
unchanged. Time is injected through the blueprint clock; nothing sleeps. Every secret,
code and key is synthetic and generated per run.
"""

import logging
import sqlite3
import time
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

import pyotp
from cryptography.fernet import Fernet

from tests.auth_helpers import create_account
from tests.test_account_login import (
    CROSS_ORIGIN,
    INVALID,
    PASSWORD,
    SAME_ORIGIN,
    THROTTLED,
    TOTP_KEY,
    AccountAppTestCase,
)
from tests.support import web_app
from web import production
from web.access import ANONYMOUS, account_principal
from web.blueprints import account as account_blueprint
from web.blueprints import teacher as teacher_blueprint
from web.services import accounts, login_throttle, quiz, totp

PENDING_KEYS = {"mfa_uid", "mfa_sk", "mfa_iat"}
ACCOUNT_KEYS = {"uid", "sk", "iat"}
CODE_INVALID = "Грешен или вече използван код."
CODE_EXPIRED = "Времето за въвеждане на кода изтече. Влез отново."
CODE_UNAVAILABLE = "Двуфакторното удостоверяване временно не е достъпно. Опитай по-късно."
NOT_ENROLLED = "Профилът изисква двуфакторно удостоверяване, което още не е настроено."
ADMIN_PATHS = ("/teacher", "/teacher/dzi-training", "/dzi")
# assignment_owner routes (Accounts PR7/PR8) deny to the account login.
ASSIGNMENT_PATHS = ("/teacher/assignments", "/teacher/practical-submissions")


class AdminTotpTestCase(AccountAppTestCase):
    def start(self, account, *, client=None, **form):
        """The password step of an admin on a fresh TOTP step; returns the TOTP page."""
        self.now += timedelta(seconds=31)
        page = self.login(account["email"], client=client, **form)
        self.assertEqual(page.status_code, 200, self.body(page))
        self.assertIn('action="/login/totp"', self.body(page))
        return page

    def other_code(self, code):
        return str((int(code) + 1) % 10**6).zfill(6)

    def assert_anonymous(self, client=None):
        client = client or self.client
        for path in (*ADMIN_PATHS, *ASSIGNMENT_PATHS, "/account/password", "/teacher/new"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], f"/login?next={path}")

    def snapshot(self, account):
        return self.query(
            "SELECT u.session_key, u.last_login_at, t.last_used_step, t.last_verified_at "
            "FROM users u LEFT JOIN totp_credentials t ON t.user_id = u.id WHERE u.id = ?",
            (account["id"],))[0]


class PasswordStepTest(AdminTotpTestCase):
    def test_teacher_and_student_sign_in_is_unchanged(self):
        for role, home in (("teacher", "/teacher/new"), ("student", "/")):
            with self.subTest(role=role):
                account, client = self.make(role), self.app.test_client()
                response = self.login(account["email"], client=client)
                self.assertEqual((response.status_code, response.headers["Location"]), (302, home))
                self.assertEqual(set(self.session(client)), ACCOUNT_KEYS)

    def test_admin_password_alone_grants_no_authority(self):
        admin = self.make("admin")
        with self.client.session_transaction() as sess:
            sess.update({"admin_authenticated": True, "tester_authenticated": True, "ui_profile": "admin",
                         "quiz_attempt_owner:5": True})
        self.start(admin)
        state = self.session()
        self.assertEqual(set(state), PENDING_KEYS | {"quiz_attempt_owner:5"})
        self.assertEqual((state["mfa_uid"], state["mfa_sk"]), (admin["id"], admin["session_key"]))
        self.assertEqual(state["mfa_iat"], int(self.now.timestamp()))
        principal, after = self.principal_for(state)
        self.assertEqual(principal, ANONYMOUS)
        self.assertEqual(after, state)  # the loader neither reads nor rewrites pending keys
        self.assert_anonymous()
        self.assertIsNone(self.user(admin["id"], "last_login_at"))

    def test_correct_password_does_not_reset_the_throttle(self):
        admin = self.make("admin")
        self.fail_login(admin["email"], 3)
        self.start(admin)
        self.assertEqual(self.failures(admin["email"]), 3)

    def test_pending_state_alone_never_becomes_a_principal(self):
        admin = self.make("admin")
        teacher = self.make("teacher")
        now = int(time.time())
        for state in ({"mfa_uid": admin["id"], "mfa_sk": admin["session_key"], "mfa_iat": now},
                      {"mfa_uid": teacher["id"], "mfa_sk": teacher["session_key"], "mfa_iat": now},
                      {"mfa_uid": admin["id"], "mfa_sk": admin["session_key"], "mfa_iat": now,
                       "ui_profile": "admin"}):
            with self.subTest(state=sorted(state)):
                principal, _ = self.principal_for(state)
                self.assertEqual(principal, ANONYMOUS)

    def test_admin_without_confirmed_totp_cannot_sign_in(self):
        unenrolled = self.make("admin", totp=False)
        unconfirmed = self.make("admin", totp=False)
        with closing(sqlite3.connect(self.db_path)) as conn:
            totp.enroll(conn, unconfirmed["id"], TOTP_KEY, unconfirmed["email"])
            conn.commit()
        for account in (unenrolled, unconfirmed):
            with self.subTest(account=account["id"]):
                client = self.app.test_client()
                with client.session_transaction() as sess:
                    sess["quiz_attempt_owner:2"] = True
                response = self.login(account["email"], client=client)
                self.assertEqual(response.status_code, 403)
                body = self.body(response)
                self.assertIn(NOT_ENROLLED, body)
                self.assertNotIn('action="/login/totp"', body)
                self.assertEqual(set(self.session(client)), {"quiz_attempt_owner:2"})
                self.assert_anonymous(client)
                self.assertIsNone(self.user(account["id"], "last_login_at"))

    def test_not_enrolled_message_needs_the_password(self):
        admin = self.make("admin", totp=False)
        response = self.login(admin["email"], "wrong password")
        self.assertEqual(response.status_code, 200)
        self.assertIn(INVALID, self.body(response))
        self.assertNotIn(NOT_ENROLLED, self.body(response))

    def test_disabled_admin_gets_the_generic_failure(self):
        admin = self.make("admin", is_active=False)
        response = self.login(admin["email"])
        self.assertIn(INVALID, self.body(response))
        self.assertEqual(set(self.session()), set())


class SecondFactorTest(AdminTotpTestCase):
    def test_correct_code_issues_the_normal_session(self):
        admin = self.make("admin")
        with self.client.session_transaction() as sess:
            sess["quiz_attempt_owner:9"] = True
        self.start(admin)
        self.fail_login(admin["email"], 2)  # earlier failures are forgotten on success
        before = int(time.time())
        response = self.submit_code(self.code(admin))
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/teacher"))
        state = self.session()
        self.assertEqual(set(state), ACCOUNT_KEYS | {"quiz_attempt_owner:9"})
        self.assertEqual((state["uid"], state["sk"]), (admin["id"], admin["session_key"]))
        self.assertTrue(before <= state["iat"] <= int(time.time()))
        for leaked in ("admin", "role", "is_admin", "admin_authenticated", "ui_profile"):
            self.assertNotIn(leaked, repr(state))
        principal, _ = self.principal_for(state)
        self.assertEqual((principal.user_id, principal.role, principal.is_admin), (admin["id"], "admin", True))
        for path in ADMIN_PATHS + ASSIGNMENT_PATHS:
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertIsNotNone(self.user(admin["id"], "last_login_at"))
        self.assertEqual(self.failures(admin["email"]), 0)

    def test_wrong_malformed_and_missing_codes_fail_and_keep_the_challenge(self):
        admin = self.make("admin")
        self.start(admin)
        good = self.code(admin)
        cases = [self.other_code(good), "", "12345", "1234567", "abcdef", "12 34 5x", "١٢٣٤٥٦",
                 "1" * 5000, good + "\x00", "2222-2222-2222"]
        for code in cases + [None]:
            with self.subTest(code=None if code is None else code[:20]):
                self.execute("DELETE FROM login_throttle")
                if code is None:  # no code field at all
                    response = self.client.post("/login/totp", data={}, headers=SAME_ORIGIN)
                else:
                    response = self.submit_code(code)
                self.assertEqual(response.status_code, 200)
                self.assertIn(CODE_INVALID, self.body(response))
                self.assertEqual(set(self.session()), PENDING_KEYS)
                self.assertEqual(self.failures(admin["email"]), 1)  # every failed code counts
        self.assert_anonymous()
        self.assertIsNone(self.snapshot(admin)[3])  # no second factor was completed

    def test_window_is_the_current_step_plus_minus_one(self):
        for offset, accepted in ((-2, False), (-1, True), (0, True), (1, True), (2, False)):
            with self.subTest(offset=offset):
                admin = self.make("admin")
                client = self.app.test_client()
                self.start(admin, client=client)
                code = self.code(admin, self.now + offset * timedelta(seconds=30))
                response = self.submit_code(code, client=client)
                self.assertEqual(response.status_code, 302 if accepted else 200)
                self.assertEqual("uid" in self.session(client), accepted)

    def test_a_used_step_is_refused(self):
        admin = self.make("admin")
        self.start(admin)
        code = self.code(admin)
        self.assertEqual(self.submit_code(code).status_code, 302)
        client = self.app.test_client()
        self.now += timedelta(seconds=5)  # still inside the same step
        self.assertEqual(self.login(admin["email"], client=client).status_code, 200)
        response = self.submit_code(code, client=client)
        self.assertIn(CODE_INVALID, self.body(response))
        self.assertNotIn("uid", self.session(client))

    def test_pending_challenge_expires_after_five_minutes(self):
        admin = self.make("admin")
        self.start(admin)
        self.now += timedelta(seconds=totp.PENDING_LIFETIME_SECONDS)
        response = self.submit_code(self.code(admin))
        self.assertEqual(response.status_code, 200)
        self.assertIn(CODE_EXPIRED, self.body(response))
        self.assertIn('action="/login"', self.body(response))
        self.assertEqual(set(self.session()), set())

    def test_pending_challenge_just_inside_five_minutes_works(self):
        admin = self.make("admin")
        self.start(admin)
        self.now += timedelta(seconds=totp.PENDING_LIFETIME_SECONDS - 1)
        self.assertEqual(self.submit_code(self.code(admin)).status_code, 302)

    def test_malformed_or_manipulated_pending_state_fails_closed(self):
        admin, other = self.make("admin"), self.make("admin")
        teacher = self.make("teacher")
        now = int(self.now.timestamp())
        cases = {
            "nothing": {},
            "no iat": {"mfa_uid": admin["id"], "mfa_sk": admin["session_key"]},
            "string iat": {"mfa_uid": admin["id"], "mfa_sk": admin["session_key"], "mfa_iat": str(now)},
            "bool iat": {"mfa_uid": admin["id"], "mfa_sk": admin["session_key"], "mfa_iat": True},
            "future iat": {"mfa_uid": admin["id"], "mfa_sk": admin["session_key"], "mfa_iat": now + 60},
            "wrong key": {"mfa_uid": admin["id"], "mfa_sk": "x" * 43, "mfa_iat": now},
            "other admin's id": {"mfa_uid": other["id"], "mfa_sk": admin["session_key"], "mfa_iat": now},
            "string uid": {"mfa_uid": str(admin["id"]), "mfa_sk": admin["session_key"], "mfa_iat": now},
            "teacher": {"mfa_uid": teacher["id"], "mfa_sk": teacher["session_key"], "mfa_iat": now},
            "unknown id": {"mfa_uid": 10**9, "mfa_sk": admin["session_key"], "mfa_iat": now},
        }
        for name, state in cases.items():
            with self.subTest(case=name):
                client = self.app.test_client()
                with client.session_transaction() as sess:
                    sess.update(state)
                response = self.submit_code(self.code(admin), client=client)
                self.assertIn(CODE_EXPIRED, self.body(response))
                self.assertFalse((PENDING_KEYS | ACCOUNT_KEYS) & set(self.session(client)))
        self.assertIsNone(self.user(admin["id"], "last_login_at"))

    def test_replayed_pending_state_is_spent_after_completion(self):
        admin = self.make("admin")
        self.start(admin)
        captured = self.session()
        self.assertEqual(self.submit_code(self.code(admin)).status_code, 302)
        thief = self.app.test_client()
        with thief.session_transaction() as sess:
            sess.update(captured)
        self.now += timedelta(seconds=31)  # a new, valid code
        response = self.submit_code(self.code(admin), client=thief)
        self.assertIn(CODE_EXPIRED, self.body(response))
        self.assertNotIn("uid", self.session(thief))

    def test_account_changes_during_the_challenge_void_it(self):
        changes = {
            "disabled": lambda conn, uid: accounts.set_active(conn, uid, False),
            "disabled without re-keying": lambda conn, uid: conn.execute(
                "UPDATE users SET is_active = 0 WHERE id = ?", (uid,)),
            "password changed": lambda conn, uid: accounts.set_password(conn, uid, "another long passphrase"),
            "demoted": lambda conn, uid: conn.execute("UPDATE users SET role = 'teacher' WHERE id = ?", (uid,)),
            "signed out everywhere": accounts.rotate_session_key,
            "totp reset": totp.reset,
            "totp unconfirmed": lambda conn, uid: conn.execute(
                "UPDATE totp_credentials SET confirmed_at = NULL WHERE user_id = ?", (uid,)),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                admin = self.make("admin")
                client = self.app.test_client()
                self.start(admin, client=client)
                with closing(sqlite3.connect(self.db_path)) as conn:
                    change(conn, admin["id"])
                    conn.commit()
                response = self.submit_code(self.code(admin), client=client)
                self.assertIn(CODE_EXPIRED, self.body(response))
                self.assertFalse((PENDING_KEYS | ACCOUNT_KEYS) & set(self.session(client)))

    def test_recovery_code_completes_once(self):
        admin = self.make("admin")
        code = admin["recovery_codes"][0]
        self.start(admin)
        response = self.submit_code(code.upper())
        self.assertEqual((response.status_code, response.headers["Location"]), (302, "/teacher"))
        self.assertEqual(set(self.session()), ACCOUNT_KEYS)
        client = self.app.test_client()
        self.start(admin, client=client)
        response = self.submit_code(code, client=client)
        self.assertIn(CODE_INVALID, self.body(response))
        self.assertNotIn("uid", self.session(client))
        self.assertEqual(self.submit_code(admin["recovery_codes"][1].replace("-", ""), client=client).status_code,
                         302)

    def test_regenerated_recovery_codes_void_the_old_ones(self):
        admin = self.make("admin")
        with closing(sqlite3.connect(self.db_path)) as conn:
            new_codes = totp.regenerate_recovery_codes(conn, admin["id"])
            conn.commit()
        self.start(admin)
        self.assertIn(CODE_INVALID, self.body(self.submit_code(admin["recovery_codes"][0])))
        self.assertEqual(self.submit_code(new_codes[0]).status_code, 302)

    def test_reset_ends_existing_sessions_and_old_factors(self):
        admin = self.make("admin")
        self.start(admin)
        self.assertEqual(self.submit_code(self.code(admin)).status_code, 302)
        self.assertEqual(self.client.get("/teacher").status_code, 200)
        with closing(sqlite3.connect(self.db_path)) as conn:
            totp.reset(conn, admin["id"])
            conn.commit()
        self.assert_anonymous()
        self.assertEqual(self.session(), {})
        self.now += timedelta(seconds=31)
        response = self.login(admin["email"])
        self.assertEqual(response.status_code, 403)
        self.assertIn(NOT_ENROLLED, self.body(response))


class SecondFactorSafetyTest(AdminTotpTestCase):
    def test_codes_share_the_login_throttle(self):
        admin = self.make("admin")
        self.fail_login(admin["email"], login_throttle.MAX_FAILURES - 1)
        self.start(admin)
        response = self.submit_code(self.other_code(self.code(admin)))
        self.assertIn(CODE_INVALID, self.body(response))
        response = self.submit_code(self.code(admin))  # correct, but the account is now blocked
        self.assertEqual(response.status_code, 429)
        self.assertIn(THROTTLED, self.body(response))
        self.assertNotIn("uid", self.session())
        self.assertEqual(self.login(admin["email"]).status_code, 429)  # and so is the password form

    def test_code_guessing_alone_is_limited(self):
        admin = self.make("admin")
        self.start(admin)
        wrong = self.other_code(self.code(admin))
        for _ in range(login_throttle.MAX_FAILURES):
            self.assertEqual(self.submit_code(wrong).status_code, 200)
        self.assertEqual(self.submit_code(self.code(admin)).status_code, 429)
        self.now += login_throttle.BLOCK_DURATION + timedelta(seconds=1)
        self.start(admin)
        self.assertEqual(self.submit_code(self.code(admin)).status_code, 302)

    def test_restarting_the_password_step_does_not_reset_code_guesses(self):
        admin = self.make("admin")
        for _ in range(login_throttle.MAX_FAILURES):
            self.start(admin)
            self.submit_code("000000" if self.code(admin) != "000000" else "111111")
        self.assertEqual(self.login(admin["email"]).status_code, 429)

    def test_missing_or_wrong_key_fails_closed(self):
        for name, key in (("unset", None), ("malformed", "not-a-key"), ("other key", Fernet.generate_key())):
            with self.subTest(key=name):
                admin = self.make("admin")
                client = self.app.test_client()
                self.start(admin, client=client)
                with patch.dict(self.app.config, {"TOTP_ENCRYPTION_KEY": key}), \
                        self.assertLogs("web.blueprints.account", logging.ERROR) as logs:
                    response = self.submit_code(self.code(admin), client=client)
                self.assertEqual(response.status_code, 503)
                self.assertIn(CODE_UNAVAILABLE, self.body(response))
                self.assertNotIn("uid", self.session(client))
                self.assertEqual(self.failures(admin["email"]), 0)
                logged = "\n".join(logs.output)
                for secret in (admin["totp_secret"], self.code(admin), TOTP_KEY.decode(), str(key)):
                    if secret != "None":
                        self.assertNotIn(secret, logged)

    def test_recovery_codes_do_not_need_the_key_and_each_use_is_logged(self):
        admin = self.make("admin")
        code = admin["recovery_codes"][0]
        self.start(admin)
        with patch.dict(self.app.config, {"TOTP_ENCRYPTION_KEY": None}), \
                self.assertLogs("web.blueprints.account", logging.WARNING) as logs:
            self.assertEqual(self.submit_code(code).status_code, 302)
        logged = "\n".join(logs.output)
        self.assertIn(f"Admin account {admin['id']} signed in with a recovery code", logged)
        self.assertIn(f"{totp.RECOVERY_CODE_COUNT - 1} unused recovery codes remain", logged)
        self.assertIn("DZI_TOTP_KEY is unavailable", logged)
        for secret in (code, code.replace("-", ""), admin["totp_secret"]):
            self.assertNotIn(secret, logged)

    def test_recovery_code_use_with_a_working_key_is_logged_without_a_key_warning(self):
        admin = self.make("admin")
        self.start(admin)
        with self.assertLogs("web.blueprints.account", logging.WARNING) as logs:
            self.assertEqual(self.submit_code(admin["recovery_codes"][0]).status_code, 302)
        self.assertNotIn("DZI_TOTP_KEY", "\n".join(logs.output))

    def test_totp_sign_in_logs_nothing(self):
        admin = self.make("admin")
        self.start(admin)
        with self.assertNoLogs("web.blueprints.account", logging.DEBUG):
            self.assertEqual(self.submit_code(self.code(admin)).status_code, 302)

    def test_cross_origin_posts_are_refused_before_any_state_change(self):
        admin = self.make("admin")
        self.start(admin)
        pending = self.session()
        before = self.snapshot(admin)
        for headers in (CROSS_ORIGIN, {"Referer": "http://evil.example/login"}):
            with self.subTest(headers=headers):
                response = self.submit_code(self.code(admin), headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.session(), pending)
                self.assertEqual(self.snapshot(admin), before)
        self.assertEqual(self.client.get("/login/totp").status_code, 405)

    def test_header_less_posts_are_refused_outside_testing(self):
        admin = self.make("admin")
        self.start(admin)
        with patch.dict(self.app.config, {"TESTING": False}):
            response = self.client.post("/login/totp", data={"code": self.code(admin)})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("uid", self.session())

    def test_redirect_targets_stay_confined(self):
        admin = self.make("admin")
        for target in ("https://evil.example/", "//evil.example/", "/\\evil.example", "javascript:alert(1)"):
            with self.subTest(next=target):
                client = self.app.test_client()
                page = self.start(admin, client=client, next=target)
                self.assertIn('name="next" value=""', self.body(page))
                response = self.submit_code(self.code(admin), client=client, next=target)
                self.assertEqual(response.headers["Location"], "/teacher")
        client = self.app.test_client()
        self.start(admin, client=client, next="/dzi")
        response = self.submit_code(self.code(admin), client=client, next="/dzi")
        self.assertEqual(response.headers["Location"], "/dzi")

    def test_logout_clears_pending_and_completed_sessions(self):
        admin = self.make("admin")
        self.start(admin)
        self.assertEqual(self.client.post("/logout", headers=SAME_ORIGIN).status_code, 302)
        self.assertEqual(self.session(), {})
        self.assertIn(CODE_EXPIRED, self.body(self.submit_code(self.code(admin))))
        self.start(admin)
        self.assertEqual(self.submit_code(self.code(admin)).status_code, 302)
        self.client.post("/logout", headers=SAME_ORIGIN)
        self.assertEqual(self.session(), {})
        self.assert_anonymous()

    def test_absolute_session_lifetime_still_applies_after_mfa(self):
        admin = self.make("admin")
        self.start(admin)
        self.submit_code(self.code(admin))
        with self.client.session_transaction() as sess:
            sess["iat"] = int(time.time()) - accounts.SESSION_LIFETIME_SECONDS["admin"] - 60
        self.assert_anonymous()
        self.assertFalse(ACCOUNT_KEYS & set(self.session()))

    def test_no_secret_or_code_reaches_a_response_or_the_cookie(self):
        admin = self.make("admin")
        ciphertext = self.query("SELECT secret_ciphertext FROM totp_credentials WHERE user_id = ?",
                                (admin["id"],))[0][0]
        secrets_ = [admin["totp_secret"], ciphertext, TOTP_KEY.decode(), *admin["recovery_codes"]]
        page = self.start(admin)
        wrong = self.submit_code(self.other_code(self.code(admin)))
        code = self.code(admin)
        done = self.submit_code(code)
        for response in (page, wrong, done):
            text = self.body(response) + repr(dict(response.headers))
            for secret in secrets_ + [code, PASSWORD]:
                self.assertNotIn(secret, text)
        self.assertNotIn(code, repr(self.session()))

    def test_historical_shared_login_keys_never_help_a_pending_admin(self):
        # What the removed shared admin login once wrote into the cookie.
        historical = {"admin_authenticated": True, "tester_authenticated": True, "ui_profile": "admin"}
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess.update(historical)
        self.assert_anonymous(client)
        # The password step replaces the whole session with the pending challenge.
        admin = self.make("admin")
        self.start(admin, client=client)
        self.assertEqual(set(self.session(client)), PENDING_KEYS)
        self.assert_anonymous(client)
        # Put back beside the pending challenge they still grant nothing, and a wrong
        # code does not change that.
        with client.session_transaction() as sess:
            sess.update(historical)
        self.assertEqual(self.principal_for(self.session(client))[0], ANONYMOUS)
        self.assert_anonymous(client)
        self.assertEqual(self.submit_code(self.other_code(self.code(admin)), client=client).status_code, 200)
        self.assert_anonymous(client)
        # Only the second factor creates the admin principal, and it drops the stray keys.
        self.assertEqual(self.submit_code(self.code(admin), client=client).status_code, 302)
        self.assertEqual(set(self.session(client)), ACCOUNT_KEYS)
        self.assertEqual(client.get("/teacher").status_code, 200)

    def test_pending_challenge_runs_no_account_lookup_on_other_pages(self):
        admin = self.make("admin")
        self.start(admin)
        with patch.object(accounts, "load_session_user", side_effect=AssertionError("looked up")):
            self.assertEqual(self.client.get("/").status_code, 200)


class AssignmentOwnershipInteractionTest(AdminTotpTestCase):
    """PR6 ownership decisions read only the principal, so a pending admin is no one."""

    def owned_assignment(self, teacher):
        with closing(web_app.quiz_db()) as conn:
            section_id = next(row["id"] for row in conn.execute("SELECT id FROM curriculum_sections ORDER BY id")
                              if quiz.quiz_section_question_ids(conn, row["id"]))
            assignment_id = quiz.quiz_create_assignment(
                conn, principal=account_principal(teacher["id"], "teacher", "Учител"),
                section_id=section_id, title_bg="Тест", question_count=1)
            conn.commit()
        return assignment_id, section_id

    def test_pending_admin_cannot_reach_or_create_assignments_until_mfa(self):
        teacher = self.make("teacher")
        assignment_id, section_id = self.owned_assignment(teacher)
        self.assertEqual(self.query("SELECT owner_user_id FROM quiz_assignments WHERE id = ?",
                                    (assignment_id,))[0][0], teacher["id"])
        admin = self.make("admin")
        self.start(admin)
        detail = f"/teacher/assignment/{assignment_id}"
        response = self.client.get(detail)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/login?next={detail}")
        before = self.query("SELECT COUNT(*) FROM quiz_assignments")[0][0]
        with patch.object(teacher_blueprint, "quiz_write_assignment_note"):
            response = self.client.post("/teacher/new", data={"section_id": section_id, "question_count": 1},
                                        headers=SAME_ORIGIN)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login?next=/teacher/new")
        self.assertEqual(self.query("SELECT COUNT(*) FROM quiz_assignments")[0][0], before)
        # After the second factor the admin sees every assignment, as PR6 defines.
        self.assertEqual(self.submit_code(self.code(admin)).status_code, 302)
        self.assertEqual(self.client.get(detail).status_code, 200)

    def test_account_teacher_ownership_is_unaffected(self):
        teacher, other = self.make("teacher"), self.make("teacher")
        assignment_id, _ = self.owned_assignment(teacher)
        owner_client, other_client = self.app.test_client(), self.app.test_client()
        self.login(teacher["email"], client=owner_client)
        self.login(other["email"], client=other_client)
        self.assertEqual(owner_client.get(f"/teacher/assignment/{assignment_id}").status_code, 200)
        self.assertEqual(other_client.get(f"/teacher/assignment/{assignment_id}").status_code, 404)


class TotpConfigurationTest(AdminTotpTestCase):
    def test_app_reads_the_key_from_the_environment_without_requiring_it(self):
        self.assertIn("TOTP_ENCRYPTION_KEY", self.app.config)
        self.assertEqual(account_blueprint.TOTP_KEY_CONFIG, "TOTP_ENCRYPTION_KEY")

    def test_production_accepts_an_unset_key_and_rejects_a_malformed_one(self):
        base = {"DZI_SECRET_KEY": "s" * 40}
        unset = production.production_config_errors(base)
        self.assertFalse([error for error in unset if "DZI_TOTP_KEY" in error])
        good = production.production_config_errors({**base, "DZI_TOTP_KEY": Fernet.generate_key().decode()})
        self.assertFalse([error for error in good if "DZI_TOTP_KEY" in error])
        bad_value = "definitely-not-a-fernet-key"
        bad = production.production_config_errors({**base, "DZI_TOTP_KEY": bad_value})
        self.assertEqual([error for error in bad if "DZI_TOTP_KEY" in error],
                         ["DZI_TOTP_KEY must be a Fernet key (32 url-safe base64-encoded bytes)"])
        self.assertNotIn(bad_value, "\n".join(bad))

    def test_unenrolled_admin_needs_no_key_to_be_refused(self):
        admin = self.make("admin", totp=False)
        with patch.dict(self.app.config, {"TOTP_ENCRYPTION_KEY": None}):
            self.assertEqual(self.login(admin["email"]).status_code, 403)

    def test_account_created_by_the_helper_without_totp_has_no_credential(self):
        account = create_account(self.db_path, "admin")
        self.assertEqual(self.query("SELECT COUNT(*) FROM totp_credentials WHERE user_id = ?",
                                    (account["id"],))[0][0], 0)

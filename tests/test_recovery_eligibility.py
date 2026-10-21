"""Password recovery (scope 8.2): eligibility at issuance and at redemption (RE1–RE11) and
session-key rotation (RK1–RK3), with operator changes made through the real CLI
(``src.manage_accounts``) wherever the test plan names a command.

Every RE case: a code issued while eligible, a change committed, then the correct code.
Expected: ``INVALIDATED``, the row void and scrubbed, and the account (password, role, key,
TOTP state, every column) exactly as the change left it; no session exists to check, since
the service issues none.
"""

import io
import os
import sqlite3
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pyotp
from werkzeug.security import generate_password_hash

from src import manage_accounts
from tests.recovery_service_support import (
    EMAIL, KEY, NEW_PASSWORD, OLD_PASSWORD, TOTP_KEY, RecoveryServiceTestCase, code_of,
)
from web.services import accounts, login_throttle, oidc_accounts, profiles, recovery, registration, totp
from web.services.mail import InMemoryMailer

Outcome = recovery.Outcome


class EligibilityTestCase(RecoveryServiceTestCase):
    def cli(self, *args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        env = {"DZI_SECRET_KEY": KEY, "DZI_TOTP_KEY": TOTP_KEY}
        with mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
            if stdin is not None:
                with mock.patch("sys.stdin", io.StringIO(stdin)):
                    status = manage_accounts.main(["--db", str(self.path), *args])
            else:
                status = manage_accounts.main(["--db", str(self.path), *args])
        self.assertEqual(status, 0, out.getvalue() + err.getvalue())
        return out.getvalue()

    def totp_state(self, user_id):
        return (self.table_rows("totp_credentials", "user_id = ?", user_id),
                self.table_rows("totp_recovery_codes", "user_id = ?", user_id))

    def enrol_totp(self, user_id, email):
        """Enrol and confirm through the TOTP service the CLI calls; returns recovery codes."""
        uri = self.tx(lambda conn: totp.enroll(conn, user_id, TOTP_KEY, email))
        secret = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)["secret"][0]
        code = pyotp.TOTP(secret).at(self.wall)
        return self.tx(lambda conn: totp.confirm(conn, user_id, TOTP_KEY, code, self.wall))

    def assertInvalidated(self, issued, user_id=None, code=None):
        account = self.user(user_id) if user_id is not None else None
        totp_rows = self.totp_state(user_id) if user_id is not None else None
        throttle = self.table_rows("login_throttle")
        result = self.redeem(issued, code or code_of(issued))
        self.assertIs(result.outcome, Outcome.INVALIDATED)
        self.assertIsNone(result.user_id)
        self.assertScrubbed(issued.recovery_id, "void")
        if user_id is not None:
            self.assertEqual(self.user(user_id), account)
            self.assertEqual(self.totp_state(user_id), totp_rows)
        self.assertEqual(self.table_rows("login_throttle"), throttle)
        # Single use: the same code redeems nothing afterwards either.
        self.assertIs(self.redeem(issued, code or code_of(issued)).outcome, Outcome.NOT_PENDING)


class RedemptionRecheckTest(EligibilityTestCase):
    def test_re1_promotion_to_admin_by_the_cli(self):
        user_id = self.verified()
        issued = self.request()
        self.cli("set-role", "--email", EMAIL, "--role", "admin")
        self.assertInvalidated(issued, user_id)
        self.assertEqual(self.user(user_id)["role"], "admin")
        self.assertTrue(self.authenticates(EMAIL, OLD_PASSWORD))

    def test_re2_role_change_between_two_eligible_roles(self):
        for start, end in (("teacher", "student"), ("student", "teacher")):
            with self.subTest(start=start):
                email = f"{start}@example.bg"
                user_id = self.verified(email, role=start)
                issued = self.request(email)
                self.cli("set-role", "--id", str(user_id), "--role", end)
                self.assertInvalidated(issued, user_id)

    def test_re3_disable_and_disable_then_enable(self):
        user_id = self.verified()
        issued = self.request()
        self.cli("disable", "--email", EMAIL)
        self.assertInvalidated(issued, user_id)
        self.cli("enable", "--id", str(user_id))
        self.wall += timedelta(seconds=1)
        issued = self.request()
        self.assertEqual(issued.kind, "code")
        self.cli("disable", "--id", str(user_id))
        self.cli("enable", "--id", str(user_id))
        self.assertInvalidated(issued, user_id)

    def test_re4_users_email_changed_by_direct_sql(self):
        # No API changes an address; this pins the recheck, not a supported path.
        user_id = self.verified()
        issued = self.request()
        self.tx(lambda conn: conn.execute("UPDATE users SET email = 'moved@example.bg' WHERE id = ?", (user_id,)))
        self.assertInvalidated(issued, user_id)

    def test_re5_password_removed_by_direct_sql(self):
        user_id = self.verified()
        issued = self.request()
        self.tx(lambda conn: conn.execute("UPDATE users SET password_hash = NULL WHERE id = ?", (user_id,)))
        self.assertInvalidated(issued, user_id)

    def test_re6_verification_cleared_by_direct_sql(self):
        user_id = self.verified()
        issued = self.request()
        self.tx(lambda conn: conn.execute("UPDATE users SET email_verified_at = NULL WHERE id = ?", (user_id,)))
        self.assertInvalidated(issued, user_id)

    def test_re7_the_row_replaced_by_another_account_with_the_same_address(self):
        old_id = self.verified()
        issued = self.request()
        editor = sqlite3.connect(self.path)
        editor.execute("PRAGMA foreign_keys = OFF")
        editor.execute("DELETE FROM users WHERE id = ?", (old_id,))
        editor.commit()
        editor.close()
        new_id = self.verified()
        self.assertNotEqual(new_id, old_id)
        self.assertInvalidated(issued, new_id)

    def test_re8_no_forced_match_on_a_shadow_redeems_anything(self):
        admin = self.cli_account("admin@example.bg", role="admin", password="admin password 12345")
        for email, user_id in (("unknown@example.bg", None), ("admin@example.bg", admin)):
            with self.subTest(email=email):
                issued = self.request(email)
                self.assertEqual(issued.kind, "notice")
                self.assertNotRegex(issued.message.text, r"\d")
                self.tx(lambda conn: conn.execute(
                    "UPDATE password_recoveries SET code_hash = ? WHERE id = ?",
                    (recovery._code_hash(KEY, issued.recovery_id, b"12345678"), issued.recovery_id)))
                self.assertInvalidated(issued, user_id, code="12345678")
        self.assertTrue(self.authenticates("admin@example.bg", "admin password 12345"))

    def test_re10_a_hand_edited_address_with_verification_left_set_is_eligible(self):
        """Unsupported operator path, documented (design 1.3): an edit of ``users.email``
        that leaves ``email_verified_at`` set makes the new address eligible, which is why
        the edit must clear it in the same statement."""
        user_id = self.verified()
        self.tx(lambda conn: conn.execute("UPDATE users SET email = 'edited@example.bg' WHERE id = ?", (user_id,)))
        self.assertEqual(self.request("edited@example.bg").kind, "code")
        self.tx(lambda conn: conn.execute(
            "UPDATE users SET email = 'cleared@example.bg', email_verified_at = NULL WHERE id = ?", (user_id,)))
        self.assertEqual(self.request("cleared@example.bg").kind, "notice")

    def test_a_linked_google_identity_does_not_change_eligibility(self):
        from web.services import identities
        user_id = self.verified()
        identities.add_identity(self.conn, user_id, issuer="https://accounts.google.com", subject="sub-2",
                                provider="google", email_at_link="other@example.bg")
        self.conn.commit()
        linked = self.table_rows("user_identities")
        issued = self.request()
        self.assertEqual(issued.kind, "code")
        self.assertIs(self.redeem(issued, code_of(issued)).outcome, Outcome.REDEEMED)
        self.assertEqual(self.table_rows("user_identities"), linked)
        self.assertEqual(self.request("other@example.bg").kind, "notice")


class DemotedAdminTest(EligibilityTestCase):
    def test_re11a_registered_student_promoted_enrolled_and_demoted_recovers_with_totp_untouched(self):
        user_id = self.verified()
        self.cli("set-role", "--email", EMAIL, "--role", "admin")
        self.cli("set-password", "--email", EMAIL, "--password-stdin", stdin="admin password 12345\n")
        self.enrol_totp(user_id, EMAIL)
        self.cli("set-role", "--email", EMAIL, "--role", "teacher")
        state = self.totp_state(user_id)
        self.assertTrue(state[0] and state[1])
        issued = self.request()
        self.assertEqual(issued.kind, "code")
        with self.statements() as seen:
            self.assertIs(self.redeem(issued, code_of(issued)).outcome, Outcome.REDEEMED)
        self.assertFalse([sql for sql in seen if "totp_" in sql])
        self.assertEqual(self.totp_state(user_id), state)
        self.assertTrue(self.authenticates(EMAIL, NEW_PASSWORD))
        # Unchanged sign-in rule: a teacher is asked no second factor; an admin is again.
        self.assertEqual(self.user(user_id)["role"], "teacher")
        self.cli("set-role", "--email", EMAIL, "--role", "admin")
        self.assertTrue(totp.has_confirmed_credential(self.conn, user_id))

    def test_re11b_cli_admin_demoted_with_totp_is_never_eligible(self):
        user_id = self.cli_account("admin@example.bg", role="admin", password="admin password 12345")
        self.enrol_totp(user_id, "admin@example.bg")
        self.cli("set-role", "--email", "admin@example.bg", "--role", "teacher")
        state = self.totp_state(user_id)
        issued = self.request("admin@example.bg")
        self.assertEqual(issued.kind, "notice")
        self.assertEqual(self.totp_state(user_id), state)

    def test_re11c_promotion_between_issuance_and_redemption(self):
        user_id = self.verified()
        self.cli("set-role", "--email", EMAIL, "--role", "admin")
        self.cli("set-password", "--email", EMAIL, "--password-stdin", stdin="admin password 12345\n")
        self.enrol_totp(user_id, EMAIL)
        self.cli("set-role", "--email", EMAIL, "--role", "student")
        issued = self.request()
        self.assertEqual(issued.kind, "code")
        self.cli("set-role", "--email", EMAIL, "--role", "admin")
        self.assertInvalidated(issued, user_id)

    def test_rv8_no_recovery_function_touches_a_totp_table(self):
        self.assertNotRegex(Path(recovery.__file__).read_text(encoding="utf-8"), r"totp")
        user_id = self.verified()
        with self.statements() as seen:
            issued = self.request()
            self.dispatch(InMemoryMailer(), issued)
            self.redeem(issued, "00000000")
            self.redeem(issued, code_of(issued))
            self.cancel(issued)
        self.assertFalse([sql for sql in seen if "totp_" in sql])
        self.assertIsNotNone(user_id)


class SessionKeyRotationTest(EligibilityTestCase):
    def test_rk1_each_rotation_source_invalidates_a_live_code(self):
        sources = {
            "cli set-password": lambda uid, e: self.cli("set-password", "--email", e, "--password-stdin",
                                                        stdin="operator password 1\n"),
            "/account/password (its service calls)": lambda uid, e: self.tx(lambda conn: (
                accounts.set_password(conn, uid, "owner password 123"),
                login_throttle.clear(conn, self.throttle_key(e)))),
            "cli set-role": lambda uid, e: self.cli("set-role", "--id", str(uid), "--role", "teacher"),
            "cli disable/enable": lambda uid, e: (self.cli("disable", "--id", str(uid)),
                                                  self.cli("enable", "--id", str(uid))),
            "cli sign-out": lambda uid, e: self.cli("sign-out", "--email", e),
            "totp enroll": lambda uid, e: self.tx(lambda conn: totp.enroll(conn, uid, TOTP_KEY, e)),
            "totp confirm": lambda uid, e: self.enrol_totp(uid, e),
            "cli totp-reset": lambda uid, e: (self.enrol_totp(uid, e), self.cli("totp-reset", "--email", e)),
        }
        for index, (name, rotate) in enumerate(sources.items()):
            with self.subTest(name):
                email = f"rotation{index}@example.bg"
                user_id = self.verified(email)
                if name == "totp confirm":
                    # The enrolment rotates too; issue after it so that only the confirmation counts.
                    uri = self.tx(lambda conn: totp.enroll(conn, user_id, TOTP_KEY, email))
                    secret = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)["secret"][0]
                    issued = self.request(email)
                    self.tx(lambda conn: totp.confirm(conn, user_id, TOTP_KEY, pyotp.TOTP(secret).at(self.wall),
                                                      self.wall))
                elif name == "cli totp-reset":
                    self.enrol_totp(user_id, email)
                    issued = self.request(email)
                    self.cli("totp-reset", "--email", email)
                else:
                    issued = self.request(email)
                    rotate(user_id, email)
                self.assertInvalidated(issued, user_id)

    def test_rk1_a_changed_secret_key_matches_no_binding_code_or_digest(self):
        self.verified()
        issued = self.request()
        # The browser's token under another key finds no row.
        self.assertIs(self.redeem(issued, code_of(issued), key="rotated key").outcome, Outcome.NO_PENDING)
        # The stored binding presented with another key: the code hash no longer matches.
        binding = recovery.binding_hash(KEY, issued.token)
        result = self.tx(recovery.redeem, binding, code_of(issued), NEW_PASSWORD, secret_key="rotated key",
                         clock=self.clock)
        self.assertIs(result.outcome, Outcome.WRONG_CODE)
        # And a digest made under another key never equals the stored one.
        user = self.user(self.row(issued.recovery_id)["user_id"])
        self.assertNotEqual(recovery.session_key_digest("rotated key", user["session_key"]),
                            self.row(issued.recovery_id)["session_key_digest"])

    def test_rk2_what_does_not_rotate_does_not_invalidate(self):
        changes = {
            "sign-in": lambda uid, e: self.assertEqual(self.sign_in(e, OLD_PASSWORD), "ok"),
            "pbkdf2 upgrade at sign-in": lambda uid, e: (
                self.tx(lambda conn: conn.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(OLD_PASSWORD, method="pbkdf2"), uid))),
                self.assertEqual(self.sign_in(e, OLD_PASSWORD), "ok"),
                self.assertTrue(self.user(uid)["password_hash"].startswith("scrypt"))),
            "set_display_name": lambda uid, e: self.run_account(accounts.set_display_name, uid, "Ново Име"),
            "mark_profile_required": lambda uid, e: self.run_account(accounts.mark_profile_required, uid),
            "profile edit": lambda uid, e: self.tx(lambda conn: (
                profiles.create_profile(conn, uid, account_type="student", first_name="Ана", last_name="Иванова",
                                        grade=9, now=self.wall),
                profiles.update_profile(conn, uid, grade=10, now=self.wall))),
        }
        for index, (name, change) in enumerate(changes.items()):
            with self.subTest(name):
                email = f"steady{index}@example.bg"
                user_id = self.verified(email)
                if name == "pbkdf2 upgrade at sign-in":
                    self.tx(lambda conn: conn.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        (generate_password_hash(OLD_PASSWORD, method="pbkdf2"), user_id)))
                key_before = self.user(user_id)["session_key"]
                issued = self.request(email)
                change(user_id, email)
                self.assertEqual(self.user(user_id)["session_key"], key_before)
                self.assertIs(self.redeem(issued, code_of(issued)).outcome, Outcome.REDEEMED)

    def test_rk3_the_digest_is_keyed_and_labelled(self):
        user_id = self.verified()
        issued = self.request()
        session_key = self.user(user_id)["session_key"]
        stored = self.row(issued.recovery_id)["session_key_digest"]
        self.assertNotEqual(stored, session_key)
        self.assertNotIn(session_key, stored)
        self.assertNotEqual(stored, oidc_accounts.session_key_digest(KEY, session_key))
        self.assertEqual(stored, recovery.session_key_digest(KEY, session_key))
        # Labels of every recovery value differ, so no value of one purpose verifies another.
        token = issued.token
        self.assertNotEqual(recovery.binding_hash(KEY, token), registration.binding_hash(KEY, token))
        self.assertRegex(stored, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    import unittest
    unittest.main()

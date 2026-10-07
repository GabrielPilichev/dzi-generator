"""Admin TOTP (Accounts PR T) — the credential and recovery-code service.

Runs on an in-memory database built from migrations 012 and 016. Every time value is
an explicit ``datetime``; nothing sleeps. Secrets and keys are synthetic and generated
per run.
"""

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pyotp
from cryptography.fernet import Fernet

from web.services import accounts, totp

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 3, 1, 9, 0, 10, tzinfo=timezone.utc)  # 10 s into a TOTP step
STEP = timedelta(seconds=30)
PENDING = "2026-03-01 08:59:00"  # a pending sign-in that began before T0


def totp_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    for name in ("012_users.sql", "016_admin_totp.sql"):
        conn.executescript((ROOT / "web" / "migrations" / name).read_text(encoding="utf-8"))
    return conn


def database_text(conn) -> str:
    """Every value stored in the account tables, for leak checks."""
    values = []
    for table in ("users", "totp_credentials", "totp_recovery_codes"):
        for row in conn.execute(f"SELECT * FROM {table}"):
            values.extend(str(value) for value in row)
    return "\n".join(values)


class TotpServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = totp_connection()
        self.addCleanup(self.conn.close)
        self.key = Fernet.generate_key()
        self.user_id = accounts.create_user(
            self.conn, display_name="Админ", email="admin@example.bg", role="admin")

    def session_key(self):
        return self.conn.execute("SELECT session_key FROM users WHERE id = ?", (self.user_id,)).fetchone()[0]

    def enroll(self):
        uri = totp.enroll(self.conn, self.user_id, self.key, "admin@example.bg")
        return pyotp.parse_uri(uri).secret

    def enroll_confirmed(self, at=T0 - 10 * STEP):
        secret = self.enroll()
        codes = totp.confirm(self.conn, self.user_id, self.key, pyotp.TOTP(secret).at(at), at)
        return secret, codes

    def row(self):
        return totp.credential(self.conn, self.user_id)


class KeyTest(TotpServiceTestCase):
    def test_key_problems(self):
        self.assertIsNone(totp.key_problem(self.key))
        self.assertIsNone(totp.key_problem(self.key.decode()))
        for bad in (None, "", b"", "too-short", "x" * 44, 12345):
            with self.subTest(key=bad):
                problem = totp.key_problem(bad)
                self.assertIn("DZI_TOTP_KEY", problem)
                if bad:
                    self.assertNotIn(str(bad), problem)

    def test_enrolment_without_a_usable_key_fails_closed_and_writes_nothing(self):
        before = self.session_key()
        for bad in (None, "", "not-a-fernet-key"):
            with self.subTest(key=bad), self.assertRaises(totp.TotpKeyError):
                totp.enroll(self.conn, self.user_id, bad, "admin@example.bg")
        self.assertIsNone(self.row())
        self.assertEqual(self.session_key(), before)

    def test_verification_without_a_usable_key_fails_closed(self):
        secret, _ = self.enroll_confirmed()
        code = pyotp.TOTP(secret).at(T0)
        for bad in (None, "", "not-a-fernet-key"):
            with self.subTest(key=bad), self.assertRaises(totp.TotpKeyError):
                totp.verify_totp(self.conn, self.user_id, bad, code, T0, PENDING)
        with self.assertRaises(totp.TotpSecretError):
            totp.verify_totp(self.conn, self.user_id, Fernet.generate_key(), code, T0, PENDING)
        self.assertIsNone(self.row()["last_verified_at"])

    def test_corrupt_ciphertext_fails_closed(self):
        self.enroll_confirmed()
        self.conn.execute("UPDATE totp_credentials SET secret_ciphertext = 'garbage'")
        with self.assertRaises(totp.TotpSecretError) as caught:
            totp.verify_totp(self.conn, self.user_id, self.key, "123456", T0, PENDING)
        self.assertNotIn("garbage", str(caught.exception))


class EnrolmentTest(TotpServiceTestCase):
    def test_enrolment_is_unconfirmed_and_stores_only_ciphertext(self):
        before = self.session_key()
        uri = totp.enroll(self.conn, self.user_id, self.key, "admin@example.bg")
        parsed = pyotp.parse_uri(uri)
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertEqual(parsed.issuer, "LearnPilot")
        self.assertEqual((parsed.digits, parsed.interval), (6, 30))
        self.assertGreaterEqual(len(parsed.secret), 32)
        row = self.row()
        self.assertIsNone(row["confirmed_at"])
        self.assertEqual(totp.credential_state(self.conn, self.user_id), "unconfirmed")
        self.assertFalse(totp.has_confirmed_credential(self.conn, self.user_id))
        self.assertNotIn(parsed.secret, database_text(self.conn))
        self.assertEqual(Fernet(self.key).decrypt(row["secret_ciphertext"].encode()).decode(), parsed.secret)
        self.assertNotEqual(self.session_key(), before)
        self.assertEqual(totp.unused_recovery_code_count(self.conn, self.user_id), 0)

    def test_each_enrolment_has_a_new_secret(self):
        self.assertNotEqual(self.enroll(), self.enroll())

    def test_unconfirmed_enrolment_cannot_sign_in(self):
        secret = self.enroll()
        self.assertFalse(totp.verify_totp(
            self.conn, self.user_id, self.key, pyotp.TOTP(secret).at(T0), T0, PENDING))

    def test_wrong_or_malformed_confirmation_code_leaves_it_unconfirmed(self):
        secret = self.enroll()
        good = pyotp.TOTP(secret).at(T0)
        for bad in ("", "abcdef", "12345", str((int(good) + 1) % 10**6).zfill(6),
                    pyotp.TOTP(secret).at(T0 - 3 * STEP), None):
            with self.subTest(code=bad), self.assertRaises(ValueError):
                totp.confirm(self.conn, self.user_id, self.key, bad, T0)
        self.assertIsNone(self.row()["confirmed_at"])
        self.assertEqual(totp.unused_recovery_code_count(self.conn, self.user_id), 0)

    def test_confirmation_activates_uses_the_step_and_issues_recovery_codes(self):
        secret = self.enroll()
        before = self.session_key()
        codes = totp.confirm(self.conn, self.user_id, self.key, pyotp.TOTP(secret).at(T0), T0)
        row = self.row()
        self.assertIsNotNone(row["confirmed_at"])
        self.assertEqual(row["last_used_step"], pyotp.TOTP(secret).timecode(T0))
        self.assertNotEqual(self.session_key(), before)
        self.assertEqual(len(codes), totp.RECOVERY_CODE_COUNT)
        self.assertEqual(len(set(codes)), len(codes))
        for code in codes:
            self.assertRegex(code, r"^[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}$")
        # The confirming code cannot also sign in.
        self.assertFalse(totp.verify_totp(
            self.conn, self.user_id, self.key, pyotp.TOTP(secret).at(T0), T0, PENDING))

    def test_confirmed_credential_is_not_replaced_or_reconfirmed(self):
        secret, _ = self.enroll_confirmed()
        with self.assertRaises(ValueError):
            totp.enroll(self.conn, self.user_id, self.key, "admin@example.bg")
        with self.assertRaises(ValueError):
            totp.confirm(self.conn, self.user_id, self.key, pyotp.TOTP(secret).at(T0), T0)
        self.assertEqual(Fernet(self.key).decrypt(self.row()["secret_ciphertext"].encode()).decode(), secret)

    def test_confirm_without_enrolment(self):
        with self.assertRaises(LookupError):
            totp.confirm(self.conn, self.user_id, self.key, "123456", T0)


class TotpVerificationTest(TotpServiceTestCase):
    def setUp(self):
        super().setUp()
        self.secret, _ = self.enroll_confirmed()
        self.otp = pyotp.TOTP(self.secret)

    def verify(self, code, now=T0, pending=PENDING):
        return totp.verify_totp(self.conn, self.user_id, self.key, code, now, pending)

    def test_current_and_adjacent_steps_are_accepted(self):
        for offset in (-1, 0, 1):
            with self.subTest(offset=offset):
                now = T0 + 10 * (offset + 2) * STEP  # a fresh window each time, after enrolment
                pending = totp.timestamp(now - timedelta(seconds=5))
                self.assertTrue(self.verify(self.otp.at(now + offset * STEP), now, pending))
                self.assertEqual(self.row()["last_used_step"], self.otp.timecode(now + offset * STEP))
                self.assertEqual(self.row()["last_verified_at"], totp.timestamp(now))

    def test_codes_outside_the_window_are_refused(self):
        for offset in (-3, -2, 2, 3):
            with self.subTest(offset=offset):
                self.assertFalse(self.verify(self.otp.at(T0 + offset * STEP)))
        self.assertIsNone(self.row()["last_verified_at"])

    def test_a_used_step_and_any_older_step_are_refused(self):
        self.assertTrue(self.verify(self.otp.at(T0)))
        later = totp.timestamp(T0 + timedelta(seconds=5))
        self.assertFalse(self.verify(self.otp.at(T0), T0 + timedelta(seconds=10), later))
        self.assertFalse(self.verify(self.otp.at(T0 - STEP), T0 + timedelta(seconds=10), later))
        self.assertTrue(self.verify(self.otp.at(T0 + STEP), T0 + timedelta(seconds=10), later))

    def test_a_pending_sign_in_is_spent_by_a_completed_one(self):
        self.assertTrue(self.verify(self.otp.at(T0)))
        # Same pending start (a replayed pending state), a newer valid code: refused.
        self.assertFalse(self.verify(self.otp.at(T0 + STEP), T0 + STEP))
        self.assertTrue(totp.pending_is_spent(self.row(), PENDING))

    def test_concurrent_requests_cannot_both_use_one_step(self):
        stale = self.row()  # both requests read the credential before either writes
        with mock.patch.object(totp, "credential", return_value=dict(stale)):
            self.assertTrue(self.verify(self.otp.at(T0)))
            self.assertFalse(self.verify(self.otp.at(T0)))
        self.assertEqual(self.row()["last_used_step"], self.otp.timecode(T0))

    def test_concurrent_completions_of_one_pending_sign_in_cannot_both_succeed(self):
        stale = self.row()
        with mock.patch.object(totp, "credential", return_value=dict(stale)):
            self.assertTrue(self.verify(self.otp.at(T0 - STEP)))
            self.assertFalse(self.verify(self.otp.at(T0)))  # newer step, same pending sign-in

    def test_unconfirmed_or_missing_credential_is_refused(self):
        self.conn.execute("UPDATE totp_credentials SET confirmed_at = NULL")
        self.assertFalse(self.verify(self.otp.at(T0)))
        totp.reset(self.conn, self.user_id)
        self.assertFalse(self.verify(self.otp.at(T0)))


class CodeFormatTest(unittest.TestCase):
    def test_totp_code_normalization(self):
        self.assertEqual(totp.normalize_totp_code("123456"), "123456")
        self.assertEqual(totp.normalize_totp_code(" 123 456 "), "123456")
        for bad in (None, 123456, "", "12345", "1234567", "12345a", "１２３４５６", "١٢٣٤٥٦",
                    "1" * 100, "123\x00456"):
            with self.subTest(code=bad):
                self.assertIsNone(totp.normalize_totp_code(bad))

    def test_recovery_code_normalization(self):
        self.assertEqual(totp.normalize_recovery_code("ABCD-EFGH-JK23"), "abcdefghjk23")
        self.assertEqual(totp.normalize_recovery_code(" abcd efgh jk23 "), "abcdefghjk23")
        for bad in (None, "", "abcd-efgh-jk2", "abcd-efgh-jk234", "abcd-efgh-jkil", "123456",
                    "a" * 100, "abcd-efgh-jk2!"):
            with self.subTest(code=bad):
                self.assertIsNone(totp.normalize_recovery_code(bad))


class RecoveryCodeTest(TotpServiceTestCase):
    def setUp(self):
        super().setUp()
        self.secret, self.codes = self.enroll_confirmed()

    def use(self, code, now=T0, pending=PENDING):
        normalized = totp.normalize_recovery_code(code)
        return totp.verify_recovery_code(self.conn, self.user_id, normalized, now, pending)

    def test_only_scrypt_hashes_are_stored(self):
        rows = self.conn.execute("SELECT code_hash, used_at FROM totp_recovery_codes").fetchall()
        self.assertEqual(len(rows), totp.RECOVERY_CODE_COUNT)
        for code_hash, used_at in rows:
            self.assertTrue(code_hash.startswith("scrypt:"))
            self.assertIsNone(used_at)
        stored = database_text(self.conn)
        for code in self.codes:
            self.assertNotIn(code, stored)
            self.assertNotIn(totp.normalize_recovery_code(code), stored)

    def test_a_valid_code_works_once(self):
        self.assertTrue(self.use(self.codes[3]))
        self.assertEqual(totp.unused_recovery_code_count(self.conn, self.user_id), totp.RECOVERY_CODE_COUNT - 1)
        self.assertEqual(self.row()["last_verified_at"], totp.timestamp(T0))
        later = T0 + timedelta(minutes=1)
        self.assertFalse(self.use(self.codes[3], later, totp.timestamp(later - timedelta(seconds=5))))
        self.assertTrue(self.use(self.codes[4].upper().replace("-", ""), later,
                                 totp.timestamp(later - timedelta(seconds=5))))

    def test_an_invalid_code_is_refused(self):
        self.assertFalse(self.use("2222-2222-2222"))
        self.assertEqual(totp.unused_recovery_code_count(self.conn, self.user_id), totp.RECOVERY_CODE_COUNT)

    def test_regeneration_voids_every_earlier_code(self):
        new_codes = totp.regenerate_recovery_codes(self.conn, self.user_id)
        self.assertEqual(len(new_codes), totp.RECOVERY_CODE_COUNT)
        self.assertFalse(set(new_codes) & set(self.codes))
        self.assertFalse(self.use(self.codes[0]))
        self.assertTrue(self.use(new_codes[0]))

    def test_regeneration_needs_a_confirmed_credential(self):
        totp.reset(self.conn, self.user_id)
        with self.assertRaises(LookupError):
            totp.regenerate_recovery_codes(self.conn, self.user_id)

    def test_concurrent_requests_cannot_both_consume_one_code(self):
        stale_codes = totp._unused_recovery_codes(self.conn, self.user_id)
        with mock.patch.object(totp, "_unused_recovery_codes", return_value=stale_codes):
            self.assertTrue(self.use(self.codes[0]))
            later = T0 + timedelta(minutes=1)
            self.assertFalse(self.use(self.codes[0], later, totp.timestamp(later - timedelta(seconds=5))))

    def test_a_spent_pending_sign_in_cannot_consume_a_code(self):
        self.assertTrue(self.use(self.codes[0]))
        self.assertFalse(self.use(self.codes[1], T0 + STEP))  # same (replayed) pending start


class ResetTest(TotpServiceTestCase):
    def test_reset_removes_credential_and_codes_and_rotates_the_session_key(self):
        secret, codes = self.enroll_confirmed()
        before = self.session_key()
        self.assertTrue(totp.reset(self.conn, self.user_id))
        self.assertNotEqual(self.session_key(), before)
        self.assertIsNone(self.row())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM totp_recovery_codes").fetchone()[0], 0)
        self.assertFalse(totp.verify_totp(
            self.conn, self.user_id, self.key, pyotp.TOTP(secret).at(T0), T0, PENDING))
        self.assertFalse(totp.verify_recovery_code(
            self.conn, self.user_id, totp.normalize_recovery_code(codes[0]), T0, PENDING))

    def test_reset_without_credential_still_signs_out(self):
        before = self.session_key()
        self.assertFalse(totp.reset(self.conn, self.user_id))
        self.assertNotEqual(self.session_key(), before)

    def test_re_enrolment_after_reset_has_a_new_secret(self):
        old, _ = self.enroll_confirmed()
        totp.reset(self.conn, self.user_id)
        self.assertNotEqual(self.enroll(), old)


class ServiceBoundaryTest(unittest.TestCase):
    def test_service_imports_neither_flask_nor_the_app(self):
        import ast

        tree = ast.parse((ROOT / "web" / "services" / "totp.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertFalse({name for name in imported if name.startswith(("flask", "web.app", "web.blueprints"))})
        self.assertIn("pyotp", imported)
        self.assertIn("cryptography.fernet", imported)


if __name__ == "__main__":
    unittest.main()

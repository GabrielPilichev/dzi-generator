"""Admin TOTP (Accounts PR T) — CLI-only enrolment, confirmation, recovery codes and reset.

Runs against temporary databases built from migrations 012 and 016 only. The TOTP key,
secrets and codes are synthetic and generated per run.
"""

import io
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pyotp
from cryptography.fernet import Fernet

from src import manage_accounts
from web.services import accounts, totp

ROOT = Path(__file__).resolve().parents[1]
ADMIN_PW = "a-long-admin-pass"
URI_LINE = re.compile(r"^otpauth://totp/\S+$", re.MULTILINE)
CODE_LINE = re.compile(r"^[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}$", re.MULTILINE)


class TotpCliTestCase(unittest.TestCase):
    migrations = ("012_users.sql", "016_admin_totp.sql")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "t.db"
        conn = sqlite3.connect(self.db)
        for name in self.migrations:
            conn.executescript((ROOT / "web" / "migrations" / name).read_text(encoding="utf-8"))
        conn.close()
        self.key = Fernet.generate_key().decode()
        env = mock.patch.dict(os.environ, {"DZI_TOTP_KEY": self.key}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DZI_DB", None)

    def run_cli(self, *argv, stdin=None, prompts=None):
        out, err = io.StringIO(), io.StringIO()
        patches = [redirect_stdout(out), redirect_stderr(err)]
        if stdin is not None:
            patches.append(mock.patch("sys.stdin", io.StringIO(stdin)))
        if prompts is not None:
            patches.append(mock.patch("getpass.getpass", side_effect=list(prompts)))
        for p in patches:
            p.__enter__()
        try:
            code = manage_accounts.main(["--db", str(self.db), *argv])
        finally:
            for p in reversed(patches):
                p.__exit__(None, None, None)
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def create(self, email="admin@example.bg", role="admin"):
        self.assertEqual(self.run_cli("create", "--email", email, "--name", "Админ Админов", "--role", role,
                                      "--password-stdin", stdin=ADMIN_PW + "\n"), 0, self.err)

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def session_key(self, email="admin@example.bg"):
        return self.query("SELECT session_key FROM users WHERE email = ?", (email,))[0][0]

    def state(self):
        rows = self.query("SELECT confirmed_at IS NOT NULL FROM totp_credentials")
        return None if not rows else ("confirmed" if rows[0][0] else "unconfirmed")

    def stored_text(self):
        values = []
        for table in ("users", "totp_credentials", "totp_recovery_codes"):
            for row in self.query(f"SELECT * FROM {table}"):
                values.extend(str(v) for v in row)
        return "\n".join(values)

    def enroll(self):
        self.assertEqual(self.run_cli("totp-enroll", "--email", "admin@example.bg"), 0, self.err)
        return pyotp.parse_uri(URI_LINE.search(self.out).group(0)).secret

    def confirm(self, secret):
        code = pyotp.TOTP(secret).now()
        self.assertEqual(self.run_cli("totp-confirm", "--email", "admin@example.bg", "--code-stdin",
                                      stdin=code + "\n"), 0, self.err)
        return CODE_LINE.findall(self.out)


class EnrolTest(TotpCliTestCase):
    def test_enrol_prints_the_uri_once_and_stays_unconfirmed(self):
        self.create()
        before = self.session_key()
        secret = self.enroll()
        self.assertEqual(len(URI_LINE.findall(self.out)), 1)
        self.assertIn("NOT active", self.out)
        self.assertIn("LearnPilot", self.out)
        self.assertEqual(self.state(), "unconfirmed")
        self.assertNotEqual(self.session_key(), before)
        self.assertNotIn(secret, self.stored_text())
        self.assertNotIn(self.key, self.out + self.err)
        # The secret is not printed by any later command.
        for argv in (("list",), ("totp-recovery-codes", "--email", "admin@example.bg")):
            self.run_cli(*argv)
            self.assertNotIn(secret, self.out + self.err)
            self.assertNotIn("otpauth", self.out + self.err)

    def test_enrol_needs_a_usable_key_and_writes_nothing_without_one(self):
        self.create()
        before = self.session_key()
        for value in (None, "", "not-a-fernet-key"):
            with self.subTest(key=value):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("DZI_TOTP_KEY", None)
                    if value is not None:
                        os.environ["DZI_TOTP_KEY"] = value
                    self.assertEqual(self.run_cli("totp-enroll", "--email", "admin@example.bg"), 1)
                self.assertIn("DZI_TOTP_KEY", self.err)
                if value:
                    self.assertNotIn(value, self.err)
                self.assertNotIn("otpauth", self.out)
                self.assertIsNone(self.state())
                self.assertEqual(self.session_key(), before)

    def test_only_admins_can_be_enrolled(self):
        self.create("teacher@example.bg", "teacher")
        self.assertEqual(self.run_cli("totp-enroll", "--email", "teacher@example.bg"), 1)
        self.assertIn("not an admin", self.err)
        self.assertIsNone(self.state())

    def test_re_enrolling_an_unconfirmed_credential_replaces_its_secret(self):
        self.create()
        first = self.enroll()
        second = self.enroll()
        self.assertNotEqual(first, second)
        self.assertEqual(self.run_cli("totp-confirm", "--email", "admin@example.bg", "--code-stdin",
                                      stdin=pyotp.TOTP(first).now() + "\n"), 1)
        self.assertEqual(len(self.confirm(second)), totp.RECOVERY_CODE_COUNT)

    def test_a_confirmed_credential_is_not_replaced_by_enrol(self):
        self.create()
        self.confirm(self.enroll())
        self.assertEqual(self.run_cli("totp-enroll", "--email", "admin@example.bg"), 1)
        self.assertIn("reset it first", self.err)
        self.assertNotIn("otpauth", self.out)
        self.assertEqual(self.state(), "confirmed")


class ConfirmTest(TotpCliTestCase):
    def setUp(self):
        super().setUp()
        self.create()
        self.secret = self.enroll()

    def test_wrong_or_malformed_code_leaves_it_unconfirmed(self):
        good = pyotp.TOTP(self.secret).now()
        for bad in (str((int(good) + 1) % 10**6).zfill(6), "abcdef", "", "12345"):
            with self.subTest(code=bad):
                self.assertEqual(self.run_cli("totp-confirm", "--email", "admin@example.bg", "--code-stdin",
                                              stdin=bad + "\n"), 1)
                self.assertNotIn(good, self.err)
                self.assertEqual(self.state(), "unconfirmed")
        self.assertEqual(self.query("SELECT COUNT(*) FROM totp_recovery_codes")[0][0], 0)
        self.assertEqual(self.run_cli("totp-confirm", "--email", "admin@example.bg", "--code-stdin",
                                      stdin="1\n2\n"), 1)
        self.assertIn("exactly one line", self.err)

    def test_confirmation_activates_and_prints_recovery_codes_once(self):
        before = self.session_key()
        codes = self.confirm(self.secret)
        self.assertEqual(self.state(), "confirmed")
        self.assertEqual(len(codes), totp.RECOVERY_CODE_COUNT)
        self.assertNotEqual(self.session_key(), before)
        hashes = [row[0] for row in self.query("SELECT code_hash FROM totp_recovery_codes")]
        self.assertEqual(len(hashes), totp.RECOVERY_CODE_COUNT)
        self.assertTrue(all(h.startswith("scrypt:") for h in hashes))
        stored = self.stored_text()
        for code in codes:
            self.assertNotIn(code, stored)
            self.assertNotIn(code.replace("-", ""), stored)

    def test_confirmation_by_prompt(self):
        code = pyotp.TOTP(self.secret).now()
        self.assertEqual(self.run_cli("totp-confirm", "--email", "admin@example.bg", prompts=[code]), 0, self.err)
        self.assertEqual(self.state(), "confirmed")

    def test_confirmation_with_another_key_fails_closed(self):
        with mock.patch.dict(os.environ, {"DZI_TOTP_KEY": Fernet.generate_key().decode()}):
            self.assertEqual(self.run_cli("totp-confirm", "--email", "admin@example.bg", "--code-stdin",
                                          stdin=pyotp.TOTP(self.secret).now() + "\n"), 1)
        self.assertIn("cannot be decrypted", self.err)
        self.assertEqual(self.state(), "unconfirmed")


class RecoveryAndResetTest(TotpCliTestCase):
    def setUp(self):
        super().setUp()
        self.create()
        self.secret = self.enroll()
        self.codes = self.confirm(self.secret)
        self.user_id = self.query("SELECT id FROM users")[0][0]

    def verify_recovery(self, code):
        conn = sqlite3.connect(self.db)
        try:
            now = datetime.now(timezone.utc)
            return totp.verify_recovery_code(conn, self.user_id, totp.normalize_recovery_code(code), now,
                                             "2000-01-01 00:00:00")
        finally:
            conn.close()

    def test_regeneration_prints_new_codes_and_voids_the_old_ones(self):
        before = self.session_key()
        self.assertEqual(self.run_cli("totp-recovery-codes", "--email", "admin@example.bg"), 0, self.err)
        new_codes = CODE_LINE.findall(self.out)
        self.assertEqual(len(new_codes), totp.RECOVERY_CODE_COUNT)
        self.assertFalse(set(new_codes) & set(self.codes))
        self.assertEqual(self.session_key(), before)
        self.assertFalse(self.verify_recovery(self.codes[0]))
        self.assertTrue(self.verify_recovery(new_codes[0]))

    def test_regeneration_needs_no_key_but_a_confirmed_credential(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DZI_TOTP_KEY", None)
            self.assertEqual(self.run_cli("totp-recovery-codes", "--email", "admin@example.bg"), 0)
            self.assertEqual(self.run_cli("totp-reset", "--email", "admin@example.bg"), 0)
            self.assertEqual(self.run_cli("totp-recovery-codes", "--email", "admin@example.bg"), 1)
        self.assertEqual(CODE_LINE.findall(self.out), [])

    def test_reset_removes_everything_and_signs_out(self):
        before = self.session_key()
        self.assertEqual(self.run_cli("totp-reset", "--email", "admin@example.bg"), 0, self.err)
        self.assertIn("existing sessions invalidated", self.out)
        self.assertNotEqual(self.session_key(), before)
        self.assertIsNone(self.state())
        self.assertEqual(self.query("SELECT COUNT(*) FROM totp_recovery_codes")[0][0], 0)
        self.assertFalse(self.verify_recovery(self.codes[0]))
        # Re-enrolment starts from a new secret.
        self.assertNotEqual(self.enroll(), self.secret)

    def test_list_shows_no_totp_material(self):
        self.assertEqual(self.run_cli("list"), 0)
        for secret in [self.secret, self.key, *self.codes]:
            self.assertNotIn(secret, self.out)
        self.assertNotIn("otpauth", self.out)


class DatabaseTargetingTest(TotpCliTestCase):
    migrations = ("012_users.sql",)

    def test_database_without_the_totp_tables_is_reported(self):
        self.create()
        for command in ("totp-enroll", "totp-confirm", "totp-recovery-codes", "totp-reset"):
            with self.subTest(command=command):
                self.assertEqual(self.run_cli(command, "--email", "admin@example.bg", stdin="123456\n"), 1)
                self.assertIn("migration 016", self.err)

    def test_no_secret_or_code_argument_exists(self):
        parser = manage_accounts.build_parser()
        subparsers = next(a for a in parser._actions if a.dest == "command").choices
        for command in ("totp-enroll", "totp-confirm", "totp-recovery-codes", "totp-reset"):
            options = {opt for action in subparsers[command]._actions for opt in action.option_strings}
            self.assertFalse({"--secret", "--code", "--key", "--uri"} & options)

    def test_cli_does_not_duplicate_totp_logic(self):
        source = (ROOT / "src" / "manage_accounts.py").read_text(encoding="utf-8")
        for fragment in ("pyotp", "Fernet", "generate_password_hash", "INSERT INTO totp", "UPDATE totp"):
            self.assertNotIn(fragment, source)
        self.assertIs(manage_accounts.accounts, accounts)


if __name__ == "__main__":
    unittest.main()

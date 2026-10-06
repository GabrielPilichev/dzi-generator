import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from werkzeug.security import check_password_hash

from src import manage_accounts
from web.services import accounts

ROOT = Path(__file__).resolve().parents[1]
GOOD = "correct horse"
ADMIN_PW = "a-long-admin-pass"  # 16 chars


class CliTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "t.db"
        conn = sqlite3.connect(self.db)
        conn.executescript((ROOT / "web" / "migrations" / "012_users.sql").read_text(encoding="utf-8"))
        conn.close()
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DZI_DB", None)

    def run_cli(self, *argv, db=True, stdin=None, prompts=None):
        full = (["--db", str(self.db)] if db is True else []) + list(argv)
        out, err = io.StringIO(), io.StringIO()
        patches = [redirect_stdout(out), redirect_stderr(err)]
        if stdin is not None:
            patches.append(mock.patch("sys.stdin", io.StringIO(stdin)))
        if prompts is not None:
            patches.append(mock.patch("getpass.getpass", side_effect=list(prompts)))
        for p in patches:
            p.__enter__()
        try:
            code = manage_accounts.main(full)
        finally:
            for p in reversed(patches):
                p.__exit__(None, None, None)
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def create(self, email="ana@example.bg", role="student", pw=GOOD):
        return self.run_cli("create", "--email", email, "--name", "Ана Иванова", "--role", role,
                            "--password-stdin", stdin=pw + "\n")

    def row(self, email="ana@example.bg"):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        finally:
            conn.close()

    def count(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            conn.close()


class DatabaseSafetyTest(CliTestCase):
    def test_no_db_fails_without_creating(self):
        cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, cwd)
        self.assertEqual(self.run_cli("list", db=False), 1)
        self.assertIn("--db", self.err)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["t.db"])

    def test_missing_db_not_created(self):
        missing = self.dir / "nope.db"
        self.assertEqual(manage_accounts.main(["--db", str(missing), "list"]), 1)
        self.assertFalse(missing.exists())

    def test_db_without_users(self):
        bare = self.dir / "bare.db"
        sqlite3.connect(bare).close()
        self.assertEqual(manage_accounts.main(["--db", str(bare), "list"]), 1)
        conn = sqlite3.connect(bare)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)
        conn.close()

    def test_env_db_and_precedence(self):
        os.environ["DZI_DB"] = str(self.db)
        self.assertEqual(self.run_cli("list", db=False), 0)
        os.environ["DZI_DB"] = str(self.dir / "other.db")
        self.assertEqual(self.run_cli("list"), 0)  # --db wins

    def test_no_traceback_for_expected_errors(self):
        self.run_cli("set-role", "--email", "x@y.bg", "--role", "admin")
        self.assertNotIn("Traceback", self.err)


class CreateTest(CliTestCase):
    def test_roles_and_normalized_hashed(self):
        for role in ("student", "teacher", "admin"):
            self.assertEqual(self.create(f"  {role.upper()}@Example.BG ", role, ADMIN_PW), 0, self.err)
            row = self.row(f"{role}@example.bg")
            self.assertEqual(row["role"], role)
            self.assertNotEqual(row["password_hash"], ADMIN_PW)
            self.assertTrue(check_password_hash(row["password_hash"], ADMIN_PW))
            self.assertNotIn(ADMIN_PW, self.out + self.err)

    def test_admin_minimum(self):
        self.assertEqual(self.create(role="admin", pw="thirteen-char"), 1)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.create(role="admin", pw="fourteen-chars"), 0)

    def test_service_minimum_still_applies(self):
        self.assertEqual(self.create(pw="short"), 1)
        self.assertEqual(self.count(), 0)

    def test_duplicate_email(self):
        self.assertEqual(self.create(), 0)
        self.assertEqual(self.create("ANA@example.bg"), 1)
        self.assertIn("already exists", self.err)
        self.assertEqual(self.count(), 1)

    def test_bad_email_and_name_leave_nothing(self):
        self.assertEqual(self.create("nope"), 1)
        self.assertEqual(self.run_cli("create", "--email", "a@b.bg", "--name", "x",
                                      "--password-stdin", stdin=GOOD + "\n"), 1)
        self.assertEqual(self.count(), 0)


class PasswordInputTest(CliTestCase):
    def test_prompt_confirmation(self):
        code = self.run_cli("create", "--email", "a@b.bg", "--name", "Ана", prompts=[GOOD, GOOD])
        self.assertEqual(code, 0, self.err)

    def test_mismatch(self):
        code = self.run_cli("create", "--email", "a@b.bg", "--name", "Ана", prompts=[GOOD, GOOD + "x"])
        self.assertEqual(code, 1)
        self.assertNotIn(GOOD, self.err + self.out)
        self.assertEqual(self.count(), 0)

    def test_empty_stdin(self):
        for data in ("", "\n"):
            self.assertEqual(self.run_cli("create", "--email", "a@b.bg", "--name", "Ана",
                                          "--password-stdin", stdin=data), 1)

    def test_multiline_stdin_rejected(self):
        self.assertEqual(self.run_cli("create", "--email", "a@b.bg", "--name", "Ана",
                                      "--password-stdin", stdin=GOOD + "\nextra\n"), 1)
        self.assertEqual(self.count(), 0)

    def test_crlf_and_no_trailing_newline(self):
        self.assertEqual(self.create(pw=GOOD), 0)
        self.assertEqual(self.run_cli("set-password", "--email", "ana@example.bg",
                                      "--password-stdin", stdin="another pass\r\n"), 0)
        self.assertTrue(check_password_hash(self.row()["password_hash"], "another pass"))

    def test_no_password_argv_option(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            manage_accounts.main(["--db", str(self.db), "create", "--email", "a@b.bg",
                                  "--name", "Ана", "--password", "secretsecret"])


class SetPasswordTest(CliTestCase):
    def test_changes_hash_key_timestamp(self):
        self.create()
        before = self.row()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE users SET password_changed_at = '2000-01-01 00:00:00'")
        conn.commit()
        conn.close()
        self.assertEqual(self.run_cli("set-password", "--email", "Ana@Example.bg",
                                      "--password-stdin", stdin="brand new pass\n"), 0, self.err)
        after = self.row()
        self.assertNotEqual(before["password_hash"], after["password_hash"])
        self.assertNotEqual(before["session_key"], after["session_key"])
        self.assertNotEqual(after["password_changed_at"], "2000-01-01 00:00:00")
        self.assertNotIn("brand new pass", self.out + self.err)

    def test_admin_minimum(self):
        self.create(role="admin", pw=ADMIN_PW)
        before = self.row()
        self.assertEqual(self.run_cli("set-password", "--email", "ana@example.bg",
                                      "--password-stdin", stdin="only twelve!\n"), 1)
        self.assertEqual(before["password_hash"], self.row()["password_hash"])

    def test_missing_user(self):
        self.assertEqual(self.run_cli("set-password", "--email", "no@one.bg",
                                      "--password-stdin", stdin=GOOD + "\n"), 1)
        self.assertIn("no account", self.err)

    def test_malformed_target(self):
        self.assertEqual(self.run_cli("set-password", "--email", "junk",
                                      "--password-stdin", stdin=GOOD + "\n"), 1)
        self.assertIn("invalid email", self.err)


class RoleStateSignOutTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.create()
        self.before = self.row()

    def test_set_role_including_admin(self):
        self.assertEqual(self.run_cli("set-role", "--email", "ana@example.bg", "--role", "admin"), 0)
        after = self.row()
        self.assertEqual(after["role"], "admin")
        self.assertNotEqual(after["session_key"], self.before["session_key"])

    def test_invalid_role_rejected(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            manage_accounts.main(["--db", str(self.db), "set-role", "--email", "ana@example.bg", "--role", "root"])
        self.assertEqual(self.row()["role"], "student")

    def test_disable_enable(self):
        self.assertEqual(self.run_cli("disable", "--email", "ana@example.bg"), 0)
        disabled = self.row()
        self.assertEqual(disabled["is_active"], 0)
        self.assertNotEqual(disabled["session_key"], self.before["session_key"])
        self.assertEqual(self.run_cli("enable", "--email", "ana@example.bg"), 0)
        enabled = self.row()
        self.assertEqual(enabled["is_active"], 1)
        self.assertNotEqual(enabled["session_key"], disabled["session_key"])

    def test_sign_out_only_rotates_key(self):
        self.assertEqual(self.run_cli("sign-out", "--email", "ana@example.bg"), 0)
        after = self.row()
        for column in ("email", "display_name", "role", "password_hash", "is_active",
                       "password_changed_at", "last_login_at", "created_at"):
            self.assertEqual(after[column], self.before[column], column)
        self.assertNotEqual(after["session_key"], self.before["session_key"])


class ListTest(CliTestCase):
    def test_safe_fields_only(self):
        self.create("b@example.bg")
        self.create("a@example.bg", "teacher")
        self.assertEqual(self.run_cli("list"), 0)
        row = self.row("a@example.bg")
        self.assertNotIn(row["password_hash"], self.out)
        self.assertNotIn(row["session_key"], self.out)
        self.assertNotIn("password_hash", self.out)
        lines = self.out.splitlines()
        self.assertEqual(lines[0].split("\t")[:4], ["id", "email", "name", "role"])
        self.assertEqual([l.split("\t")[1] for l in lines[1:]], ["b@example.bg", "a@example.bg"])
        self.assertEqual(lines[2].split("\t")[3:5], ["teacher", "yes"])


class TransactionTest(CliTestCase):
    def test_rollback_on_mutation_failure(self):
        self.create()
        before = self.row()
        with mock.patch.object(accounts, "set_role", side_effect=sqlite3.OperationalError("boom")):
            self.assertEqual(self.run_cli("set-role", "--email", "ana@example.bg", "--role", "admin"), 1)
        self.assertEqual(tuple(self.row()), tuple(before))

    def test_no_commit_when_create_fails_midway(self):
        with mock.patch.object(accounts, "create_user", side_effect=sqlite3.OperationalError("boom")):
            self.assertEqual(self.create(), 1)
        self.assertEqual(self.count(), 0)


class ImportBoundaryTest(unittest.TestCase):
    def test_no_flask_coupling(self):
        source = (ROOT / "src" / "manage_accounts.py").read_text(encoding="utf-8")
        self.assertIn("web.services", source)
        for forbidden in ("flask", "web.app", "web.blueprints", "hashlib", "generate_password_hash"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()

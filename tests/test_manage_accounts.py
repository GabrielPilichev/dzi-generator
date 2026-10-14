import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from werkzeug.security import check_password_hash

from src import manage_accounts
from web.services import accounts, identities, login_throttle

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

    def test_every_message_names_the_account_by_its_id(self):
        account_id = self.before["id"]
        cases = (
            (("set-role", "--email", "ana@example.bg", "--role", "teacher"),
             f"role of account {account_id} (ana@example.bg) is now teacher; existing sessions invalidated"),
            (("disable", "--email", "ana@example.bg"),
             f"disabled account {account_id} (ana@example.bg, teacher); existing sessions invalidated"),
            (("enable", "--email", "ana@example.bg"),
             f"enabled account {account_id} (ana@example.bg, teacher); existing sessions invalidated"),
            (("sign-out", "--email", "ana@example.bg"),
             f"sessions of account {account_id} (ana@example.bg, teacher) invalidated"),
        )
        for argv, message in cases:
            with self.subTest(command=argv[0]):
                self.assertEqual(self.run_cli(*argv), 0, self.err)
                self.assertEqual(self.out.strip(), message)
        self.assertEqual(self.run_cli("set-password", "--email", "ana@example.bg",
                                      "--password-stdin", stdin="another long pass\n"), 0, self.err)
        self.assertEqual(self.out.strip(),
                         f"password changed for account {account_id} (ana@example.bg); existing sessions invalidated")


class AccountSelectionByIdTest(CliTestCase):
    """An account created through Google sign-in has no email: ``disable``, ``enable``,
    ``sign-out`` and ``set-role`` select by exactly one of ``--email`` and ``--id``."""

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db)
        conn.executescript((ROOT / "web" / "migrations" / "019_user_identities.sql").read_text(encoding="utf-8"))
        # As Google sign-in creates one: no email, no password, an identity.
        self.google_id = accounts.create_user(conn, display_name="Мария Петрова", role="student")
        identities.add_identity(conn, self.google_id, issuer="https://accounts.google.com",
                                subject="SUBJECT-NEVER-PRINTED", provider="google", email_at_link="maria.g@example.bg")
        conn.commit()
        conn.close()
        self.create()   # ana@example.bg, with a password
        self.password_id = self.row()["id"]

    def by_id(self, account_id):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT * FROM users WHERE id = ?", (account_id,)).fetchone()
        finally:
            conn.close()

    def test_disable_enable_and_sign_out_by_id_rotate_the_key_as_by_email(self):
        before = self.by_id(self.google_id)
        self.assertEqual(self.run_cli("disable", "--id", str(self.google_id)), 0, self.err)
        # No email is printed as "no email", never as "None".
        self.assertEqual(self.out.strip(),
                         f"disabled account {self.google_id} (no email, student); existing sessions invalidated")
        disabled = self.by_id(self.google_id)
        self.assertEqual(disabled["is_active"], 0)
        self.assertNotEqual(disabled["session_key"], before["session_key"])

        self.assertEqual(self.run_cli("enable", "--id", str(self.google_id)), 0, self.err)
        self.assertEqual(self.out.strip(),
                         f"enabled account {self.google_id} (no email, student); existing sessions invalidated")
        enabled = self.by_id(self.google_id)
        self.assertEqual(enabled["is_active"], 1)
        self.assertNotEqual(enabled["session_key"], disabled["session_key"])

        self.assertEqual(self.run_cli("sign-out", "--id", str(self.google_id)), 0, self.err)
        self.assertEqual(self.out.strip(), f"sessions of account {self.google_id} (no email, student) invalidated")
        self.assertNotEqual(self.by_id(self.google_id)["session_key"], enabled["session_key"])
        self.assertNotIn("None", self.out + self.err)

    def test_an_account_with_an_email_can_be_selected_by_id_too(self):
        before = self.by_id(self.password_id)
        self.assertEqual(self.run_cli("sign-out", "--id", str(self.password_id)), 0, self.err)
        self.assertEqual(self.out.strip(),
                         f"sessions of account {self.password_id} (ana@example.bg, student) invalidated")
        self.assertNotEqual(self.by_id(self.password_id)["session_key"], before["session_key"])
        # Only the selected account was touched.
        self.assertEqual(self.by_id(self.google_id)["is_active"], 1)

    def test_set_role_by_id_grants_teacher_to_an_account_created_through_google(self):
        before = self.by_id(self.google_id)
        self.assertEqual(self.run_cli("set-role", "--id", str(self.google_id), "--role", "teacher"), 0, self.err)
        self.assertEqual(self.out.strip(),
                         f"role of account {self.google_id} (no email) is now teacher; existing sessions invalidated")
        after = self.by_id(self.google_id)
        self.assertEqual(after["role"], "teacher")
        self.assertNotEqual(after["session_key"], before["session_key"])

    def test_admin_is_refused_for_an_account_without_a_password_and_changes_nothing(self):
        # Decision T5: an admin signs in with a password and a second factor only, so
        # the account would have no way in.
        before = tuple(self.by_id(self.google_id))
        self.assertEqual(self.run_cli("set-role", "--id", str(self.google_id), "--role", "admin"), 1)
        self.assertEqual(self.out, "")
        self.assertIn(f"account {self.google_id} (no email, student) has no password and cannot be made an admin",
                      self.err)
        self.assertNotIn("Traceback", self.err)
        self.assertEqual(tuple(self.by_id(self.google_id)), before)
        # With a password the same grant works, by id as by email.
        self.assertEqual(self.run_cli("set-role", "--id", str(self.password_id), "--role", "admin"), 0, self.err)
        self.assertEqual(self.by_id(self.password_id)["role"], "admin")

    def test_exactly_one_selector_is_required(self):
        for command in ("disable", "enable", "sign-out"):
            for argv in ((command,), (command, "--email", "ana@example.bg", "--id", str(self.password_id))):
                with self.subTest(argv=argv), redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
                    manage_accounts.main(["--db", str(self.db), *argv])
        for argv in (("set-role", "--role", "teacher"),
                     ("set-role", "--role", "teacher", "--email", "ana@example.bg", "--id", "1")):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                manage_accounts.main(["--db", str(self.db), *argv])
        self.assertEqual(self.by_id(self.password_id)["is_active"], 1)
        self.assertEqual(self.by_id(self.password_id)["role"], "student")

    def test_a_non_numeric_zero_or_negative_id_is_refused(self):
        for value in ("abc", "0", "-1", "1.5", "", " 1", "0x1", "١", str(2**63)):
            with self.subTest(id=value), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                manage_accounts.main(["--db", str(self.db), "disable", "--id", value])
        self.assertEqual((self.by_id(self.google_id)["is_active"], self.by_id(self.password_id)["is_active"]), (1, 1))

    def test_an_unknown_id_is_reported(self):
        for command in (("disable",), ("enable",), ("sign-out",), ("set-role", "--role", "teacher")):
            with self.subTest(command=command[0]):
                self.assertEqual(self.run_cli(*command, "--id", "999999"), 1)
                self.assertEqual(self.err.strip(), "error: no account with id 999999")
                self.assertEqual(self.out, "")

    def test_the_commands_that_keep_email_only_still_refuse_id(self):
        # They act on a password or on an admin's second factor, and such an account has an email.
        for command in (("set-password", "--password-stdin"), ("clear-throttle",), ("totp-enroll",),
                        ("totp-confirm",), ("totp-recovery-codes",), ("totp-reset",)):
            with self.subTest(command=command[0]), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                manage_accounts.main(["--db", str(self.db), *command, "--id", str(self.password_id)])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            manage_accounts.main(["--db", str(self.db), "create", "--id", "5", "--name", "Ана"])

    def test_list_shows_the_password_and_google_columns_and_no_subject(self):
        conn = sqlite3.connect(self.db)
        silent = accounts.create_user(conn, display_name="Без Имейл", role="student")
        identities.add_identity(conn, silent, issuer="https://accounts.google.com", subject="ANOTHER-SUBJECT",
                                provider="google")   # Google reported no address
        conn.commit()
        conn.close()
        self.assertEqual(self.run_cli("list"), 0, self.err)
        lines = [line.split("\t") for line in self.out.splitlines()]
        self.assertEqual(lines[0], ["id", "email", "name", "role", "active", "password", "google", "created_at",
                                    "last_login_at", "verified", "type"])
        rows = {int(line[0]): dict(zip(lines[0], line)) for line in lines[1:]}
        self.assertEqual((rows[self.google_id]["email"], rows[self.google_id]["password"],
                          rows[self.google_id]["google"]), ("-", "no", "maria.g@example.bg"))
        self.assertEqual((rows[self.password_id]["email"], rows[self.password_id]["password"],
                          rows[self.password_id]["google"]), ("ana@example.bg", "yes", "-"))
        self.assertEqual((rows[silent]["password"], rows[silent]["google"]), ("no", "yes"))
        # Subjects are never printed, and neither is a hash or a session key.
        for secret in ("SUBJECT-NEVER-PRINTED", "ANOTHER-SUBJECT", self.by_id(self.password_id)["password_hash"],
                       self.by_id(self.google_id)["session_key"]):
            self.assertNotIn(secret, self.out)


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
        # A database from before the identity storage simply has no Google sign-in.
        self.assertEqual(lines[0].split("\t")[5:7], ["password", "google"])
        self.assertEqual([l.split("\t")[5:7] for l in lines[1:]], [["yes", "-"], ["yes", "-"]])


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


class ClearThrottleTest(CliTestCase):
    SECRET = "s3cr3t-application-key-of-at-least-32-chars"
    TARGET = "ana@example.bg"
    OTHER = "boris@example.bg"
    NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def setUp(self):
        super().setUp()
        os.environ["DZI_SECRET_KEY"] = self.SECRET
        conn = sqlite3.connect(self.db)
        conn.executescript(
            (ROOT / "web" / "migrations" / "013_login_throttle.sql").read_text(encoding="utf-8"))
        # Lock both identifiers out exactly as the login route does.
        for email in (self.TARGET, self.OTHER):
            for _ in range(login_throttle.MAX_FAILURES):
                login_throttle.record_failure(conn, self.key(email), self.NOW)
        conn.commit()
        conn.close()

    def key(self, email, secret=None):
        return login_throttle.identifier_hash(secret or self.SECRET, accounts.throttle_identifier(email))

    def rows(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT * FROM login_throttle ORDER BY identifier_hash").fetchall()
        finally:
            conn.close()

    def blocked(self, email):
        conn = sqlite3.connect(self.db)
        try:
            return login_throttle.is_blocked(conn, self.key(email), self.NOW)
        finally:
            conn.close()

    def test_clears_only_the_requested_identifier(self):
        self.assertTrue(self.blocked(self.TARGET))
        self.assertEqual(self.run_cli("clear-throttle", "--email", self.TARGET), 0, self.err)
        self.assertFalse(self.blocked(self.TARGET))
        self.assertTrue(self.blocked(self.OTHER))
        self.assertEqual([row[0] for row in self.rows()], [self.key(self.OTHER)])
        self.assertEqual(self.out.strip(), f"login throttle cleared for {self.TARGET}")

    def test_email_is_normalized_like_the_login_form(self):
        self.assertEqual(self.run_cli("clear-throttle", "--email", "  ANA@Example.BG "), 0, self.err)
        self.assertFalse(self.blocked(self.TARGET))

    def test_account_need_not_exist_and_a_missing_row_is_not_an_error(self):
        self.assertEqual(self.count(), 0)  # no users at all
        before = self.rows()
        for _ in range(2):  # idempotent
            self.assertEqual(self.run_cli("clear-throttle", "--email", "nobody@example.bg"), 0, self.err)
        self.assertEqual(self.rows(), before)

    def test_missing_or_empty_secret_fails_without_mutation(self):
        before = self.rows()
        for value in (None, ""):
            with self.subTest(secret=value):
                os.environ.pop("DZI_SECRET_KEY", None)
                if value is not None:
                    os.environ["DZI_SECRET_KEY"] = value
                self.assertEqual(self.run_cli("clear-throttle", "--email", self.TARGET), 1)
                self.assertIn("DZI_SECRET_KEY is not set", self.err)
                self.assertNotIn("Traceback", self.err)
                self.assertEqual(self.out, "")
                self.assertEqual(self.rows(), before)

    def test_there_is_no_fallback_key(self):
        # A different secret addresses a different row: nothing of the real lockout is touched.
        before = self.rows()
        os.environ["DZI_SECRET_KEY"] = "another-secret-entirely-also-32-chars-long"
        self.assertEqual(self.run_cli("clear-throttle", "--email", self.TARGET), 0)
        self.assertEqual(self.rows(), before)
        source = (ROOT / "src" / "manage_accounts.py").read_text(encoding="utf-8")
        self.assertNotIn("token_urlsafe", source)
        self.assertNotIn("import secrets", source)

    def test_malformed_email_fails_without_mutation(self):
        before = self.rows()
        for bad in ("", "not-an-email", "a@@b.bg", "a b@c.bg", "x" * 300 + "@b.bg"):
            with self.subTest(email=bad[:20]):
                self.assertEqual(self.run_cli("clear-throttle", "--email", bad), 1)
                self.assertEqual(self.err.strip(), "error: invalid email")
                self.assertEqual(self.rows(), before)

    def test_database_without_the_throttle_table_is_reported(self):
        bare = self.dir / "users-only.db"
        conn = sqlite3.connect(bare)
        conn.executescript((ROOT / "web" / "migrations" / "012_users.sql").read_text(encoding="utf-8"))
        conn.close()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = manage_accounts.main(["--db", str(bare), "clear-throttle", "--email", self.TARGET])
        self.assertEqual(code, 1)
        self.assertIn("migration 013 required", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        conn = sqlite3.connect(bare)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertNotIn("login_throttle", tables)  # never created by the CLI

    def test_database_targeting_safety_is_unchanged(self):
        cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, cwd)
        before = self.rows()
        self.assertEqual(self.run_cli("clear-throttle", "--email", self.TARGET, db=False), 1)
        self.assertIn("--db", self.err)
        missing = self.dir / "nope.db"
        with redirect_stderr(io.StringIO()):
            self.assertEqual(
                manage_accounts.main(["--db", str(missing), "clear-throttle", "--email", self.TARGET]), 1)
        self.assertFalse(missing.exists())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["t.db"])
        self.assertEqual(self.rows(), before)

    def test_rollback_when_clearing_fails(self):
        before = self.rows()
        with mock.patch.object(login_throttle, "clear", side_effect=sqlite3.OperationalError("boom")):
            self.assertEqual(self.run_cli("clear-throttle", "--email", self.TARGET), 1)
        self.assertNotIn("Traceback", self.err)
        self.assertEqual(self.rows(), before)

    def test_output_never_contains_the_secret_or_the_stored_key(self):
        outputs = []
        for argv in (("clear-throttle", "--email", self.TARGET),
                     ("clear-throttle", "--email", "nobody@example.bg"),
                     ("clear-throttle", "--email", "not-an-email"),
                     ("list",)):
            self.run_cli(*argv)
            outputs.append(self.out + self.err)
        os.environ.pop("DZI_SECRET_KEY")
        self.run_cli("clear-throttle", "--email", self.OTHER)
        outputs.append(self.out + self.err)
        text = "\n".join(outputs)
        for leaked in (self.SECRET, self.key(self.TARGET), self.key(self.OTHER),
                       self.key("nobody@example.bg"), "not-an-email"):
            self.assertNotIn(leaked, text)

    def test_command_takes_no_secret_hash_or_password_argument(self):
        for flag in ("--secret", "--secret-key", "--hash", "--key", "--password", "--password-stdin"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                manage_accounts.main(["--db", str(self.db), "clear-throttle", "--email", self.TARGET, flag, "x"])

    def test_throttle_sql_and_hashing_are_not_duplicated_in_the_cli(self):
        source = (ROOT / "src" / "manage_accounts.py").read_text(encoding="utf-8")
        for forbidden in ("hmac", "sha256", "DELETE FROM login_throttle", "identifier_hash ="):
            self.assertNotIn(forbidden, source)
        self.assertIn("login_throttle.clear(", source)
        self.assertIn("login_throttle.identifier_hash(", source)


class ImportBoundaryTest(unittest.TestCase):
    def test_no_flask_coupling(self):
        source = (ROOT / "src" / "manage_accounts.py").read_text(encoding="utf-8")
        self.assertIn("web.services", source)
        for forbidden in ("flask", "web.app", "web.blueprints", "hashlib", "generate_password_hash"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()

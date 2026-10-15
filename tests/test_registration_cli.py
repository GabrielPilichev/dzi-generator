"""Registration PR 4: the operator commands of ``src.manage_accounts`` (design 3.8, 6.5, 6.6).

``registrations-cleanup [--check]``, ``registrations-clock-accept``,
``clear-registration-limit`` and the two new ``list`` columns, against temporary
databases only. The commands read the real clock; rows are dated relative to it.
"""

import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.registration_support import EMAIL, KEY, password_hash, stamp, student
from tests.support import ROOT, build_test_database
from src import manage_accounts
from web.services import accounts, lifecycle
from web.services import registration as registration_service


class RegistrationCliTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="registration-cli-")
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.db = build_test_database(self.dir / "registration.db")
        environ = mock.patch.dict(os.environ, {"DZI_SECRET_KEY": KEY})
        environ.start()
        self.addCleanup(environ.stop)

    def run_cli(self, *argv, db=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = manage_accounts.main(["--db", str(db or self.db), *argv])
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def connect(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def submit(self, *, minutes_ago=0, email=EMAIL):
        """A pending registration created ``minutes_ago`` minutes before now, by the service."""
        moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = registration_service.create_pending(
                conn, cleaned=student(email), account_type="student", password_hash=password_hash(),
                previous_binding=None, secret_key=KEY, clock=lambda: moment, sender_busy=False)
            conn.commit()
        return result

    def rows(self, sql, *params):
        with closing(self.connect()) as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    def digest(self):
        with closing(self.connect()) as conn:
            return [tuple(row) for table in ("pending_registrations", "registration_sends", "registration_control",
                                             "users", "user_profiles")
                    for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]

    def set_mark(self, moment):
        with closing(self.connect()) as conn:
            conn.execute("UPDATE registration_control SET high_water_at = ? WHERE id = 1", (stamp(moment),))
            conn.commit()


class CleanupCommandTest(RegistrationCliTestCase):
    def test_scrubs_overdue_rows_without_a_secret(self):
        # The first is overdue, the second is not. (A submission's own inline cleanup
        # would scrub an overdue row, so the second is created before the first's expiry.)
        overdue = self.submit(minutes_ago=31)
        fresh = self.submit(minutes_ago=29, email="fresh@example.bg")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.run_cli("registrations-cleanup"), 0, self.err)
        self.assertIn("1 pending registration(s) expired and scrubbed", self.out)
        states = {row["id"]: row for row in self.rows("SELECT * FROM pending_registrations")}
        self.assertEqual(states[overdue.pending_id]["state"], "expired")
        self.assertIsNone(states[overdue.pending_id]["email"])
        self.assertEqual(states[fresh.pending_id]["state"], "pending")
        self.assertNotIn(EMAIL, self.out + self.err)

    def test_check_reads_only_and_fails_for_a_row_older_than_an_hour(self):
        self.submit(minutes_ago=59)
        before = self.digest()
        self.assertEqual(self.run_cli("registrations-cleanup", "--check"), 0, self.out + self.err)
        self.assertIn("OK", self.out)
        self.submit(minutes_ago=61, email="old@example.bg")
        before = self.digest()
        self.assertEqual(self.run_cli("registrations-cleanup", "--check"), 1)
        self.assertIn("FAIL", self.out)
        self.assertIn("1 pending registration(s) older than an hour", self.out)
        self.assertEqual(self.digest(), before)
        # A cleanup run then ends the failure.
        self.assertEqual(self.run_cli("registrations-cleanup"), 0)
        self.assertEqual(self.run_cli("registrations-cleanup", "--check"), 0)

    def test_refuses_while_a_restore_lock_exists(self):
        self.submit(minutes_ago=31)
        lock = Path(lifecycle.restore_lock_path(self.db))
        lock.write_text("restore in progress\n")
        before = self.digest()
        for argv in (("registrations-cleanup",), ("registrations-cleanup", "--check")):
            self.assertEqual(self.run_cli(*argv), 1)
            self.assertIn("restore", self.err)
        self.assertEqual(self.digest(), before)

    def test_a_clock_behind_the_mark_changes_nothing_and_fails(self):
        self.submit(minutes_ago=31)
        self.set_mark(datetime.now(timezone.utc) + timedelta(hours=1))
        before = self.digest()
        self.assertEqual(self.run_cli("registrations-cleanup"), 1)
        self.assertIn("clock is behind", self.err)
        self.assertEqual(self.digest(), before)

    def test_a_held_write_lock_fails_the_run_and_scrubs_nothing(self):
        self.submit(minutes_ago=31)
        before = self.digest()
        holder = sqlite3.connect(self.db)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        real_connect = sqlite3.connect
        with mock.patch.object(manage_accounts.sqlite3, "connect",
                               lambda *args, **kwargs: real_connect(*args, **{**kwargs, "timeout": 0.1})):
            self.assertEqual(self.run_cli("registrations-cleanup"), 1)
        self.assertIn("locked", self.err)
        holder.rollback()
        self.assertEqual(self.digest(), before)


class ClockAcceptTest(RegistrationCliTestCase):
    def test_lowers_the_mark_to_the_present_and_reopens_submissions(self):
        self.submit()
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        self.set_mark(future)
        with closing(self.connect()) as conn:
            self.assertEqual(registration_service.availability(conn, datetime.now(timezone.utc), sender_busy=False),
                             registration_service.Availability.CLOCK_BEHIND)
        self.assertEqual(self.run_cli("registrations-clock-accept"), 0, self.err)
        self.assertIn(f"was {stamp(future)} UTC", self.out)
        mark = self.rows("SELECT * FROM registration_control")[0]
        self.assertLess(mark["high_water_at"], stamp(future))
        self.assertIsNone(mark["hold_reason"])
        with closing(self.connect()) as conn:
            self.assertEqual(registration_service.availability(conn, datetime.now(timezone.utc), sender_busy=False),
                             registration_service.Availability.AVAILABLE)

    def test_without_a_mark_nothing_is_written_and_a_hold_is_not_touched(self):
        before = self.digest()
        self.assertEqual(self.run_cli("registrations-clock-accept"), 0)
        self.assertIn("nothing to accept", self.out)
        self.assertEqual(self.digest(), before)
        self.submit()
        with closing(self.connect()) as conn:
            conn.execute("UPDATE registration_control SET hold_reason = 'restore', hold_set_at = high_water_at")
            conn.commit()
        self.assertEqual(self.run_cli("registrations-clock-accept"), 0)
        self.assertEqual(self.rows("SELECT hold_reason FROM registration_control")[0]["hold_reason"], "restore")


class ClearLimitTest(RegistrationCliTestCase):
    def test_says_the_consequence_first_and_touches_only_that_address(self):
        for minutes in (5, 4, 3, 2, 1):
            self.submit(minutes_ago=minutes)
        other = self.submit(email="other@example.bg")
        pending_before = self.rows("SELECT * FROM pending_registrations")
        with closing(self.connect()) as conn:
            now = datetime.now(timezone.utc)
            self.assertTrue(registration_service.address_capped(conn, registration_service.email_hash(KEY, EMAIL), now))
        self.assertEqual(self.run_cli("clear-registration-limit", "--email", " ANA@example.bg "), 0, self.err)
        note, result = self.out.index("8 more codes"), self.out.index("record(s) deleted")
        self.assertLess(note, result)
        self.assertIn("5 record(s) deleted", self.out)
        self.assertEqual(self.rows("SELECT * FROM pending_registrations"), pending_before)
        remaining = self.rows("SELECT * FROM registration_sends")
        self.assertEqual([row["id"] for row in remaining], [other.send_id])
        with closing(self.connect()) as conn:
            self.assertFalse(registration_service.address_capped(
                conn, registration_service.email_hash(KEY, EMAIL), datetime.now(timezone.utc)))

    def test_needs_the_secret_and_a_valid_email_and_never_prints_a_key(self):
        self.submit()
        before = self.digest()
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.run_cli("clear-registration-limit", "--email", EMAIL), 1)
        self.assertIn("DZI_SECRET_KEY is not set", self.err)
        self.assertEqual(self.run_cli("clear-registration-limit", "--email", "not-an-email"), 1)
        self.assertEqual(self.err.strip(), "error: invalid email")
        self.assertEqual(self.digest(), before)
        self.run_cli("clear-registration-limit", "--email", EMAIL)
        for secret in (KEY, registration_service.email_hash(KEY, EMAIL)):
            self.assertNotIn(secret, self.out + self.err)


class SchemaTest(RegistrationCliTestCase):
    def test_a_database_without_the_registration_storage_is_reported(self):
        bare = self.dir / "users-only.db"
        with closing(sqlite3.connect(bare)) as conn:
            conn.executescript((ROOT / "web" / "migrations" / "012_users.sql").read_text(encoding="utf-8"))
        for argv in (("registrations-cleanup",), ("registrations-clock-accept",),
                     ("clear-registration-limit", "--email", EMAIL)):
            with self.subTest(command=argv[0]):
                self.assertEqual(self.run_cli(*argv, db=bare), 1)
                self.assertIn("migrations 021 and 022 required", self.err)
                self.assertNotIn("Traceback", self.err)
        # list still works there: the new columns are "-".
        with closing(sqlite3.connect(bare)) as conn:
            accounts.create_user(conn, display_name="Учител Тестов", email="t@example.bg", role="teacher")
            conn.commit()
        self.assertEqual(self.run_cli("list", db=bare), 0, self.err)
        header, row = (line.split("\t") for line in self.out.splitlines()[:2])
        self.assertEqual(header[-2:], ["verified", "type"])
        self.assertEqual(row[-2:], ["-", "-"])

    def test_list_shows_the_verification_date_and_declared_type(self):
        result = self.submit()
        code = next(part for part in result.message.text.split() if part.isdigit() and len(part) == 6)
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            activated = registration_service.verify(
                conn, registration_service.binding_hash(KEY, result.token), code, secret_key=KEY,
                clock=lambda: datetime.now(timezone.utc))
            conn.commit()
            accounts.create_user(conn, display_name="Учител Тестов", email="t@example.bg", role="teacher")
            conn.commit()
        self.assertEqual(activated.outcome, registration_service.Outcome.ACTIVATED)
        self.assertEqual(self.run_cli("list"), 0, self.err)
        lines = [line.split("\t") for line in self.out.splitlines()]
        rows = {line[1]: dict(zip(lines[0], line)) for line in lines[1:]}
        self.assertEqual(rows[EMAIL]["type"], "student")
        self.assertRegex(rows[EMAIL]["verified"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        # A CLI account: never verified by registration, no profile, and still a normal account.
        self.assertEqual((rows["t@example.bg"]["verified"], rows["t@example.bg"]["type"]), ("-", "-"))


class ProfileMarkOperatorTest(RegistrationCliTestCase):
    """PR 6, test plan J11 and J12: no command marks anybody, and ``set-role`` writes neither a
    profile nor the mark (the mark is written by Google onboarding only)."""

    MARK = "2026-10-15 09:00:00"
    COLUMNS = ("id", "email", "display_name", "role", "password_hash", "is_active", "session_key", "email_verified_at",
               "profile_required_at")

    def make(self, email, *, role="student", marked=False, profile=False):
        with closing(self.connect()) as conn:
            user_id = accounts.create_user(conn, display_name="Ана Иванова", email=email, role=role,
                                           password="a-long-enough-password")
            if marked:
                conn.execute("UPDATE users SET profile_required_at = ? WHERE id = ?", (self.MARK, user_id))
            if profile:
                conn.execute("INSERT INTO user_profiles (user_id, account_type, first_name, last_name, grade, "
                             "grade_set_at) VALUES (?, 'student', 'Ана', 'Иванова', 9, ?)", (user_id, self.MARK))
            conn.commit()
        return user_id

    def snapshot(self):
        with closing(self.connect()) as conn:
            return ([tuple(r) for r in conn.execute(f"SELECT {', '.join(self.COLUMNS)} FROM users ORDER BY id")],
                    [tuple(r) for r in conn.execute("SELECT * FROM user_profiles ORDER BY user_id")],
                    [tuple(r) for r in conn.execute("SELECT * FROM teacher_subjects")])

    def test_set_role_changes_the_role_and_the_key_and_nothing_about_the_profile_or_the_mark(self):  # J12
        marked, profiled = self.make("marked@example.bg", marked=True), self.make("profiled@example.bg", profile=True)
        for email, user_id in (("marked@example.bg", marked), ("profiled@example.bg", profiled)):
            for role in ("teacher", "student", "admin", "student"):
                before = self.snapshot()
                self.assertEqual(self.run_cli("set-role", "--email", email, "--role", role), 0, self.err)
                after = self.snapshot()
                self.assertEqual(after[1:], before[1:])      # profiles and subjects: byte for byte
                for old, new in zip(before[0], after[0]):
                    keep = [i for i, name in enumerate(self.COLUMNS) if name not in ("role", "session_key")]
                    self.assertEqual([new[i] for i in keep], [old[i] for i in keep])

    def test_no_command_marks_an_existing_account_and_none_is_named_for_it(self):  # J11
        self.make("a@example.bg"), self.make("b@example.bg", role="teacher")
        before = self.snapshot()
        for argv in (("list",), ("sign-out", "--email", "a@example.bg"), ("disable", "--email", "a@example.bg"),
                     ("enable", "--email", "a@example.bg"), ("set-role", "--email", "a@example.bg", "--role", "student"),
                     ("registrations-cleanup",), ("registrations-clock-accept",),
                     ("clear-registration-limit", "--email", "a@example.bg")):
            self.run_cli(*argv)
        self.assertEqual([row[-1] for row in self.snapshot()[0]], [None, None])
        for name in ("require-profile", "mark-profile", "require_profile"):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                manage_accounts.main(["--db", str(self.db), name, "--email", "a@example.bg"])
        self.assertEqual(self.snapshot()[1:], before[1:])
        source = (ROOT / "src" / "manage_accounts.py").read_text(encoding="utf-8")
        self.assertNotIn("profile_required_at", source)
        self.assertNotIn("require-profile", source)

    def test_list_is_unchanged_by_the_mark(self):
        self.make("marked@example.bg", marked=True)
        self.assertEqual(self.run_cli("list"), 0, self.err)
        header = self.out.splitlines()[0].split("\t")
        self.assertEqual(header[-2:], ["verified", "type"])
        self.assertNotIn(self.MARK, self.out)


if __name__ == "__main__":
    unittest.main()

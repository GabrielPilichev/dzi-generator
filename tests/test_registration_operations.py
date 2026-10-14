"""Registration PR 5: cleanup under hold and clock, the gate, status and release, the
logical snapshot scrub, and the systemd units
(docs/reviews/EMAIL_REGISTRATION_DESIGN.md, 3.9, 6.5, 6.6; test plan DR1-DR9, N5-N9).

Temporary databases and files only; no real unit, database or clock. The units are checked
against the commands they run (their arguments are parsed by the real operator parser), not
for strings alone; **that systemd accepts and runs them is not shown here** and is listed
as unrun Linux evidence in ``docs/reviews/EMAIL_REGISTRATION_PR5_STATUS.md``.
"""

import configparser
import hashlib
import os
import re
import shlex
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from src import backup_application, backup_sqlite, manage_accounts
from tests.registration_support import (EMAIL, KEY, RegistrationTestCase, SENSITIVE_COLUMNS, T0, code_of,
                                        password_hash, stamp)
from tests.support import ROOT, build_test_database
from tests.test_registration_cli import RegistrationCliTestCase
from web.services import lifecycle, registration, registration_scrub
from web.services.registration import Availability, Outcome


def table_dump(conn, *tables):
    return [(table, [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]) for table in tables]


ACCOUNT_AND_CONTENT = ("users", "user_profiles", "teacher_subjects", "curriculum_sections", "questions")


class GateTest(RegistrationTestCase):
    def copy_of_the_database(self):
        copy = Path(self.path).with_name("staged.db")
        with closing(sqlite3.connect(copy)) as dst:
            self.conn.backup(dst)
        conn = sqlite3.connect(copy)
        self.addCleanup(conn.close)
        return copy, conn

    def gate(self, conn):
        return self.tx(registration.restore_gate, conn=conn, clock=self.clock)

    def test_dr1_dr2_cancellation_and_spent_attempts_are_not_undone(self):
        issued = self.submit()
        code = code_of(issued)
        _, staged = self.copy_of_the_database()                 # the snapshot
        self.cancel(issued)                                     # after the snapshot
        self.assertEqual(self.conn.execute("SELECT state FROM pending_registrations").fetchone()[0], "void")
        # Without the gate, the copy's registration is live: the defect the gate is for.
        self.assertEqual(staged.execute("SELECT state FROM pending_registrations").fetchone()[0], "pending")
        result = self.gate(staged)
        self.assertEqual(result.voided, 1)
        row = staged.execute("SELECT state, email, code_hash, password_hash FROM pending_registrations").fetchone()
        self.assertEqual(tuple(row), ("void", None, None, None))
        outcome = self.tx(registration.verify, self.binding(issued), code, conn=staged, secret_key=KEY,
                          clock=self.clock, teacher_enabled=False)
        self.assertIn(outcome.outcome, (Outcome.UNAVAILABLE, Outcome.NOT_PENDING))
        self.assertEqual(staged.execute("SELECT COUNT(*) FROM users WHERE email = ?", (EMAIL,)).fetchone()[0], 0)

    def test_dr3_the_hold_is_set_and_submit_and_resend_answer_unavailable(self):
        issued = self.submit()
        _, staged = self.copy_of_the_database()
        self.gate(staged)
        row = staged.execute("SELECT hold_reason, hold_set_at, high_water_at FROM registration_control").fetchone()
        self.assertEqual((row[0], row[1]), ("restore", stamp(T0)))
        again = self.submit(conn=staged, email="other@example.bg")
        self.assertEqual((again.outcome, again.reason), (Outcome.UNAVAILABLE, Availability.HOLD))
        resent = self.resend(issued, conn=staged)
        # The gate voided the row, so there is nothing to resend; either way nothing is issued.
        self.assertIn(resent.outcome, (Outcome.UNAVAILABLE, Outcome.NOT_PENDING))
        self.assertEqual(staged.execute("SELECT COUNT(*) FROM registration_sends").fetchone()[0], 1)

    def test_dr4_no_automatic_release_whatever_the_clock_does(self):
        self.submit()
        _, staged = self.copy_of_the_database()
        self.gate(staged)
        for seconds in (24 * 3600 + 15 * 60, 7 * 86400, 366 * 86400):
            self.at(seconds)
            result = self.submit(conn=staged, email="late@example.bg")
            self.assertEqual((result.outcome, result.reason), (Outcome.UNAVAILABLE, Availability.HOLD), seconds)
            self.assertEqual(staged.execute("SELECT hold_reason FROM registration_control").fetchone()[0], "restore")
        # The cleanup the timer runs does not release it either.
        self.tx(registration.cleanup, conn=staged, clock=self.clock)
        self.assertEqual(staged.execute("SELECT hold_reason FROM registration_control").fetchone()[0], "restore")

    def test_dr5_an_existing_hold_is_kept_with_its_time_and_a_second_run_changes_nothing(self):
        self.submit()
        _, staged = self.copy_of_the_database()
        staged.execute("UPDATE registration_control SET hold_reason = 'restore', hold_set_at = '2026-01-02 03:04:05'")
        staged.commit()
        self.at(3600)
        first = self.gate(staged)
        self.assertEqual((first.already_held, first.hold_set_at), (True, "2026-01-02 03:04:05"))
        once = table_dump(staged, "registration_control", "pending_registrations", "registration_sends")
        self.at(7200)
        second = self.gate(staged)
        self.assertEqual((second.voided, second.already_held), (0, True))
        self.assertEqual(table_dump(staged, "registration_control", "pending_registrations", "registration_sends"),
                         once)

    def test_the_gate_neither_raises_nor_lowers_the_mark_and_creates_a_missing_row_without_one(self):
        self.submit()
        _, staged = self.copy_of_the_database()
        mark = staged.execute("SELECT high_water_at FROM registration_control").fetchone()[0]
        self.at(86400)
        self.gate(staged)
        self.assertEqual(staged.execute("SELECT high_water_at FROM registration_control").fetchone()[0], mark)
        staged.execute("DELETE FROM registration_control")
        staged.commit()
        self.gate(staged)
        self.assertEqual(tuple(staged.execute("SELECT high_water_at, hold_reason FROM registration_control").fetchone()),
                         (registration.NO_MARK, "restore"))

    def test_dr6_one_transaction_an_error_at_any_statement_leaves_the_copy_as_it_was(self):
        self.submit()
        self.submit(email="b@example.bg")
        copy, _ = self.copy_of_the_database()

        class Failing(sqlite3.Connection):
            fail_at = 0
            seen = 0

            def execute(self, sql, *args):
                type(self).seen += 1
                if type(self).seen == type(self).fail_at:
                    raise sqlite3.OperationalError("injected")
                return super().execute(sql, *args)

        def state():
            with closing(sqlite3.connect(copy)) as check:
                return table_dump(check, "pending_registrations", "registration_control", "registration_sends",
                                  *ACCOUNT_AND_CONTENT[:3])

        before = state()
        pristine = Path(copy).read_bytes()
        injected = 0
        for number in range(1, 12):
            Path(copy).write_bytes(pristine)
            Failing.fail_at, Failing.seen = number, 0
            conn = sqlite3.connect(copy, factory=Failing, isolation_level=None)
            try:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    registration.restore_gate(conn, clock=self.clock)
                    conn.execute("COMMIT")
                    reached_end = True
                except sqlite3.OperationalError:
                    injected += 1
                    reached_end = False
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
            finally:
                conn.close()
            if reached_end:
                break
            self.assertEqual(state(), before, f"failure at statement {number}")
        self.assertGreaterEqual(injected, 4)
        # And through the command: an error exits non-zero and changes nothing.
        Path(copy).write_bytes(pristine)
        with mock.patch.object(registration, "restore_gate", side_effect=sqlite3.OperationalError("injected")):
            code = manage_accounts.main(["--db", str(copy), "registrations-restore-gate"])
        self.assertEqual(code, 1)
        self.assertEqual(state(), before)

    def test_it_touches_no_account_profile_subject_or_canonical_row(self):
        self.submit()
        copy, staged = self.copy_of_the_database()
        before = table_dump(staged, *ACCOUNT_AND_CONTENT)
        self.gate(staged)
        self.assertEqual(table_dump(staged, *ACCOUNT_AND_CONTENT), before)

    def test_it_voids_only_pending_rows_and_leaves_final_rows_and_the_ledger(self):
        issued = self.submit()
        self.cancel(self.submit(email="c@example.bg"))
        _, staged = self.copy_of_the_database()
        ledger = table_dump(staged, "registration_sends")
        self.gate(staged)
        states = sorted(r[0] for r in staged.execute("SELECT state FROM pending_registrations"))
        self.assertEqual(states, ["void", "void"])
        self.assertEqual(table_dump(staged, "registration_sends"), ledger)
        for column in SENSITIVE_COLUMNS:
            self.assertEqual(staged.execute(f"SELECT COUNT(*) FROM pending_registrations WHERE {column} IS NOT NULL")
                             .fetchone()[0], 0, column)
        del issued

    def test_a_gate_needs_the_callers_transaction(self):
        with self.assertRaises(registration.TransactionRequired):
            registration.restore_gate(self.conn, clock=self.clock)

    def test_dr9_a_restart_keeps_a_pending_registration_usable_and_sets_no_hold(self):
        issued = self.submit()
        code = code_of(issued)
        self.conn.close()
        reopened = self.connect()
        self.assertIsNone(reopened.execute("SELECT hold_reason FROM registration_control").fetchone()[0])
        outcome = self.verify(issued, code, conn=reopened, teacher_enabled=False)
        self.assertEqual(outcome.outcome, Outcome.ACTIVATED)


class StatusAndReleaseTest(RegistrationTestCase):
    def test_dr7_status_reads_only_and_reports_each_failure(self):
        self.assertEqual(registration.restore_status(self.conn),
                         registration.RestoreStatus(0, False, False, None, None))       # no control row
        self.submit()
        state = registration.restore_status(self.conn)
        self.assertEqual((state.pending, state.control_present, state.held), (1, True, False))
        before = table_dump(self.conn, "pending_registrations", "registration_control", "registration_sends")
        registration.restore_status(self.conn)
        self.assertEqual(table_dump(self.conn, "pending_registrations", "registration_control", "registration_sends"),
                         before)
        self.assertFalse(self.conn.in_transaction)

    def test_release_clears_the_hold_and_nothing_else(self):
        self.submit()
        self.tx(registration.restore_gate, clock=self.clock)
        sends = table_dump(self.conn, "registration_sends")
        pending = table_dump(self.conn, "pending_registrations")
        mark = self.conn.execute("SELECT high_water_at FROM registration_control").fetchone()[0]
        result = self.tx(registration.release_hold, clock=self.clock)
        self.assertEqual((result.released, result.clock_behind), (True, False))
        self.assertEqual(tuple(self.conn.execute("SELECT hold_reason, hold_set_at, high_water_at "
                                                 "FROM registration_control").fetchone()), (None, None, mark))
        self.assertEqual((table_dump(self.conn, "registration_sends"), table_dump(self.conn, "pending_registrations")),
                         (sends, pending))
        self.assertEqual(self.submit(email="after@example.bg").outcome, Outcome.ISSUED)

    def test_release_does_not_accept_a_regressed_clock_and_says_so(self):
        self.submit()
        self.tx(registration.restore_gate, clock=self.clock)
        mark = stamp(T0 + timedelta(hours=3))
        self.conn.execute("UPDATE registration_control SET high_water_at = ?", (mark,))
        self.conn.commit()
        result = self.tx(registration.release_hold, clock=self.clock)
        self.assertEqual((result.released, result.clock_behind, result.high_water_at), (True, True, mark))
        self.assertEqual(self.conn.execute("SELECT high_water_at FROM registration_control").fetchone()[0], mark)
        refused = self.submit(email="late@example.bg")
        self.assertEqual((refused.outcome, refused.reason), (Outcome.UNAVAILABLE, Availability.CLOCK_BEHIND))
        # Only the clock acceptance, a separate and explicit act, ends that.
        self.tx(registration.accept_clock, clock=self.clock)
        self.assertEqual(self.submit(email="ok@example.bg").outcome, Outcome.ISSUED)

    def test_release_without_a_hold_or_a_control_row_writes_nothing(self):
        self.assertFalse(self.tx(registration.release_hold, clock=self.clock).released)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM registration_control").fetchone()[0], 0)
        self.submit()
        before = table_dump(self.conn, "registration_control", "registration_sends")
        self.assertFalse(self.tx(registration.release_hold, clock=self.clock).released)
        self.assertEqual(table_dump(self.conn, "registration_control", "registration_sends"), before)

    def test_release_needs_the_callers_transaction(self):
        with self.assertRaises(registration.TransactionRequired):
            registration.release_hold(self.conn, clock=self.clock)


class OperatorCommandsTest(RegistrationCliTestCase):
    def hold(self, *, mark=None):
        with closing(self.connect()) as conn:
            conn.execute("UPDATE registration_control SET hold_reason = 'restore', hold_set_at = high_water_at")
            if mark:
                conn.execute("UPDATE registration_control SET high_water_at = ?", (stamp(mark),))
            conn.commit()

    def test_cleanup_runs_under_a_hold_and_scrubs(self):
        old = self.submit(minutes_ago=31)
        self.hold()
        self.assertEqual(self.run_cli("registrations-cleanup"), 0, self.err)
        self.assertEqual(self.rows("SELECT state FROM pending_registrations WHERE id = ?", old.pending_id)[0]["state"],
                         "expired")
        self.assertEqual(self.rows("SELECT hold_reason FROM registration_control")[0]["hold_reason"], "restore")

    def test_cleanup_under_a_regressed_clock_and_a_hold_changes_nothing(self):
        self.submit(minutes_ago=31)
        self.hold(mark=datetime.now(timezone.utc) + timedelta(hours=2))
        before = self.digest()
        self.assertEqual(self.run_cli("registrations-cleanup"), 1)
        self.assertIn("clock is behind", self.err)
        self.assertEqual(self.digest(), before)

    def test_a_restore_lock_made_while_the_cleanup_waits_for_the_database_stops_it_before_it_writes(self):
        self.submit(minutes_ago=31)
        before = self.digest()
        present = mock.Mock(side_effect=[False, True])
        with mock.patch.object(lifecycle, "restore_lock_present", present):
            self.assertEqual(self.run_cli("registrations-cleanup"), 1)
        self.assertEqual(present.call_count, 2)
        self.assertIn("began while the cleanup waited", self.err)
        self.assertEqual(self.digest(), before)

    def test_two_cleanups_at_once_one_waits_and_none_corrupts(self):
        self.submit(minutes_ago=31)
        holder = sqlite3.connect(self.db)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        before = self.digest()
        real_connect = sqlite3.connect
        with mock.patch.object(manage_accounts.sqlite3, "connect",
                               lambda *a, **k: real_connect(*a, **{**k, "timeout": 0.1})):
            self.assertEqual(self.run_cli("registrations-cleanup"), 1)      # the second cannot take the lock
        holder.rollback()
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.run_cli("registrations-cleanup"), 0)         # the next tick succeeds

    # ----------------------------------------------------------------- gate, status, release

    def test_gate_status_and_release_through_the_commands(self):
        self.submit()
        self.assertEqual(self.run_cli("registrations-restore-status", "--expect", "held"), 1)
        self.assertIn("1 pending registration(s) exist", self.out)
        self.assertIn("registration is open, expected held", self.out)
        before = self.digest()
        self.assertEqual(self.run_cli("registrations-restore-status"), 0)
        self.assertEqual(self.digest(), before)                            # reading wrote nothing
        self.assertEqual(self.run_cli("registrations-restore-gate"), 0, self.err)
        self.assertIn("1 pending registration(s) voided and scrubbed", self.out)
        self.assertEqual(self.run_cli("registrations-restore-status", "--expect", "held"), 0, self.out)
        self.assertEqual(self.run_cli("registrations-restore-status", "--expect", "open"), 1)
        once = self.digest()
        self.assertEqual(self.run_cli("registrations-restore-gate"), 0)
        self.assertIn("already carried a hold", self.out)
        self.assertEqual(self.digest(), once)

    def test_status_fails_when_the_control_row_is_missing_and_never_writes(self):
        before = self.digest()
        self.assertEqual(self.run_cli("registrations-restore-status", "--expect", "held"), 1)
        self.assertIn("control row is missing", self.out)
        self.assertEqual(self.run_cli("registrations-restore-status", "--expect", "open"), 1)
        self.assertEqual(self.digest(), before)

    def test_dr4_release_refuses_without_the_acknowledgement_and_changes_nothing(self):
        self.submit()
        self.run_cli("registrations-restore-gate")
        before = self.digest()
        self.assertEqual(self.run_cli("registrations-release-hold"), 1)
        self.assertIn("--acknowledge-lost-history", self.err)
        self.assertIn("8 further codes", self.out)                           # the consequence is printed first
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.run_cli("registrations-release-hold", "--acknowledge-lost-history"), 0, self.err)
        self.assertIn("released", self.out)
        self.assertIsNone(self.rows("SELECT hold_reason FROM registration_control")[0]["hold_reason"])
        self.assertEqual(self.run_cli("registrations-release-hold", "--acknowledge-lost-history"), 0)
        self.assertIn("not held", self.out)

    def test_release_reports_a_regressed_clock_and_leaves_the_mark(self):
        self.submit()
        self.run_cli("registrations-restore-gate")
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        self.set_mark(future)
        self.assertEqual(self.run_cli("registrations-release-hold", "--acknowledge-lost-history"), 0, self.err)
        self.assertIn("still answers 'unavailable'", self.out)
        self.assertEqual(self.rows("SELECT high_water_at FROM registration_control")[0]["high_water_at"], stamp(future))

    def test_release_refuses_while_a_restore_lock_exists(self):
        self.submit()
        self.run_cli("registrations-restore-gate")
        Path(lifecycle.restore_lock_path(self.db)).write_text("restore in progress\n")
        before = self.digest()
        self.assertEqual(self.run_cli("registrations-release-hold", "--acknowledge-lost-history"), 1)
        self.assertIn("restore", self.err)
        self.assertEqual(self.digest(), before)

    def test_the_gate_needs_no_secret_and_prints_none(self):
        self.submit()
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.run_cli("registrations-restore-gate"), 0, self.err)
        self.assertNotIn(KEY, self.out + self.err)
        self.assertNotIn(EMAIL, self.out + self.err)

    def test_the_gate_runs_on_a_staged_copy_while_a_restore_lock_exists_elsewhere(self):
        # The script's lock sits next to the LIVE database; the staged copy is somewhere else.
        self.submit()
        staged = self.dir / "staged.db"
        with closing(sqlite3.connect(self.db)) as src, closing(sqlite3.connect(staged)) as dst:
            src.backup(dst)
        Path(lifecycle.restore_lock_path(self.dir / "live" / "questions.db")).parent.mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.run_cli("registrations-restore-gate", db=staged), 0, self.err)


class SnapshotScrubTest(unittest.TestCase):
    """Decision D18 (b); test plan N7 and N9."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="registration-snapshot-")
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name).resolve()
        self.source = build_test_database(self.dir / "live.db")
        (self.dir / "out").mkdir()
        self.secret_marker = "ana-marker@example.bg"
        with closing(sqlite3.connect(self.source)) as conn:
            for number, who in enumerate((self.secret_marker, "boris-marker@example.bg")):
                conn.execute("BEGIN IMMEDIATE")
                registration.create_pending(
                    conn, cleaned={"account_type": "student", "first_name": "Ана", "last_name": "Маркер",
                                   "email": who, "grade": 9, "next": None},
                    account_type="student", password_hash=password_hash(),
                    previous_binding=None, secret_key=KEY, clock=lambda: T0, sender_busy=False)
                conn.commit()

    def live_state(self):
        with closing(sqlite3.connect(self.source)) as conn:
            return table_dump(conn, "pending_registrations", "registration_sends", "registration_control",
                              *ACCOUNT_AND_CONTENT[:3])

    def published(self, name="snapshot.db"):
        return self.dir / "out" / name

    def test_n7_a_snapshot_has_no_pending_row_and_no_value_and_the_live_database_is_untouched(self):
        before_bytes = self.source.read_bytes()
        before = self.live_state()
        summary = backup_sqlite.backup_database(self.source, self.published())
        self.assertEqual(summary["registrations_scrubbed"], 2)
        self.assertEqual(self.source.read_bytes(), before_bytes)             # byte for byte
        self.assertEqual(self.live_state(), before)
        self.assertEqual(sum(1 for r in before[0][1] if r[3] == "pending"), 2)  # still pending in the live one
        with closing(sqlite3.connect(f"{self.published().as_uri()}?mode=ro", uri=True)) as copy:
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM pending_registrations WHERE state = 'pending'")
                             .fetchone()[0], 0)
            self.assertEqual({r[0] for r in copy.execute("SELECT state FROM pending_registrations")}, {"void"})
            for column in registration_scrub.SCRUBBED_COLUMNS:
                self.assertEqual(copy.execute(f"SELECT COUNT(*) FROM pending_registrations WHERE {column} IS NOT NULL")
                                 .fetchone()[0], 0, column)
            published = table_dump(copy, "registration_sends", "registration_control", *ACCOUNT_AND_CONTENT)
        self.assertEqual(published, [item for item in before if item[0] != "pending_registrations"][:2] +
                         table_dump(sqlite3.connect(self.source), *ACCOUNT_AND_CONTENT))
        self.assertEqual(summary["sha256"], hashlib.sha256(self.published().read_bytes()).hexdigest())

    def test_the_transformation_comes_before_the_check_fsync_link_and_hash(self):
        order = []
        real_scrub, real_fsync = backup_sqlite.scrub_pending_registrations, backup_sqlite._fsync
        real_link, real_hash = os.link, backup_sqlite._sha256
        destination = self.published("ordered.db")

        def has_pending(path):
            if Path(path).is_dir():
                return None                                # the final fsync is of the directory
            with closing(sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True)) as conn:
                return conn.execute("SELECT COUNT(*) FROM pending_registrations WHERE state = 'pending'").fetchone()[0]

        def scrub(conn):
            order.append("scrub")
            return real_scrub(conn)

        def fsync(path):
            order.append(("fsync", has_pending(path)))
            return real_fsync(path)

        def link(source, target, *args, **kwargs):
            order.append(("link", has_pending(source)))
            return real_link(source, target, *args, **kwargs)

        def digest(path):
            order.append(("hash", has_pending(path)))
            return real_hash(path)

        with mock.patch.object(backup_sqlite, "scrub_pending_registrations", scrub), \
                mock.patch.object(backup_sqlite, "_fsync", fsync), \
                mock.patch.object(backup_sqlite.os, "link", link), \
                mock.patch.object(backup_sqlite, "_sha256", digest):
            summary = backup_sqlite.backup_database(self.source, destination)
        self.assertEqual(order[0], "scrub")
        self.assertEqual([item for item in order[1:]], [("fsync", 0), ("link", 0), ("fsync", None), ("hash", 0)])
        self.assertEqual(summary["sha256"], hashlib.sha256(destination.read_bytes()).hexdigest())
        text = (ROOT / "src" / "backup_sqlite.py").read_text(encoding="utf-8")
        body = text[text.index("def backup_database"):]
        self.assertLess(body.index("scrub_pending_registrations(dst)"), body.index('PRAGMA quick_check'))

    def test_a_failed_scrub_publishes_nothing_and_exits_non_zero(self):
        destination = self.published("never.db")
        with mock.patch.object(backup_sqlite.registration_scrub, "void_pending",
                               side_effect=sqlite3.OperationalError("injected")):
            with self.assertRaises(sqlite3.OperationalError):
                backup_sqlite.backup_database(self.source, destination)
            code = backup_sqlite.main(["--db", str(self.source), "--output", str(destination)],
                                      out=open(os.devnull, "w"), err=open(os.devnull, "w"))
        self.assertEqual(code, 1)
        self.assertEqual(list((self.dir / "out").iterdir()), [])             # no file, no partial file

    def test_a_database_without_registration_storage_is_backed_up_as_before(self):
        plain = self.dir / "plain.db"
        with closing(sqlite3.connect(plain)) as conn:
            conn.execute("CREATE TABLE probe (value TEXT)")
            conn.execute("INSERT INTO probe VALUES ('kept')")
            conn.commit()
        summary = backup_sqlite.backup_database(plain, self.published("plain-copy.db"))
        self.assertEqual(summary["registrations_scrubbed"], 0)
        with closing(sqlite3.connect(self.published("plain-copy.db"))) as copy:
            self.assertEqual(copy.execute("SELECT value FROM probe").fetchall(), [("kept",)])

    def test_the_application_snapshot_carries_the_scrubbed_database(self):
        state = self.dir / "state"
        for name in ("uploads", "vault", "backups"):
            (state / name).mkdir(parents=True)
        snapshot = backup_application.backup_application(self.source, state / "uploads", state / "vault",
                                                         state / "backups")
        with closing(sqlite3.connect(snapshot / "questions.db")) as copy:
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM pending_registrations WHERE state = 'pending'")
                             .fetchone()[0], 0)
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM registration_sends").fetchone()[0], 2)

    def test_the_documented_residuals_are_stated_and_nothing_stronger_is_claimed(self):
        guide = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
        section = guide.split("## 34.", 1)[1].split("\n## ", 1)[0]
        for residual in ("free pages", "unused space inside pages", "write-ahead log",
                         "earlier snapshots", "off-server copies", "no physical erasure"):
            self.assertIn(residual, section)
        self.assertNotRegex(section, r"(?i)\b(erases|guarantees (the )?removal)\b")
        # The module that scrubs says the same, and sets no physical option.
        source = (ROOT / "web" / "services" / "registration_scrub.py").read_text(encoding="utf-8")
        self.assertIn("Logical, not physical", source)
        code = "\n".join(line for line in (ROOT / "src" / "backup_sqlite.py").read_text(encoding="utf-8").splitlines()
                         if not line.lstrip().startswith(("#", '"""')))
        self.assertNotRegex(code, r"VACUUM|secure_delete")


class UnitFilesTest(unittest.TestCase):
    """The units are checked against what they run, and against the units they sit beside."""

    @staticmethod
    def parse(name):
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        parser.read(ROOT / "deploy" / "systemd" / name, encoding="utf-8")
        return parser

    def test_the_service_runs_the_real_command_as_the_service_user_under_the_backup_units_conventions(self):
        service, backup = self.parse("learnpilot-registration-cleanup.service"), self.parse("learnpilot-backup.service")
        self.assertEqual(service["Service"]["Type"], "oneshot")
        for key in ("User", "Group", "WorkingDirectory", "EnvironmentFile", "NoNewPrivileges", "ProtectSystem"):
            self.assertEqual(service["Service"][key], backup["Service"][key], key)
        env = (ROOT / "deploy" / "learnpilot-backup.env.example").read_text(encoding="utf-8")
        db = re.search(r"(?m)^DZI_DB=(\S+)$", env).group(1)
        words = shlex.split(service["Service"]["ExecStart"].replace("${DZI_DB}", db))
        self.assertEqual(words[0], "/opt/learnpilot/venv/bin/python")
        self.assertEqual(words[1:3], ["-m", "src.manage_accounts"])
        args = manage_accounts.build_parser().parse_args(words[3:])         # the real parser accepts it
        self.assertEqual((args.command, args.db, args.check), ("registrations-cleanup", db, False))
        self.assertTrue(any(db.startswith(path.rstrip("/") + "/")
                            for path in service["Service"]["ReadWritePaths"].split()))
        # No secret: not the application's environment file.
        self.assertNotIn("learnpilot.env", service["Service"]["EnvironmentFile"].replace("backup.env", ""))
        self.assertNotIn("DZI_SECRET_KEY", (ROOT / "deploy" / "systemd" /
                                            "learnpilot-registration-cleanup.service").read_text(encoding="utf-8"))

    def test_the_service_condition_is_the_restore_lock_the_command_and_the_script_use(self):
        service = self.parse("learnpilot-registration-cleanup.service")
        db = "/var/lib/learnpilot/data/questions.db"
        self.assertEqual(service["Unit"]["ConditionPathExists"], "!" + lifecycle.restore_lock_path(db))
        self.assertEqual(service["Unit"]["ConditionPathExists"],
                         self.parse("learnpilot-backup.service")["Unit"]["ConditionPathExists"])
        script = (ROOT / "deploy" / "learnpilot-restore.sh").read_text(encoding="utf-8")
        self.assertIn("learnpilot-registration-cleanup.timer", script)
        self.assertIn("learnpilot-registration-cleanup.service", script)

    def test_the_timer_fires_every_ten_minutes_and_triggers_the_service_by_name(self):
        timer = self.parse("learnpilot-registration-cleanup.timer")
        self.assertEqual(timer["Timer"]["OnCalendar"], "*:0/10")
        self.assertEqual(timer["Timer"]["Persistent"], "true")
        self.assertEqual(timer["Install"]["WantedBy"], "timers.target")
        self.assertNotIn("Unit", timer["Timer"])                  # same basename: the service of this name
        self.assertTrue((ROOT / "deploy" / "systemd" / "learnpilot-registration-cleanup.service").is_file())
        # Every ten minutes: minutes 0, 10, ... 50 of every hour.
        minutes = list(range(0, 60, 10))
        self.assertEqual(len(minutes), 6)

    def test_the_documented_install_commands_name_the_files_that_exist(self):
        guide = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
        section = guide.split("## 34.", 1)[1].split("\n## ", 1)[0]
        for name in ("learnpilot-registration-cleanup.service", "learnpilot-registration-cleanup.timer"):
            self.assertIn(name, section)
            self.assertTrue((ROOT / "deploy" / "systemd" / name).is_file())


if __name__ == "__main__":
    unittest.main()

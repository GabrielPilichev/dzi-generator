"""Assignment lifecycle: clock health (docs/ASSIGNMENT_LIFECYCLE_DESIGN.md, 3.6).

The checks every lifecycle writer transaction makes against the shared
``lifecycle_clock`` row: the row itself, regression against the high-water mark,
steps against the boot anchor, synchronisation evidence on a new boot, and the
single bookkeeping write that records a fault. Case ids refer to section 15.1.
"""

import sqlite3
import threading
import time
import unittest
from contextlib import closing
from datetime import timedelta

from tests.lifecycle_support import LifecycleTestCase, ts
from tests.support import reset_lifecycle_clock_row, set_lifecycle_clock_row, web_app
from web.services import lifecycle

STEP = lifecycle.CLOCK_STEP_TOLERANCE_SECONDS
REGRESSION = lifecycle.CLOCK_REGRESSION_TOLERANCE_SECONDS


class ClockTestCase(LifecycleTestCase):
    def write(self):
        """One admitted lifecycle write; returns its ``now``."""
        with closing(web_app.quiz_db()) as conn:
            with lifecycle.write_transaction(conn) as transaction:
                transaction.admit()
                return transaction.now

    def refused(self) -> str:
        with self.assertRaises(lifecycle.ClockUnavailable) as caught:
            self.write()
        return caught.exception.reason

    def dump(self) -> dict:
        """Every row of every table, to prove what a refusal did not write."""
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            return {table: conn.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall() for table in tables}

    def clock_state(self) -> tuple:
        row = self.clock_row()
        return row["state"], row["state_reason"]


class AdmittedWriteTest(ClockTestCase):
    def test_l111_the_sentinel_seed_never_looks_like_a_regression(self):
        reset_lifecycle_clock_row(self.db_path)  # as migration 018 leaves it; T0 is in the past
        self.assertEqual(self.clock_row()["high_water_at"], "1970-01-01 00:00:00")
        self.assertEqual(self.write(), self.T0)
        row = self.clock_row()
        self.assertEqual((row["state"], row["high_water_at"]), ("ok", ts(self.T0)))
        # The first lifecycle write of a boot writes the boot anchor.
        self.assertEqual((row["ref_wall_at"], row["ref_boot_id"]), (ts(self.T0), self.clock.boot_id()))

    def test_a_write_raises_the_mark_and_never_moves_the_anchor(self):
        anchor = tuple(self.clock_row())[5:]
        self.clock.advance(100)
        self.assertEqual(self.write(), self.T0 + timedelta(seconds=100))
        self.clock.advance(5000)
        self.write()
        row = self.clock_row()
        self.assertEqual(row["high_water_at"], ts(self.T0 + timedelta(seconds=5100)))
        self.assertEqual(tuple(row)[5:], anchor)  # ref_wall_at, ref_boot_id, ref_boottime_ms

    def test_a_discarded_transaction_writes_nothing_at_all(self):
        before = self.dump()
        self.clock.advance(60)
        with closing(web_app.quiz_db()) as conn:
            with lifecycle.write_transaction(conn) as transaction:
                transaction.admit()
                transaction.discard()
        self.assertEqual(self.dump(), before)

    def test_now_is_sampled_after_the_writer_lock_was_obtained(self):
        result = {}
        with closing(sqlite3.connect(str(self.db_path))) as holder:
            holder.execute("BEGIN IMMEDIATE")
            worker = threading.Thread(target=lambda: result.setdefault("now", self.write()))
            worker.start()
            time.sleep(0.3)             # the worker is waiting for the lock
            self.clock.advance(90)      # time passes while it waits
            holder.rollback()
            worker.join(timeout=10)
        self.assertEqual(result["now"], self.T0 + timedelta(seconds=90))

    def test_a_write_must_start_outside_a_transaction(self):
        with closing(web_app.quiz_db()) as conn:
            conn.execute("BEGIN")
            with self.assertRaises(RuntimeError):
                with lifecycle.write_transaction(conn):
                    pass


class RegressionTest(ClockTestCase):
    def test_l77_a_regression_within_the_tolerance_holds_time_at_the_mark(self):
        self.clock.advance(10)
        self.write()
        self.clock.step_wall(-REGRESSION)
        self.assertEqual(self.write(), self.T0 + timedelta(seconds=10))  # lifecycle time never decreases
        self.assertEqual(self.clock_state(), ("ok", None))
        self.assertEqual(self.clock_row()["high_water_at"], ts(self.T0 + timedelta(seconds=10)))

    def test_l78_l131_a_regression_beyond_the_tolerance_writes_only_its_own_bookkeeping(self):
        self.assignment(deadline_at=self.T0 + timedelta(days=1))
        self.clock.advance(1000)
        self.write()
        before = self.dump()
        self.clock.step_wall(-(REGRESSION + 1) - 0)
        reading = self.clock.wall()

        self.assertEqual(self.refused(), lifecycle.CLOCK_REASON_REGRESSION)

        after = self.dump()
        old_clock, new_clock = before.pop("lifecycle_clock")[0], after.pop("lifecycle_clock")[0]
        self.assertEqual(after, before)  # no quiz_* row, no marker, no hold
        # state, reason, reading: nothing else. The mark and the anchor are untouched.
        self.assertEqual(new_clock[2:5], ("suspect", lifecycle.CLOCK_REASON_REGRESSION, ts(reading)))
        self.assertEqual((new_clock[0], new_clock[1]) + new_clock[5:], (old_clock[0], old_clock[1]) + old_clock[5:])

        # A second detection while suspect writes nothing.
        snapshot = self.dump()
        self.assertEqual(self.refused(), "clock_suspect")
        self.assertEqual(self.dump(), snapshot)

    def test_l79_a_plausible_clock_does_not_clear_suspect(self):
        self.clock.advance(1000)
        self.write()
        self.clock.step_wall(-600)
        self.refused()
        self.clock.step_wall(600)   # the clock is "correct" again
        self.clock.advance(3600)
        self.assertEqual(self.refused(), "clock_suspect")
        self.assertEqual(self.clock_state(), ("suspect", lifecycle.CLOCK_REASON_REGRESSION))

    def test_l108_suspect_survives_new_connections(self):
        self.clock.advance(1000)
        self.write()
        self.clock.step_wall(-600)
        self.refused()
        for _ in range(3):  # every new connection (a restarted worker) reads the same row
            self.assertEqual(self.refused(), "clock_suspect")

    def test_read_pages_never_run_behind_the_mark(self):
        self.clock.advance(1000)
        self.write()
        self.clock.step_wall(-600)
        with closing(web_app.quiz_db()) as conn:
            read = lifecycle.read_clock(conn)
        self.assertEqual(read, (self.T0 + timedelta(seconds=1000), False))
        self.clock.step_wall(600)
        with closing(web_app.quiz_db()) as conn:
            self.assertEqual(lifecycle.read_clock(conn), (self.T0 + timedelta(seconds=1000), True))


class BootAnchorTest(ClockTestCase):
    def test_l106_a_step_beyond_the_tolerance_is_a_fault_in_both_directions(self):
        self.clock.step_wall(STEP + 1)
        self.assertEqual(self.refused(), lifecycle.CLOCK_REASON_STEP)
        self.assertEqual(self.clock_state(), ("suspect", lifecycle.CLOCK_REASON_STEP))

    def test_l106_a_backward_step_is_found_even_above_the_mark(self):
        self.clock.advance(1000)            # real time passes, nothing is written
        self.clock.step_wall(-(STEP + 1))   # still later than the high-water mark
        self.assertGreater(self.clock.wall(), self.T0)
        self.assertEqual(self.refused(), lifecycle.CLOCK_REASON_STEP)

    def test_l129_a_forward_error_below_the_tolerance_is_not_detected(self):
        # The stated limit of 3.6: lifecycle time simply runs 30 s early.
        self.clock.step_wall(30)
        self.assertEqual(self.write(), self.T0 + timedelta(seconds=30))
        self.assertEqual(self.clock_state(), ("ok", None))

    def test_l130_small_steps_add_up_against_the_anchor(self):
        anchor = tuple(self.clock_row())[5:]
        for _ in range(2):
            self.clock.step_wall(30)
            self.write()  # each is below the tolerance; the sum is exactly at it
            self.assertEqual(tuple(self.clock_row())[5:], anchor)
        self.clock.step_wall(30)  # the sum is now 90 s
        self.assertEqual(self.refused(), lifecycle.CLOCK_REASON_STEP)
        self.assertEqual(tuple(self.clock_row())[5:], anchor)

    def test_a_new_boot_with_synchronisation_evidence_writes_a_new_anchor(self):
        self.clock.advance(500)
        self.clock.reboot(boottime_ms=4_000)
        self.assertEqual(self.write(), self.T0 + timedelta(seconds=500))
        row = self.clock_row()
        self.assertEqual((row["ref_wall_at"], row["ref_boot_id"], row["ref_boottime_ms"]),
                         (ts(self.T0 + timedelta(seconds=500)), self.clock.boot_id(), 4_000))

    def test_l105_a_new_boot_without_evidence_is_refused_then_becomes_a_fault(self):
        for evidence in (None, False):  # unreadable, and "not synchronised"
            with self.subTest(evidence=evidence):
                set_lifecycle_clock_row(self.db_path, high_water=self.clock.wall())
                self.clock.reboot(boottime_ms=5_000)
                self.clock.sync = evidence
                before = self.dump()
                self.assertEqual(self.refused(), lifecycle.CLOCK_REASON_NO_SYNC)
                self.assertEqual(self.dump(), before)  # only refused: nothing recorded yet
                # After the bounded wait the absence itself is a clock fault.
                self.clock.advance(lifecycle.CLOCK_SYNC_WAIT_SECONDS)
                self.assertEqual(self.refused(), lifecycle.CLOCK_REASON_NO_SYNC)
                self.assertEqual(self.clock_state(), ("suspect", lifecycle.CLOCK_REASON_NO_SYNC))
                self.clock.sync = True

    def test_without_an_anchor_evidence_is_required_first(self):
        set_lifecycle_clock_row(self.db_path, high_water=self.T0, anchored=False)
        self.clock.sync = None
        self.clock._boottime_ms = 1_000
        self.assertEqual(self.refused(), lifecycle.CLOCK_REASON_NO_SYNC)
        self.clock.sync = True
        self.write()
        self.assertEqual(self.clock_row()["ref_boot_id"], self.clock.boot_id())


class ClockRowTest(ClockTestCase):
    def test_l109_a_missing_row_refuses_and_is_never_reseeded(self):
        self.execute("DELETE FROM lifecycle_clock")
        for _ in range(2):
            self.assertEqual(self.refused(), "clock_row_invalid")
        self.assertEqual(self.query("SELECT COUNT(*) FROM lifecycle_clock")[0][0], 0)
        with closing(web_app.quiz_db()) as conn:
            self.assertIn("0 rows", lifecycle.clock_row_problem(conn))
            self.assertFalse(lifecycle.read_clock(conn).healthy)

    def test_l110_a_malformed_row_refuses(self):
        def corrupt(statement):
            with closing(sqlite3.connect(str(self.db_path))) as conn:
                conn.execute("PRAGMA ignore_check_constraints = ON")
                conn.execute(statement)
                conn.commit()

        cases = (
            "UPDATE lifecycle_clock SET high_water_at = '2026-03-02T09:00:00'",
            "UPDATE lifecycle_clock SET high_water_at = '2026-02-30 09:00:00'",
            "UPDATE lifecycle_clock SET state = 'fine'",
            "UPDATE lifecycle_clock SET ref_boottime_ms = NULL",
            "UPDATE lifecycle_clock SET ref_wall_at = NULL, ref_boottime_ms = NULL",
            "UPDATE lifecycle_clock SET ref_wall_at = 'yesterday'",
            "UPDATE lifecycle_clock SET ref_boottime_ms = -5",
            "UPDATE lifecycle_clock SET state_changed_at = 'x'",
            "INSERT INTO lifecycle_clock (id, high_water_at) VALUES (2, '2026-01-01 00:00:00')",
        )
        for statement in cases:
            with self.subTest(statement=statement):
                set_lifecycle_clock_row(self.db_path, high_water=self.T0)
                corrupt(statement)
                before = self.dump()
                self.assertEqual(self.refused(), "clock_row_invalid")
                self.assertEqual(self.dump(), before)
                self.execute("DELETE FROM lifecycle_clock WHERE id <> 1")

    def test_a_well_formed_row_has_no_problem(self):
        with closing(web_app.quiz_db()) as conn:
            self.assertIsNone(lifecycle.clock_row_problem(conn))
            row = lifecycle.read_clock_row(conn)
        self.assertEqual((row.state, row.high_water_at, row.has_anchor), ("ok", self.T0, True))


class FactualTimestampTest(ClockTestCase):
    def test_a_factual_timestamp_ahead_of_the_clock_is_a_fault(self):
        owner = self.account("teacher")
        assignment_id = self.assignment(owner)
        self.attempt(assignment_id, self.account(), started_at=self.T0 + timedelta(hours=1))
        before = self.lifecycle_columns(assignment_id)
        with closing(web_app.quiz_db()) as conn:
            with self.assertRaises(lifecycle.ClockUnavailable) as caught:
                lifecycle.close_assignment(conn, assignment_id, principal_user_id=owner["id"])
        self.assertEqual(caught.exception.reason, lifecycle.CLOCK_REASON_FUTURE_TIMESTAMP)
        self.assertEqual(self.clock_state(), ("suspect", lifecycle.CLOCK_REASON_FUTURE_TIMESTAMP))
        self.assertEqual(self.lifecycle_columns(assignment_id), before)  # nothing was closed

    def test_a_terminal_marker_ahead_of_the_clock_is_a_fault(self):
        for marker in lifecycle.TERMINAL_MARKER_COLUMNS:
            with self.subTest(marker=marker):
                set_lifecycle_clock_row(self.db_path, high_water=self.clock.wall())
                assignment_id = self.assignment(**{marker: self.T0 + timedelta(hours=1)})
                with closing(web_app.quiz_db()) as conn:
                    self.assertFalse(lifecycle.observe(conn, assignment_id))
                self.assertEqual(self.clock_state(), ("suspect", lifecycle.CLOCK_REASON_FUTURE_TIMESTAMP))

    def test_l126_a_scheduled_deadline_in_the_future_is_never_a_finding(self):
        owner = self.account("teacher")
        assignment_id = self.assignment(owner, deadline_at=self.T0 + timedelta(days=179))
        with closing(web_app.quiz_db()) as conn:
            self.assertTrue(lifecycle.observe(conn, assignment_id))
            self.assertEqual(lifecycle.close_assignment(conn, assignment_id, principal_user_id=owner["id"]),
                             lifecycle.CLOSE_CLOSED)
        self.assertEqual(self.clock_state(), ("ok", None))

    def test_a_held_assignment_is_refused_before_its_timestamps_are_compared(self):
        # Held because a marker lies in the future: requesting it raises no new clock fault.
        assignment_id = self.assignment(closed_at=self.T0 + timedelta(hours=1), lifecycle_hold="clock_review")
        with closing(web_app.quiz_db()) as conn:
            with self.assertRaises(lifecycle.AssignmentHeld):
                with lifecycle.write_transaction(conn) as transaction:
                    transaction.examine(lifecycle.fetch_lifecycle_row(conn, assignment_id))
        self.assertEqual(self.clock_state(), ("ok", None))


class SharedStateTest(ClockTestCase):
    def test_l107_one_process_detecting_a_fault_stops_all_and_the_operator_restores_all(self):
        process_a = web_app.quiz_db()
        process_b = web_app.quiz_db()
        self.addCleanup(process_a.close)
        self.addCleanup(process_b.close)

        def write(conn):
            with lifecycle.write_transaction(conn) as transaction:
                transaction.admit()

        write(process_a)
        write(process_b)
        self.clock.step_wall(STEP + 30)
        with self.assertRaises(lifecycle.ClockUnavailable):
            write(process_a)            # A detects the step
        with self.assertRaises(lifecycle.ClockUnavailable) as caught:
            write(process_b)            # B has detected nothing itself
        self.assertEqual(caught.exception.reason, "clock_suspect")

        code, out, _err = self.cli("clock-recover", "--verified-utc", ts(self.clock.wall()), writer=True)
        self.assertEqual(code, 0, out)
        write(process_a)
        write(process_b)
        self.assertEqual(self.clock_row()["state"], "ok")


if __name__ == "__main__":
    unittest.main()

"""Registration PR 5: the restore script's registration gate and cleanup coordination
(docs/reviews/EMAIL_REGISTRATION_DESIGN.md, 3.9; test plan N6, DR and the restore half of D20).

These run ``deploy/learnpilot-restore.sh`` itself, under the harness of
``tests.test_lifecycle_restore_script``: a temporary state directory, a live database, a
snapshot, and shims standing in for ``sudo``, ``systemctl`` and ``curl``. **The systemd
behaviour here is simulated by those shims.** What they show is the script's own control flow
(the order of its calls, what it reads from the unit states, what it does on each ending). They
do not show that real systemd reports those states, that a timer stops a service, or that a
cleanup run is refused by a real ``ConditionPathExists``. Linux evidence is listed in
``docs/reviews/EMAIL_REGISTRATION_PR5_STATUS.md`` and has not been produced.

The operator CLI, the migration runner and the pre-flight are the repository's own.
"""

import io
import os
import re
import sqlite3
import stat
import time
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout

from src import manage_accounts
from tests import test_lifecycle_restore_script as base
from tests.registration_support import EMAIL, KEY, password_hash, student
from web.services import registration

SCRIPT = base.SCRIPT
BACKUP = {"name": "backup", "call": "timer", "flag": "timer-active", "note": "pending-backup-timer",
          "label": "backup timer", "unit": "learnpilot-backup.timer"}
CLEANUP = {"name": "cleanup", "call": "cleanup-timer", "flag": "cleanup-timer-active",
           "note": "pending-registration-cleanup-timer", "label": "registration cleanup timer",
           "unit": "learnpilot-registration-cleanup.timer"}
TIMERS = (BACKUP, CLEANUP)
OTHER = {"backup": CLEANUP, "cleanup": BACKUP}
ROOT = base.ROOT


def run_cli(*argv):
    """``manage_accounts.main`` in this process: (exit status, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = manage_accounts.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def add_pending(path, email=EMAIL):
    """A pending registration in the database at ``path``, made by the service."""
    from datetime import datetime, timezone

    with closing(sqlite3.connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        registration.create_pending(
            conn, cleaned=student(email), account_type="student", password_hash=password_hash(),
            previous_binding=None, secret_key=KEY, clock=lambda: datetime.now(timezone.utc), sender_busy=False)
        conn.commit()


class RegistrationRestoreTestCase(base.RestoreScriptTestCase):
    def cleanup_timer_active(self, server) -> bool:
        return (server.run_dir / "cleanup-timer-active").exists()

    def control(self, path):
        rows = base.run_sql(path, "SELECT * FROM registration_control")
        return [tuple(row) for row in rows]

    def hook(self, server, name, body):
        path = server.root / name
        path.write_text("#!/bin/bash\n" + body + "\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def pending_snapshot(self, hold=None):
        snapshot = self.variant(self.snapshot_db, "pending-snapshot")
        add_pending(snapshot)
        if hold:
            base.run_sql(snapshot, "UPDATE registration_control SET hold_reason = 'restore', hold_set_at = ?", (hold,))
        return snapshot

    def state(self, server):
        """(lock, backup active, backup note, cleanup active, cleanup note)."""
        return (server.locked(), server.timer_active(), (server.records / BACKUP["note"]).exists(),
                self.cleanup_timer_active(server), (server.records / CLEANUP["note"]).exists())

    def state_of(self, server, timer):
        """(lock, active, noted) for one timer."""
        return (server.locked(), (server.run_dir / timer["flag"]).exists(), (server.records / timer["note"]).exists())

    def assert_recovered_by_a_clean_run(self, server):
        earlier = len(server.all_calls())
        result = server.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state(server), (False, True, False, True, False))
        self.assertIn("RESTORE COMPLETE", result.stderr)
        return server.all_calls()[earlier:]


class RegistrationGateOnTheStagedCopyTest(RegistrationRestoreTestCase):
    def test_a_restore_voids_pending_credentials_and_holds_registration(self):
        snapshot = self.pending_snapshot()
        server = self.server(snapshot_db=snapshot)
        users_before = [tuple(r) for r in base.run_sql(snapshot, "SELECT * FROM users ORDER BY id")]
        sections_before = base.run_sql(snapshot, "SELECT COUNT(*) FROM curriculum_sections")[0][0]
        mark_before = base.run_sql(snapshot, "SELECT high_water_at FROM registration_control")[0][0]
        sends_before = [tuple(r) for r in base.run_sql(snapshot, "SELECT * FROM registration_sends ORDER BY id")]
        self.assertEqual(base.run_sql(snapshot, "SELECT COUNT(*) FROM pending_registrations WHERE state='pending'")[0][0], 1)

        result = server.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("REGISTRATION IS HELD", result.stderr)
        self.assertIn("registrations-release-hold --acknowledge-lost-history", result.stderr)
        live = server.live_db
        row = base.run_sql(live, "SELECT state, email, password_hash, code_hash, first_name FROM pending_registrations")[0]
        self.assertEqual(tuple(row), ("void", None, None, None, None))
        control = base.run_sql(live, "SELECT hold_reason, hold_set_at, high_water_at FROM registration_control")[0]
        self.assertEqual(control["hold_reason"], "restore")
        self.assertIsNotNone(control["hold_set_at"])
        self.assertEqual(control["high_water_at"], mark_before)             # the mark is not touched
        # Accounts, canonical content and the snapshot's own ledger are what the snapshot held.
        self.assertEqual([tuple(r) for r in base.run_sql(live, "SELECT * FROM users ORDER BY id")], users_before)
        self.assertEqual(base.run_sql(live, "SELECT COUNT(*) FROM curriculum_sections")[0][0], sections_before)
        self.assertEqual([tuple(r) for r in base.run_sql(live, "SELECT * FROM registration_sends ORDER BY id")],
                         sends_before)

    def test_registration_is_held_even_when_the_snapshot_never_had_a_control_row(self):
        server = self.server()
        self.assertEqual(base.run_sql(self.snapshot_db, "SELECT COUNT(*) FROM registration_control")[0][0], 0)
        result = server.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (row,) = self.control(server.live_db)
        self.assertEqual((row[1], row[2]), (registration.NO_MARK, "restore"))

    def test_a_hold_the_snapshot_already_carries_is_kept_with_its_own_time(self):
        snapshot = self.pending_snapshot(hold="2026-01-02 03:04:05")
        server = self.server(snapshot_db=snapshot)
        self.assertEqual(server.run().returncode, 0)
        (row,) = self.control(server.live_db)
        self.assertEqual((row[2], row[3]), ("restore", "2026-01-02 03:04:05"))

    def test_nothing_of_the_replaced_database_is_carried_over_and_a_released_hold_is_not_remembered(self):
        # The live database released its hold and has its own history; the snapshot has neither.
        live = self.variant(self.live_db, "live-released")
        add_pending(live)
        base.run_sql(live, "UPDATE registration_control SET hold_reason = NULL, hold_set_at = NULL")
        base.run_sql(live, "INSERT INTO registration_sends (email_hash, kind, attempted_at) VALUES (?, 'code', "
                           "'2026-01-01 00:00:00')", ("a" * 64,))
        server = self.server(live_db=live)
        self.assertEqual(server.run().returncode, 0)
        self.assertEqual(base.run_sql(server.live_db, "SELECT COUNT(*) FROM registration_sends")[0][0], 0)
        self.assertEqual(base.run_sql(server.live_db, "SELECT hold_reason FROM registration_control")[0][0], "restore")
        # The replaced database itself is kept, untouched, where the script says.
        (replaced,) = server.replaced()
        self.assertEqual(base.run_sql(replaced / "questions.db", "SELECT COUNT(*) FROM registration_sends")[0][0], 2)

    def test_the_script_never_runs_the_release_command(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "registrations-release-hold" in line:
                self.assertRegex(line.strip(), r'^(say|#)', line)

    def test_a_failing_gate_replaces_nothing_and_starts_nothing(self):
        server = self.server()
        result = server.run(fail="py:registrations-restore-gate#1=9")
        self.assert_nothing_replaced(server, result, status=9)
        self.assertTrue(self.cleanup_timer_active(server))
        self.assertEqual(len(server.live_files()), len(server.original))

    def test_a_status_check_that_fails_on_the_copy_stops_the_restore_before_the_swap(self):
        server = self.server()
        result = server.run(fail="py:registrations-restore-status#1=1")
        self.assert_nothing_replaced(server, result, status=1)
        self.assertTrue(self.cleanup_timer_active(server))

    def test_a_status_check_that_fails_after_the_swap_keeps_the_lock_and_the_timers_off(self):
        server = self.server()
        result = server.run(fail="py:registrations-restore-status#2=1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(server.started())
        self.assertTrue(server.locked())
        self.assertEqual((server.timer_active(), self.cleanup_timer_active(server)), (False, False))

    def test_a_gate_that_reports_success_without_holding_is_caught_by_the_status_check(self):
        server = self.server()
        result = server.run(RESTORE_TEST_PRETEND="registrations-restore-gate")
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse(server.started())
        self.assertIn("registrations-restore-status", result.stderr + result.stdout)


class CleanupCoordinationTest(RegistrationRestoreTestCase):
    def test_the_cleanup_timer_is_stopped_after_the_lock_and_before_the_service_and_comes_back(self):
        server = self.server()
        self.assertEqual(server.run().returncode, 0)
        calls = server.all_calls()
        self.assertLess(calls.index("timer-stop#1"), calls.index("cleanup-timer-stop#1"))
        self.assertLess(calls.index("cleanup-timer-stop#1"), calls.index("systemctl-stop#1"))
        self.assertLess(calls.index("cleanup-timer-stop#1"), calls.index("py:fingerprint#1"))
        self.assertLess(calls.index("systemctl-start#1"), calls.index("cleanup-timer-start#1"))
        self.assertEqual(calls.count("cleanup-timer-start#1"), 1)
        self.assertTrue(self.cleanup_timer_active(server))

    def test_a_cleanup_timer_that_was_not_active_is_never_started(self):
        server = self.server()
        (server.run_dir / "cleanup-timer-active").unlink()
        self.assertEqual(server.run().returncode, 0)
        self.assertFalse([call for call in server.all_calls() if call.startswith("cleanup-")])
        self.assertFalse(self.cleanup_timer_active(server))

    def test_it_is_not_started_again_on_a_failed_run_if_it_was_inactive(self):
        server = self.server()
        (server.run_dir / "cleanup-timer-active").unlink()
        result = server.run(fail="py:verify#1=1")
        self.assert_nothing_replaced(server, result, status=1)
        self.assertFalse(self.cleanup_timer_active(server))
        self.assertFalse([call for call in server.all_calls() if call.startswith("cleanup-")])

    def test_a_cleanup_already_running_is_seen_at_the_first_look_and_nothing_is_touched(self):
        for state in ("activating", "active", "reloading", "deactivating"):
            with self.subTest(state=state):
                server = self.server()
                (server.run_dir / "cleanup-state").write_text(state, encoding="utf-8")
                result = server.run()
                self.assert_untouched(server, result, status=1)
                self.assertIn(f"a registration cleanup is running (learnpilot-registration-cleanup.service is {state})",
                              result.stderr)
                self.assertTrue(self.cleanup_timer_active(server))

    def test_a_cleanup_that_starts_after_the_first_look_is_seen_once_the_lock_is_held(self):
        # Stopping the timer does not stop a run that has begun: the service state is read again after it.
        server = self.server()
        hook = self.hook(server, "a-cleanup-starts", 'echo activating > "$RESTORE_RUN/cleanup-state"')
        result = server.run(RESTORE_TEST_HOOK=f"cleanup-timer-stop#1={hook}")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("a registration cleanup is running (learnpilot-registration-cleanup.service is activating)",
                      result.stderr)
        self.assertIn(base.NOT_REPLACED, result.stderr)
        self.assertEqual((server.active(), server.locked(), server.timer_active()), (True, False, True))
        self.assertTrue(self.cleanup_timer_active(server))          # it had been active: put back
        self.assertFalse([call for call in server.calls() if call.startswith("systemctl-stop")])
        self.assertEqual(server.live_files(), server.original)

    def test_a_failed_cleanup_service_does_not_stop_a_restore_and_unknown_states_do(self):
        server = self.server()
        (server.run_dir / "cleanup-state").write_text("failed", encoding="utf-8")
        self.assertEqual(server.run().returncode, 0)
        for state in ("maintenance", "", "ERROR"):
            with self.subTest(state=state):
                other = self.server()
                (other.run_dir / "cleanup-state").write_text(state, encoding="utf-8")
                result = other.run()
                self.assert_untouched(other, result, status=1)

    def test_an_unreadable_or_unknown_cleanup_timer_state_stops_the_restore(self):
        server = self.server()
        (server.run_dir / "cleanup-timer-state").write_text("reloading", encoding="utf-8")
        result = server.run()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("the state of the registration cleanup timer cannot be told", result.stderr)
        self.assertEqual((server.active(), server.locked(), server.timer_active()), (True, False, True))

    def test_a_cleanup_command_run_while_the_lock_exists_is_refused_and_writes_nothing(self):
        server = self.server()
        add_pending(server.live_db)
        before = [tuple(r) for r in base.run_sql(server.live_db, "SELECT * FROM pending_registrations")]
        server.lock.mkdir()
        code, _, err = run_cli("--db", str(server.live_db), "registrations-cleanup")
        self.assertEqual(code, 1)
        self.assertIn("restore of this database is in progress", err)
        self.assertEqual([tuple(r) for r in base.run_sql(server.live_db, "SELECT * FROM pending_registrations")], before)


class InterruptedRestoreTest(RegistrationRestoreTestCase):
    def test_after_an_interruption_both_timers_stay_off_until_a_restore_has_finished(self):
        server = self.server()
        self.assertEqual(server.run(kill="mv#4").returncode, -9)
        self.assertEqual((server.timer_active(), self.cleanup_timer_active(server)), (False, False))
        self.assertTrue((server.records / CLEANUP["note"]).exists())       # in the restore's own records
        self.assertFalse(server.lock.joinpath("registration-cleanup-timer-was-active").exists())
        # The command refuses under the lock, whoever starts it.
        code, _, err = run_cli("--db", str(server.live_db), "registrations-cleanup")
        self.assertEqual(code, 1)
        self.assertIn("restore of this database is in progress", err)
        self.assertEqual(server.run(fail="py:verify#1=1").returncode, 1)       # a failed attempt is no safe end
        self.assertEqual((server.timer_active(), self.cleanup_timer_active(server)), (False, False))
        self.assertEqual(server.run().returncode, 0)
        self.assertEqual((server.timer_active(), self.cleanup_timer_active(server)), (True, True))

    def test_an_interrupted_run_leaves_registration_held_on_the_next_complete_run_whatever_came_before(self):
        snapshot = self.pending_snapshot()
        server = self.server(snapshot_db=snapshot)
        self.assertEqual(server.run(kill="mv#5").returncode, -9)             # cut off as the database is replaced
        self.assertEqual(server.run().returncode, 0)
        self.assertEqual(self.control(server.live_db)[0][2], "restore")
        self.assertEqual(base.run_sql(server.live_db, "SELECT COUNT(*) FROM pending_registrations "
                                                      "WHERE state = 'pending'")[0][0], 0)

    def test_the_timer_comes_back_on_a_failed_run_that_gave_the_lock_back(self):
        for fail in ("py:registrations-restore-gate#1=3", "py:verify#1=1", "py:clock-recover#1=1"):
            with self.subTest(fail=fail):
                server = self.server()
                result = server.run(fail=fail)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(server.started())
                self.assertEqual((server.locked(), server.timer_active(), self.cleanup_timer_active(server)),
                                 (False, True, True))

    # --- design 3.9: the timers come back, or the run says so, fails, and the next run brings them back ---
    # Review of b719728: a restart that failed, or a SIGKILL around the restarts, lost the intent.

    def test_a_restart_that_fails_once_is_retried_and_the_run_still_fails(self):
        def check(timer):
            server = self.server()
            result = server.run(fail=f"{timer['call']}-start#1=1")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertNotIn("RESTORE COMPLETE", result.stderr)
            self.assertIn(f"WARNING: the {timer['label']} could not be started again", result.stderr)
            self.assertEqual(self.state(server), (False, True, False, True, False))   # the retry started it
            calls = server.all_calls()
            self.assertIn(f"{timer['call']}-start#2", calls)
            self.assertIn(f"{OTHER[timer['name']]['call']}-start#1", calls)           # the other was attempted
        self.each(TIMERS, check)

    def test_a_restart_that_fails_twice_keeps_its_note_fails_and_a_clean_run_recovers_it(self):
        def check(timer):
            server = self.server()
            result = server.run(fail=f"{timer['call']}-start#1=1 {timer['call']}-start#2=1")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("NOT ACTIVE AGAIN YET", result.stderr)
            self.assertTrue(server.active())                              # the restore itself is in place
            expected = {"backup": (False, False, True, True, False), "cleanup": (False, True, False, False, True)}
            self.assertEqual(self.state(server), expected[timer["name"]])  # the other one came back
            calls = self.assert_recovered_by_a_clean_run(server)
            self.assertLess(calls.index(f"{timer['call']}-start#1"), calls.index("py:preflight#1"))
        self.each(TIMERS, check)

    def test_repeated_failure_keeps_the_note_whatever_the_timer_is_found_in_and_fails_each_time(self):
        def check(timer):
            server = self.server()
            server.run(fail=f"{timer['call']}-start#1=1 {timer['call']}-start#2=1")
            failing = " ".join(f"{timer['call']}-start#{n}=1" for n in range(1, 5))
            for attempt in range(2):
                result = server.run(fail=failing)                 # pre-flight recovery, end and retry all fail
                self.assertEqual(result.returncode, 1, (attempt, result.stderr))
                self.assertNotIn("RESTORE COMPLETE", result.stderr)
                # Found inactive under the lock, and still noted: the note is not dropped.
                self.assertTrue((server.records / timer["note"]).exists())
                self.assertFalse((server.run_dir / timer["flag"]).exists())
                self.assertFalse(server.locked())
            self.assert_recovered_by_a_clean_run(server)
        self.each(TIMERS, check)

    def test_a_signal_right_after_a_restart_still_brings_both_back_and_keeps_the_signal_status(self):
        cases = [(timer, how, status) for timer in TIMERS for how, status in (("TERM", 143), ("INT", 130), ("HUP", 129))]

        def check(case):
            timer, how, status = case
            server = self.server()
            result = server.run(after=f"{timer['call']}-start#1={how}")      # for the backup timer: between the two
            self.assertEqual(result.returncode, status, result.stderr)
            self.assertTrue(server.active())
            self.assertEqual(self.state(server), (False, True, False, True, False))
            self.assertIn("The registration cleanup timer is active again.", result.stderr)
            self.assert_recovered_by_a_clean_run(server)
        self.each(cases, check)

    def test_a_restart_and_its_retry_failing_around_a_signal_keep_the_note_and_the_signal_status(self):
        def check(timer):
            server = self.server()
            other = OTHER[timer["name"]]
            # The signal arrives right after the other timer's start; this one fails before and in the retry.
            result = server.run(fail=f"{timer['call']}-start#1=1 {timer['call']}-start#2=1",
                                after=f"{other['call']}-start#1=TERM")
            self.assertEqual(result.returncode, 143, result.stderr)
            self.assertTrue((server.records / timer["note"]).exists())
            self.assertFalse((server.run_dir / timer["flag"]).exists())
            self.assertTrue((server.run_dir / other["flag"]).exists())
            self.assertFalse((server.records / other["note"]).exists())
            self.assertFalse(server.locked())
            self.assert_recovered_by_a_clean_run(server)
        self.each(TIMERS, check)

    def test_sigkill_around_the_restarts_loses_nothing_and_the_next_run_recovers(self):
        cases = (
            # killed before the first restart: both noted, both off
            ("kill", "timer-start#1", (False, False, True, False, True)),
            # killed between the two restarts: the backup timer is back, the cleanup timer noted
            ("kill", "cleanup-timer-start#1", (False, True, False, False, True)),
            # killed after a start and before its note was removed: started again, harmlessly
            ("after", "timer-start#1=KILL", (False, True, True, False, True)),
            ("after", "cleanup-timer-start#1=KILL", (False, True, False, True, True)),
        )

        def check(case):
            how, where, expected = case
            server = self.server()
            result = server.run(**{how: where})
            self.assertEqual(result.returncode, -9, result.stderr)
            self.assertTrue(server.active())                    # the service had been started
            self.assertEqual(self.state(server), expected)
            calls = self.assert_recovered_by_a_clean_run(server)
            self.assertLess(calls.index("cleanup-timer-start#1"), calls.index("py:preflight#1"))
        self.each(cases, check)

    def test_sigkill_after_a_note_is_on_disk_and_before_the_stop_is_taken_over_under_the_lock(self):
        for sync in ("sync#1", "sync#2"):
            with self.subTest(after=sync):
                server = self.server()
                result = server.run(after=f"{sync}=KILL")
                self.assertEqual(result.returncode, -9, result.stderr)
                self.assertTrue(server.locked())                # nothing is started under the lock
                noted = (server.records / BACKUP["note"]).exists(), (server.records / CLEANUP["note"]).exists()
                self.assertEqual(noted, (True, sync == "sync#2"))
                earlier = len(server.all_calls())
                self.assertEqual(server.run().returncode, 0)
                calls = server.all_calls()[earlier:]
                self.assertNotIn("timer-start#1", calls[:calls.index("systemctl-start#1")])   # not before the end
                self.assertEqual(self.state(server), (False, True, False, True, False))

    def test_an_unresolved_note_survives_a_run_that_leaves_the_lock_and_the_next_one_takes_it_over(self):
        server = self.server()
        server.run(fail="cleanup-timer-start#1=1 cleanup-timer-start#2=1")
        self.assertEqual(self.state(server), (False, True, False, False, True))
        # The next run cannot start it in its pre-flight, and is then cut off while replacing the state.
        result = server.run(fail="cleanup-timer-start#1=1", kill="mv#4")
        self.assertEqual(result.returncode, -9)
        self.assertTrue(server.locked())
        self.assertTrue((server.records / CLEANUP["note"]).exists())
        self.assertFalse(self.cleanup_timer_active(server))
        # The run that takes the lock over finds it inactive, keeps the note, and starts it at its end.
        earlier = len(server.all_calls())
        self.assertEqual(server.run().returncode, 0)
        calls = server.all_calls()[earlier:]
        self.assertNotIn("cleanup-timer-stop#1", calls)
        self.assertGreater(calls.index("cleanup-timer-start#1"), calls.index("systemctl-start#1"))
        self.assertEqual(self.state(server), (False, True, False, True, False))

    def test_a_run_that_fails_its_pre_flight_still_recovers_a_note_first_or_says_it_could_not(self):
        server = self.server()
        server.run(fail="cleanup-timer-start#1=1 cleanup-timer-start#2=1")
        result = server.run(fail="cleanup-timer-start#1=1 py:preflight#1=1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(base.UNTOUCHED, result.stderr)
        self.assertIn("NOT ACTIVE AGAIN YET", result.stderr)
        self.assertEqual(self.state(server), (False, True, False, False, True))
        result = server.run(fail="py:preflight#1=1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(base.UNTOUCHED, result.stderr)
        self.assertNotIn("NOT ACTIVE AGAIN YET", result.stderr)
        self.assertEqual(self.state(server), (False, True, False, True, False))   # recovered, then untouched

    def test_a_timer_that_was_inactive_stays_inactive_whatever_interrupts_the_restarts(self):
        cases = [(inactive, how) for inactive in TIMERS
                 for how in ({"after": f"{OTHER[inactive['name']]['call']}-start#1=TERM"},
                             {"fail": f"{OTHER[inactive['name']]['call']}-start#1=1 "
                                      f"{OTHER[inactive['name']]['call']}-start#2=1"},
                             {"kill": f"{OTHER[inactive['name']]['call']}-start#1"},
                             {})]

        def check(case):
            inactive, how = case
            server = self.server()
            (server.run_dir / inactive["flag"]).unlink()
            server.run(**how)
            self.assertFalse((server.run_dir / inactive["flag"]).exists())
            self.assertFalse((server.records / inactive["note"]).exists())
            self.assertEqual(server.run().returncode, 0)                    # and the next run keeps it so
            self.assertFalse((server.run_dir / inactive["flag"]).exists())
            self.assertFalse((server.records / inactive["note"]).exists())
            self.assertFalse([call for call in server.all_calls() if call.startswith(f"{inactive['call']}-start")])
            self.assertTrue((server.run_dir / OTHER[inactive["name"]]["flag"]).exists())
        self.each(cases, check)

    def test_the_notes_are_made_in_the_records_and_flushed_before_each_timer_is_stopped(self):
        server = self.server()
        result = server.run(kill="systemctl-stop#1")            # both timers stopped, the service not yet
        self.assertEqual(result.returncode, -9)
        calls = server.all_calls()
        self.assertEqual([call for call in calls if call.startswith(("sync#", "timer-stop", "cleanup-timer-stop"))],
                         ["sync#1", "timer-stop#1", "sync#2", "cleanup-timer-stop#1"])
        # Each note, its flush and its stop are one step under the guard, after the lock was taken.
        steps = [call for call in calls if call.startswith(("guard#", "sync#", "timer-stop", "cleanup-timer-stop"))]
        self.assertEqual(steps[-6:], ["guard#4", "sync#1", "timer-stop#1", "guard#5", "sync#2", "cleanup-timer-stop#1"])
        self.assertEqual(stat.S_IMODE(server.records.stat().st_mode), 0o700)     # made by the script
        self.assertFalse(server.records.is_relative_to(server.state))
        for timer in TIMERS:
            note = server.records / timer["note"]
            self.assertEqual(note.read_text(encoding="utf-8"), f"{timer['unit']}\n")
            self.assertEqual(stat.S_IMODE(note.stat().st_mode), 0o600)
        self.assertEqual((server.records / "restore-owner").read_text(encoding="utf-8").strip(),
                         (server.run_dir / "script.pid").read_text(encoding="utf-8").strip())


class ConcurrentRestoreTest(RegistrationRestoreTestCase):
    """Review of 39157f5, blocker 1. A recovery that had seen no lock could pause, and then remove the
    note of a restore that had meanwhile taken the lock, noted its timer and stopped it; that restore,
    killed, left the timer inactive with nothing to say it was to be started again, and the next run
    ended with 0 without it. The same after a run had given its lock back and before it had started its
    timers. Each such step is now one command under the guard, and a recovery starts and removes only
    while no lock exists.

    These runs really overlap: two scripts on one server, each paused where the shims are told to pause
    it. What keeps them apart is flock(2) on the records directory, taken by util-linux flock(1) where it
    is installed and otherwise by ``tests.test_lifecycle_restore_script.FLOCK_PY``: the kernel's lock in
    both cases, on whatever system these run on."""

    def leave_noted(self, server, timer):
        """An earlier restore that could not start the timer again: inactive, noted, no lock."""
        result = server.run(fail=f"{timer['call']}-start#1=1 {timer['call']}-start#2=1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.state_of(server, timer), (False, False, True))

    def assert_other(self, server, timer, other_active, *, paused_by_the_restore):
        other = OTHER[timer["name"]]
        if not other_active:
            self.assertEqual(self.state_of(server, other), (True, False, False))
        elif paused_by_the_restore:
            self.assertEqual(self.state_of(server, other), (True, False, True))
        else:
            self.assertEqual(self.state_of(server, other), (True, True, False))

    def assert_brought_back(self, server, timer, other_active):
        """The run after that takes the lock over, and brings back what was active, and only that."""
        other = OTHER[timer["name"]]
        result = server.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("An earlier restore did not finish", result.stderr)
        self.assertEqual(self.state_of(server, timer), (False, True, False))
        self.assertEqual(self.state_of(server, other), (False, other_active, False))
        if not other_active:
            started = [call for tag in ("", "-a", "-b") for call in server.all_calls(tag)
                       if call.startswith(f"{other['call']}-start")]
            self.assertEqual(started, [])

    def test_a_recovery_paused_in_its_pre_flight_never_takes_the_note_of_a_restore_that_took_the_lock(self):
        cases = [(timer, other_active, where) for timer in TIMERS for other_active in (True, False)
                 for where in ("before the start", "after the start")]

        def check(case):
            timer, other_active, where = case
            server = self.server()
            if not other_active:
                (server.run_dir / OTHER[timer["name"]]["flag"]).unlink()
            self.leave_noted(server, timer)
            point = f"{timer['call']}-start#1"
            # A: a run whose pre-flight starts the noted timer, paused just before that start or just
            # after it, before the note is removed. Its own pre-flight then fails: it does nothing else.
            pausing = {"pause": point} if where == "before the start" else {"after": f"{point}=PAUSE"}
            recovering = server.start(tag="-a", fail="py:preflight#1=1", **pausing)
            server.wait_until(lambda: server.paused("-a", point) or recovering.poll() is not None,
                              "the recovery is paused")
            self.assertTrue(server.paused("-a", point), server.wait(recovering).stderr if recovering.poll() else "")
            # B: a restore, killed outright right after it has stopped that timer. Without the guard it
            # gets that far while A waits; with it, it waits for the guard until A has finished its step.
            restoring = server.start(tag="-b", after=f"{timer['call']}-stop#1=KILL")
            server.wait_until(lambda: restoring.poll() is not None or "flock#1" in server.all_calls("-b"),
                              "the restore has stopped the timer, or waits for the guard")
            server.go_on("-a", point)
            recovered, killed = server.wait(recovering), server.wait(restoring)
            self.assertEqual(recovered.returncode, 1, recovered.stderr)
            self.assertIn(base.UNTOUCHED, recovered.stderr)
            self.assertIn(f"The {timer['label']} is active again.", recovered.stderr)
            self.assertEqual(killed.returncode, -9, killed.stderr)
            # B held the lock, noted the timer and stopped it, and its note is there.
            self.assertEqual(self.state_of(server, timer), (True, False, True))
            self.assert_other(server, timer, other_active, paused_by_the_restore=timer is CLEANUP)
            self.assert_brought_back(server, timer, other_active)
        self.each(cases, check)

    def test_a_run_that_gave_its_lock_back_never_takes_the_notes_of_a_restore_that_began_since(self):
        cases = [(timer, other_active) for timer in TIMERS for other_active in (True, False)]

        def check(case):
            timer, other_active = case
            server = self.server()
            if not other_active:
                (server.run_dir / OTHER[timer["name"]]["flag"]).unlink()
            # A: a restore that has given its lock back and started the service, paused before its
            # health check and the start of its timers.
            finishing = server.start(tag="-a", pause="curl#1")
            server.wait_until(lambda: server.paused("-a", "curl#1") or finishing.poll() is not None,
                              "the first restore is paused")
            self.assertTrue(server.paused("-a", "curl#1"), server.wait(finishing).stderr if finishing.poll() else "")
            self.assertEqual(self.state_of(server, timer), (False, False, True))
            # B: the next restore. With no lock there, its pre-flight starts A's noted timers; then it
            # takes the lock, notes and stops them, and is killed outright right after it stopped this one.
            killed = server.wait(server.start(tag="-b", after=f"{timer['call']}-stop#1=KILL"))
            self.assertEqual(killed.returncode, -9, killed.stderr)
            self.assertEqual(self.state_of(server, timer), (True, False, True))
            # A goes on. It starts nothing under B's lock, takes none of B's notes, and does not end with 0.
            server.go_on("-a", "curl#1")
            finished = server.wait(finishing)
            self.assertEqual(finished.returncode, 1, finished.stderr)
            self.assertIn(f"WARNING: the {timer['label']} was not started again: a restore lock exists again",
                          finished.stderr)
            self.assertIn("NOT ACTIVE AGAIN YET", finished.stderr)
            self.assertNotIn("RESTORE COMPLETE", finished.stderr)
            self.assertIn("REGISTRATION IS HELD", finished.stderr)             # what it restored is still said
            self.assertIn("No assignment is held for the restore.", finished.stderr)
            self.assertEqual(self.state_of(server, timer), (True, False, True))
            self.assertFalse([call for call in server.all_calls("-a")[server.all_calls("-a").index("curl#1"):]
                              if "-start#" in call and not call.startswith("systemctl")])
            self.assert_other(server, timer, other_active, paused_by_the_restore=timer is CLEANUP)
            self.assert_brought_back(server, timer, other_active)
        self.each(cases, check)

    def test_two_runs_that_find_an_abandoned_lock_do_not_both_take_it_over(self):
        # The lock and its holder are one step: no run looks at a lock that another is half-way through
        # making or taking over (the old mkdir-then-pid window, and two takeovers at once).
        server = self.server()
        self.assertEqual(server.run(kill="py:reconcile#1").returncode, -9)          # an abandoned lock
        self.assertEqual(self.state(server), (True, False, True, False, True))
        # A is paused while it decides that the lock is abandoned, and once more when it stops the service.
        first = server.start(tag="-a", pause="ps#1 systemctl-stop#1")
        server.wait_until(lambda: server.paused("-a", "ps#1") or first.poll() is not None, "A looks at the holder")
        self.assertTrue(server.paused("-a", "ps#1"), server.wait(first).stderr if first.poll() else "")
        second = server.start(tag="-b", pause="systemctl-stop#1")
        server.wait_until(lambda: second.poll() is not None or server.paused("-b", "systemctl-stop#1")
                          or "flock#1" in server.all_calls("-b"), "B has taken the lock over, or waits for the guard")
        server.go_on("-a", "ps#1")
        server.wait_until(lambda: server.paused("-a", "systemctl-stop#1") or first.poll() is not None,
                          "A stops the service")
        server.go_on("-b", "systemctl-stop#1")                      # if B got that far, it goes on
        refused = server.wait(second)
        holder = (server.run_dir / "script-a.pid").read_text(encoding="utf-8").strip()
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn(f"another restore is running (process {holder})", refused.stderr)
        self.assertIn(base.UNTOUCHED, refused.stderr)
        self.assertEqual((server.records / "restore-owner").read_text(encoding="utf-8").strip(), holder)
        self.assertFalse([call for call in server.all_calls("-b") if "-stop#" in call or "-start#" in call])
        server.go_on("-a", "systemctl-stop#1")
        done = server.wait(first)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_restored(server, abandoned=1)
        self.assertEqual(self.state(server), (False, True, False, True, False))
        self.assertFalse((server.records / "restore-owner").exists())

    def test_a_power_loss_in_the_middle_of_a_step_under_the_guard_loses_nothing_and_frees_the_guard(self):
        # KILLALL: the script, the step under the guard and everything in it die at once. The guard is
        # the kernel's and goes with them: the next run is not kept waiting.
        cases = (
            ("guard#3=KILLALL", (False, True, False, True, False)),         # holding the guard, lock not made
            ("sync#1=KILLALL", (True, True, True, True, False)),            # noted and flushed, not stopped
            ("timer-stop#1=KILLALL", (True, False, True, True, False)),     # stopped, the next one not noted
            ("timer-start#1=KILLALL", (False, True, True, False, True)),    # started, its note not yet removed
            ("cleanup-timer-start#1=KILLALL", (False, True, False, True, True)),
        )

        def check(case):
            after, expected = case
            server = self.server()
            result = server.run(after=after)
            self.assertEqual(result.returncode, -9, result.stderr)
            self.assertEqual(self.state(server), expected)
            started = time.monotonic()
            calls = self.assert_recovered_by_a_clean_run(server)
            self.assertLess(time.monotonic() - started, 60)
            if not expected[0] and (expected[2] or expected[4]):           # no lock left: the pre-flight recovers
                self.assertLess(calls.index("cleanup-timer-start#1" if expected[4] else "timer-start#1"),
                                calls.index("py:preflight#1"))
        self.each(cases, check)


class RecordsStorageTest(RegistrationRestoreTestCase):
    """Review of 39157f5, blocker 2. The notes were in the state directory, which the service can write,
    and root wrote them through a symbolic link planted there (a dangling one created its target, one to
    an existing file truncated it). The restore's records are now in a directory that only root can
    change, checked from / down before anything is stopped, and a record that is not a plain file of
    root's, with a single name, is refused: nothing is written, read or removed through it."""

    SENTINEL = b"not the restore's: must stay byte for byte as it is\n"
    RECORDS = ("pending-backup-timer", "pending-registration-cleanup-timer", "restore-owner")

    def made(self, server):
        server.records.mkdir(mode=0o700)
        sentinel = server.root / "sentinel"
        sentinel.write_bytes(self.SENTINEL)
        return sentinel

    def assert_refused(self, server, result, message):
        self.assert_untouched(server, result, status=1)
        self.assertIn(message, result.stderr)
        self.assertIn("gate failed: the restore's own records are where only root can change them", result.stderr)
        self.assertTrue(self.cleanup_timer_active(server))
        self.assertFalse([call for call in server.all_calls() if call.startswith(("cleanup-timer", "sync#", "flock#"))])

    def test_a_record_that_is_not_a_plain_file_of_its_own_is_refused_before_anything_is_stopped(self):
        def dangling(server, path, sentinel):
            path.symlink_to(server.root / "created-through-the-link")

        def existing(server, path, sentinel):
            path.symlink_to(sentinel)

        def directory(server, path, sentinel):
            path.mkdir()

        def pipe(server, path, sentinel):
            os.mkfifo(path)

        def second_name(server, path, sentinel):
            os.link(sentinel, path)

        kinds = {"a dangling symbolic link": (dangling, "a symbolic link where a record of the restore belongs"),
                 "a symbolic link to a file": (existing, "a symbolic link where a record of the restore belongs"),
                 "a directory": (directory, "not a plain file where a record of the restore belongs"),
                 "a named pipe": (pipe, "not a plain file where a record of the restore belongs"),
                 "a second name of a file": (second_name, "a record of the restore that is not its own")}
        cases = [(name, kind) for name in self.RECORDS for kind in kinds]

        def check(case):
            name, kind = case
            plant, message = kinds[kind]
            server = self.server()
            sentinel = self.made(server)
            path = server.records / name
            plant(server, path, sentinel)
            result = server.run()
            self.assert_refused(server, result, f"{message}")
            self.assertIn(str(path), result.stderr)
            self.assertEqual(sentinel.read_bytes(), self.SENTINEL)                 # nothing written through it
            self.assertFalse((server.root / "created-through-the-link").exists())
            self.assertEqual(path.is_symlink(), kind.startswith("a symbolic") or kind == "a dangling symbolic link")
            if kind == "a directory":
                self.assertEqual(list(path.iterdir()), [])
        self.each(cases, check)

    def test_a_records_directory_that_others_could_change_is_refused(self):
        def link(server):
            real = server.root / "real-records"
            real.mkdir(mode=0o700)
            server.records.symlink_to(real)
            return {}, "a symbolic link, not a directory"

        def file(server):
            server.records.write_bytes(self.SENTINEL)
            return {}, "not a directory"

        def mode(bits):
            def made(server):
                server.records.mkdir()
                server.records.chmod(bits)
                return {}, "no one but its owner may have access to it"
            return made

        def foreign(server):           # root's directory, seen by a run that is not root
            return {"LEARNPILOT_RESTORE_RECORDS": "/usr"}, "/usr"

        def writable_above(bits):
            def made(server):
                above = server.root / "open"
                above.mkdir()
                above.chmod(bits)
                return {"LEARNPILOT_RESTORE_RECORDS": str(above / "records")}, "others can write to it"
            return made

        def linked_above(server):
            (server.root / "real").mkdir()
            (server.root / "link").symlink_to(server.root / "real")
            return {"LEARNPILOT_RESTORE_RECORDS": str(server.root / "link" / "records")}, "a symbolic link, not a directory"

        cases = {"a symbolic link": link, "a file": file, "mode 0750": mode(0o750), "mode 0705": mode(0o705),
                 "mode 0770": mode(0o770), "owned by another": foreign, "a directory above that the group can write":
                 writable_above(0o770), "a directory above that anyone can write": writable_above(0o777),
                 "a symbolic link above": linked_above}

        def check(label):
            server = self.server()
            environment, message = cases[label](server)
            result = server.run(**environment)
            self.assert_refused(server, result, message)
            if label == "owned by another" and os.geteuid() != 0:
                self.assertIn("owned by uid 0, not by uid", result.stderr)
            if label == "a file":
                self.assertEqual(server.records.read_bytes(), self.SENTINEL)
            for left in ("records", "real-records", "real", "open"):
                self.assertFalse((server.root / left / "restore-owner").exists())
                self.assertFalse((server.root / left / "records").exists())
        self.each(cases, check)

    def test_a_sticky_directory_above_is_accepted_and_a_missing_records_directory_is_made(self):
        server = self.server()
        sticky = server.root / "sticky"
        sticky.mkdir()
        sticky.chmod(0o1777)
        records = sticky / "records"
        result = server.run(LEARNPILOT_RESTORE_RECORDS=str(records))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(records.stat().st_mode), 0o700)
        self.assertEqual(sorted(path.name for path in records.iterdir()), [])        # nothing left once it is done

    def test_a_records_path_that_is_not_plain_or_is_inside_the_state_directory_is_refused_first(self):
        def check(records):
            server = self.server()
            result = server.run(LEARNPILOT_RESTORE_RECORDS=records(server))
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("LEARNPILOT_RESTORE_RECORDS", result.stderr)
            self.assertEqual((server.all_calls(), server.live_files()), ([], server.original))
            self.assertEqual((server.active(), server.timer_active(), server.locked()), (True, True, False))

        self.each((lambda server: str(server.state / "records"), lambda server: str(server.state),
                   lambda server: "records", lambda server: f"{server.root}/records/",
                   lambda server: f"{server.root}/x/../records", lambda server: f"{server.root}//records",
                   lambda server: "/"), check)

    def test_markers_an_earlier_version_left_in_the_lock_become_notes_and_only_their_timers_come_back(self):
        markers = {BACKUP["name"]: "backup-timer-was-active", CLEANUP["name"]: "registration-cleanup-timer-was-active"}
        cases = ((BACKUP,), (CLEANUP,), TIMERS)

        def check(marked):
            server = self.server()
            self.assertEqual(server.run(kill="py:reconcile#1").returncode, -9)       # an abandoned lock
            for timer in TIMERS:                    # as an earlier version left it: no note, a marker in the lock
                (server.records / timer["note"]).unlink()
            for timer in marked:
                (server.lock / markers[timer["name"]]).write_text("", encoding="utf-8")
            self.assertEqual(self.state(server), (True, False, False, False, False))
            # Taken over and killed before anything is replaced: the markers are notes now.
            self.assertEqual(server.run(kill="systemctl-stop#1").returncode, -9)
            for timer in TIMERS:
                self.assertEqual((server.records / timer["note"]).exists(), timer in marked, timer["name"])
            result = server.run()
            self.assertEqual(result.returncode, 0, result.stderr)
            for timer in TIMERS:
                self.assertEqual(self.state_of(server, timer), (False, timer in marked, False), timer["name"])
        self.each(cases, check)

    def test_a_note_where_an_unreleased_build_kept_it_is_not_followed_or_acted_on(self):
        def check(kind):
            server = self.server()
            old = server.state / "restore-pending-registration-cleanup-timer"
            if kind == "file":
                old.write_text("learnpilot-registration-cleanup.timer\n", encoding="utf-8")
            else:
                old.symlink_to(server.root / "created-through-the-link")
            server.original = server.live_files()
            result = server.run()
            self.assert_untouched(server, result, status=1)
            self.assertIn(f"there is a timer note where no released version of this script keeps one: {old}",
                          result.stderr)
            self.assertFalse((server.root / "created-through-the-link").exists())
            self.assertTrue(old.is_symlink() if kind == "link" else old.is_file())
            self.assertFalse([call for call in server.all_calls() if call.startswith("cleanup-timer")])
        self.each(("file", "link"), check)

    def test_a_note_that_cannot_be_removed_is_told_apart_from_a_timer_that_cannot_be_started(self):
        def check(timer):
            server = self.server()
            result = server.run(RESTORE_TEST_RM_REFUSES=str(server.records / timer["note"]))
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f"WARNING: the {timer['label']} was started again, but its note could not be removed",
                          result.stderr)
            self.assertNotIn(f"the {timer['label']} could not be started again", result.stderr)
            self.assertNotIn("RESTORE COMPLETE", result.stderr)
            self.assertEqual(self.state_of(server, timer), (False, True, True))        # active, and still noted
            calls = self.assert_recovered_by_a_clean_run(server)                         # started once more, removed
            self.assertLess(calls.index(f"{timer['call']}-start#1"), calls.index("py:preflight#1"))
        self.each(TIMERS, check)

    def test_a_restore_that_needed_the_retry_still_says_what_it_holds(self):
        snapshot = self.variant(self.snapshot_db, "held-snapshot", "UPDATE quiz_assignments SET closed_at = ? "
                                "WHERE id IN (?, ?)", (self.closed_at, self.unchanged, self.extended))
        server = self.server(snapshot_db=snapshot)
        result = server.run("--accept-held", "2", fail="timer-start#1=1")       # the retry in finish succeeds
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("RESTORED, NOT RECOVERED: 2 assignment(s) stay held and are closed to everyone.", result.stderr)
        self.assertIn("REGISTRATION IS HELD", result.stderr)
        self.assertNotIn("RESTORE COMPLETE", result.stderr)
        self.assertEqual(self.state(server), (False, True, False, True, False))


class RestoreScriptOrderTest(unittest.TestCase):
    """Test plan N6 on the text of the script: where the coordination and the gate sit."""

    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text(encoding="utf-8")
        cls.code = "\n".join(line for line in cls.text.splitlines() if not line.lstrip().startswith("#"))

    def index(self, marker, start=0):
        found = self.code.index(marker, start)
        return found

    def test_cleanup_is_coordinated_after_the_lock_and_before_the_live_database_is_read(self):
        lock = self.index('sudo flock "$RECORDS" sh -c "$CHECKED$ACQUIRE"')
        stop = self.index('"$CLEANUP_TIMER_NOTE" "$CLEANUP_TIMER" "$STOP_IT"')
        service_look = self.code.index('show -p ActiveState "$CLEANUP_SERVICE"', stop)
        checkpoint = self.index("wal_checkpoint(TRUNCATE)")
        fingerprint = self.index('LIVE_SEEN=$("${FINGERPRINT[@]}" "$LIVE_DB")')
        self.assertLess(lock, stop)
        self.assertLess(stop, service_look)
        self.assertLess(service_look, checkpoint)
        self.assertLess(checkpoint, fingerprint)

    def test_the_gate_runs_after_the_migration_of_the_copy_and_before_the_final_fingerprint(self):
        migrate = self.index('"${MIGRATE[@]}" --apply')
        gate = self.index("registrations-restore-gate")
        final = self.index('VERIFIED=$("${FINGERPRINT[@]}" "$STAGED_DB")')
        self.assertLess(migrate, gate)
        self.assertLess(gate, final)
        self.assertEqual(self.code.count('"${REGISTRATIONS[@]}" registrations-restore-gate'), 1)
        # Nothing of the gate after the fingerprint: only the read-only status checks.
        after = self.code[final:]
        self.assertNotIn('"${REGISTRATIONS[@]}" registrations-restore-gate', after)
        self.assertEqual(len(re.findall(r"registrations-restore-status --expect held", after)), 2)

    def test_the_cleanup_timer_is_started_wherever_the_backup_timer_is(self):
        # Both in restart_timers, one after the other, by the same step under the guard.
        backup = [m.start() for m in re.finditer(r'"\$CHECKED\$RESTART" sh "\$RECORDS" "\$LOCK" "\$TIMER_NOTE" '
                                                 r'"\$BACKUP_TIMER"', self.code)]
        cleanup = [m.start() for m in re.finditer(r'"\$CHECKED\$RESTART" sh "\$RECORDS" "\$LOCK" "\$CLEANUP_TIMER_NOTE" '
                                                  r'"\$CLEANUP_TIMER"', self.code)]
        self.assertEqual((len(backup), len(cleanup)), (1, 1))
        body = self.code.split("restart_timers() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('"$TIMER_NOTE" "$BACKUP_TIMER" "$1"', body)
        self.assertIn('"$CLEANUP_TIMER_NOTE" "$CLEANUP_TIMER" "$1"', body)
        self.assertLess(backup[0], cleanup[0])
        self.assertEqual(self.code.count('"$TIMER_WAS_ACTIVE" = yes'), self.code.count('"$CLEANUP_TIMER_WAS_ACTIVE" = yes'))


if __name__ == "__main__":
    unittest.main()

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
import re
import sqlite3
import stat
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout

from src import manage_accounts
from tests import test_lifecycle_restore_script as base
from tests.registration_support import EMAIL, KEY, password_hash, student
from web.services import registration

SCRIPT = base.SCRIPT
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
        self.assertTrue(server.lock.joinpath("registration-cleanup-timer-was-active").exists())
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

    def test_a_restart_of_the_cleanup_timer_that_fails_is_said_loudly_and_is_not_a_failed_restore(self):
        server = self.server()
        result = server.run(fail="cleanup-timer-start#1=1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING: the registration cleanup timer could not be started again", result.stderr)
        self.assertTrue(server.active())

    # --- the service is already running again: an ending that is not success still restores the timers ---

    def assert_timers(self, server, backup, cleanup):
        self.assertEqual((server.locked(), server.timer_active(), self.cleanup_timer_active(server)),
                         (False, backup, cleanup))

    def test_a_signal_between_the_two_timer_restarts_still_restores_the_second_timer(self):
        for how, status in (("TERM", 143), ("INT", 130), ("HUP", 129)):
            with self.subTest(signal=how):
                server = self.server()
                result = server.run(after=f"timer-start#1={how}")
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertTrue(server.active())                      # the service is on the restored state
                self.assert_timers(server, True, True)
                calls = server.all_calls()
                self.assertEqual((calls.count("timer-start#1"), calls.count("cleanup-timer-start#1")), (1, 1))
                self.assertIn("The registration cleanup timer is active again.", result.stderr)
                # A restore after it starts from what is really there, and ends with both timers.
                self.assertEqual(server.run().returncode, 0)
                self.assert_timers(server, True, True)

    def test_a_signal_during_the_last_restart_leaves_nothing_undone(self):
        server = self.server()
        result = server.run(after="cleanup-timer-start#1=TERM")
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assert_timers(server, True, True)

    def test_a_failed_backup_timer_restart_does_not_keep_the_cleanup_timer_off(self):
        server = self.server()
        result = server.run(fail="timer-start#1=1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING: the backup timer could not be started again", result.stderr)
        self.assert_timers(server, False, True)

    def test_a_timer_that_was_inactive_stays_inactive_whatever_interrupts_the_restarts(self):
        for inactive, signal_after in (("cleanup-timer-active", "timer-start#1"),
                                       ("timer-active", "cleanup-timer-start#1")):
            with self.subTest(inactive=inactive):
                server = self.server()
                (server.run_dir / inactive).unlink()
                active_after = signal_after
                result = server.run(after=f"{active_after}=TERM")
                self.assertEqual(result.returncode, 143, result.stderr)
                again = {"cleanup-timer-active": (True, False), "timer-active": (False, True)}[inactive]
                self.assert_timers(server, *again)
                calls = server.all_calls()
                if inactive == "cleanup-timer-active":
                    self.assertFalse([call for call in calls if call.startswith("cleanup-")])
                else:
                    self.assertNotIn("timer-start#1", calls)
                self.assertEqual(server.run().returncode, 0)            # and the next restore keeps it so
                self.assert_timers(server, *again)


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
        lock = self.index('sudo mkdir -- "$LOCK"')
        stop = self.index('sudo systemctl stop "$CLEANUP_TIMER"')
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
        backup = [m.start() for m in re.finditer(r'systemctl start "\$BACKUP_TIMER"', self.code)]
        cleanup = [m.start() for m in re.finditer(r'systemctl start "\$CLEANUP_TIMER"', self.code)]
        self.assertEqual(len(backup), len(cleanup))
        for left, right in zip(backup, cleanup):
            self.assertLess(left, right)
            self.assertLess(right - left, 600)
        self.assertEqual(self.code.count('"$TIMER_WAS_ACTIVE" = yes'), self.code.count('"$CLEANUP_TIMER_WAS_ACTIVE" = yes'))


if __name__ == "__main__":
    unittest.main()

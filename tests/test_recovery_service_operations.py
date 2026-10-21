"""Password recovery (scope 8.2): the 8.1 operations re-tested on rows the service creates
(design 10: RR1–RR3 end to end, RR5, RN1–RN4), through the real CLI commands and backup module.

A "snapshot" here is a copy made with SQLite's online backup API of a temporary database;
nothing reads ``data/questions.db``. The CLI cleanup and ``--check`` read the real clock
(``_utcnow``), which is days after the fixed test time ``T0``; that is stated where used.
"""

import io
import os
import sqlite3
from unittest.mock import patch

from src import backup_sqlite, manage_accounts
from tests.recovery_service_support import (
    EMAIL, KEY, NEW_PASSWORD, OLD_PASSWORD, SCRUBBED, RecoveryServiceTestCase, code_of,
)
from tests.recovery_support import cli
from web.services import recovery, recovery_scrub, registration

Outcome = recovery.Outcome


class ServiceRowsOperationsTest(RecoveryServiceTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self.verified()
        self.issued = self.request()
        self.shadow = self.request("unknown@example.bg")

    def snapshot(self, name="copy.db"):
        path = self.root / name
        target = sqlite3.connect(path)
        self.conn.backup(target)
        target.close()
        return path

    def on_copy(self, path, function, *args, **kwargs):
        conn = sqlite3.connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = function(conn, *args, **kwargs)
            conn.commit()
            return result
        finally:
            conn.close()

    def copy_redeem(self, path, issued, code):
        return self.on_copy(path, recovery.redeem, self.binding(issued), code, NEW_PASSWORD, secret_key=KEY,
                            clock=self.clock)

    def copy_rows(self, path, sql, *params):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def test_rr1_a_cancelled_code_is_not_revived_by_a_restore(self):
        copy = self.snapshot()
        self.cancel(self.issued)
        status, output = cli(copy, "registrations-restore-gate")
        self.assertEqual(status, 0, output)
        self.assertIn("2 pending recovery(s) voided and scrubbed", output)
        self.assertEqual(self.copy_rows(copy, "SELECT COUNT(*) AS n FROM password_recoveries WHERE state = "
                                              "'pending'")[0]["n"], 0)
        result = self.copy_redeem(copy, self.issued, code_of(self.issued))
        self.assertEqual((result.outcome, result.final_state), (Outcome.NOT_PENDING, "void"))

    def test_rr1_without_the_recovery_part_of_the_gate_the_copys_code_is_live(self):
        """The defect the gate prevents, asserted: a copy gated without voiding recoveries,
        once its hold is released, redeems the code cancelled on the original."""
        copy = self.snapshot()
        self.cancel(self.issued)
        with patch.object(manage_accounts.recovery_scrub, "void_pending", return_value=0):
            status, output = cli(copy, "registrations-restore-gate")
        self.assertEqual(status, 0, output)
        status, output = cli(copy, "registrations-release-hold", "--acknowledge-lost-history")
        self.assertEqual(status, 0, output)
        self.assertIs(self.copy_redeem(copy, self.issued, code_of(self.issued)).outcome, Outcome.REDEEMED)

    def test_rr2_spent_attempts_are_not_returned(self):
        copy = self.snapshot()
        for _ in range(5):
            self.redeem(self.issued, "00000000")
        self.assertEqual(self.row(self.issued.recovery_id)["code_attempts"], 5)
        status, output = cli(copy, "registrations-restore-gate")
        self.assertEqual(status, 0, output)
        row = self.copy_rows(copy, "SELECT * FROM password_recoveries WHERE id = ?", self.issued.recovery_id)[0]
        self.assertEqual(row["state"], "void")
        for column in SCRUBBED:
            self.assertIsNone(row[column])

    def test_rr3_a_redeemed_recovery_is_not_replayed_and_the_old_credentials_return(self):
        snapshot_user = self.user(self.user_id)
        copy = self.snapshot()
        self.assertIs(self.redeem(self.issued, code_of(self.issued)).outcome, Outcome.REDEEMED)
        status, output = cli(copy, "registrations-restore-gate")
        self.assertEqual(status, 0, output)
        self.assertEqual(self.copy_redeem(copy, self.issued, code_of(self.issued)).outcome, Outcome.NOT_PENDING)
        restored = self.copy_rows(copy, "SELECT * FROM users WHERE id = ?", self.user_id)[0]
        # The documented consequence (design 6.6): the snapshot's hash and key are back.
        self.assertEqual(restored["password_hash"], snapshot_user["password_hash"])
        self.assertEqual(restored["session_key"], snapshot_user["session_key"])
        self.assertNotEqual(restored["session_key"], self.user(self.user_id)["session_key"])
        status, output = cli(copy, "registrations-restore-status", "--expect", "held")
        self.assertEqual(status, 0, output)

    def test_rr5_restore_status_fails_while_a_service_made_recovery_is_pending(self):
        status, output = cli(self.path, "registrations-restore-status", "--expect", "held")
        self.assertEqual(status, 1)
        self.assertIn("2 pending recovery(s) exist", output)
        status, output = cli(self.path, "registrations-restore-gate")
        self.assertEqual(status, 0, output)
        status, output = cli(self.path, "registrations-restore-status", "--expect", "held")
        self.assertEqual(status, 0, output)
        # After the gate the service refuses everything but cancellation (and the hold stays).
        self.assertIs(self.request("new@example.bg").outcome, Outcome.UNAVAILABLE)

    def test_rn1_rn2_quiet_site_cleanup_in_the_registration_cleanup_transaction(self):
        self.at(31 * 60)
        result = self.tx(lambda conn: (registration.cleanup(conn, clock=self.clock),
                                       recovery_scrub.cleanup(conn, self.wall)))
        self.assertEqual(result[1].scrubbed, 2)
        self.assertScrubbed(self.issued.recovery_id, "expired")
        self.assertScrubbed(self.shadow.recovery_id, "expired")
        self.at(24 * 3600)
        self.assertEqual(self.tx(lambda conn: recovery_scrub.cleanup(conn, self.wall)).deleted, 2)
        self.at(48 * 3600)
        self.assertEqual(self.tx(lambda conn: registration.cleanup(conn, clock=self.clock)).deleted_sends, 2)
        self.assertEqual(self.count("registration_sends"), 0)

    def test_rn2_rn3_the_cli_cleanup_and_check_cover_service_rows(self):
        # The CLI reads the real clock, days after T0: these rows are overdue for it.
        status, output = cli(self.path, "registrations-cleanup", "--check")
        self.assertEqual(status, 1)
        self.assertIn("2 pending recovery(s) older than an hour", output)
        status, output = cli(self.path, "registrations-cleanup")
        self.assertEqual(status, 0, output)
        self.assertIn("2 pending recovery(s) expired and scrubbed", output)
        status, output = cli(self.path, "registrations-cleanup", "--check")
        self.assertEqual(status, 0, output)

    def test_cleanup_runs_during_a_hold_and_cancel_is_allowed_during_it(self):
        self.set_control(high_water=self.wall, hold="restore")
        self.assertIs(self.cancel(self.shadow).outcome, Outcome.CANCELLED)
        self.at(31 * 60)
        result = self.tx(lambda conn: (registration.cleanup(conn, clock=self.clock),
                                       recovery_scrub.cleanup(conn, self.wall)))
        self.assertFalse(result[0].clock_behind)
        self.assertEqual(result[1].scrubbed, 1)
        self.assertEqual(self.control()["hold_reason"], "restore")

    def test_rn4_a_backup_scrubs_service_made_rows_and_leaves_the_live_database(self):
        before = self.path.read_bytes()
        target = self.root / "backup.db"
        result = backup_sqlite.backup_database(self.path, target)
        self.assertEqual(result["recoveries_scrubbed"], 2)
        self.assertEqual(self.path.read_bytes(), before)
        for row in self.copy_rows(target, "SELECT * FROM password_recoveries"):
            self.assertEqual(row["state"], "void")
            for column in SCRUBBED:
                self.assertIsNone(row[column])
        # The ledger keeps its rows (counted history), and the live rows are still pending.
        self.assertEqual(len(self.copy_rows(target, "SELECT * FROM registration_sends")), 2)
        self.assertEqual(self.row(self.issued.recovery_id)["state"], "pending")
        self.assertIs(self.redeem(self.issued, code_of(self.issued)).outcome, Outcome.REDEEMED)
        self.assertTrue(self.authenticates(EMAIL, NEW_PASSWORD))
        self.assertFalse(self.authenticates(EMAIL, OLD_PASSWORD))

    def test_rq1_operator_set_password_invalidates_a_pending_recovery(self):
        with patch.dict(os.environ, {"DZI_SECRET_KEY": KEY}), patch("sys.stdin", io.StringIO("operator pass 12\n")):
            status, output = cli(self.path, "set-password", "--email", EMAIL, "--password-stdin")
        self.assertEqual(status, 0, output)
        self.assertIs(self.redeem(self.issued, code_of(self.issued)).outcome, Outcome.INVALIDATED)
        self.assertTrue(self.authenticates(EMAIL, "operator pass 12"))


if __name__ == "__main__":
    import unittest
    unittest.main()

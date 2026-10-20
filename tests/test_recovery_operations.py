"""RN1–3/5, RR1–7, RH3, RQ, RCl and RS4; temporary SQL fixture rows."""
import os
import sqlite3
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch

from src import manage_accounts as cli_module
from tests.recovery_support import RecoveryTestCase, add_recovery, add_send, cli, historical, ROOT
from tests.registration_support import T0, stamp, KEY, student, password_hash
from web.services import recovery_scrub as scrub, registration, lifecycle


class OperationsTest(RecoveryTestCase):
    def setUp(self):
        super().setUp()
        self.now = patch.object(cli_module, "_utcnow", return_value=T0 + timedelta(minutes=31))
        self.now.start()
        self.addCleanup(self.now.stop)

    def test_quiet_cleanup_retention_shared_ledger_and_no_secret(self):
        add_recovery(self.conn)
        add_recovery(self.conn, binding_hash="f"*64, created_at=stamp(T0-timedelta(days=1)))
        add_send(self.conn, recovery_id=1, attempted_at=stamp(T0-timedelta(hours=48)))
        add_send(self.conn, pending_id=2, issuance_id=None)
        self.conn.commit()
        with patch.dict(os.environ, {}, clear=True):
            status, output = cli(self.path, "registrations-cleanup")
        self.assertEqual(status, 0, output)
        self.assertIn("2 pending recovery(s)", output)
        self.assertIn("1 final recovery row(s)", output)
        self.assertEqual(self.conn.execute("SELECT state, email, code_hash FROM password_recoveries").fetchall(),
                         [("expired", None, None)])
        self.assertEqual(self.conn.execute("SELECT pending_id FROM registration_sends").fetchall(), [(2,)])

    def test_clock_behind_writes_nothing_and_future_expiry_waits_for_acceptance(self):
        add_recovery(self.conn, created_at=stamp(T0+timedelta(days=2)))
        self.conn.execute("INSERT INTO registration_control VALUES(1, ?, 'restore', ?)",
                          (stamp(T0+timedelta(days=2)), stamp(T0)))
        self.conn.commit()
        before = self.path.read_bytes()
        self.assertEqual(cli(self.path, "registrations-cleanup")[0], 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(cli(self.path, "registrations-clock-accept")[0], 0)
        self.assertEqual(cli(self.path, "registrations-cleanup")[0], 0)
        self.assertEqual(self.conn.execute("SELECT state FROM password_recoveries").fetchone()[0], "expired")
        self.assertEqual(self.conn.execute("SELECT hold_reason FROM registration_control").fetchone()[0], "restore")

    def test_check_pending_older_than_hour_and_restore_lock(self):
        add_recovery(self.conn, created_at=stamp(T0-timedelta(hours=1)))
        self.conn.commit()
        before = self.path.read_bytes()
        code, output = cli(self.path, "registrations-cleanup", "--check")
        self.assertEqual(code, 1)
        self.assertIn("1 pending recovery(s)", output)
        self.assertEqual(self.path.read_bytes(), before)
        lock = Path(lifecycle.restore_lock_path(self.path))
        lock.mkdir()
        self.assertEqual(cli(self.path, "registrations-cleanup")[0], 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_lock_rechecked_under_writer_lock(self):
        add_recovery(self.conn)
        self.conn.commit()
        before = self.path.read_bytes()
        with patch.object(lifecycle, "restore_lock_present", side_effect=[False, True]):
            self.assertEqual(cli(self.path, "registrations-cleanup")[0], 1)
        self.assertEqual(before, self.path.read_bytes())

    def test_restore_status_gate_idempotency_and_hold_never_ages_out(self):
        add_recovery(self.conn)
        self.conn.commit()
        code, output = cli(self.path, "registrations-restore-status", "--expect", "held")
        self.assertEqual(code, 1)
        self.assertIn("1 pending recovery(s)", output)
        accounts_before = self.conn.execute("SELECT * FROM users").fetchall()
        self.assertEqual(cli(self.path, "registrations-restore-gate")[0], 0)
        before = self.path.read_bytes()
        self.assertEqual(cli(self.path, "registrations-restore-gate")[0], 0)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(cli(self.path, "registrations-restore-status", "--expect", "held")[0], 0)
        for days in (1, 7, 365):
            with patch.object(cli_module, "_utcnow", return_value=T0+timedelta(days=days)):
                self.assertEqual(cli(self.path, "registrations-cleanup")[0], 0)
            self.assertEqual(self.conn.execute("SELECT hold_reason FROM registration_control").fetchone()[0], "restore")
        self.assertEqual(self.conn.execute("SELECT * FROM users").fetchall(), accounts_before)
        self.assertEqual(cli(self.path, "registrations-release-hold")[0], 1)
        code, output = cli(self.path, "registrations-release-hold", "--acknowledge-lost-history")
        self.assertEqual(code, 0)
        self.assertIn("registration or recovery", output)
        self.assertIn("repairs no account", output)

    def test_snapshot_fixture_cancel_attempts_redeem_cannot_return_after_gate(self):
        # 8.1 has no redemption API. These are state fixtures, not service evidence.
        for state in ("void", "pending", "redeemed"):
            with self.subTest(state=state):
                self.conn.execute("DELETE FROM password_recoveries")
                add_recovery(self.conn)
                self.conn.commit()
                copied = self.root / (state + ".db")
                with sqlite3.connect(copied) as dst:
                    self.conn.backup(dst)
                if state == "pending":
                    self.conn.execute("UPDATE password_recoveries SET code_attempts=5")
                else:
                    self.conn.execute(f"UPDATE password_recoveries SET state=?, {scrub.SCRUB_ASSIGNMENTS}", (state,))
                self.conn.commit()
                self.assertEqual(cli(copied, "registrations-restore-gate")[0], 0)
                with sqlite3.connect(copied) as dst:
                    self.assertEqual(dst.execute("SELECT state, code_hash FROM password_recoveries").fetchall(), [("void", None)])

    def test_atomic_rollback_at_every_write_of_cleanup_and_gate(self):
        add_recovery(self.conn)
        self.conn.execute("BEGIN IMMEDIATE") if not self.conn.in_transaction else None
        registration.create_pending(self.conn, cleaned=student(), account_type="student",
            password_hash=password_hash(), previous_binding=None, secret_key=KEY,
            clock=lambda: T0, sender_busy=False)
        self.conn.commit()
        original = self.path.read_bytes()
        for command in ("registrations-cleanup", "registrations-restore-gate"):
            writes = []
            class Failing(sqlite3.Connection):
                failure = None
                def execute(inner, sql, params=()):
                    if sql.lstrip().split()[0].upper() in {"UPDATE", "INSERT", "DELETE"}:
                        writes.append(sql)
                        if len(writes) == inner.failure:
                            raise sqlite3.OperationalError("injected")
                    return super().execute(sql, params)
            def open_conn(*args, **kwargs):
                conn = sqlite3.connect(self.path, factory=Failing)
                conn.row_factory = sqlite3.Row
                return conn
            with patch.object(cli_module, "open_database", side_effect=open_conn):
                self.assertEqual(cli(self.path, command)[0], 0)
            count = len(writes)
            self.conn.close()
            self.path.write_bytes(original)
            for i in range(1, count+1):
                writes.clear()
                Failing.failure = i
                with self.subTest(command=command, statement=i), patch.object(cli_module, "open_database", side_effect=open_conn):
                    self.assertEqual(cli(self.path, command)[0], 1)
                self.assertEqual(self.path.read_bytes(), original)

    def test_historical_absence_missing_recorded_extra_missing_columns(self):
        old = historical(self.root, 24)
        self.assertEqual(cli(old, "registrations-restore-gate")[0], 0)
        for mutation in ("DROP TABLE password_recoveries",
                         "ALTER TABLE password_recoveries ADD COLUMN unknown_secret TEXT",
                         "ALTER TABLE password_recoveries RENAME COLUMN code_hash TO other_hash",
                         "ALTER TABLE registration_sends DROP COLUMN recovery_id"):
            copy = self.root / (str(len(mutation)) + ".db")
            with sqlite3.connect(copy) as conn:
                self.conn.backup(conn)
                if "DROP COLUMN" in mutation:
                    conn.execute("DROP INDEX idx_registration_sends_recovery")
                conn.execute(mutation)
                conn.commit()
            before = copy.read_bytes()
            for command in ("registrations-cleanup", "registrations-restore-gate", "registrations-restore-status"):
                code, output = cli(copy, command)
                self.assertEqual(code, 1, output)
                self.assertIn("apply migrations", output)
                self.assertEqual(copy.read_bytes(), before)

    def test_clear_limit_both_purposes_and_settlement_isolation(self):
        address = registration.email_hash(KEY, "fixture@example.bg")
        row = add_send(self.conn, recovery_id=1, email_hash=address)
        add_send(self.conn, pending_id=2, email_hash=address, issuance_id=None)
        legacy = add_send(self.conn, issuance_id="e"*32)
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        before = list(self.conn.iterdump())
        for outcome in ("accepted", "not_sent", "refused", "unknown"):
            self.assertEqual(registration.settle_send(self.conn, row, "d"*32, outcome, clock=lambda:T0),
                             registration.Settlement.NOT_THIS_ISSUANCE)
        self.assertEqual(before, list(self.conn.iterdump()))
        self.assertEqual(registration.settle_send(self.conn, legacy, "e"*32, "accepted", clock=lambda:T0),
                         registration.Settlement.SETTLED)
        self.conn.commit()
        with patch.dict(os.environ, {"DZI_SECRET_KEY": KEY}):
            code, output = cli(self.path, "clear-registration-limit", "--email", "fixture@example.bg")
        self.assertEqual(code, 0, output)
        self.assertIn("recovery budget", output)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM registration_sends WHERE email_hash=?", (address,)).fetchone()[0], 0)

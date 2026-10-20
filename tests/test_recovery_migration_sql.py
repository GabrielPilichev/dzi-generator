"""RA1–RA6: additive upgrade, invariants, legacy ledger compatibility."""
import io
import sqlite3
from contextlib import closing

from src import canonical_tables, manage_migrations
from tests.recovery_support import RecoveryTestCase, add_recovery, add_send, historical, ROOT
from tests.schema_snapshot import compare, snapshot


class MigrationTest(RecoveryTestCase):
    def test_upgrade_preserves_every_row_and_fresh_schema_parity(self):
        path = historical(self.root, 24)
        with closing(sqlite3.connect(path)) as conn:
            for pending in (None, 42):
                add_send(conn, pending_id=pending, issuance_id=None)
            conn.commit()
            before = {t: conn.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall()
                      for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for _ in range(2):
            self.assertEqual(manage_migrations.apply_migrations(path, ROOT / "web/migrations", out=io.StringIO()), 0)
        with closing(sqlite3.connect(path)) as conn:
            for table, rows in before.items():
                if table == "schema_migrations":
                    continue
                after = conn.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()
                if table == "registration_sends":
                    self.assertTrue(all(r[-1] is None for r in after))
                    after = [r[:-1] for r in after]
                self.assertEqual(after, rows, table)
            self.assertEqual(compare(snapshot(conn), snapshot(self.conn)), [])
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_runtime_empty_indexes(self):
        self.assertEqual(canonical_tables.TABLE_CLASSIFICATION["password_recoveries"][0], "runtime")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM password_recoveries").fetchone()[0], 0)
        indexes = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({"idx_password_recoveries_state", "idx_registration_sends_recovery"} <= indexes)
        self.assertNotIn("password_recoveries", canonical_tables.tables_of("canonical"))

    def test_final_columns_pending_identity_and_hash_checks(self):
        for state in ("void", "redeemed", "expired"):
            for col, value in (("email", "x@y.bg"), ("user_id", 1), ("session_key_digest", "a"*64),
                               ("code_hash", "b"*64), ("code_expires_at", "2026-01-01")):
                with self.subTest(state=state, col=col), self.assertRaises(sqlite3.IntegrityError):
                    add_recovery(self.conn, **dict(dict(state=state, email=None, user_id=None,
                        session_key_digest=None, code_hash=None, code_expires_at=None), **{col: value}))
                self.conn.rollback()
        for bad in ({"email": None}, {"state": "other"}, {"is_shadow": 3},
                    {"is_shadow": 0}, {"user_id": 1}, {"session_key_digest": "a"*64},
                    {"code_attempts": -1}, {"send_count": 1.5}):
            with self.subTest(bad=bad), self.assertRaises(sqlite3.IntegrityError):
                add_recovery(self.conn, **bad)
            self.conn.rollback()
        for column in ("binding_hash", "code_hash", "session_key_digest"):
            for value in ("A"*64, "a"*63, "a"*65, "g"*64, "a"*64+"\0", b"a"*64):
                with self.subTest(column=column), self.assertRaises(sqlite3.IntegrityError):
                    add_recovery(self.conn, **{column: value})
                self.conn.rollback()
        add_recovery(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            add_recovery(self.conn)

    def test_ledger_affinity_at_most_one_and_old_inserts(self):
        for value in (0, -1, "-3", "abc", "", "0x10", 5.5, "5.5", b"5"):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                add_send(self.conn, recovery_id=value)
            self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            add_send(self.conn, recovery_id=1, pending_id=1)
        self.conn.rollback()
        for value, expected in (("5", 5), ("5.0", 5), (" 7 ", 7), ("1e2", 100), (5.0, 5)):
            row_id = add_send(self.conn, recovery_id=value, issuance_id=None)
            self.assertEqual(self.conn.execute("SELECT recovery_id, typeof(recovery_id) FROM registration_sends WHERE id=?",
                                               (row_id,)).fetchone(), (expected, "integer"))
        for pending in (None, 4):
            row_id = add_send(self.conn, pending_id=pending, issuance_id=None)
            self.assertIsNone(self.conn.execute("SELECT recovery_id FROM registration_sends WHERE id=?", (row_id,)).fetchone()[0])

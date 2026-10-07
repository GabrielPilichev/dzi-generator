"""Accounts PR 4 — migration 013 (``login_throttle``) and the throttle service.

The table is operational state keyed by an HMAC of the identifier; the service
blocks an identifier after ten failures in fifteen minutes, for fifteen minutes.
Time is always passed in, so nothing here sleeps.
"""

import ast
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import canonical_tables as ct
from tests.support import build_test_database
from web.services import login_throttle as throttle

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "web" / "migrations" / "013_login_throttle.sql"
KEY = "a" * 64
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def minutes(count):
    return timedelta(minutes=count)


def throttle_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(MIGRATION.read_text(encoding="utf-8"))
    return conn


class LoginThrottleMigrationSqlTest(unittest.TestCase):
    def setUp(self):
        self.conn = throttle_connection()
        self.addCleanup(self.conn.close)

    def test_columns_and_defaults(self):
        columns = [row[1] for row in self.conn.execute("PRAGMA table_info(login_throttle)")]
        self.assertEqual(columns, ["identifier_hash", "failure_count", "window_started_at", "blocked_until"])
        self.conn.execute("INSERT INTO login_throttle (identifier_hash) VALUES (?)", (KEY,))
        count, started, blocked = self.conn.execute(
            "SELECT failure_count, window_started_at, blocked_until FROM login_throttle").fetchone()
        self.assertEqual((count, blocked), (0, None))
        self.assertRegex(started, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_only_the_throttle_table_is_created(self):
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"login_throttle"})

    def test_table_is_without_rowid_and_keyed_by_the_hash(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute("SELECT rowid FROM login_throttle")
        self.conn.execute("INSERT INTO login_throttle (identifier_hash) VALUES (?)", (KEY,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO login_throttle (identifier_hash) VALUES (?)", (KEY,))

    def test_identifier_hash_must_be_64_characters(self):
        for bad in ("a" * 63, "a" * 65, "", "ana@example.bg"):
            with self.subTest(value=bad), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute("INSERT INTO login_throttle (identifier_hash) VALUES (?)", (bad,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO login_throttle (identifier_hash) VALUES (NULL)")

    def test_failure_count_cannot_be_negative(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO login_throttle (identifier_hash, failure_count) VALUES (?, -1)", (KEY,))

    def test_window_index_exists(self):
        indexes = {row[1]: row for row in self.conn.execute("PRAGMA index_list(login_throttle)")}
        self.assertIn("idx_login_throttle_window", indexes)
        columns = [row[2] for row in self.conn.execute("PRAGMA index_info(idx_login_throttle_window)")]
        self.assertEqual(columns, ["window_started_at"])

    def test_migration_is_idempotent(self):
        self.conn.executescript(MIGRATION.read_text(encoding="utf-8"))


class LoginThrottleClassificationTest(unittest.TestCase):
    def test_table_is_operational_not_canonical(self):
        self.assertEqual(ct.TABLE_CLASSIFICATION["login_throttle"][0], ct.OPERATIONAL)
        self.assertIn("login_throttle", ct.tables_of(ct.OPERATIONAL))
        self.assertNotIn("login_throttle", ct.tables_of(ct.CANONICAL))
        self.assertNotIn("login_throttle", ct.tables_of(ct.RUNTIME))
        self.assertFalse((ROOT / "data" / "canonical" / "login_throttle.jsonl").exists())

    def test_manifest_records_the_level_and_the_exclusion_only(self):
        manifest = json.loads((ROOT / "data" / "canonical" / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("013_login_throttle.sql", manifest["schema"]["migrations"])
        self.assertIn("login_throttle", manifest["operational_exclusions"])
        self.assertIn("login_throttle", manifest["classification"]["operational"])
        self.assertNotIn("login_throttle", manifest["tables"])
        self.assertNotIn("login_throttle", manifest["canonical_tables"])

    def test_fresh_build_includes_013_with_an_empty_table_and_clean_foreign_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_test_database(Path(tmp) / "fresh.db")
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM login_throttle").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT * FROM pragma_foreign_key_check").fetchall(), [])
                applied = [r[0] for r in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")]
                self.assertIn("013_login_throttle.sql", applied)
                self.assertEqual(ct.classification_problems(conn), [])


class LoginThrottleServiceTest(unittest.TestCase):
    def setUp(self):
        self.conn = throttle_connection()
        self.addCleanup(self.conn.close)

    def row(self, key=KEY):
        return self.conn.execute(
            "SELECT failure_count, window_started_at, blocked_until FROM login_throttle "
            "WHERE identifier_hash = ?", (key,)).fetchone()

    def fail(self, times, at=T0, key=KEY):
        for _ in range(times):
            throttle.record_failure(self.conn, key, at)

    def test_policy_is_ten_failures_in_fifteen_minutes_for_fifteen_minutes(self):
        self.assertEqual((throttle.MAX_FAILURES, throttle.WINDOW, throttle.BLOCK_DURATION),
                         (10, minutes(15), minutes(15)))

    def test_identifier_hash_is_a_keyed_sha256_hex_digest(self):
        digest = throttle.identifier_hash("secret", "ana@example.bg")
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(digest, throttle.identifier_hash(b"secret", "ana@example.bg"))
        self.assertNotEqual(digest, throttle.identifier_hash("other", "ana@example.bg"))
        self.assertNotEqual(digest, throttle.identifier_hash("secret", "ana@example.com"))
        import hashlib
        self.assertNotEqual(digest, hashlib.sha256(b"ana@example.bg").hexdigest())  # keyed, not a bare hash

    def test_unknown_identifier_is_not_blocked_and_reading_writes_nothing(self):
        self.assertFalse(throttle.is_blocked(self.conn, KEY, T0))
        self.assertIsNone(self.row())
        self.assertFalse(self.conn.in_transaction)

    def test_nine_failures_do_not_block_and_the_tenth_does(self):
        self.fail(9)
        self.assertFalse(throttle.is_blocked(self.conn, KEY, T0))
        self.assertEqual(self.row(), (9, "2026-01-01 12:00:00", None))
        self.fail(1, at=T0 + minutes(14))
        self.assertEqual(self.row(), (10, "2026-01-01 12:00:00", "2026-01-01 12:29:00"))
        self.assertTrue(throttle.is_blocked(self.conn, KEY, T0 + minutes(14)))

    def test_block_ends_exactly_at_blocked_until(self):
        self.fail(10)
        self.assertTrue(throttle.is_blocked(self.conn, KEY, T0 + minutes(15) - timedelta(seconds=1)))
        self.assertFalse(throttle.is_blocked(self.conn, KEY, T0 + minutes(15)))

    def test_failures_spread_over_more_than_the_window_never_block(self):
        self.fail(9)
        self.fail(9, at=T0 + minutes(15))  # the old window is over: counting restarts
        self.assertEqual(self.row(), (9, "2026-01-01 12:15:00", None))
        self.assertFalse(throttle.is_blocked(self.conn, KEY, T0 + minutes(15)))

    def test_a_block_outlives_its_window_and_then_the_identifier_starts_over(self):
        self.fail(9)
        self.fail(1, at=T0 + minutes(14))          # blocked until 12:29
        other = "b" * 64
        self.fail(1, at=T0 + minutes(20), key=other)  # a write while the block is active keeps it
        self.assertTrue(throttle.is_blocked(self.conn, KEY, T0 + minutes(20)))
        self.fail(1, at=T0 + minutes(29))
        self.assertEqual(self.row(), (1, "2026-01-01 12:29:00", None))

    def test_stale_rows_of_other_identifiers_are_deleted_on_write(self):
        stale, fresh, blocked = "b" * 64, "c" * 64, "d" * 64
        self.fail(3, key=stale)
        self.fail(10, at=T0 + minutes(10), key=blocked)  # blocked until 12:25
        self.fail(2, at=T0 + minutes(10), key=fresh)
        self.fail(1, at=T0 + minutes(16))
        remaining = {r[0] for r in self.conn.execute("SELECT identifier_hash FROM login_throttle")}
        self.assertEqual(remaining, {fresh, blocked, KEY})

    def test_clear_forgets_the_identifier_only(self):
        other = "b" * 64
        self.fail(10)
        self.fail(1, key=other)
        throttle.clear(self.conn, KEY)
        self.assertFalse(throttle.is_blocked(self.conn, KEY, T0))
        self.assertIsNone(self.row())
        self.assertIsNotNone(self.row(other))

    def test_identifiers_are_counted_separately(self):
        other = "b" * 64
        self.fail(10)
        self.assertFalse(throttle.is_blocked(self.conn, other, T0))

    def test_service_never_commits(self):
        self.fail(1)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertIsNone(self.row())

    def test_service_is_independent_of_flask_and_the_app(self):
        tree = ast.parse((ROOT / "web/services/login_throttle.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertFalse({name for name in imported if name.split(".")[0] in ("flask", "web")})


if __name__ == "__main__":
    unittest.main()

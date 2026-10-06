import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src import canonical_tables as ct
from tests.support import build_test_database

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "web" / "migrations" / "012_users.sql"
KEY = "k" * 32


def users_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(MIGRATION.read_text(encoding="utf-8"))
    return conn


def insert(conn, **overrides):
    values = {"email": "a@b.bg", "display_name": "Ана", "session_key": KEY}
    values.update(overrides)
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    return conn.execute(f"INSERT INTO users ({columns}) VALUES ({marks})", tuple(values.values()))


class UsersMigrationSqlTest(unittest.TestCase):
    def setUp(self):
        self.conn = users_connection()
        self.addCleanup(self.conn.close)

    def test_columns_and_defaults(self):
        columns = {row[1]: row for row in self.conn.execute("PRAGMA table_info(users)")}
        self.assertEqual(
            list(columns),
            ["id", "email", "display_name", "role", "password_hash", "is_active", "session_key",
             "password_changed_at", "last_login_at", "created_at", "updated_at"],
        )
        insert(self.conn)
        row = self.conn.execute("SELECT role, is_active, created_at, updated_at FROM users").fetchone()
        self.assertEqual(row[:2], ("student", 1))
        self.assertTrue(row[2] and row[3])

    def test_no_speculative_tables(self):
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables - {"sqlite_sequence"}, {"users"})

    def test_rejects_invalid_role_and_active(self):
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, role="owner")
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, is_active=2)

    def test_email_invariant(self):
        for bad in ("A@B.bg", " a@b.bg", "a@b.bg ", "@b.bg", "ab", "x" * 250 + "@b.bg"):
            with self.subTest(email=bad), self.assertRaises(sqlite3.IntegrityError):
                insert(self.conn, email=bad)
        insert(self.conn, email=None, display_name="Без имейл")

    def test_duplicate_email_rejected_but_nulls_allowed(self):
        insert(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn)
        insert(self.conn, email=None)
        insert(self.conn, email=None)

    def test_password_requires_email_and_non_empty(self):
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, email=None, password_hash="h")
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, password_hash="")
        insert(self.conn, password_hash="h")

    def test_display_name_and_session_key_bounds(self):
        for name in ("а", "   ", "x" * 81):
            with self.subTest(name=name), self.assertRaises(sqlite3.IntegrityError):
                insert(self.conn, display_name=name)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.conn, session_key="k" * 31)
        insert(self.conn, session_key="k" * 32)


class UsersFreshBuildTest(unittest.TestCase):
    def test_users_is_runtime_not_canonical(self):
        self.assertEqual(ct.TABLE_CLASSIFICATION["users"][0], ct.RUNTIME)
        self.assertIn("users", ct.tables_of(ct.RUNTIME))
        self.assertNotIn("users", ct.tables_of(ct.CANONICAL))
        self.assertFalse((ROOT / "data" / "canonical" / "users.jsonl").exists())

    def test_fresh_build_has_empty_users_and_clean_foreign_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_test_database(Path(tmp) / "fresh.db")
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT * FROM pragma_foreign_key_check").fetchall(), [])
                applied = [r[0] for r in conn.execute("SELECT filename FROM schema_migrations")]
                self.assertIn("012_users.sql", applied)
                self.assertEqual(ct.classification_problems(conn), [])


if __name__ == "__main__":
    unittest.main()

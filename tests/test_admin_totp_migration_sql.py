"""Admin TOTP (Accounts PR T) — migration 016 (``totp_credentials``, ``totp_recovery_codes``).

Applied to in-memory or temporary databases only; never to ``data/questions.db``.
"""

import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src import canonical_tables as ct
from src import manage_migrations
from tests.support import build_test_database

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "web" / "migrations"
MIGRATION = MIGRATIONS / "016_admin_totp.sql"
KEY = "k" * 32


def connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript((MIGRATIONS / "012_users.sql").read_text(encoding="utf-8"))
    return conn


def add_user(conn, email, role, session_key=KEY):
    return conn.execute(
        "INSERT INTO users (email, display_name, role, session_key) VALUES (?, 'Име', ?, ?)",
        (email, role, session_key),
    ).lastrowid


class AdminTotpMigrationSqlTest(unittest.TestCase):
    def setUp(self):
        self.conn = connection()
        self.addCleanup(self.conn.close)
        self.admin = add_user(self.conn, "admin@example.bg", "admin")
        self.conn.executescript(MIGRATION.read_text(encoding="utf-8"))

    def columns(self, table):
        return [row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")]

    def test_number_does_not_take_the_reserved_014_and_015(self):
        names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        self.assertIn("016_admin_totp.sql", names)
        self.assertFalse([name for name in names if name.startswith("016_") and name != "016_admin_totp.sql"])
        self.assertNotIn("014_admin_totp.sql", names)
        self.assertNotIn("015_admin_totp.sql", names)

    def test_columns(self):
        self.assertEqual(self.columns("totp_credentials"),
                         ["user_id", "secret_ciphertext", "confirmed_at", "last_used_step",
                          "last_verified_at", "created_at", "updated_at"])
        self.assertEqual(self.columns("totp_recovery_codes"),
                         ["id", "user_id", "code_hash", "used_at", "created_at"])
        self.assertIn("idx_totp_recovery_codes_user",
                      {row[1] for row in self.conn.execute("PRAGMA index_list(totp_recovery_codes)")})

    def test_no_plaintext_secret_or_code_column(self):
        for table in ("totp_credentials", "totp_recovery_codes"):
            for column in self.columns(table):
                with self.subTest(table=table, column=column):
                    self.assertNotIn(column, {"secret", "code", "otp", "recovery_code", "plaintext"})

    def test_credential_constraints(self):
        insert = "INSERT INTO totp_credentials (user_id, secret_ciphertext, last_used_step) VALUES (?, ?, ?)"
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(insert, (self.admin, "", None))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(insert, (self.admin, "token", -1))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(insert, (9999, "token", None))  # no such user
        self.conn.execute(insert, (self.admin, "token", None))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(insert, (self.admin, "other", None))  # one credential per account

    def test_recovery_codes_belong_to_a_credential_and_go_with_it(self):
        insert = "INSERT INTO totp_recovery_codes (user_id, code_hash) VALUES (?, ?)"
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(insert, (self.admin, "scrypt:x"))  # no credential yet
        self.conn.execute("INSERT INTO totp_credentials (user_id, secret_ciphertext) VALUES (?, 'token')",
                          (self.admin,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(insert, (self.admin, ""))
        self.conn.execute(insert, (self.admin, "scrypt:x"))
        self.conn.execute("DELETE FROM totp_credentials WHERE user_id = ?", (self.admin,))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM totp_recovery_codes").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_migration_is_idempotent(self):
        self.conn.executescript(MIGRATION.read_text(encoding="utf-8"))


class AdminSessionRotationTest(unittest.TestCase):
    def test_only_admin_session_keys_are_rotated(self):
        conn = connection()
        self.addCleanup(conn.close)
        ids = {role: add_user(conn, f"{role}@example.bg", role) for role in ("student", "teacher", "admin")}
        conn.executescript(MIGRATION.read_text(encoding="utf-8"))
        keys = dict(conn.execute("SELECT role, session_key FROM users"))
        self.assertEqual(keys["student"], KEY)
        self.assertEqual(keys["teacher"], KEY)
        self.assertNotEqual(keys["admin"], KEY)
        self.assertRegex(keys["admin"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(ids), 3)


class RunnerUpgradeThrough014And016Test(unittest.TestCase):
    """An existing 013 database upgraded by the real runner through 014 and then 016."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="admin-totp-upgrade-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.path = root / "upgrade.db"
        sqlite3.connect(self.path).close()
        previous = root / "migrations"
        previous.mkdir()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < "014_":
                shutil.copyfile(path, previous / path.name)
        self.assertEqual(manage_migrations.apply_migrations(self.path, previous, out=io.StringIO()), 0)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript(f"""
                PRAGMA foreign_keys = ON;
                INSERT INTO curriculum_sections (id, section_slug, title_bg, class, section_type)
                    VALUES (11, 'legacy', 'Стар раздел', 12, 'section');
                INSERT INTO users (id, email, display_name, role, session_key) VALUES
                    (1, 'admin@example.bg', 'Админ', 'admin', '{KEY}'),
                    (2, 'teacher@example.bg', 'Учител', 'teacher', '{KEY}');
                INSERT INTO quiz_assignments (id, section_id, title_bg, question_count)
                    VALUES (71, 11, 'Стар тест', 2);
                INSERT INTO quiz_attempts (id, assignment_id, student_name, seed, question_ids_json)
                    VALUES (91, 71, 'Ученик', 'seed', '[]');
            """)
            conn.commit()

    def test_both_migrations_apply_in_order_and_preserve_existing_rows(self):
        out = io.StringIO()
        self.assertEqual(manage_migrations.apply_migrations(self.path, MIGRATIONS, out=out), 0)
        applied = out.getvalue().split("applied migrations:")[1]
        # 015 (the quiz-attempt rebuild) arrived after 016 but runs between them.
        self.assertLess(applied.index("014_assignment_owner.sql"), applied.index("015_quiz_attempts_user.sql"))
        self.assertLess(applied.index("015_quiz_attempts_user.sql"), applied.index("016_admin_totp.sql"))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT id, title_bg, owner_user_id FROM quiz_assignments").fetchall(),
                             [(71, "Стар тест", None)])  # nothing backfilled
            self.assertEqual(conn.execute("SELECT id, assignment_id FROM quiz_attempts").fetchall(), [(91, 71)])
            keys = dict(conn.execute("SELECT role, session_key FROM users"))
            self.assertEqual(keys["teacher"], KEY)
            self.assertNotEqual(keys["admin"], KEY)
            for table in ("totp_credentials", "totp_recovery_codes"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            history = [row[0] for row in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")]
            self.assertEqual(
                history[-4:],
                ["014_assignment_owner.sql", "015_quiz_attempts_user.sql", "016_admin_totp.sql",
                 "017_assignment_public_token.sql"],
            )

    def test_reapplying_is_a_noop_and_does_not_rotate_again(self):
        self.assertEqual(manage_migrations.apply_migrations(self.path, MIGRATIONS, out=io.StringIO()), 0)
        with closing(sqlite3.connect(self.path)) as conn:
            rotated = conn.execute("SELECT session_key FROM users WHERE role = 'admin'").fetchone()[0]
        self.assertEqual(manage_migrations.apply_migrations(self.path, MIGRATIONS, out=io.StringIO()), 0)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT session_key FROM users WHERE role = 'admin'").fetchone()[0],
                             rotated)

    def test_016_is_never_adopted_without_running_it(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual(manage_migrations.baseline_status(conn, "016_admin_totp.sql")[0],
                             "manual review required")


class AdminTotpClassificationTest(unittest.TestCase):
    def test_tables_are_runtime_not_canonical(self):
        for table in ("totp_credentials", "totp_recovery_codes"):
            with self.subTest(table=table):
                self.assertEqual(ct.TABLE_CLASSIFICATION[table][0], ct.RUNTIME)
                self.assertNotIn(table, ct.tables_of(ct.CANONICAL))
                self.assertFalse((ROOT / "data" / "canonical" / f"{table}.jsonl").exists())

    def test_manifest_records_the_level_and_the_exclusions(self):
        manifest = json.loads((ROOT / "data" / "canonical" / "manifest.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(manifest["schema"]["migration_level"], "016_admin_totp.sql")
        self.assertIn("016_admin_totp.sql", manifest["schema"]["migrations"])
        for table in ("totp_credentials", "totp_recovery_codes"):
            self.assertIn(table, manifest["runtime_exclusions"])
            self.assertIn(table, manifest["classification"]["runtime"])
            self.assertNotIn(table, manifest["tables"])

    def test_fresh_build_is_at_016_with_empty_tables_and_clean_foreign_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_test_database(Path(tmp) / "fresh.db")
            with closing(sqlite3.connect(path)) as conn:
                for table in ("totp_credentials", "totp_recovery_codes", "users"):
                    self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT * FROM pragma_foreign_key_check").fetchall(), [])
                applied = [r[0] for r in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")]
                self.assertIn("016_admin_totp.sql", applied)
                self.assertEqual(ct.classification_problems(conn), [])


if __name__ == "__main__":
    unittest.main()

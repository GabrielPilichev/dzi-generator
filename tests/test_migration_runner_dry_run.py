import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src import manage_migrations


class MigrationRunnerDryRunTest(unittest.TestCase):
    def make_temp_db(self, *, applied=None):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        db_path = Path(temp_dir.name) / "test.db"
        conn = sqlite3.connect(db_path)
        try:
            if applied is not None:
                conn.execute("""
                    CREATE TABLE schema_migrations (
                        filename TEXT PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    )
                """)
                for filename in applied:
                    conn.execute(
                        "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, CURRENT_TIMESTAMP)",
                        (filename,),
                    )
            conn.commit()
        finally:
            conn.close()
        return temp_dir, db_path

    def make_migrations_dir(self, filenames):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        migrations_dir = Path(temp_dir.name) / "migrations"
        migrations_dir.mkdir()
        for filename in filenames:
            (migrations_dir / filename).write_text("-- test migration\n", encoding="utf-8")
        return temp_dir, migrations_dir

    def test_lists_pending_migrations_without_schema_migrations_table(self):
        _db_temp, db_path = self.make_temp_db()
        _migrations_temp, migrations_dir = self.make_migrations_dir([
            "002_second.sql",
            "001_first.sql",
        ])
        out = io.StringIO()

        exit_code = manage_migrations.main(
            ["--db", str(db_path), "--migrations-dir", str(migrations_dir), "--dry-run"],
            out=out,
            err=io.StringIO(),
        )

        self.assertEqual(exit_code, 0)
        output = out.getvalue()
        self.assertIn("applied migrations:\n  - none", output)
        self.assertIn("  - 001_first.sql", output)
        self.assertIn("  - 002_second.sql", output)
        self.assertIn("DRY RUN ONLY: no changes applied", output)

    def test_detects_applied_migrations_from_temp_db(self):
        _db_temp, db_path = self.make_temp_db(applied=["001_first.sql"])
        _migrations_temp, migrations_dir = self.make_migrations_dir([
            "001_first.sql",
            "002_second.sql",
        ])
        out = io.StringIO()

        exit_code = manage_migrations.main(
            ["--db", str(db_path), "--migrations-dir", str(migrations_dir), "--dry-run"],
            out=out,
            err=io.StringIO(),
        )

        self.assertEqual(exit_code, 0)
        output = out.getvalue()
        self.assertIn("applied migrations:\n  - 001_first.sql", output)
        self.assertIn("pending migrations:\n  - 002_second.sql", output)

    def test_apply_refuses_without_modifying(self):
        _db_temp, db_path = self.make_temp_db()
        _migrations_temp, migrations_dir = self.make_migrations_dir(["001_first.sql"])
        err = io.StringIO()

        exit_code = manage_migrations.main(
            ["--db", str(db_path), "--migrations-dir", str(migrations_dir), "--apply"],
            out=io.StringIO(),
            err=err,
        )

        self.assertEqual(exit_code, 2)
        self.assertIn("--apply is not implemented yet", err.getvalue())
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("""
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'schema_migrations'
            """).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row)

    def test_dry_run_does_not_create_schema_migrations(self):
        _db_temp, db_path = self.make_temp_db()
        _migrations_temp, migrations_dir = self.make_migrations_dir(["001_first.sql"])

        exit_code = manage_migrations.main(
            ["--db", str(db_path), "--migrations-dir", str(migrations_dir), "--dry-run"],
            out=io.StringIO(),
            err=io.StringIO(),
        )

        self.assertEqual(exit_code, 0)
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("""
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'schema_migrations'
            """).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()

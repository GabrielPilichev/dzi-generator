import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import manage_migrations


ROOT = Path(__file__).resolve().parents[1]
REAL_DB_PATH = ROOT / "data" / "questions.db"


class MigrationRunnerDryRunTest(unittest.TestCase):
    def make_temp_db(self, *, applied=None):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        db_path = Path(temp_dir.name) / "test.db"
        self.assertNotEqual(db_path.resolve(), REAL_DB_PATH.resolve())
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

    def make_migrations_dir(self, migrations):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        migrations_dir = Path(temp_dir.name) / "migrations"
        migrations_dir.mkdir()
        if isinstance(migrations, dict):
            items = migrations.items()
        else:
            items = ((filename, "-- test migration\n") for filename in migrations)
        for filename, sql in items:
            (migrations_dir / filename).write_text(sql, encoding="utf-8")
        return temp_dir, migrations_dir

    def make_temp_project_default_db(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        project_root = Path(temp_dir.name)
        db_dir = project_root / "data"
        db_dir.mkdir()
        db_path = db_dir / "questions.db"
        self.assertNotEqual(db_path.resolve(), REAL_DB_PATH.resolve())
        conn = sqlite3.connect(db_path)
        conn.close()
        migrations_dir = project_root / "web" / "migrations"
        migrations_dir.mkdir(parents=True)
        return temp_dir, project_root, db_path, migrations_dir

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

    def test_apply_creates_schema_migrations(self):
        _db_temp, db_path = self.make_temp_db()
        _migrations_temp, migrations_dir = self.make_migrations_dir({
            "001_first.sql": "CREATE TABLE first_table (id INTEGER PRIMARY KEY);\n",
        })

        exit_code = manage_migrations.main(
            ["--db", str(db_path), "--migrations-dir", str(migrations_dir), "--apply"],
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
            applied = conn.execute("SELECT filename FROM schema_migrations").fetchall()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(applied, [("001_first.sql",)])

    def test_explicit_temp_db_apply_does_not_create_local_backups(self):
        _db_temp, db_path = self.make_temp_db()
        _migrations_temp, migrations_dir = self.make_migrations_dir({
            "001_first.sql": "CREATE TABLE first_table (id INTEGER PRIMARY KEY);\n",
        })
        backup_dir = db_path.parent / "local_backups"

        exit_code = manage_migrations.main(
            ["--db", str(db_path), "--migrations-dir", str(migrations_dir), "--apply"],
            out=io.StringIO(),
            err=io.StringIO(),
        )

        self.assertEqual(exit_code, 0)
        self.assertFalse(backup_dir.exists())

    def test_default_db_apply_creates_backup_before_applying_in_temp_project(self):
        _temp, project_root, db_path, migrations_dir = self.make_temp_project_default_db()
        (migrations_dir / "001_first.sql").write_text(
            "CREATE TABLE first_table (id INTEGER PRIMARY KEY);\n",
            encoding="utf-8",
        )
        out = io.StringIO()

        with mock.patch.object(manage_migrations, "PROJECT_ROOT", project_root):
            exit_code = manage_migrations.main(
                ["--db", str(db_path), "--migrations-dir", str(migrations_dir), "--apply"],
                out=out,
                err=io.StringIO(),
            )

        self.assertEqual(exit_code, 0)
        backup_dir = project_root / "local_backups"
        backups = list(backup_dir.glob("questions.db.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn(f"backup path: {backups[0]}", out.getvalue())
        conn = sqlite3.connect(backups[0])
        try:
            first_table = conn.execute("""
                SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'first_table'
            """).fetchone()
            schema_migrations = conn.execute("""
                SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'
            """).fetchone()
        finally:
            conn.close()
        self.assertIsNone(first_table)
        self.assertIsNone(schema_migrations)

    def test_apply_applies_pending_migrations_in_filename_order(self):
        _db_temp, db_path = self.make_temp_db()
        _migrations_temp, migrations_dir = self.make_migrations_dir({
            "002_second.sql": "INSERT INTO migration_order (name) VALUES ('second');\n",
            "001_first.sql": "CREATE TABLE migration_order (name TEXT); INSERT INTO migration_order (name) VALUES ('first');\n",
        })

        exit_code = manage_migrations.main(
            ["--db", str(db_path), "--migrations-dir", str(migrations_dir), "--apply"],
            out=io.StringIO(),
            err=io.StringIO(),
        )

        self.assertEqual(exit_code, 0)
        conn = sqlite3.connect(db_path)
        try:
            values = conn.execute("SELECT name FROM migration_order").fetchall()
            applied = conn.execute("SELECT filename FROM schema_migrations ORDER BY filename").fetchall()
        finally:
            conn.close()
        self.assertEqual(values, [("first",), ("second",)])
        self.assertEqual(applied, [("001_first.sql",), ("002_second.sql",)])

    def test_apply_skips_already_applied_migrations(self):
        _db_temp, db_path = self.make_temp_db(applied=["001_first.sql"])
        _migrations_temp, migrations_dir = self.make_migrations_dir({
            "001_first.sql": "CREATE TABLE should_not_exist (id INTEGER PRIMARY KEY);\n",
            "002_second.sql": "CREATE TABLE second_table (id INTEGER PRIMARY KEY);\n",
        })
        out = io.StringIO()

        exit_code = manage_migrations.main(
            ["--db", str(db_path), "--migrations-dir", str(migrations_dir), "--apply"],
            out=out,
            err=io.StringIO(),
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("skipped migrations:\n  - 001_first.sql", out.getvalue())
        conn = sqlite3.connect(db_path)
        try:
            skipped_table = conn.execute("""
                SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'should_not_exist'
            """).fetchone()
            second_table = conn.execute("""
                SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'second_table'
            """).fetchone()
            applied = conn.execute("SELECT filename FROM schema_migrations ORDER BY filename").fetchall()
        finally:
            conn.close()
        self.assertIsNone(skipped_table)
        self.assertIsNotNone(second_table)
        self.assertEqual(applied, [("001_first.sql",), ("002_second.sql",)])

    def test_failed_migration_exits_nonzero_and_does_not_record_it(self):
        _db_temp, db_path = self.make_temp_db()
        _migrations_temp, migrations_dir = self.make_migrations_dir({
            "001_bad.sql": "CREATE TABLE partial_table (id INTEGER PRIMARY KEY);\nINSERT INTO missing_table VALUES (1);\n",
        })

        exit_code = manage_migrations.main(
            ["--db", str(db_path), "--migrations-dir", str(migrations_dir), "--apply"],
            out=io.StringIO(),
            err=io.StringIO(),
        )

        self.assertNotEqual(exit_code, 0)
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
            partial_table = conn.execute("""
                SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'partial_table'
            """).fetchone()
        finally:
            conn.close()
        self.assertEqual(rows, [])
        self.assertIsNone(partial_table)

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

    def test_dry_run_does_not_create_backup_for_default_db_in_temp_project(self):
        _temp, project_root, db_path, migrations_dir = self.make_temp_project_default_db()
        (migrations_dir / "001_first.sql").write_text("-- dry run only\n", encoding="utf-8")

        with mock.patch.object(manage_migrations, "PROJECT_ROOT", project_root):
            exit_code = manage_migrations.main(
                ["--db", str(db_path), "--migrations-dir", str(migrations_dir), "--dry-run"],
                out=io.StringIO(),
                err=io.StringIO(),
            )

        self.assertEqual(exit_code, 0)
        self.assertFalse((project_root / "local_backups").exists())

    def test_local_backups_directory_is_ignored(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("local_backups/", gitignore)


if __name__ == "__main__":
    unittest.main()

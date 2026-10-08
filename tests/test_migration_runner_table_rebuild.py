"""Table-rebuild migrations and the runner's automatic backup.

Every database here is a temporary file built from synthetic migrations; the
repository's migrations are only parsed, never applied or extended. The real
rebuild migration (015) has its own tests in test_quiz_attempts_user_migration_sql.
"""

import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import manage_migrations
from src.manage_migrations import MigrationError


ROOT = Path(__file__).resolve().parents[1]
REAL_DB_PATH = ROOT / "data" / "questions.db"
DIRECTIVE = "-- learnpilot-migration: table-rebuild"

# parent has a table-level UNIQUE (only a rebuild can drop it), an AUTOINCREMENT
# high-water mark (9) above its largest id (3), a trigger, and two referencing
# tables: one cascading, one plain.
SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE parent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    UNIQUE (name)
);
CREATE TABLE child_cascade (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER NOT NULL REFERENCES parent(id) ON DELETE CASCADE,
    label TEXT NOT NULL
);
CREATE TABLE child_plain (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER NOT NULL REFERENCES parent(id),
    label TEXT NOT NULL
);
CREATE TRIGGER parent_name_guard BEFORE UPDATE OF name ON parent
BEGIN
    SELECT RAISE(ABORT, 'name is immutable');
END;

INSERT INTO parent (id, name) VALUES (1, 'one'), (2, 'two'), (3, 'three'), (9, 'gone');
DELETE FROM parent WHERE id = 9;
INSERT INTO child_cascade (id, parent_id, label) VALUES (10, 1, 'c1'), (11, 2, 'c2'), (12, 2, 'c2b');
INSERT INTO child_plain (id, parent_id, label) VALUES (20, 2, 'p2'), (21, 3, 'p3');
"""

# The SQLite-documented order: build the new table, copy, drop the old one,
# rename. Referencing tables keep pointing at "parent". Index, trigger and the
# AUTOINCREMENT high-water mark are the migration's job, not the runner's.
REBUILD_BODY = """
CREATE TABLE parent_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    owner_id INTEGER
);
INSERT INTO parent_new (id, name) SELECT id, name FROM parent{where};
UPDATE sqlite_sequence
   SET seq = (SELECT seq FROM sqlite_sequence WHERE name = 'parent')
 WHERE name = 'parent_new';
DROP TABLE parent;
ALTER TABLE parent_new RENAME TO parent;
CREATE UNIQUE INDEX uniq_parent_legacy_name ON parent(name) WHERE owner_id IS NULL;
CREATE TRIGGER parent_name_guard BEFORE UPDATE OF name ON parent
BEGIN
    SELECT RAISE(ABORT, 'name is immutable');
END;
"""
VALID_REBUILD_SQL = f"{DIRECTIVE}\nPRAGMA foreign_keys = ON;\n" + REBUILD_BODY.format(where="")
ORPHANING_REBUILD_SQL = f"{DIRECTIVE}\n" + REBUILD_BODY.format(where=" WHERE id <> 2")
FK_PROBE_SQL = """
CREATE TABLE IF NOT EXISTS fk_probe (source TEXT NOT NULL, enabled INTEGER NOT NULL);
INSERT INTO fk_probe SELECT '{source}', foreign_keys FROM pragma_foreign_keys;
"""


def snapshot(db_path):
    """Schema and every row of every table, read without writing."""
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        schema = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )]
        return schema, {name: conn.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall() for name in tables}
    finally:
        conn.close()


def query(db_path, sql, params=()):
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class TableRebuildDirectiveTest(unittest.TestCase):
    def test_exact_directive_in_the_leading_comments_opts_in(self):
        for sql in (
            f"{DIRECTIVE}\nCREATE TABLE t (id INTEGER);\n",
            f"-- why this rebuild exists\n\n  {DIRECTIVE}  \nCREATE TABLE t (id INTEGER);\n",
        ):
            with self.subTest(sql=sql):
                self.assertTrue(manage_migrations.is_table_rebuild_migration("m.sql", sql))

    def test_ordinary_files_and_ordinary_comments_do_not_opt_in(self):
        for sql in (
            "",
            "CREATE TABLE t (id INTEGER);\n",
            "PRAGMA foreign_keys = ON;\n-- a table-rebuild is not needed here\nCREATE TABLE t (id INTEGER);\n",
            "-- see the learnpilot-migration: table-rebuild directive in the docs\nCREATE TABLE t (id INTEGER);\n",
            "CREATE TABLE t (note TEXT DEFAULT 'x -- learnpilot-migration: table-rebuild');\n",
        ):
            with self.subTest(sql=sql):
                self.assertFalse(manage_migrations.is_table_rebuild_migration("m.sql", sql))

    def test_malformed_or_unsupported_directives_are_refused(self):
        for line in (
            "-- learnpilot: table-rebuild",
            "-- learnpilot-migration: rebuild",
            "-- learnpilot-migration: table-rebuild, no-backup",
            "-- learnpilot-migration table-rebuild",
            "-- LearnPilot-Migration: table-rebuild",
            "--learnpilot-migration: table-rebuild",
        ):
            with self.subTest(line=line):
                with self.assertRaises(MigrationError) as caught:
                    manage_migrations.is_table_rebuild_migration("m.sql", f"{line}\nSELECT 1;\n")
                self.assertIn("m.sql:1", str(caught.exception))

    def test_directive_after_sql_or_repeated_is_refused(self):
        for sql in (
            f"CREATE TABLE t (id INTEGER);\n{DIRECTIVE}\n",
            f"{DIRECTIVE}\n{DIRECTIVE}\nCREATE TABLE t (id INTEGER);\n",
        ):
            with self.subTest(sql=sql):
                with self.assertRaises(MigrationError):
                    manage_migrations.is_table_rebuild_migration("m.sql", sql)

    def test_repository_migrations_carry_no_malformed_directive(self):
        migrations_dir = ROOT / "web" / "migrations"
        names = manage_migrations.migration_filenames(migrations_dir)
        manage_migrations.table_rebuild_migrations(migrations_dir, names)


class TableRebuildTestCase(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)
        self.db = self.root / "test.db"
        self.assertNotEqual(self.db.resolve(), REAL_DB_PATH.resolve())
        sqlite3.connect(self.db).close()
        self.migrations = self.root / "migrations"
        self.migrations.mkdir()
        self.write("014_schema.sql", SCHEMA_SQL)
        self.assertEqual(self.apply()[0], 0)
        self.before = snapshot(self.db)

    def write(self, filename, sql):
        (self.migrations / filename).write_text(sql, encoding="utf-8")

    def run_main(self, *flags):
        out, err = io.StringIO(), io.StringIO()
        code = manage_migrations.main(
            ["--db", str(self.db), "--migrations-dir", str(self.migrations), *flags], out=out, err=err
        )
        return code, out.getvalue(), err.getvalue()

    def apply(self):
        return self.run_main("--apply")

    def ledger(self):
        return [row[0] for row in query(self.db, "SELECT filename FROM schema_migrations ORDER BY rowid")]

    def rebuild_directly(self, sql, filename="015_rebuild_parent.sql"):
        """Run the rebuild step on the runner's own kind of connection.

        Returns (exception or None, foreign_keys afterwards, in_transaction afterwards).
        """
        conn = manage_migrations.open_writable_db(self.db)
        try:
            error = None
            try:
                manage_migrations.apply_table_rebuild_migration(conn, filename, sql)
            except Exception as exc:  # noqa: BLE001 - the test inspects it
                error = exc
            return error, conn.execute("PRAGMA foreign_keys").fetchone()[0], conn.in_transaction
        finally:
            conn.close()

    def assert_untouched(self):
        self.assertEqual(snapshot(self.db), self.before)
        self.assertEqual(self.ledger(), ["014_schema.sql"])


class OrdinaryMigrationTest(TableRebuildTestCase):
    def test_ordinary_migration_applies_and_is_recorded_once(self):
        self.write("015_add_column.sql", "ALTER TABLE child_plain ADD COLUMN note TEXT;\n" + FK_PROBE_SQL.format(source="015"))

        code, out, _err = self.apply()

        self.assertEqual(code, 0)
        self.assertIn("  - 015_add_column.sql\n", out)
        self.assertNotIn("table-rebuild", out)
        self.assertEqual(self.ledger(), ["014_schema.sql", "015_add_column.sql"])
        self.assertEqual(query(self.db, "SELECT enabled FROM fk_probe"), [(1,)])
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(self.ledger(), ["014_schema.sql", "015_add_column.sql"])

    def test_ordinary_migration_still_enforces_foreign_keys(self):
        self.write("015_orphan.sql", "INSERT INTO child_plain (id, parent_id, label) VALUES (99, 404, 'orphan');\n")

        code, _out, err = self.apply()

        self.assertEqual(code, 1)
        self.assertIn("FOREIGN KEY", err)
        self.assert_untouched()

    def test_rebuild_sql_without_the_directive_gets_no_rebuild_semantics(self):
        # Foreign keys stay on, so the DROP's implicit delete is refused by
        # child_plain: the rebuild cannot happen by accident.
        self.write("015_rebuild_parent.sql", REBUILD_BODY.format(where=""))

        code, _out, err = self.apply()

        self.assertEqual(code, 1)
        self.assertIn("FOREIGN KEY", err)
        self.assert_untouched()


class ValidRebuildTest(TableRebuildTestCase):
    def test_rebuild_keeps_parent_and_child_rows_and_is_recorded_once(self):
        self.write("015_rebuild_parent.sql", VALID_REBUILD_SQL)

        code, out, err = self.apply()

        self.assertEqual((code, err), (0, ""))
        self.assertIn("  - 015_rebuild_parent.sql (table-rebuild)\n", out)
        self.assertIn("foreign_key_check: ok", out)
        schema_before, data_before = self.before
        _schema, data = snapshot(self.db)
        self.assertEqual(data["parent"], [(pid, name, None) for pid, name in data_before["parent"]])
        self.assertEqual(data["child_cascade"], data_before["child_cascade"])
        self.assertEqual(data["child_plain"], data_before["child_plain"])
        self.assertEqual(
            query(self.db, "SELECT c.id, p.name FROM child_cascade c JOIN parent p ON p.id = c.parent_id ORDER BY c.id"),
            [(10, "one"), (11, "two"), (12, "two")],
        )
        self.assertEqual(query(self.db, "PRAGMA foreign_key_check"), [])
        self.assertEqual(query(self.db, "PRAGMA foreign_key_list(child_cascade)")[0][2], "parent")
        self.assertEqual(self.ledger(), ["014_schema.sql", "015_rebuild_parent.sql"])
        names = {row[0] for row in query(self.db, "SELECT name FROM sqlite_master")}
        self.assertIn("uniq_parent_legacy_name", names)
        self.assertIn("parent_name_guard", names)
        self.assertNotIn("parent_new", names)
        self.assertNotIn("UNIQUE (name)", query(self.db, "SELECT sql FROM sqlite_master WHERE name = 'parent'")[0][0])

        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(self.ledger(), ["014_schema.sql", "015_rebuild_parent.sql"])

    def test_rebuild_can_preserve_the_autoincrement_high_water_mark(self):
        self.assertEqual(query(self.db, "SELECT seq FROM sqlite_sequence WHERE name = 'parent'"), [(9,)])
        self.write("015_rebuild_parent.sql", VALID_REBUILD_SQL)

        self.assertEqual(self.apply()[0], 0)

        self.assertEqual(query(self.db, "SELECT name, seq FROM sqlite_sequence"), [("parent", 9)])
        conn = sqlite3.connect(self.db)
        try:
            new_id = conn.execute("INSERT INTO parent (name) VALUES ('four')").lastrowid
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(new_id, 10)

    def test_directive_behind_a_utf8_byte_order_mark_still_gets_rebuild_behavior(self):
        path = self.migrations / "015_rebuild_parent.sql"
        path.write_text(VALID_REBUILD_SQL, encoding="utf-8-sig")
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf" + DIRECTIVE.encode()))

        self.assertIn("  - 015_rebuild_parent.sql (table-rebuild)\n", self.run_main("--dry-run")[1])
        code, out, err = self.apply()

        # Run as an ordinary migration, the DROP would fail on child_plain and
        # cascade into child_cascade.
        self.assertEqual((code, err), (0, ""))
        self.assertIn("  - 015_rebuild_parent.sql (table-rebuild)\n", out)
        _schema, data = snapshot(self.db)
        self.assertEqual(data["child_cascade"], self.before[1]["child_cascade"])
        self.assertEqual(data["child_plain"], self.before[1]["child_plain"])
        self.assertEqual(self.ledger(), ["014_schema.sql", "015_rebuild_parent.sql"])

    def test_malformed_directive_behind_a_utf8_byte_order_mark_is_still_refused(self):
        (self.migrations / "015_rebuild_parent.sql").write_text(
            "-- learnpilot-migration: rebuild\n" + REBUILD_BODY.format(where=""), encoding="utf-8-sig"
        )

        for flag in ("--dry-run", "--apply"):
            with self.subTest(flag=flag):
                code, _out, err = self.run_main(flag)
                self.assertEqual(code, 1)
                self.assertIn("015_rebuild_parent.sql:1: unsupported migration directive", err)
                self.assert_untouched()

    def test_foreign_keys_are_off_inside_and_on_after_the_rebuild(self):
        error, foreign_keys, in_transaction = self.rebuild_directly(
            VALID_REBUILD_SQL + FK_PROBE_SQL.format(source="rebuild")
        )

        self.assertIsNone(error)
        self.assertEqual((foreign_keys, in_transaction), (1, False))
        self.assertEqual(query(self.db, "SELECT source, enabled FROM fk_probe"), [("rebuild", 0)])
        self.assertEqual(self.ledger(), ["014_schema.sql", "015_rebuild_parent.sql"])

    def test_foreign_keys_are_back_on_before_the_next_migration_runs(self):
        self.write("015_rebuild_parent.sql", VALID_REBUILD_SQL)
        self.write("016_probe.sql", FK_PROBE_SQL.format(source="016"))
        self.write("017_orphan.sql", "INSERT INTO child_plain (id, parent_id, label) VALUES (99, 404, 'orphan');\n")

        code, _out, err = self.apply()

        self.assertEqual(code, 1)
        self.assertIn("FOREIGN KEY", err)
        self.assertEqual(query(self.db, "SELECT source, enabled FROM fk_probe"), [("016", 1)])
        self.assertEqual(self.ledger(), ["014_schema.sql", "015_rebuild_parent.sql", "016_probe.sql"])


class FailedRebuildTest(TableRebuildTestCase):
    def assert_rebuild_fails_cleanly(self, sql, error_type, message):
        error, foreign_keys, in_transaction = self.rebuild_directly(sql)
        self.assertIsInstance(error, error_type)
        self.assertIn(message, str(error))
        self.assertEqual((foreign_keys, in_transaction), (1, False))
        self.assert_untouched()

    def test_foreign_key_violation_rolls_everything_back(self):
        self.assert_rebuild_fails_cleanly(ORPHANING_REBUILD_SQL, MigrationError, "foreign_key_check found 3 violation(s)")

    def test_foreign_key_violation_through_the_cli_stops_the_run(self):
        self.write("015_rebuild_parent.sql", ORPHANING_REBUILD_SQL)
        self.write("016_later.sql", "CREATE TABLE later_table (id INTEGER PRIMARY KEY);\n")

        code, _out, err = self.apply()

        self.assertEqual(code, 1)
        self.assertIn("015_rebuild_parent.sql rolled back", err)
        self.assert_untouched()

    def test_sql_failure_after_the_drop_rolls_everything_back(self):
        sql = VALID_REBUILD_SQL + "INSERT INTO missing_table VALUES (1);\n"
        self.assert_rebuild_fails_cleanly(sql, sqlite3.OperationalError, "missing_table")

        self.write("015_rebuild_parent.sql", sql)
        self.assertEqual(self.apply()[0], 1)
        self.assert_untouched()

    def test_ledger_failure_rolls_the_rebuild_back(self):
        sql = VALID_REBUILD_SQL + "INSERT INTO schema_migrations (filename) VALUES ('015_rebuild_parent.sql');\n"
        self.assert_rebuild_fails_cleanly(sql, sqlite3.IntegrityError, "schema_migrations.filename")

    def test_failure_during_validation_rolls_the_rebuild_back(self):
        with mock.patch.object(
            manage_migrations, "foreign_key_check_rows", side_effect=sqlite3.OperationalError("check failed")
        ):
            self.assert_rebuild_fails_cleanly(VALID_REBUILD_SQL, sqlite3.OperationalError, "check failed")

    def test_nothing_is_visible_to_another_connection_before_the_commit(self):
        seen = []

        def check_from_outside(conn):
            seen.append(snapshot(self.db))
            return conn.execute("PRAGMA foreign_key_check").fetchall()

        with mock.patch.object(manage_migrations, "foreign_key_check_rows", side_effect=check_from_outside):
            error, foreign_keys, _in_transaction = self.rebuild_directly(VALID_REBUILD_SQL)

        self.assertIsNone(error)
        self.assertEqual(foreign_keys, 1)
        self.assertEqual(seen, [self.before])
        self.assertEqual(self.ledger(), ["014_schema.sql", "015_rebuild_parent.sql"])

    def test_migration_sql_cannot_leave_the_runner_transaction(self):
        # None of these is line-leading: SQLite's parser, not a text match,
        # refuses them, and what ran before them is rolled back.
        mutation = "DROP TABLE child_plain; DELETE FROM child_cascade; CREATE TABLE escaped (id INTEGER PRIMARY KEY)"
        other_db = self.root / "other.db"
        cases = {
            "COMMIT": "transaction control",
            "commit": "transaction control",
            "END": "transaction control",
            "END TRANSACTION": "transaction control",
            "ROLLBACK": "transaction control",
            "BEGIN": "transaction control",
            "BEGIN EXCLUSIVE": "transaction control",
            "COMMIT; BEGIN": "transaction control",
            "SAVEPOINT s": "a savepoint",
            "RELEASE s": "a savepoint",
            "ROLLBACK TO s": "a savepoint",
            f"ATTACH '{other_db}' AS other": "ATTACH",
            "DETACH main": "DETACH",
            "/* hidden */ COMMIT": "transaction control",
        }
        for statement, reason in cases.items():
            with self.subTest(statement=statement):
                sql = f"{DIRECTIVE}\n{mutation}; {statement}; CREATE TABLE after_escape (id INTEGER PRIMARY KEY);\n"
                self.assert_rebuild_fails_cleanly(sql, MigrationError, f"{reason} statement is not allowed")
        self.assertFalse(other_db.exists())

    def test_escape_attempt_through_the_cli_changes_nothing(self):
        self.write("015_rebuild_parent.sql", VALID_REBUILD_SQL.rstrip() + " COMMIT; DROP TABLE child_plain;\n")
        self.write("016_later.sql", "CREATE TABLE later_table (id INTEGER PRIMARY KEY);\n")

        code, _out, err = self.apply()

        self.assertEqual(code, 1)
        self.assertIn("015_rebuild_parent.sql rolled back: transaction control", err)
        self.assert_untouched()

    def test_transaction_words_that_are_not_statements_are_accepted(self):
        sql = VALID_REBUILD_SQL + """
-- COMMIT; ROLLBACK; BEGIN; ATTACH 'x' AS y;
/* COMMIT;
   ROLLBACK; SAVEPOINT s; */
CREATE TABLE words (body TEXT NOT NULL DEFAULT 'x; COMMIT; y', "COMMIT" TEXT);
INSERT INTO words (body) VALUES ('BEGIN; ROLLBACK; END; DETACH main;');
CREATE TRIGGER words_guard BEFORE DELETE ON words
BEGIN
    SELECT RAISE(ABORT, 'COMMIT; is only text here');
END;
"""
        error, foreign_keys, in_transaction = self.rebuild_directly(sql)

        self.assertIsNone(error)
        self.assertEqual((foreign_keys, in_transaction), (1, False))
        self.assertEqual(self.ledger(), ["014_schema.sql", "015_rebuild_parent.sql"])
        self.assertEqual(
            query(self.db, "SELECT body FROM words"), [("BEGIN; ROLLBACK; END; DETACH main;",)]
        )
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM child_cascade"), [(3,)])

    def test_statement_that_rolls_the_transaction_back_itself_fails_the_migration(self):
        # ON CONFLICT ROLLBACK ends the transaction inside SQLite; the statement
        # still fails, so the script stops and nothing after it runs.
        sql = (
            f"{DIRECTIVE}\nDROP TABLE child_plain;\n"
            "INSERT OR ROLLBACK INTO parent (id, name) VALUES (1, 'duplicate');\n"
            "CREATE TABLE after_rollback (id INTEGER PRIMARY KEY);\n"
        )
        self.assert_rebuild_fails_cleanly(sql, sqlite3.IntegrityError, "UNIQUE constraint failed")

    def test_no_authorizer_is_left_on_the_connection(self):
        for sql in (VALID_REBUILD_SQL, f"{DIRECTIVE}\nSELECT 1; COMMIT;\n"):
            with self.subTest(sql=sql[-30:]):
                conn = manage_migrations.open_writable_db(self.db)
                try:
                    try:
                        manage_migrations.apply_table_rebuild_migration(conn, f"{len(sql)}_rebuild.sql", sql)
                    except MigrationError:
                        pass
                    # Each of these is denied while the rebuild authorizer is installed.
                    conn.executescript("BEGIN; SAVEPOINT s; RELEASE s; COMMIT; BEGIN; ROLLBACK;")
                    conn.execute("ATTACH ':memory:' AS scratch")
                    conn.execute("DETACH scratch")
                finally:
                    conn.close()

    def test_ordinary_migration_after_a_rebuild_is_not_restricted(self):
        self.write("015_rebuild_parent.sql", VALID_REBUILD_SQL)
        self.write("016_savepoint.sql", "SAVEPOINT s;\nCREATE TABLE later_table (id INTEGER PRIMARY KEY);\nRELEASE s;\n")

        code, _out, err = self.apply()

        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.ledger(), ["014_schema.sql", "015_rebuild_parent.sql", "016_savepoint.sql"])

    def test_rebuild_refuses_to_start_inside_an_open_transaction(self):
        conn = manage_migrations.open_writable_db(self.db)
        try:
            conn.execute("BEGIN")
            with self.assertRaises(MigrationError):
                manage_migrations.apply_table_rebuild_migration(conn, "015_rebuild_parent.sql", VALID_REBUILD_SQL)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            conn.close()
        self.assert_untouched()

    def test_malformed_directive_stops_the_run_before_any_migration(self):
        self.write("015_first.sql", "CREATE TABLE first_table (id INTEGER PRIMARY KEY);\n")
        self.write("016_rebuild.sql", "-- learnpilot: table-rebuild\n" + REBUILD_BODY.format(where=""))

        code, _out, err = self.apply()

        self.assertEqual(code, 1)
        self.assertIn("016_rebuild.sql:1", err)
        self.assert_untouched()


class MigrationOrderTest(TableRebuildTestCase):
    LATER_SQL = "UPDATE parent SET owner_id = 5 WHERE id = 1;\n" + FK_PROBE_SQL.format(source="016")

    def test_lower_numbered_rebuild_is_planned_and_applied_after_a_recorded_016(self):
        self.write("016_later.sql", "CREATE TABLE later_table (id INTEGER PRIMARY KEY);\n")
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(self.ledger(), ["014_schema.sql", "016_later.sql"])
        self.write("015_rebuild_parent.sql", VALID_REBUILD_SQL)
        recorded = snapshot(self.db)

        code, out, _err = self.run_main("--dry-run")

        self.assertEqual(code, 0)
        self.assertIn("pending migrations:\n  - 015_rebuild_parent.sql (table-rebuild)\n", out)
        self.assertEqual(snapshot(self.db), recorded)

        code, out, _err = self.apply()

        self.assertEqual(code, 0)
        self.assertIn("skipped migrations:\n  - 014_schema.sql\n  - 016_later.sql\n", out)
        self.assertIn("applied migrations:\n  - 015_rebuild_parent.sql (table-rebuild)\n", out)
        self.assertEqual(self.ledger(), ["014_schema.sql", "016_later.sql", "015_rebuild_parent.sql"])
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM child_cascade"), [(3,)])
        self.assertEqual(self.run_main("--dry-run")[1].count("  - none"), 1)

    def test_fresh_database_runs_015_before_016(self):
        fresh = self.root / "fresh.db"
        sqlite3.connect(fresh).close()
        self.write("015_rebuild_parent.sql", VALID_REBUILD_SQL)
        self.write("016_later.sql", self.LATER_SQL)  # needs the column 015 adds

        code = manage_migrations.apply_migrations(fresh, self.migrations, out=io.StringIO())

        self.assertEqual(code, 0)
        self.assertEqual(
            [row[0] for row in query(fresh, "SELECT filename FROM schema_migrations ORDER BY rowid")],
            ["014_schema.sql", "015_rebuild_parent.sql", "016_later.sql"],
        )
        self.assertEqual(query(fresh, "SELECT owner_id FROM parent WHERE id = 1"), [(5,)])
        self.assertEqual(query(fresh, "SELECT source, enabled FROM fk_probe"), [("016", 1)])
        self.assertEqual(query(fresh, "PRAGMA foreign_key_check"), [])


class ReadOnlyPathsTest(TableRebuildTestCase):
    def test_dry_run_and_baseline_paths_never_execute_a_pending_rebuild(self):
        self.write("015_rebuild_parent.sql", VALID_REBUILD_SQL)

        for flag in ("--dry-run", "--baseline-dry-run", "--baseline-apply"):
            with self.subTest(flag=flag):
                code, out, err = self.run_main(flag)
                self.assertEqual((code, err), (0, ""))
                self.assertIn("015_rebuild_parent.sql", out)
                self.assert_untouched()

    def test_dry_run_reports_a_malformed_directive(self):
        self.write("015_rebuild_parent.sql", "-- learnpilot-migration: rebuild\nSELECT 1;\n")

        code, _out, err = self.run_main("--dry-run")

        self.assertEqual(code, 1)
        self.assertIn("unsupported migration directive", err)
        self.assert_untouched()


class AutomaticBackupTest(unittest.TestCase):
    """The backup taken for the repository-default database path (a temp project here)."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.project = Path(temp_dir.name)
        (self.project / "data").mkdir()
        self.db = self.project / "data" / "questions.db"
        self.assertNotEqual(self.db.resolve(), REAL_DB_PATH.resolve())
        self.migrations = self.project / "web" / "migrations"
        self.migrations.mkdir(parents=True)
        (self.migrations / "001_first.sql").write_text(
            "CREATE TABLE first_table (id INTEGER PRIMARY KEY);\n", encoding="utf-8"
        )
        patcher = mock.patch.object(manage_migrations, "PROJECT_ROOT", self.project)
        patcher.start()
        self.addCleanup(patcher.stop)
        # A writer that stays open keeps the committed rows in the -wal file:
        # nothing checkpoints them into the main database file.
        self.writer = sqlite3.connect(self.db)
        self.addCleanup(self.writer.close)
        self.assertEqual(self.writer.execute("PRAGMA journal_mode = WAL").fetchone()[0], "wal")
        self.writer.execute("PRAGMA wal_autocheckpoint = 0")
        self.writer.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        self.writer.executemany("INSERT INTO notes (body) VALUES (?)", [(f"row {n}",) for n in range(200)])
        self.writer.commit()

    def apply(self):
        out, err = io.StringIO(), io.StringIO()
        code = manage_migrations.main(
            ["--db", str(self.db), "--migrations-dir", str(self.migrations), "--apply"], out=out, err=err
        )
        return code, out.getvalue(), err.getvalue()

    def backups(self):
        return sorted((self.project / "local_backups").glob("questions.db.backup-*"))

    def test_backup_contains_committed_rows_that_are_only_in_the_wal(self):
        self.assertGreater(Path(f"{self.db}-wal").stat().st_size, 0)
        main_file_only = self.project / "main-file-only.db"
        main_file_only.write_bytes(self.db.read_bytes())
        with self.assertRaises(sqlite3.Error):  # what a plain file copy would have kept
            query(main_file_only, "SELECT COUNT(*) FROM notes")

        code, out, err = self.apply()

        self.assertEqual((code, err), (0, ""))
        (backup,) = self.backups()
        self.assertIn(f"backup path: {backup}", out)
        self.assertEqual(sorted(path.name for path in backup.parent.iterdir()), [backup.name])
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        standalone = self.project / "standalone" / "copy.db"
        standalone.parent.mkdir()
        standalone.write_bytes(backup.read_bytes())
        self.assertEqual(query(standalone, "SELECT COUNT(*), MAX(body) FROM notes"), [(200, "row 99")])
        self.assertEqual(query(standalone, "PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(query(standalone, "PRAGMA journal_mode"), [("delete",)])
        # Taken before the migration ran.
        self.assertEqual(query(standalone, "SELECT name FROM sqlite_master WHERE name = 'first_table'"), [])
        self.assertEqual(query(self.db, "SELECT name FROM sqlite_master WHERE name = 'first_table'"), [("first_table",)])

    def test_backups_in_the_same_second_do_not_overwrite_each_other(self):
        first = manage_migrations.create_backup_for_default_db(self.db)
        second = manage_migrations.create_backup_for_default_db(self.db)

        self.assertNotEqual(first, second)
        self.assertEqual(self.backups(), sorted([first, second]))

    def test_migration_does_not_start_when_the_backup_fails(self):
        (self.project / "local_backups").write_text("not a directory", encoding="utf-8")

        for flag in ("--apply", "--baseline-apply"):
            with self.subTest(flag=flag):
                err = io.StringIO()
                code = manage_migrations.main(
                    ["--db", str(self.db), "--migrations-dir", str(self.migrations), flag],
                    out=io.StringIO(), err=err,
                )
                self.assertEqual(code, 1)
                self.assertIn("nothing was migrated", err.getvalue())
                self.assertEqual(
                    [row[0] for row in self.writer.execute("SELECT name FROM sqlite_master ORDER BY name")],
                    ["notes"],
                )

    def test_backup_that_fails_verification_is_not_published(self):
        self.writer.close()
        self.db.write_bytes(b"this is not a sqlite database, " * 200)

        with self.assertRaises(MigrationError):
            manage_migrations.create_backup_for_default_db(self.db)

        self.assertEqual(list((self.project / "local_backups").iterdir()), [])


if __name__ == "__main__":
    unittest.main()

"""Schema-only baseline migration (G0): 000 + 001-011 build the schema, and an existing
pre-baseline database adopts 000 without changing anything else.

Every database here is a temporary file. The pre-baseline database is a small synthetic one
(``tests.support.build_legacy_database``): the pre-000 schema at migration level 010 plus a few
rows. No binary fixture is involved.
"""

import hashlib
import io
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src import manage_migrations
from src.schema_introspection import INTERNAL_TABLES, user_table_names
from tests.schema_snapshot import compare, snapshot
from tests.support import LEGACY_LEVEL, ROOT, build_legacy_database
from web import production

MIGRATIONS_DIR = ROOT / "web" / "migrations"
BASELINE = MIGRATIONS_DIR / "000_baseline.sql"
ALL_MIGRATIONS = sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql"))
NUMBERED = [name for name in ALL_MIGRATIONS if name != "000_baseline.sql"]
# A pre-baseline database sits at migration level 010; later migrations are pending on it.
PENDING_ON_LEGACY = [name for name in ALL_MIGRATIONS if name > LEGACY_LEVEL]
LEGACY_MIGRATIONS = [name for name in ALL_MIGRATIONS if name <= LEGACY_LEVEL]


def run_runner(db_path, *flags):
    out, err = io.StringIO(), io.StringIO()
    code = manage_migrations.main(
        ["--db", str(db_path), "--migrations-dir", str(MIGRATIONS_DIR), *flags], out=out, err=err
    )
    return code, out.getvalue(), err.getvalue()


def schema_of(db_path, *, bookkeeping=True):
    with closing(sqlite3.connect(db_path)) as conn:
        shot = snapshot(conn)
    if not bookkeeping:
        shot["tables"].pop("schema_migrations", None)
    return shot


def content_digests(db_path):
    """Per-table hash of every row, excluding migration bookkeeping."""
    digests = {}
    with closing(sqlite3.connect(db_path)) as conn:
        for table in user_table_names(conn):
            if table == "schema_migrations":
                continue
            digest = hashlib.sha256()
            for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
                digest.update(repr(row).encode("utf-8"))
            digests[table] = (conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0], digest.hexdigest())
    return digests


def migration_rows(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT filename, applied_at FROM schema_migrations ORDER BY filename").fetchall()


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class BaselineTestCase(unittest.TestCase):
    def tempdir(self):
        directory = tempfile.TemporaryDirectory(prefix="learnpilot-baseline-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def empty_db(self):
        path = self.tempdir() / "empty.db"
        sqlite3.connect(path).close()
        return path

    def fresh_db(self):
        path = self.empty_db()
        code, out, err = run_runner(path, "--apply")
        self.assertEqual(code, 0, out + err)
        return path

    def legacy_db(self):
        return build_legacy_database(self.tempdir() / "legacy.db")

    def legacy_db_at_current_level(self):
        path = self.legacy_db()
        self.assertEqual(run_runner(path, "--baseline-apply")[0], 0)
        code, out, err = run_runner(path, "--apply")
        self.assertEqual(code, 0, out + err)
        return path

    def db_from_baseline_sql(self, sql):
        path = self.tempdir() / "partial.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(sql)
        return path


class BaselineOwnershipTest(BaselineTestCase):
    """000 owns the pre-001 schema; every later object has exactly one owner."""

    @staticmethod
    def created_objects(sql):
        sql = re.sub(r"--[^\n]*", "", sql)
        tables = re.findall(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", sql, re.I)
        indexes = re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", sql, re.I)
        columns = re.findall(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", sql, re.I)
        return set(tables), set(indexes), set(columns)

    def test_baseline_creates_nothing_owned_by_a_numbered_migration(self):
        base_tables, base_indexes, base_columns = self.created_objects(BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(base_columns, set())
        self.assertGreater(len(base_tables), 20)
        owned_columns = set()
        for name in NUMBERED:
            tables, indexes, columns = self.created_objects((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
            self.assertEqual(base_tables & tables, set(), name)
            self.assertEqual(base_indexes & indexes, set(), name)
            owned_columns |= columns
        # Baseline-only schema: ALTER-added columns are absent until their migration runs.
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.executescript(BASELINE.read_text(encoding="utf-8"))
            for table, column in owned_columns:
                present = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
                self.assertNotIn(column, present, f"{table}.{column} belongs to a numbered migration")
        self.assertIn(("curriculum_sections", "source_url"), owned_columns)
        self.assertIn(("topic_section_assignments", "display_order"), owned_columns)

    def test_baseline_is_schema_only(self):
        sql = re.sub(r"--[^\n]*", "", BASELINE.read_text(encoding="utf-8"))
        statements = re.findall(r"(?im)^\s*(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER)\b", sql)
        self.assertEqual(statements, [])
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.executescript(BASELINE.read_text(encoding="utf-8"))
            for table in user_table_names(conn):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0], 0, table)

    def test_every_table_is_created_by_exactly_one_migration(self):
        owners = {}
        for name in ALL_MIGRATIONS:
            tables, _indexes, _columns = self.created_objects((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
            for table in tables:
                owners.setdefault(table, []).append(name)
        self.assertEqual({t: o for t, o in owners.items() if len(o) > 1}, {})
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.executescript(BASELINE.read_text(encoding="utf-8"))
            baseline_tables = set(user_table_names(conn))
        self.assertTrue(baseline_tables.issuperset({"questions", "curriculum_topics", "exams", "fill_in_subquestions"}))


class FreshDatabaseTest(BaselineTestCase):
    def test_empty_database_applies_every_migration(self):
        path = self.empty_db()
        code, out, err = run_runner(path, "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([name for name, _ in migration_rows(path)], ALL_MIGRATIONS)
        self.assertEqual(ALL_MIGRATIONS[0], "000_baseline.sql")
        self.assertEqual(ALL_MIGRATIONS[-1], "012_users.sql")
        self.assertIn("foreign_key_check: ok", out)

    def test_fresh_database_has_no_content_rows(self):
        digests = content_digests(self.fresh_db())
        self.assertEqual({table: count for table, (count, _) in digests.items() if count}, {})

    def test_fresh_schema_equals_an_adopted_existing_database(self):
        """Hard gate: building from scratch and adopting a pre-baseline database agree, logically."""
        differences = compare(
            schema_of(self.fresh_db()), schema_of(self.legacy_db_at_current_level()), "migrated", "adopted"
        )
        self.assertEqual(differences, [], "\n".join(differences))

    def test_baseline_alone_is_not_the_final_schema(self):
        # Guards the ownership rule: 001-010 must still have work to do after 000.
        path = self.empty_db()
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(BASELINE.read_text(encoding="utf-8"))
        self.assertNotEqual(compare(schema_of(path), schema_of(self.legacy_db())), [])
        with closing(sqlite3.connect(path)) as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(curriculum_sections)")}
            tsa = {r[1] for r in conn.execute("PRAGMA table_info(topic_section_assignments)")}
        self.assertNotIn("dzi_relevance_notes", columns)
        self.assertNotIn("display_order", tsa)


class SchemaComparatorTest(BaselineTestCase):
    """The parity gate must be able to fail."""

    def mutated(self, statement):
        path = self.fresh_db()
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(statement)
        return compare(schema_of(path), schema_of(self.fresh_db()))

    def test_detects_schema_drift(self):
        for statement in (
            "ALTER TABLE questions ADD COLUMN extra TEXT",
            "CREATE INDEX extra_idx ON questions(prompt)",
            "DROP INDEX idx_questions_topic",
            "DROP INDEX uniq_questions_source_exam_number",
            "CREATE TABLE extra_table (id INTEGER)",
            "DROP TABLE scrape_log",
        ):
            with self.subTest(statement=statement):
                self.assertNotEqual(self.mutated(statement), [])

    def test_detects_constraint_drift(self):
        base = BASELINE.read_text(encoding="utf-8")
        variants = {
            "check": base.replace("'short_answer'", "'short_answer', 'extra_type'"),
            "default": base.replace("difficulty TEXT DEFAULT 'medium',\n    points", "difficulty TEXT DEFAULT 'easy',\n    points"),
            "not null": base.replace("title_bg TEXT NOT NULL,\n    class INTEGER NOT NULL,\n    section_type", "title_bg TEXT,\n    class INTEGER NOT NULL,\n    section_type"),
            "autoincrement": base.replace("CREATE TABLE IF NOT EXISTS generated_exams (\n    id INTEGER PRIMARY KEY AUTOINCREMENT", "CREATE TABLE IF NOT EXISTS generated_exams (\n    id INTEGER PRIMARY KEY"),
            "foreign key": base.replace("REFERENCES curriculum_sections(id) ON DELETE CASCADE,\n    lesson_number", "REFERENCES curriculum_sections(id),\n    lesson_number"),
            "partial index": base.replace("WHERE module_slug IS NOT NULL", "WHERE module_slug IS NOT NULL AND module_number > 0"),
        }
        reference = schema_of(self.fresh_db())
        for label, sql in variants.items():
            with self.subTest(label=label):
                self.assertNotEqual(sql, base, "variant did not change the baseline")
                path = self.tempdir() / f"{label.replace(' ', '_')}.db"
                with closing(sqlite3.connect(path)) as conn:
                    conn.executescript(sql)
                    for name in NUMBERED:
                        conn.executescript((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
                self.assertNotEqual(compare(schema_of(path), reference), [])


class ExistingDatabaseAdoptionTest(BaselineTestCase):
    def test_legacy_database_is_baselineable_and_dry_run_writes_nothing(self):
        path = self.legacy_db()
        before = file_sha256(path)
        code, out, err = run_runner(path, "--baseline-dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("000_baseline.sql: baselineable", out)
        self.assertIn("remaining pending migrations:\n  - " + "\n  - ".join(PENDING_ON_LEGACY), out)
        self.assertEqual(file_sha256(path), before)
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])

    def test_baseline_apply_changes_only_migration_bookkeeping(self):
        path = self.legacy_db()
        schema_before, content_before = schema_of(path), content_digests(path)
        rows_before = migration_rows(path)
        self.assertNotIn("000_baseline.sql", [name for name, _ in rows_before])

        code, out, err = run_runner(path, "--baseline-apply")
        self.assertEqual(code, 0, out + err)
        self.assertIn("  - 000_baseline.sql", out)
        self.assertIn("foreign_key_check: ok", out)

        self.assertEqual(compare(schema_before, schema_of(path)), [])
        self.assertEqual(content_digests(path), content_before)
        rows_after = migration_rows(path)
        self.assertEqual([name for name, _ in rows_after], LEGACY_MIGRATIONS)
        # History is preserved byte for byte; only 000 is new.
        self.assertEqual([row for row in rows_after if row[0] != "000_baseline.sql"], rows_before)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_baseline_apply_is_idempotent(self):
        path = self.legacy_db()
        self.assertEqual(run_runner(path, "--baseline-apply")[0], 0)
        schema, content, rows = schema_of(path), content_digests(path), migration_rows(path)
        code, out, err = run_runner(path, "--baseline-apply")
        self.assertEqual(code, 0, out + err)
        self.assertIn("recorded baseline migrations:\n  - none", out)
        self.assertEqual(compare(schema, schema_of(path)), [])
        self.assertEqual(content_digests(path), content)
        self.assertEqual(migration_rows(path), rows)
        code, out, _ = run_runner(path, "--baseline-dry-run")
        self.assertNotIn("000_baseline.sql", out.split("remaining pending")[0])

    def test_normal_apply_after_adoption_has_nothing_to_do(self):
        path = self.legacy_db()
        run_runner(path, "--baseline-apply")
        rows = migration_rows(path)
        code, out, err = run_runner(path, "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertIn("applied migrations:\n  - " + "\n  - ".join(PENDING_ON_LEGACY), out)
        self.assertEqual([name for name, _ in migration_rows(path)], ALL_MIGRATIONS)
        code, out, err = run_runner(path, "--apply")
        self.assertIn("applied migrations:\n  - none", out)

    def test_legacy_database_without_adoption_has_pending_000(self):
        path = self.legacy_db()
        pending = production.pending_schema_migrations(path, lambda p: sqlite3.connect(p))
        self.assertEqual(pending, ["000_baseline.sql", *PENDING_ON_LEGACY])
        run_runner(path, "--baseline-apply")
        self.assertEqual(production.pending_schema_migrations(path, lambda p: sqlite3.connect(p)), PENDING_ON_LEGACY)


class IncompatibleDatabaseTest(BaselineTestCase):
    def assert_not_adoptable(self, path):
        schema, content = schema_of(path, bookkeeping=False), content_digests(path)
        code, out, err = run_runner(path, "--baseline-dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("000_baseline.sql: not baselineable", out)
        self.assertNotIn("000_baseline.sql: baselineable", out)
        code, out, err = run_runner(path, "--baseline-apply")
        self.assertEqual(code, 0, err)
        self.assertIn("remaining pending migrations:\n  - 000_baseline.sql", out)
        with closing(sqlite3.connect(path)) as conn:
            recorded = [r[0] for r in conn.execute("SELECT filename FROM schema_migrations")]
        self.assertNotIn("000_baseline.sql", recorded)
        self.assertEqual(compare(schema, schema_of(path, bookkeeping=False)), [])
        self.assertEqual(content_digests(path), content)

    def test_single_baseline_table_is_not_enough(self):
        path = self.tempdir() / "one-table.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE questions (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        self.assert_not_adoptable(path)

    def test_missing_column_is_rejected(self):
        sql = BASELINE.read_text(encoding="utf-8").replace("    quality_score REAL,", "")
        self.assertNotEqual(sql, BASELINE.read_text(encoding="utf-8"))
        self.assert_not_adoptable(self.db_from_baseline_sql(sql))

    def test_wrong_column_shape_is_rejected(self):
        sql = BASELINE.read_text(encoding="utf-8").replace(
            "event_slug TEXT UNIQUE NOT NULL", "event_slug TEXT UNIQUE"
        )
        self.assert_not_adoptable(self.db_from_baseline_sql(sql))

    def test_missing_index_is_rejected(self):
        sql = BASELINE.read_text(encoding="utf-8").replace(
            "CREATE INDEX IF NOT EXISTS idx_questions_topic ON questions(topic);", ""
        )
        self.assertNotEqual(sql, BASELINE.read_text(encoding="utf-8"))
        self.assert_not_adoptable(self.db_from_baseline_sql(sql))

    def test_missing_check_constraint_is_rejected(self):
        sql = BASELINE.read_text(encoding="utf-8").replace(
            "    CHECK (topic_id != requires_topic_id)\n", "    CHECK (topic_id != requires_topic_id OR 1)\n"
        )
        self.assertNotEqual(sql, BASELINE.read_text(encoding="utf-8"))
        self.assert_not_adoptable(self.db_from_baseline_sql(sql))

    def test_missing_table_is_rejected_and_named(self):
        sql = BASELINE.read_text(encoding="utf-8").replace(
            "CREATE TABLE IF NOT EXISTS scrape_log (", "CREATE TABLE IF NOT EXISTS scrape_log_renamed ("
        ).replace("ON scrape_log(url)", "ON scrape_log_renamed(url)")
        path = self.db_from_baseline_sql(sql)
        code, out, _ = run_runner(path, "--baseline-dry-run")
        self.assertIn("table scrape_log is missing", out)

    def test_empty_database_is_not_adopted_and_runs_normally(self):
        path = self.empty_db()
        code, out, _ = run_runner(path, "--baseline-dry-run")
        self.assertIn("000_baseline.sql: not baselineable (database is empty", out)
        run_runner(path, "--baseline-apply")
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE tbl_name <> 'schema_migrations'").fetchone()[0], 0
            )
        code, out, err = run_runner(path, "--apply")
        self.assertEqual(code, 0, out + err)

    def test_normal_apply_refuses_a_non_empty_database_without_000(self):
        path = self.tempdir() / "partial.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE questions (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        before = file_sha256(path)
        code, out, err = run_runner(path, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("--baseline-apply", err)
        self.assertEqual(file_sha256(path), before)  # nothing written, not even schema_migrations

    def test_normal_apply_refuses_a_complete_legacy_database_until_adopted(self):
        path = self.legacy_db()
        before = file_sha256(path)
        code, _out, err = run_runner(path, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("000_baseline.sql", err)
        self.assertEqual(file_sha256(path), before)


class BaselineScopeTest(unittest.TestCase):
    def test_application_code_never_references_the_baseline(self):
        for path in sorted((ROOT / "web").rglob("*.py")):
            self.assertNotIn("000_baseline", path.read_text(encoding="utf-8"), str(path))

    def test_internal_tables_are_not_part_of_the_comparison(self):
        self.assertIn("sqlite_sequence", INTERNAL_TABLES)


if __name__ == "__main__":
    unittest.main()

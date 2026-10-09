"""018 adds the assignment lifecycle columns and the clock row additively.

Eight nullable columns on ``quiz_assignments`` and the single-row
``lifecycle_clock`` table, seeded with a fixed sentinel. No rebuild, no backfill,
nothing of a later phase. Every database here is temporary and built by the
shared migration runner from the repository's migration files. Case ids refer to
docs/ASSIGNMENT_LIFECYCLE_DESIGN.md, section 15.1 (L70, L93, L140).
"""

import io
import json
import re
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path

from src import canonical_tables as ct
from src import manage_migrations
from tests.support import ROOT, build_test_database
from tests.test_assignment_public_token_migration_sql import POPULATE_SQL
from web.services import lifecycle

MIGRATIONS = ROOT / "web" / "migrations"
MIGRATION = "018_assignment_lifecycle.sql"
COLUMNS = ("deadline_at", "display_timezone", "closed_at", "closed_by_user_id", "expiry_observed_at",
           "submissions_closed_at", "retention_base_at", "lifecycle_hold")
CLOCK_COLUMNS = ("id", "high_water_at", "state", "state_reason", "state_changed_at", "ref_wall_at",
                 "ref_boot_id", "ref_boottime_ms")


def apply(path, migrations_dir=MIGRATIONS):
    out = io.StringIO()
    code = manage_migrations.apply_migrations(path, migrations_dir, out=out)
    return code, out.getvalue()


def statements() -> str:
    """The migration without its comments."""
    return "\n".join(line for line in (MIGRATIONS / MIGRATION).read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("--"))


class LifecycleMigrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lifecycle-migration-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "upgrade.db"
        sqlite3.connect(self.path).close()
        previous = self.root / "migrations"
        previous.mkdir()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < MIGRATION:
                shutil.copyfile(path, previous / path.name)
        self.assertEqual(apply(self.path, previous)[0], 0)
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(POPULATE_SQL)
        self.conn.commit()

    def migrate(self):
        code, report = apply(self.path)
        self.assertEqual(code, 0, report)
        return report

    def contents(self):
        return {name: self.conn.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall()
                for (name,) in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'schema_migrations'")}

    def test_l93_upgrade_adds_eight_null_columns_and_changes_no_row(self):
        before = self.contents()
        self.migrate()
        after = self.contents()
        self.assertEqual(after.pop("quiz_assignments"),
                         [(*row, *([None] * 8)) for row in before.pop("quiz_assignments")])
        self.assertEqual(after.pop("lifecycle_clock"), [(1, "1970-01-01 00:00:00", "ok", None, None, None, None, None)])
        self.assertEqual(after, before)  # attempts, answers, uploads, accounts: untouched
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        new_id = self.conn.execute(
            "INSERT INTO quiz_assignments (section_id, title_bg, question_count) VALUES (11, 'Нов', 1)").lastrowid
        self.assertEqual(new_id, 1001)  # AUTOINCREMENT continues: no id is reused

    def test_l93_the_columns_are_nullable_without_default_and_in_the_approved_order(self):
        indexes_before = sorted(row[1] for row in self.conn.execute("PRAGMA index_list(quiz_assignments)"))
        self.migrate()
        info = {row[1]: row for row in self.conn.execute("PRAGMA table_info(quiz_assignments)")}
        self.assertEqual(tuple(info)[-8:], COLUMNS)
        for name in COLUMNS:
            _cid, _name, declared, notnull, default, pk = info[name]
            self.assertEqual((declared, notnull, default, pk),
                             ("INTEGER" if name == "closed_by_user_id" else "TEXT", 0, None, 0), name)
        foreign_keys = {row[3]: (row[2], row[4]) for row in self.conn.execute(
            "PRAGMA foreign_key_list(quiz_assignments)")}
        self.assertEqual(foreign_keys["closed_by_user_id"], ("users", "id"))
        # No index is added: request paths read one row by primary key or by the token.
        self.assertEqual(sorted(row[1] for row in self.conn.execute("PRAGMA index_list(quiz_assignments)")),
                         indexes_before)
        self.assertIn("idx_quiz_assignments_public_token", indexes_before)

    def test_l93_the_clock_table_holds_exactly_one_well_formed_seed_row(self):
        self.migrate()
        info = self.conn.execute("PRAGMA table_info(lifecycle_clock)").fetchall()
        self.assertEqual(tuple(row[1] for row in info), CLOCK_COLUMNS)
        with self.assertRaises(sqlite3.IntegrityError):  # at most one row
            self.conn.execute("INSERT INTO lifecycle_clock (id, high_water_at) VALUES (2, '2026-01-01 00:00:00')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE lifecycle_clock SET state = 'unknown'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE lifecycle_clock SET high_water_at = NULL")
        clock = lifecycle.read_clock_row(self.conn)
        self.assertEqual((clock.high_water_at, clock.state, clock.has_anchor), (datetime(1970, 1, 1), "ok", False))
        self.assertEqual(lifecycle.CLOCK_SEED, "1970-01-01 00:00:00")

    def test_l93_it_is_a_plain_additive_migration_not_a_rebuild(self):
        before_root = self.conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE type = 'table' AND name = 'quiz_assignments'").fetchone()
        dry = io.StringIO()
        manage_migrations.dry_run(self.path, MIGRATIONS, out=dry)
        self.assertIn(f"pending migrations:\n  - {MIGRATION}\n", dry.getvalue())
        report = self.migrate()
        self.assertIn(f"applied migrations:\n  - {MIGRATION}\n", report)
        self.assertNotIn("table-rebuild", report)
        self.assertEqual(
            self.conn.execute(
                "SELECT rootpage FROM sqlite_master WHERE type = 'table' AND name = 'quiz_assignments'").fetchone(),
            before_root)
        self.assertFalse(manage_migrations.is_table_rebuild_migration(
            MIGRATION, (MIGRATIONS / MIGRATION).read_text(encoding="utf-8")))

    def test_reapplying_is_a_noop_and_never_resets_the_clock_row(self):
        self.migrate()
        self.conn.execute("UPDATE lifecycle_clock SET high_water_at = '2026-03-02 09:00:00', state = 'suspect', "
                          "state_reason = 'clock_step'")
        self.conn.execute("UPDATE quiz_assignments SET deadline_at = '2026-04-03 21:00:00', "
                          "display_timezone = 'Europe/Sofia' WHERE id = 71")
        self.conn.commit()
        before = self.contents()
        report = self.migrate()
        self.assertNotIn(MIGRATION, report.split("applied migrations:")[-1].split("skipped")[0])
        self.assertEqual(self.contents(), before)
        # Even run by hand again, the seed is INSERT OR IGNORE: an existing row is kept.
        with self.assertRaises(sqlite3.OperationalError):  # the columns already exist
            self.conn.executescript((MIGRATIONS / MIGRATION).read_text(encoding="utf-8"))
        self.conn.execute("INSERT OR IGNORE INTO lifecycle_clock (id, high_water_at) VALUES (1, '1970-01-01 00:00:00')")
        self.assertEqual(self.conn.execute("SELECT high_water_at, state FROM lifecycle_clock").fetchall(),
                         [("2026-03-02 09:00:00", "suspect")])

    def test_upgraded_schema_equals_a_fresh_database(self):
        self.migrate()
        fresh_path = self.root / "fresh.db"
        sqlite3.connect(fresh_path).close()
        self.assertEqual(apply(fresh_path)[0], 0)
        with closing(sqlite3.connect(fresh_path)) as fresh:
            for table in ("quiz_assignments", "lifecycle_clock"):
                self.assertEqual(fresh.execute(f"PRAGMA table_info({table})").fetchall(),
                                 self.conn.execute(f"PRAGMA table_info({table})").fetchall())
                self.assertEqual(
                    fresh.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchall(),
                    self.conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchall())
            self.assertEqual(fresh.execute("SELECT * FROM lifecycle_clock").fetchall(),
                             self.conn.execute("SELECT * FROM lifecycle_clock").fetchall())
            ledger = [row[0] for row in fresh.execute("SELECT filename FROM schema_migrations ORDER BY rowid")]
        self.assertEqual(ledger[ledger.index("017_assignment_public_token.sql") + 1], MIGRATION)

    def test_l70_rows_from_before_the_migration_are_open_with_no_deadline(self):
        self.migrate()
        self.conn.row_factory = sqlite3.Row
        for assignment_id in (71, 81):
            row = lifecycle.fetch_lifecycle_row(self.conn, assignment_id)
            self.assertEqual(row, {"id": assignment_id, **{column: None for column in COLUMNS}})
            for now in (datetime(2020, 1, 1), datetime(2026, 3, 2), datetime(2040, 1, 1)):
                view = lifecycle.evaluate(row, now)
                self.assertEqual((view.state, view.is_closed, view.is_held), (lifecycle.STATE_OPEN, False, False))
            # Its existing attempts stay submittable: nothing was closed retroactively.
            attempt = self.conn.execute("SELECT started_at, submitted_at FROM quiz_attempts WHERE id = 91").fetchone()
            self.assertTrue(lifecycle.attempt_submittable(lifecycle.evaluate(row, datetime(2030, 1, 1)), attempt))

    def test_018_is_never_adopted_without_running_it(self):
        self.conn.row_factory = sqlite3.Row
        self.assertEqual(manage_migrations.baseline_status(self.conn, MIGRATION)[0], "manual review required")


class LifecycleMigrationTextTest(unittest.TestCase):
    def test_l93_the_file_is_the_approved_schema_and_nothing_else(self):
        sql = statements()
        added = re.findall(r"ALTER TABLE quiz_assignments ADD COLUMN (\w+) (\w+)", sql)
        self.assertEqual(added, [(name, "INTEGER" if name == "closed_by_user_id" else "TEXT") for name in COLUMNS])
        self.assertEqual(len(re.findall(r"\bADD COLUMN\b", sql)), 8)
        self.assertIn("closed_by_user_id INTEGER REFERENCES users(id)", sql)
        self.assertEqual(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql), ["lifecycle_clock"])
        self.assertIn("id INTEGER PRIMARY KEY CHECK (id = 1)", sql)
        self.assertIn("state TEXT NOT NULL DEFAULT 'ok' CHECK (state IN ('ok', 'suspect'))", sql)
        self.assertIn("INSERT OR IGNORE INTO lifecycle_clock (id, high_water_at) VALUES (1, '1970-01-01 00:00:00');",
                      sql)
        self.assertEqual(len(re.findall(r"\bINSERT\b", sql)), 1)
        self.assertNotIn("learnpilot-migration", (MIGRATIONS / MIGRATION).read_text(encoding="utf-8"))

    def test_l140_nothing_destructive_and_nothing_of_a_later_phase(self):
        sql = statements().upper()
        for forbidden in ("DROP ", "DELETE ", "UPDATE ", "RENAME", "CREATE INDEX", "CREATE TRIGGER",
                          "CURRENT_TIMESTAMP", "DETAILS_CLEANED_AT", "CLOSURE_UPPER_BOUND_AT", "DATABASE_IDENTITY",
                          "HISTORY", "LEDGER", "NOT NULL DEFAULT CURRENT"):
            self.assertNotIn(forbidden, sql)
        # No deadline and no zone is backfilled: existing tests stay open.
        self.assertNotIn("EUROPE/SOFIA", sql)

    def test_the_files_are_numbered_without_a_collision(self):
        names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        self.assertEqual([name for name in names if name.startswith("018_")], [MIGRATION])
        self.assertEqual(names[names.index(MIGRATION) - 1], "017_assignment_public_token.sql")
        self.assertEqual([name[:3] for name in names], [f"{n:03d}" for n in range(len(names))])


class LifecycleClassificationTest(unittest.TestCase):
    def test_the_clock_is_runtime_state_with_one_seeded_row(self):
        self.assertEqual(ct.TABLE_CLASSIFICATION["lifecycle_clock"][0], ct.RUNTIME)
        self.assertEqual(ct.TABLE_CLASSIFICATION["quiz_assignments"][0], ct.RUNTIME)
        self.assertEqual(ct.MIGRATION_SEEDED_ROWS, {"lifecycle_clock": 1})
        manifest = json.loads((ROOT / "data" / "canonical" / "manifest.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(manifest["schema"]["migration_level"], MIGRATION)
        self.assertIn(MIGRATION, manifest["schema"]["migrations"])
        self.assertIn("lifecycle_clock", manifest["runtime_exclusions"])
        self.assertIn("lifecycle_clock", manifest["classification"]["runtime"])
        self.assertNotIn("lifecycle_clock", manifest["tables"])   # never exported to data/canonical
        self.assertFalse((ROOT / "data" / "canonical" / "lifecycle_clock.jsonl").exists())

    def test_a_fresh_canonical_build_has_the_schema_the_seed_and_clean_foreign_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_test_database(Path(tmp) / "fresh.db")
            with closing(sqlite3.connect(path)) as conn:
                applied = [r[0] for r in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")]
                self.assertIn(MIGRATION, applied)
                self.assertTrue(set(COLUMNS) <= {r[1] for r in conn.execute("PRAGMA table_info(quiz_assignments)")})
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM lifecycle_clock").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_assignments").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT * FROM pragma_foreign_key_check").fetchall(), [])
                self.assertEqual(ct.classification_problems(conn), [])


if __name__ == "__main__":
    unittest.main()

"""024 adds the assignment creation ledger (EMAIL_REGISTRATION PR 7).

One new table, ``assignment_creations``, and one index. **No row is written**: the history of
assignments that already exist is recorded by the first admission of each owner's next creation
(``tests.test_assignment_accounting``), under one set of timestamp rules. Nothing else changes. Every database in
this module is temporary: built by the shared migration runner from the repository's
migration files, or cloned by ``tests.support.build_test_database``. None of them is
``data/questions.db``.
"""

import io
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src import canonical_tables as ct
from src import manage_migrations
from tests.support import ROOT, build_test_database
from tests.test_assignment_public_token_migration_sql import POPULATE_SQL
from tests.test_user_identities_migration_sql import ACCOUNTS_SQL

MIGRATIONS = ROOT / "web" / "migrations"
MIGRATION = "024_assignment_creations.sql"
TABLE = "assignment_creations"
EPOCH = "1970-01-01 00:00:00"

# On top of POPULATE_SQL (assignment 71, owner 51, created_at '2025-01-01': not in the
# lifecycle layout; assignment 81, no owner) and ACCOUNTS_SQL (61 admin, 62 disabled teacher).
EXTRA_SQL = """
INSERT INTO quiz_assignments (id, section_id, title_bg, question_count, created_at, owner_user_id) VALUES
    (72, 11, 'Валиден час', 1, '2026-03-02 09:00:00', 51),
    (73, 11, 'Друг учител', 1, '2026-03-02 10:30:15', 62),
    (74, 11, 'Администратор', 1, '2026-03-03 08:00:00', 61),
    (75, 11, 'ISO със T', 1, '2026-03-02T09:00:00', 51),
    (76, 11, 'Без час', 1, NULL, 51);
UPDATE quiz_assignments SET created_at = NULL WHERE id = 76;
UPDATE quiz_assignments SET closed_at = '2026-03-02 12:00:00', closed_by_user_id = 51 WHERE id = 72;
"""


def apply(path, migrations_dir=MIGRATIONS):
    out = io.StringIO()
    code = manage_migrations.apply_migrations(path, migrations_dir, out=out)
    return code, out.getvalue()


def migration_text() -> str:
    return (MIGRATIONS / MIGRATION).read_text(encoding="utf-8")


def statements() -> str:
    """The migration without its comments."""
    return "\n".join(line for line in migration_text().splitlines() if not line.lstrip().startswith("--"))


class UpgradedDatabaseTestCase(unittest.TestCase):
    """A database at migration 023 that already holds owned, unowned and closed assignments."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="creations-migration-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "upgrade.db"
        sqlite3.connect(self.path).close()
        previous = self.root / "through-023"
        previous.mkdir()
        self.through = self.root / "through-024"
        self.through.mkdir()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < MIGRATION:
                shutil.copyfile(path, previous / path.name)
            if path.name <= MIGRATION:
                shutil.copyfile(path, self.through / path.name)
        self.assertEqual(apply(self.path, previous)[0], 0)
        self.conn = None
        self.reopen()
        self.conn.executescript(POPULATE_SQL + ACCOUNTS_SQL + EXTRA_SQL)
        self.conn.commit()

    def reopen(self):
        if self.conn is not None:
            self.conn.close()
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys = ON")

    def migrate(self):
        code, report = apply(self.path, self.through)
        self.assertEqual(code, 0, report)
        self.reopen()
        return report

    def contents(self, skip=()):
        return {name: self.conn.execute(f'SELECT * FROM "{name}" ORDER BY 1, 2').fetchall()
                for (name,) in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'schema_migrations'")
                if name not in skip}

    def ledger(self):
        return self.conn.execute(
            f"SELECT owner_user_id, assignment_id, created_at FROM {TABLE} ORDER BY assignment_id").fetchall()


class UpgradeTest(UpgradedDatabaseTestCase):
    def test_nothing_but_the_new_table_and_index_changes(self):
        before = self.contents()
        objects_before = self.conn.execute(
            "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master ORDER BY type, name").fetchall()
        self.migrate()
        after = self.contents(skip=(TABLE,))
        self.assertEqual(after, before)  # every other table, row for row
        objects = self.conn.execute(
            "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master ORDER BY type, name").fetchall()
        added = {row[1] for row in objects if row not in objects_before}
        self.assertEqual(added, {TABLE, "idx_assignment_creations_owner", f"sqlite_autoindex_{TABLE}_1"})
        self.assertEqual({row[1] for row in objects_before} - {row[1] for row in objects}, set())
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])

    def test_the_migration_writes_no_ledger_row(self):
        self.migrate()
        self.assertEqual(self.ledger(), [])
        # Every owned assignment awaits recording (including the badly dated ones, which the
        # first admission of their owner places or refuses); the unowned one is nobody's.
        from web.services import assignment_quota

        self.assertEqual([row[0] for row in assignment_quota.unrecorded_assignments(self.conn)],
                         [71, 72, 73, 74, 75, 76])

    def test_existing_assignments_are_not_touched_or_closed(self):
        before = self.conn.execute("SELECT * FROM quiz_assignments ORDER BY id").fetchall()
        self.migrate()
        self.assertEqual(self.conn.execute("SELECT * FROM quiz_assignments ORDER BY id").fetchall(), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 4)

    def test_it_is_a_plain_additive_migration_not_a_rebuild(self):
        dry = io.StringIO()
        manage_migrations.dry_run(self.path, self.through, out=dry)
        self.assertIn(f"pending migrations:\n  - {MIGRATION}\n", dry.getvalue())
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ?", (TABLE,)).fetchone())
        report = self.migrate()
        self.assertIn(f"applied migrations:\n  - {MIGRATION}\n", report)
        self.assertIn("foreign_key_check: ok", report)
        self.assertNotIn("table-rebuild", report)
        self.assertFalse(manage_migrations.is_table_rebuild_migration(MIGRATION, migration_text()))
        sql = statements().upper()
        for forbidden in ("DROP ", "ALTER ", "DELETE FROM", "UPDATE ", "TRIGGER", "INSERT "):
            self.assertNotIn(forbidden, sql)

    def test_reapplying_is_a_noop_in_the_runner(self):
        self.migrate()
        before = self.ledger()
        code, report = apply(self.path, self.through)
        self.assertEqual(code, 0, report)
        self.reopen()
        self.assertEqual(self.ledger(), before)

    def test_code_that_predates_the_migration_creates_assignments_unchanged(self):
        """Explicit column lists: an insert that does not know the ledger still works (it just is not counted)."""
        self.migrate()
        self.conn.execute("INSERT INTO quiz_assignments (section_id, title_bg, question_count, owner_user_id) "
                          "VALUES (11, 'Старият код', 1, 51)")
        self.conn.commit()
        self.assertEqual(self.ledger(), [])    # it records nothing: web.services.assignment_quota.reconcile does


class TableDefinitionTest(UpgradedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.migrate()
        self.conn.execute(
            "INSERT INTO assignment_creations (owner_user_id, assignment_id, created_at) VALUES "
            "(51, 72, '2026-03-02 09:00:00'), (62, 73, '2026-03-02 10:30:15'), (61, 74, '2026-03-03 08:00:00')")
        self.conn.commit()

    def test_columns(self):
        columns = [(row[1], row[2], row[3], row[5]) for row in self.conn.execute(f"PRAGMA table_info({TABLE})")]
        self.assertEqual(columns, [("id", "INTEGER", 0, 1), ("owner_user_id", "INTEGER", 1, 0),
                                   ("assignment_id", "INTEGER", 1, 0), ("created_at", "TEXT", 1, 0)])

    def test_only_the_owner_is_a_foreign_key_and_it_restricts(self):
        keys = self.conn.execute(f"PRAGMA foreign_key_list({TABLE})").fetchall()
        self.assertEqual([(row[2], row[3], row[4], row[6]) for row in keys], [("users", "owner_user_id", "id", "RESTRICT")])
        # No foreign key to quiz_assignments: that is what keeps a deletion from touching the ledger.
        self.assertNotIn("quiz_assignments", [row[2] for row in keys])

    def test_assignment_is_counted_once(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(f"INSERT INTO {TABLE} (owner_user_id, assignment_id, created_at) "
                              "VALUES (51, 72, '2026-03-02 09:00:00')")

    def test_the_stored_time_must_be_in_the_lifecycle_layout(self):
        bad = ["2026-03-02", "2026-03-02T09:00:00", "2026-3-2 09:00:00", "26-03-02 09:00:00", "", " 2026-03-02 09:00:00",
               "2026-03-02 09:00:00 ", "2026-03-02 09:00:00Z", "abcd-ef-gh ij:kl:mn", "2026-03-02 09:00:0\x00",
               "2026-03-02 09:00:00\x00", 20260302090000]
        for index, value in enumerate(bad):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(f"INSERT INTO {TABLE} (owner_user_id, assignment_id, created_at) VALUES (51, ?, ?)",
                                  (500 + index, value))
        self.assertEqual(len(self.ledger()), 3)

    def test_owner_and_assignment_are_required_and_the_owner_must_exist(self):
        for owner, assignment in ((None, 600), (51, None), (51, 0), (51, -1), (51, "seven"), (9999, 601)):
            with self.subTest(owner=owner, assignment=assignment), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(f"INSERT INTO {TABLE} (owner_user_id, assignment_id, created_at) "
                                  "VALUES (?, ?, '2026-03-02 09:00:00')", (owner, assignment))

    def test_deleting_an_assignment_leaves_its_creation_counted(self):
        before = self.ledger()
        self.conn.execute("DELETE FROM quiz_assignments WHERE id = 73")
        self.conn.commit()
        self.assertEqual(self.ledger(), before)

    def test_an_account_with_history_cannot_be_deleted(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM users WHERE id = 62")

    def test_the_owner_index_serves_the_window_query(self):
        plan = " ".join(row[3] for row in self.conn.execute(
            f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM {TABLE} WHERE owner_user_id = 51 AND created_at > '2026-03-01 00:00:00'"))
        self.assertIn("idx_assignment_creations_owner", plan)


class FreshDatabaseTest(unittest.TestCase):
    def test_a_fresh_build_has_the_table_empty_and_the_same_definition_as_an_upgrade(self):
        directory = tempfile.TemporaryDirectory(prefix="creations-fresh-")
        self.addCleanup(directory.cleanup)
        path = build_test_database(Path(directory.name) / "fresh.db")
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0], 0)
            fresh = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE tbl_name = ? ORDER BY type, name",
                                 (TABLE,)).fetchall()
        upgraded_root = Path(directory.name) / "up"
        upgraded_root.mkdir()
        upgraded = upgraded_root / "u.db"
        sqlite3.connect(upgraded).close()
        self.assertEqual(apply(upgraded)[0], 0)
        with closing(sqlite3.connect(upgraded)) as conn:
            upgrade = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE tbl_name = ? ORDER BY type, name",
                                   (TABLE,)).fetchall()
        self.assertEqual(fresh, upgrade)

    def test_the_table_is_runtime_state_and_never_canonical(self):
        self.assertIn(TABLE, ct.tables_of(ct.RUNTIME))
        directory = tempfile.TemporaryDirectory(prefix="creations-class-")
        self.addCleanup(directory.cleanup)
        path = build_test_database(Path(directory.name) / "class.db")
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(ct.classification_problems(conn), [])


if __name__ == "__main__":
    unittest.main()

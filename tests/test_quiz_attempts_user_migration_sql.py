"""015 rebuilds quiz_attempts: account ownership and partial uniqueness.

Every database is a temporary file migrated by the real shared runner from the
repository's migration files. The existing-database tests record 016 first and
only then let the runner find 015, as on a deployed database.
"""

import io
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src import canonical_tables, manage_migrations
from tests import schema_snapshot
from tests.support import ROOT

MIGRATIONS = ROOT / "web" / "migrations"
MIGRATION = "015_quiz_attempts_user.sql"
REAL_DB_PATH = ROOT / "data" / "questions.db"
SESSION_KEY = "12345678901234567890123456789012"

OLD_COLUMNS = (
    "id", "assignment_id", "student_name", "seed", "question_ids_json",
    "started_at", "submitted_at", "score_correct", "score_total",
)
# (name, unique, key columns, partial predicate)
EXPECTED_INDEXES = {
    ("idx_quiz_attempts_assignment", 0, ("assignment_id",), None),
    ("uniq_quiz_attempts_user", 1, ("assignment_id", "user_id"), "user_id IS NOT NULL"),
    ("uniq_quiz_attempts_legacy_name", 1, ("assignment_id", "student_name"), "user_id IS NULL"),
    ("idx_quiz_attempts_user", 0, ("user_id", "started_at", "id"), "user_id IS NOT NULL"),
}
# child table -> (column, ON DELETE)
CHILD_FOREIGN_KEYS = {
    "quiz_answers": ("attempt_id", "NO ACTION"),
    "quiz_text_answers": ("attempt_id", "CASCADE"),
    "practical_submissions": ("quiz_attempt_id", "CASCADE"),
}

# Historical attempts with every column filled differently, children of each
# kind, a deleted attempt (9) that leaves the sequence above MAX(id), and a
# second assignment reusing a student name.
POPULATE_SQL = f"""
    INSERT INTO curriculum_sections (id, section_slug, title_bg, class, section_type)
        VALUES (11, 'legacy', 'Стар раздел', 12, 'section');
    INSERT INTO questions (id, source_exam, question_type, points, prompt)
        VALUES (21, 'legacy', 'short_answer', 3, 'Въпрос');
    INSERT INTO exams (id, subject, level, year, source_file)
        VALUES (31, 'informatika_it', 'DZI', 2025, 'legacy.pdf');
    INSERT INTO exam_tasks (id, exam_id, task_number, exam_part, task_kind, points)
        VALUES (41, 31, 26, 2, 'practical_spreadsheet', 15);
    INSERT INTO users (id, email, display_name, role, session_key) VALUES
        (51, 'ivan@example.bg', 'Иван Петров', 'student', '{SESSION_KEY}'),
        (52, NULL, 'Иван Петров', 'student', '{SESSION_KEY}'),
        (53, NULL, 'Учител', 'teacher', '{SESSION_KEY}');
    INSERT INTO quiz_assignments (id, section_id, title_bg, question_count, owner_user_id)
        VALUES (71, 11, 'Стар тест', 2, 53), (72, 11, 'Друг тест', 1, NULL);
    INSERT INTO quiz_attempts
        (id, assignment_id, student_name, seed, question_ids_json, started_at, submitted_at,
         score_correct, score_total)
    VALUES
        (1, 71, 'Иван Петров', 'seed-1', '[21]', '2025-01-01 08:00:00', '2025-01-01 08:30:00', 1, 2),
        (2, 71, 'Мария', 'seed-2', '{{"question_ids":[21],"open_question_ids":[21]}}',
         '2025-01-02 09:00:00', NULL, NULL, 1),
        (3, 72, 'Иван Петров', 'seed-3', '[]', NULL, NULL, NULL, NULL),
        (9, 72, 'Изтрит', 'seed-9', '[]', '2025-01-03 10:00:00', NULL, NULL, NULL);
    DELETE FROM quiz_attempts WHERE id = 9;
    INSERT INTO quiz_answers (id, attempt_id, question_id, chosen_letter, is_correct)
        VALUES (101, 1, 21, 'А', 1), (102, 2, 21, NULL, 0);
    INSERT INTO quiz_text_answers (id, attempt_id, question_id, subquestion_number, raw_answer)
        VALUES (111, 1, 21, 1, 'Отговор'), (112, 2, 21, 1, 'Друг');
    INSERT INTO practical_submissions (id, quiz_attempt_id, exam_task_id, status)
        VALUES (121, 1, 41, 'submitted'), (122, 3, 41, 'draft');
    INSERT INTO practical_submission_files
        (id, practical_submission_id, stored_path, original_filename, size_bytes)
        VALUES (131, 121, '1/41/answer.xlsx', 'answer.xlsx', 42);
"""


def copy_migrations(destination: Path, *, without: str | None = None) -> Path:
    destination.mkdir()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name != without:
            shutil.copyfile(path, destination / path.name)
    return destination


def ledger(conn):
    """Recorded migrations in the order they were applied."""
    return [row[0] for row in conn.execute("SELECT filename FROM schema_migrations ORDER BY rowid")]


def sequence(conn, table="quiz_attempts"):
    return conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchall()


def table_contents(conn):
    return {
        name: conn.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall()
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT IN ('schema_migrations', 'sqlite_sequence')")
    }


def new_attempt(conn, assignment_id, student_name, user_id=None):
    return conn.execute(
        "INSERT INTO quiz_attempts (assignment_id, student_name, seed, question_ids_json, user_id) "
        "VALUES (?, ?, 'seed', '[]', ?)", (assignment_id, student_name, user_id)).lastrowid


class MigrationFileTest(unittest.TestCase):
    def test_file_opts_in_to_the_rebuild_and_is_the_only_015(self):
        names = manage_migrations.migration_filenames(MIGRATIONS)
        self.assertEqual([name for name in names if name.startswith("015_")], [MIGRATION])
        sql = (MIGRATIONS / MIGRATION).read_text(encoding="utf-8")
        self.assertEqual(sql.splitlines()[0], "-- learnpilot-migration: table-rebuild")
        self.assertEqual(manage_migrations.table_rebuild_migrations(MIGRATIONS, names), {MIGRATION})

    def test_rebuild_follows_the_supported_statement_order(self):
        sql = (MIGRATIONS / MIGRATION).read_text(encoding="utf-8")
        steps = [
            "CREATE TABLE quiz_attempts_new",
            "INSERT INTO quiz_attempts_new",
            "INSERT INTO sqlite_sequence",
            "DROP TABLE quiz_attempts;",
            "ALTER TABLE quiz_attempts_new RENAME TO quiz_attempts;",
            "CREATE INDEX idx_quiz_attempts_assignment",
        ]
        positions = [sql.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        # The old table is never renamed away, and no row is deleted from it.
        self.assertNotIn("RENAME TO quiz_attempts_old", sql)
        self.assertNotIn("ALTER TABLE quiz_attempts RENAME", sql)
        statements = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
        self.assertNotIn("SET NULL", statements)
        self.assertNotIn("UPDATE quiz_attempts", statements)  # nothing is backfilled

    def test_table_stays_runtime_state(self):
        self.assertEqual(canonical_tables.TABLE_CLASSIFICATION["quiz_attempts"][0], canonical_tables.RUNTIME)
        self.assertFalse((ROOT / "data" / "canonical" / "quiz_attempts.jsonl").exists())


class MigrationTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="quiz-attempts-user-migration-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "upgrade.db"
        self.assertNotEqual(self.path.resolve(), REAL_DB_PATH.resolve())
        sqlite3.connect(self.path).close()

    def connect(self, path=None):
        conn = sqlite3.connect(path or self.path)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def apply(self, migrations_dir=MIGRATIONS, path=None):
        out = io.StringIO()
        code = manage_migrations.apply_migrations(path or self.path, migrations_dir, out=out)
        return code, out.getvalue()

    def existing_database(self, populate_sql=POPULATE_SQL):
        """A database that applied everything except 015 (so 016 is recorded)."""
        previous = copy_migrations(self.root / "previous", without=MIGRATION)
        self.assertEqual(self.apply(previous)[0], 0)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(populate_sql)
            self.assertNotIn(MIGRATION, ledger(conn))
            self.assertIn("016_admin_totp.sql", ledger(conn))


class ExistingDatabaseUpgradeTest(MigrationTestCase):
    """N: 016 already recorded, 015 pending, applied by the shared runner."""

    def setUp(self):
        super().setUp()
        self.existing_database()
        with closing(sqlite3.connect(self.path)) as conn:
            self.before = table_contents(conn)
            self.sequence_before = sequence(conn)
            self.ledger_before = ledger(conn)

    def test_015_is_discovered_after_a_recorded_016_and_recorded_exactly_once(self):
        out = io.StringIO()
        manage_migrations.dry_run(self.path, MIGRATIONS, out=out)
        self.assertIn(f"pending migrations:\n  - {MIGRATION} (table-rebuild)\n", out.getvalue())

        code, report = self.apply()

        self.assertEqual(code, 0, report)
        self.assertIn(f"applied migrations:\n  - {MIGRATION} (table-rebuild)\n", report)
        self.assertIn("foreign_key_check: ok", report)
        conn = self.connect()
        self.assertEqual(ledger(conn), [*self.ledger_before, MIGRATION])
        # Recorded last, after 016 and every later migration the database already had.
        self.assertLess(ledger(conn).index("016_admin_totp.sql"), ledger(conn).index(MIGRATION))
        self.assertEqual(ledger(conn)[-1], MIGRATION)

        code, report = self.apply()  # nothing left to do, nothing recorded twice
        self.assertEqual(code, 0, report)
        self.assertEqual(ledger(conn).count(MIGRATION), 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0], 3)

    def test_every_row_id_and_column_survives_with_no_owner(self):
        self.assertEqual(self.apply()[0], 0)
        conn = self.connect()
        after = table_contents(conn)

        old_rows = self.before.pop("quiz_attempts")
        new_rows = after.pop("quiz_attempts")
        self.assertEqual([row[0] for row in new_rows], [1, 2, 3])
        self.assertEqual(len(new_rows), len(old_rows))
        # Same values in the same columns, plus a trailing NULL user_id.
        self.assertEqual(new_rows, [(*row, None) for row in old_rows])
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM quiz_attempts WHERE user_id IS NOT NULL").fetchone()[0], 0)
        # A name that equals an account's display name (and its email's owner) is not linked.
        self.assertEqual(conn.execute(
            "SELECT user_id FROM quiz_attempts WHERE student_name = 'Иван Петров'").fetchall(),
            [(None,), (None,)])
        # Every other table, the children included, is byte-for-byte what it was.
        self.assertEqual(after, self.before)

    def test_child_rows_still_reference_their_attempts(self):
        self.assertEqual(self.apply()[0], 0)
        conn = self.connect()
        self.assertEqual(conn.execute(
            "SELECT id, attempt_id FROM quiz_answers ORDER BY id").fetchall(), [(101, 1), (102, 2)])
        self.assertEqual(conn.execute(
            "SELECT id, attempt_id FROM quiz_text_answers ORDER BY id").fetchall(), [(111, 1), (112, 2)])
        self.assertEqual(conn.execute(
            "SELECT id, quiz_attempt_id FROM practical_submissions ORDER BY id").fetchall(),
            [(121, 1), (122, 3)])
        self.assertEqual(conn.execute(
            "SELECT id, practical_submission_id FROM practical_submission_files").fetchall(), [(131, 121)])
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        # The children really point at the rebuilt table: they are enforced against it.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO quiz_answers (attempt_id, question_id) VALUES (999, 21)")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM quiz_attempts WHERE id = 1")  # quiz_answers has no cascade
        conn.execute("DELETE FROM quiz_answers WHERE attempt_id = 1")
        conn.execute("DELETE FROM quiz_attempts WHERE id = 1")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM quiz_text_answers WHERE attempt_id = 1").fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM practical_submissions WHERE quiz_attempt_id = 1").fetchone()[0], 0)

    def test_sequence_high_water_mark_above_max_id_is_preserved(self):
        self.assertEqual(self.sequence_before, [(9,)])
        self.assertEqual(self.apply()[0], 0)
        conn = self.connect()
        self.assertEqual(conn.execute("SELECT MAX(id) FROM quiz_attempts").fetchone()[0], 3)
        self.assertEqual(sequence(conn), [(9,)])
        self.assertEqual(sequence(conn, "quiz_attempts_new"), [])
        self.assertEqual(new_attempt(conn, 72, "Нов"), 10)  # not 4: id 9 is never reused

    def test_foreign_key_check_is_clean_and_enforcement_is_back_on(self):
        self.assertEqual(self.apply()[0], 0)
        conn = manage_migrations.open_writable_db(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual([tuple(row) for row in conn.execute("PRAGMA integrity_check")], [("ok",)])


class SequenceEdgeCaseTest(MigrationTestCase):
    def test_populated_table_without_gaps_continues_after_max_id(self):
        self.existing_database(POPULATE_SQL.replace("DELETE FROM quiz_attempts WHERE id = 9;", ""))
        self.assertEqual(self.apply()[0], 0)
        conn = self.connect()
        self.assertEqual(sequence(conn), [(9,)])
        self.assertEqual(new_attempt(conn, 72, "Нов"), 10)

    def test_never_used_table_gets_no_sequence_row_and_starts_at_one(self):
        self.existing_database("""
            INSERT INTO curriculum_sections (id, section_slug, title_bg, class, section_type)
                VALUES (11, 'legacy', 'Стар раздел', 12, 'section');
            INSERT INTO quiz_assignments (id, section_id, title_bg, question_count)
                VALUES (71, 11, 'Стар тест', 2);
        """)
        self.assertEqual(self.apply()[0], 0)
        conn = self.connect()
        self.assertEqual(sequence(conn), [])
        self.assertEqual(sequence(conn, "quiz_attempts_new"), [])
        self.assertEqual(new_attempt(conn, 71, "Първи"), 1)
        self.assertEqual(sequence(conn), [(1,)])

    def test_emptied_table_keeps_its_high_water_mark(self):
        self.existing_database(POPULATE_SQL + """
            DELETE FROM practical_submissions;
            DELETE FROM quiz_text_answers;
            DELETE FROM quiz_answers;
            DELETE FROM quiz_attempts;
        """)
        self.assertEqual(self.apply()[0], 0)
        conn = self.connect()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0], 0)
        self.assertEqual(sequence(conn), [(9,)])
        self.assertEqual(new_attempt(conn, 72, "Нов"), 10)


class ResultingSchemaTest(MigrationTestCase):
    """G: the schema is asserted explicitly; foreign_key_check cannot see it."""

    def assert_schema(self, conn):
        columns = conn.execute("PRAGMA table_info(quiz_attempts)").fetchall()
        self.assertEqual([column[1] for column in columns], [*OLD_COLUMNS, "user_id"])
        by_name = {column[1]: column for column in columns}
        # (type, notnull, default, pk)
        self.assertEqual(by_name["id"][2:], ("INTEGER", 0, None, 1))
        self.assertEqual(by_name["assignment_id"][2:], ("INTEGER", 1, None, 0))
        for name in ("student_name", "seed", "question_ids_json"):
            self.assertEqual(by_name[name][2:], ("TEXT", 1, None, 0), name)
        self.assertEqual(by_name["started_at"][2:], ("TEXT", 0, "CURRENT_TIMESTAMP", 0))
        self.assertEqual(by_name["submitted_at"][2:], ("TEXT", 0, None, 0))
        for name in ("score_correct", "score_total", "user_id"):
            self.assertEqual(by_name[name][2:], ("INTEGER", 0, None, 0), name)
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'quiz_attempts'").fetchone()[0]
        self.assertIn("AUTOINCREMENT", table_sql)

        # (table, from, to, on_update, on_delete)
        foreign_keys = {row[3]: row[2:7] for row in conn.execute("PRAGMA foreign_key_list(quiz_attempts)")}
        self.assertEqual(foreign_keys, {
            "assignment_id": ("quiz_assignments", "assignment_id", "id", "NO ACTION", "NO ACTION"),
            "user_id": ("users", "user_id", "id", "NO ACTION", "RESTRICT"),
        })

        # The table-level UNIQUE is gone: no automatic index, only the named ones.
        self.assertNotIn("UNIQUE", table_sql.upper())
        indexes = set()
        for _seq, name, unique, origin, partial in conn.execute("PRAGMA index_list(quiz_attempts)"):
            self.assertEqual(origin, "c", name)  # created by CREATE INDEX, not by a constraint
            keys = tuple(row[2] for row in conn.execute(f"PRAGMA index_info({name})"))
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0]
            predicate = " ".join(sql.split("WHERE", 1)[1].split()) if partial else None
            indexes.add((name, unique, keys, predicate))
        self.assertEqual(indexes, EXPECTED_INDEXES)

        for child, (column, on_delete) in CHILD_FOREIGN_KEYS.items():
            targets = [row[2:7] for row in conn.execute(f"PRAGMA foreign_key_list({child})")
                       if row[3] == column]
            self.assertEqual(targets, [("quiz_attempts", column, "id", "NO ACTION", on_delete)], child)
        # Nothing else references the table, and nothing still names the temporary one.
        referencing = sorted(
            name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if any(row[2] == "quiz_attempts" for row in conn.execute(f'PRAGMA foreign_key_list("{name}")')))
        self.assertEqual(referencing, sorted(CHILD_FOREIGN_KEYS))
        self.assertEqual(conn.execute(
            "SELECT name FROM sqlite_master WHERE sql LIKE '%quiz_attempts_new%' "
            "OR name LIKE '%quiz_attempts_new%'").fetchall(), [])
        # The table never had a trigger or a view; the rebuild adds none.
        self.assertEqual(conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('trigger', 'view') "
            "AND (tbl_name = 'quiz_attempts' OR sql LIKE '%quiz_attempts%')").fetchall(), [])

    def test_schema_after_upgrading_an_existing_database(self):
        self.existing_database()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('trigger', 'view')").fetchall(), [])
            old_indexes = [row[1] for row in conn.execute("PRAGMA index_list(quiz_attempts)")]
            self.assertIn("idx_quiz_attempts_assignment", old_indexes)
        self.assertEqual(self.apply()[0], 0)
        self.assert_schema(self.connect())

    def test_schema_on_a_fresh_database(self):
        self.assertEqual(self.apply()[0], 0)
        self.assert_schema(self.connect())

    def test_upgraded_and_fresh_databases_have_the_same_schema(self):
        self.existing_database()
        self.assertEqual(self.apply()[0], 0)
        fresh = self.root / "fresh.db"
        sqlite3.connect(fresh).close()
        self.assertEqual(self.apply(path=fresh)[0], 0)
        with closing(sqlite3.connect(self.path)) as upgraded, closing(sqlite3.connect(fresh)) as new:
            self.assertEqual(schema_snapshot.compare(
                schema_snapshot.snapshot(upgraded), schema_snapshot.snapshot(new), "upgraded", "fresh"), [])


class FreshDatabaseOrderTest(MigrationTestCase):
    """O: on an empty database 015 runs before 016."""

    def test_fresh_database_applies_015_before_016(self):
        code, report = self.apply()
        self.assertEqual(code, 0, report)
        conn = self.connect()
        applied = ledger(conn)
        self.assertEqual(applied, manage_migrations.migration_filenames(MIGRATIONS))
        self.assertEqual(applied.index(MIGRATION) + 1, applied.index("016_admin_totp.sql"))
        self.assertEqual(applied.index("014_assignment_owner.sql") + 1, applied.index(MIGRATION))
        self.assertEqual(applied.count(MIGRATION), 1)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(sequence(conn), [])


class UniquenessAndForeignKeyTest(MigrationTestCase):
    def setUp(self):
        super().setUp()
        self.existing_database()
        self.assertEqual(self.apply()[0], 0)
        self.conn = self.connect()

    def test_one_attempt_per_account_and_assignment(self):
        new_attempt(self.conn, 71, "Иван Петров", 51)
        with self.assertRaises(sqlite3.IntegrityError):
            new_attempt(self.conn, 71, "Иван Петров", 51)
        with self.assertRaises(sqlite3.IntegrityError):
            new_attempt(self.conn, 71, "Друго име след преименуване", 51)
        # The same account on another assignment is a different attempt.
        new_attempt(self.conn, 72, "Иван Петров", 51)

    def test_different_accounts_may_share_a_display_name(self):
        first = new_attempt(self.conn, 71, "Иван Петров", 51)
        second = new_attempt(self.conn, 71, "Иван Петров", 52)
        self.assertNotEqual(first, second)
        # ...even though a historical attempt 1 already holds that name there.
        self.assertEqual(self.conn.execute(
            "SELECT id, user_id FROM quiz_attempts WHERE assignment_id = 71 "
            "AND student_name = 'Иван Петров' ORDER BY id").fetchall(),
            [(1, None), (first, 51), (second, 52)])

    def test_historical_attempts_keep_one_per_name_among_themselves(self):
        with self.assertRaises(sqlite3.IntegrityError):
            new_attempt(self.conn, 71, "Иван Петров")  # attempt 1 has that name
        with self.assertRaises(sqlite3.IntegrityError):
            new_attempt(self.conn, 71, "Мария")
        # A historical name does not block the same name on another assignment
        # (as before), and NULL never collides with NULL in the account index.
        new_attempt(self.conn, 72, "Мария")
        new_attempt(self.conn, 71, "Нов исторически")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM quiz_attempts WHERE assignment_id = 71 AND user_id IS NULL").fetchone()[0], 3)

    def test_an_owned_attempt_cannot_lose_its_owner_into_a_historical_name(self):
        new_attempt(self.conn, 71, "Мария", 51)  # same name as historical attempt 2
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE quiz_attempts SET user_id = NULL WHERE user_id = 51")

    def test_user_id_must_be_an_existing_account(self):
        for user_id in (999999, -1):
            with self.subTest(user_id=user_id), self.assertRaises(sqlite3.IntegrityError):
                new_attempt(self.conn, 72, "Никой", user_id)

    def test_account_with_attempts_cannot_be_deleted_and_never_becomes_null(self):
        attempt_id = new_attempt(self.conn, 71, "Иван Петров", 51)
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM users WHERE id = 51")
        self.assertEqual(self.conn.execute(
            "SELECT user_id FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone(), (51,))
        # Disabling (the only supported way to retire an account) keeps ownership.
        self.conn.execute("UPDATE users SET is_active = 0 WHERE id = 51")
        self.assertEqual(self.conn.execute(
            "SELECT user_id FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone(), (51,))
        # An account without attempts is not held back by this table.
        self.conn.execute("DELETE FROM users WHERE id = 52")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])


class RollbackTest(MigrationTestCase):
    def test_a_violation_found_by_the_check_leaves_the_database_untouched(self):
        self.existing_database()
        with closing(sqlite3.connect(self.path)) as conn:  # foreign keys off: plant an orphan
            conn.execute("INSERT INTO quiz_answers (id, attempt_id, question_id) VALUES (199, 4242, 21)")
            conn.commit()
            before = table_contents(conn)
            schema_before = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
            ledger_before = ledger(conn)
            sequence_before = sequence(conn)

        with self.assertRaises(manage_migrations.MigrationError):
            manage_migrations.apply_migrations(self.path, MIGRATIONS, out=io.StringIO())

        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(table_contents(conn), before)
            self.assertEqual(conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall(), schema_before)
            self.assertEqual(ledger(conn), ledger_before)
            self.assertEqual(sequence(conn), sequence_before)


if __name__ == "__main__":
    unittest.main()

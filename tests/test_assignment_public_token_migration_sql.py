"""017 adds quiz_assignments.public_token additively: no rebuild, no generated tokens.

Every database here is temporary and built by the shared migration runner from the
repository's migration files.
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
from tests.support import ROOT, build_test_database

MIGRATIONS = ROOT / "web" / "migrations"
MIGRATION = "017_assignment_public_token.sql"
INDEX = "idx_quiz_assignments_public_token"
TOKEN_A = "A" * 43
TOKEN_B = "B" * 43

POPULATE_SQL = """
    INSERT INTO curriculum_sections (id, section_slug, title_bg, class, section_type)
        VALUES (11, 'legacy', 'Стар раздел', 12, 'section');
    INSERT INTO questions (id, source_exam, question_type, points, prompt)
        VALUES (21, 'legacy', 'short_answer', 3, 'Въпрос');
    INSERT INTO exams (id, subject, level, year, source_file)
        VALUES (31, 'informatika_it', 'DZI', 2025, 'legacy.pdf');
    INSERT INTO exam_tasks (id, exam_id, task_number, exam_part, task_kind, points)
        VALUES (41, 31, 26, 2, 'practical_spreadsheet', 15);
    INSERT INTO users (id, display_name, role, session_key) VALUES
        (51, 'Учител', 'teacher', '12345678901234567890123456789012'),
        (52, 'Ученик', 'student', '22345678901234567890123456789012');
    INSERT INTO quiz_assignments
        (id, section_id, title_bg, question_count, time_limit_minutes, question_plan_json,
         created_at, owner_user_id)
        VALUES (71, 11, 'Стар тест', 2, 15, '{"question_ids":[21],"open_question_ids":[21]}',
                '2025-01-01', 51);
    INSERT INTO quiz_assignments (id, section_id, title_bg, question_count)
        VALUES (81, 11, 'Тест без собственик', 1);
    INSERT INTO quiz_attempts (id, assignment_id, user_id, student_name, seed, question_ids_json)
        VALUES (91, 71, 52, 'Ученик', 'seed', '[21]');
    INSERT INTO quiz_attempts (id, assignment_id, student_name, seed, question_ids_json)
        VALUES (92, 71, 'Исторически', 'seed', '[21]');
    INSERT INTO quiz_answers (id, attempt_id, question_id, chosen_letter, is_correct)
        VALUES (101, 91, 21, 'А', 1);
    INSERT INTO quiz_text_answers (id, attempt_id, question_id, subquestion_number, raw_answer)
        VALUES (111, 91, 21, 1, 'Отговор');
    INSERT INTO practical_submissions (id, quiz_attempt_id, exam_task_id, status)
        VALUES (121, 91, 41, 'submitted');
    INSERT INTO practical_submission_files
        (id, practical_submission_id, stored_path, original_filename, size_bytes)
        VALUES (131, 121, '91/41/answer.xlsx', 'answer.xlsx', 42);
    UPDATE sqlite_sequence SET seq = 1000 WHERE name = 'quiz_assignments';
"""


def apply(path, migrations_dir=MIGRATIONS):
    out = io.StringIO()
    code = manage_migrations.apply_migrations(path, migrations_dir, out=out)
    return code, out.getvalue()


def ledger(conn):
    return [row[0] for row in conn.execute("SELECT filename FROM schema_migrations ORDER BY rowid")]


def assignment_schema(conn):
    """Everything sqlite_master records about quiz_assignments and its indexes."""
    return conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE tbl_name = 'quiz_assignments' ORDER BY type, name"
    ).fetchall()


class AssignmentPublicTokenMigrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="assignment-token-migration-")
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

    def test_upgrade_preserves_ids_owners_plans_attempts_and_child_rows(self):
        before = self.contents()
        self.migrate()
        after = self.contents()
        # The only change anywhere: a trailing NULL public_token on each assignment.
        self.assertEqual(after.pop("quiz_assignments"),
                         [(*row, None) for row in before.pop("quiz_assignments")])
        self.assertEqual(after, before)
        self.assertEqual(
            self.conn.execute(
                "SELECT id, owner_user_id, question_plan_json, public_token FROM quiz_assignments ORDER BY id"
            ).fetchall(),
            [(71, 51, '{"question_ids":[21],"open_question_ids":[21]}', None), (81, None, None, None)])
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        # AUTOINCREMENT continues where it was: no id is reused.
        new_id = self.conn.execute(
            "INSERT INTO quiz_assignments (section_id, title_bg, question_count) VALUES (11, 'Нов', 1)"
        ).lastrowid
        self.assertEqual(new_id, 1001)

    def test_nullable_text_column_and_unique_index(self):
        self.migrate()
        column = {r[1]: r for r in self.conn.execute("PRAGMA table_info(quiz_assignments)")}
        self.assertEqual(column["public_token"][2:], ("TEXT", 0, None, 0))
        index = {r[1]: r for r in self.conn.execute("PRAGMA index_list(quiz_assignments)")}[INDEX]
        self.assertEqual(index[2], 1)  # unique
        self.assertEqual(index[4], 0)  # not partial
        self.assertEqual([r[2] for r in self.conn.execute(f"PRAGMA index_info({INDEX})")], ["public_token"])

    def test_unique_index_rejects_a_repeated_token_and_allows_many_nulls(self):
        self.migrate()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM quiz_assignments WHERE public_token IS NULL").fetchone()[0], 2)
        self.conn.execute("UPDATE quiz_assignments SET public_token = ? WHERE id = 71", (TOKEN_A,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE quiz_assignments SET public_token = ? WHERE id = 81", (TOKEN_A,))
        self.conn.execute("UPDATE quiz_assignments SET public_token = ? WHERE id = 81", (TOKEN_B,))

    def test_table_is_altered_in_place_not_rebuilt(self):
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
        statements = "\n".join(
            line for line in (MIGRATIONS / MIGRATION).read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("--")).upper()
        for forbidden in ("CREATE TABLE", "DROP TABLE", "RENAME", "RANDOMBLOB", "UPDATE ", "INSERT ", "DELETE "):
            self.assertNotIn(forbidden, statements)

    def test_runner_reapply_is_a_noop(self):
        self.migrate()
        self.conn.execute("UPDATE quiz_assignments SET public_token = ? WHERE id = 71", (TOKEN_A,))
        self.conn.commit()
        before = self.contents()
        report = self.migrate()
        self.assertNotIn(MIGRATION, report.split("applied migrations:")[-1].split("skipped")[0])
        self.assertEqual(self.contents(), before)
        self.assertEqual(ledger(self.conn).count(MIGRATION), 1)

    def test_upgraded_schema_equals_a_fresh_database(self):
        self.migrate()
        fresh_path = self.root / "fresh.db"
        sqlite3.connect(fresh_path).close()
        self.assertEqual(apply(fresh_path)[0], 0)
        with closing(sqlite3.connect(fresh_path)) as fresh:
            self.assertEqual(assignment_schema(fresh), assignment_schema(self.conn))
            self.assertEqual(
                fresh.execute("PRAGMA table_info(quiz_assignments)").fetchall(),
                self.conn.execute("PRAGMA table_info(quiz_assignments)").fetchall())
            # 015, 016 and 017 are applied in file order on an empty database.
            self.assertEqual(ledger(fresh)[-3:],
                             ["015_quiz_attempts_user.sql", "016_admin_totp.sql", MIGRATION])
        self.assertEqual(ledger(self.conn)[-1], MIGRATION)

    def test_017_is_never_adopted_without_running_it(self):
        self.conn.row_factory = sqlite3.Row
        self.assertEqual(manage_migrations.baseline_status(self.conn, MIGRATION)[0], "manual review required")


class AssignmentPublicTokenClassificationTest(unittest.TestCase):
    def test_migration_files_end_with_017_and_earlier_ones_are_present(self):
        names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        self.assertEqual(names[-3:], ["015_quiz_attempts_user.sql", "016_admin_totp.sql", MIGRATION])
        self.assertEqual([name for name in names if name.startswith("017_")], [MIGRATION])

    def test_manifest_records_the_level_and_keeps_assignments_runtime(self):
        manifest = json.loads((ROOT / "data" / "canonical" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"]["migration_level"], MIGRATION)
        self.assertEqual(manifest["schema"]["migrations"][-1], MIGRATION)
        self.assertEqual(ct.TABLE_CLASSIFICATION["quiz_assignments"][0], ct.RUNTIME)
        self.assertIn("quiz_assignments", manifest["runtime_exclusions"])
        self.assertNotIn("quiz_assignments", manifest["tables"])

    def test_fresh_canonical_build_is_at_017_with_clean_foreign_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_test_database(Path(tmp) / "fresh.db")
            with closing(sqlite3.connect(path)) as conn:
                applied = [r[0] for r in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")]
                self.assertEqual(applied[-1], MIGRATION)
                self.assertIn("public_token", [r[1] for r in conn.execute("PRAGMA table_info(quiz_assignments)")])
                self.assertEqual(conn.execute("SELECT * FROM pragma_foreign_key_check").fetchall(), [])
                self.assertEqual(ct.classification_problems(conn), [])


if __name__ == "__main__":
    unittest.main()

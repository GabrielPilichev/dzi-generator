"""014 is additive even with populated dependents and foreign_keys enabled."""

import io
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src import canonical_tables, manage_migrations
from tests.support import ROOT, build_test_database

MIGRATIONS = ROOT / "web" / "migrations"
MIGRATION = "014_assignment_owner.sql"


class AssignmentOwnerMigrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="assignment-owner-migration-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "upgrade.db"
        sqlite3.connect(self.path).close()
        previous = self.root / "migrations"
        previous.mkdir()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < MIGRATION:
                shutil.copyfile(path, previous / path.name)
        self.assertEqual(manage_migrations.apply_migrations(
            self.path, previous, out=io.StringIO()), 0)
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript("""
            INSERT INTO curriculum_sections (id, section_slug, title_bg, class, section_type)
                VALUES (11, 'legacy', 'Стар раздел', 12, 'section');
            INSERT INTO questions (id, source_exam, question_type, points, prompt)
                VALUES (21, 'legacy', 'short_answer', 3, 'Въпрос');
            INSERT INTO exams (id, subject, level, year, source_file)
                VALUES (31, 'informatika_it', 'DZI', 2025, 'legacy.pdf');
            INSERT INTO exam_tasks (id, exam_id, task_number, exam_part, task_kind, points)
                VALUES (41, 31, 26, 2, 'practical_spreadsheet', 15);
            INSERT INTO users (id, display_name, role, session_key)
                VALUES (51, 'Учител', 'teacher', '12345678901234567890123456789012');
            INSERT INTO quiz_assignments
                (id, section_id, title_bg, question_count, time_limit_minutes, question_plan_json, created_at)
                VALUES (71, 11, 'Стар тест', 2, 15, '{"question_ids":[21]}', '2025-01-01');
            INSERT INTO quiz_assignments (id, section_id, title_bg, question_count)
                VALUES (81, 11, 'Друг тест', 1);
            INSERT INTO quiz_attempts (id, assignment_id, student_name, seed, question_ids_json)
                VALUES (91, 71, 'Ученик', 'seed', '[21]');
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
        """)

    def migrate(self):
        self.assertEqual(manage_migrations.apply_migrations(
            self.path, MIGRATIONS, out=io.StringIO()), 0)

    def contents(self):
        return {name: self.conn.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall()
                for (name,) in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'schema_migrations'")}

    def test_upgrade_preserves_every_row_relationship_id_and_sequence(self):
        before = self.contents()
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.migrate()
        after = self.contents()
        # 014's owner_user_id, 017's public_token and 018's eight lifecycle columns:
        # all trailing, all NULL for old rows.
        self.assertEqual(after.pop("quiz_assignments"),
                         [(*row, *([None] * 10)) for row in before.pop("quiz_assignments")])
        # 015 rebuilds quiz_attempts with a trailing user_id, NULL for every old row.
        self.assertEqual(after.pop("quiz_attempts"),
                         [(*row, None) for row in before.pop("quiz_attempts")])
        # Tables created by later migrations (016's TOTP tables) start empty; 018's
        # lifecycle clock holds exactly its seeded row.
        for table in set(after) - set(before):
            self.assertEqual(len(after.pop(table)), canonical_tables.MIGRATION_SEEDED_ROWS.get(table, 0), table)
        self.assertEqual(after, before)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        new_id = self.conn.execute("""
            INSERT INTO quiz_assignments (section_id, title_bg, question_count)
            VALUES (11, 'Нов тест', 1)
        """).lastrowid
        self.assertEqual(new_id, 1001)

    def test_exact_nullable_column_fk_and_index(self):
        self.migrate()
        column = {r[1]: r for r in self.conn.execute("PRAGMA table_info(quiz_assignments)")}
        self.assertEqual(column["owner_user_id"][2:], ("INTEGER", 0, None, 0))
        fk = [r for r in self.conn.execute("PRAGMA foreign_key_list(quiz_assignments)")
              if r[3] == "owner_user_id"]
        self.assertEqual(len(fk), 1)
        self.assertEqual(fk[0][2:7], ("users", "owner_user_id", "id", "NO ACTION", "NO ACTION"))
        self.assertEqual([r[2] for r in self.conn.execute(
            "PRAGMA index_info(idx_quiz_assignments_owner)")], ["owner_user_id", "created_at", "id"])

    def test_invalid_owners_rejected_and_deactivation_keeps_ownership(self):
        self.migrate()
        for owner in (999999, -1, "not-an-id", 51.5):
            with self.subTest(owner=owner), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute("UPDATE quiz_assignments SET owner_user_id = ? WHERE id = 71", (owner,))
        self.conn.execute("UPDATE quiz_assignments SET owner_user_id = 51 WHERE id = 71")
        self.conn.execute("UPDATE users SET is_active = 0 WHERE id = 51")
        self.assertEqual(self.conn.execute(
            "SELECT owner_user_id FROM quiz_assignments WHERE id = 71").fetchone(), (51,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM users WHERE id = 51")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_runner_reapply_is_a_noop(self):
        self.migrate()
        before = self.contents()
        history = self.conn.execute("SELECT * FROM schema_migrations ORDER BY filename").fetchall()
        self.migrate()
        self.assertEqual(self.contents(), before)
        self.assertEqual(self.conn.execute(
            "SELECT * FROM schema_migrations ORDER BY filename").fetchall(), history)
        self.assertEqual(sum(row[0] == MIGRATION for row in history), 1)

    def test_owner_filter_and_order_use_covering_index_without_sort(self):
        self.migrate()
        for predicate, params in (("owner_user_id = ?", (51,)), ("owner_user_id IS NULL", ())):
            with self.subTest(predicate=predicate):
                plan = self.conn.execute(
                    "EXPLAIN QUERY PLAN SELECT id FROM quiz_assignments WHERE " + predicate
                    + " ORDER BY created_at DESC, id DESC LIMIT 20", params).fetchall()
                details = " ".join(row[3] for row in plan)
                self.assertIn("SEARCH", details)
                self.assertIn("COVERING INDEX idx_quiz_assignments_owner", details)
                self.assertNotIn("TEMP B-TREE", details)


class AssignmentOwnerFreshBuildTest(unittest.TestCase):
    def test_canonical_build_has_empty_runtime_assignments_at_014(self):
        with tempfile.TemporaryDirectory() as directory:
            path = build_test_database(Path(directory) / "fresh.db")
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_assignments").fetchone(), (0,))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone(), (0,))
                self.assertIn((MIGRATION,), conn.execute("SELECT filename FROM schema_migrations").fetchall())
                self.assertIn("owner_user_id", [r[1] for r in conn.execute("PRAGMA table_info(quiz_assignments)")])
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(canonical_tables.classification_problems(conn), [])
                self.assertEqual(canonical_tables.TABLE_CLASSIFICATION["quiz_assignments"][0],
                                 canonical_tables.RUNTIME)


if __name__ == "__main__":
    unittest.main()

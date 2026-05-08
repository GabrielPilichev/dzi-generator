import atexit
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src import manage_migrations


_TMP = tempfile.TemporaryDirectory()
atexit.register(_TMP.cleanup)

_ROOT = Path(__file__).resolve().parents[1]
_TMP_DB = Path(_TMP.name) / "questions.db"
_TMP_VAULT = Path(_TMP.name) / "vault"
shutil.copy2(_ROOT / "data" / "questions.db", _TMP_DB)
_TMP_VAULT.mkdir()

os.environ["DZI_DB"] = str(_TMP_DB)
os.environ["DZI_VAULT"] = str(_TMP_VAULT)
os.environ["DZI_ADMIN_PASSWORD"] = "admin-pass"
os.environ["DZI_TESTER_PASSWORD"] = "tester-pass"

from web import app as web_app  # noqa: E402


MIGRATION_SQL = (_ROOT / "web" / "migrations" / "006_assignment_question_plan.sql").read_text(encoding="utf-8")


class AssignmentQuestionPlanMigrationTest(unittest.TestCase):
    def make_temp_db(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        db_path = Path(temp_dir.name) / "test.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("""
                CREATE TABLE quiz_assignments (
                    id INTEGER PRIMARY KEY,
                    section_id INTEGER NOT NULL,
                    title_bg TEXT NOT NULL,
                    question_count INTEGER NOT NULL,
                    time_limit_minutes INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_migration_adds_nullable_question_plan_json(self):
        db_path = self.make_temp_db()
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(MIGRATION_SQL)
            columns = {
                row[1]: row
                for row in conn.execute("PRAGMA table_info(quiz_assignments)").fetchall()
            }
        finally:
            conn.close()

        self.assertIn("question_plan_json", columns)
        self.assertEqual(columns["question_plan_json"][2], "TEXT")
        self.assertEqual(columns["question_plan_json"][3], 0)

    def test_migration_runner_applies_006(self):
        db_path = self.make_temp_db()
        migrations_dir = Path(db_path.parent) / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "006_assignment_question_plan.sql").write_text(MIGRATION_SQL, encoding="utf-8")

        exit_code = manage_migrations.main(
            ["--db", str(db_path), "--migrations-dir", str(migrations_dir), "--apply"],
            out=io.StringIO(),
            err=io.StringIO(),
        )

        self.assertEqual(exit_code, 0)
        conn = sqlite3.connect(db_path)
        try:
            applied = conn.execute("SELECT filename FROM schema_migrations").fetchall()
            columns = [row[1] for row in conn.execute("PRAGMA table_info(quiz_assignments)").fetchall()]
        finally:
            conn.close()

        self.assertEqual(applied, [("006_assignment_question_plan.sql",)])
        self.assertIn("question_plan_json", columns)

    def test_mc_only_null_and_mixed_json_store_safely(self):
        db_path = self.make_temp_db()
        mixed_plan = json.dumps({
            "mixed_open_enabled": True,
            "question_ids": [1, 2],
            "open_question_ids": [2],
            "include_open_answers_in_final_score": False,
        })
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(MIGRATION_SQL)
            conn.execute("""
                INSERT INTO quiz_assignments (id, section_id, title_bg, question_count, question_plan_json)
                VALUES (1, 10, 'MC only', 1, NULL)
            """)
            conn.execute("""
                INSERT INTO quiz_assignments (id, section_id, title_bg, question_count, question_plan_json)
                VALUES (2, 10, 'Mixed', 2, ?)
            """, (mixed_plan,))
            conn.commit()
            rows = conn.execute("""
                SELECT id, question_plan_json
                FROM quiz_assignments
                ORDER BY id
            """).fetchall()
        finally:
            conn.close()

        self.assertIsNone(rows[0][1])
        self.assertEqual(json.loads(rows[1][1])["open_question_ids"], [2])
        self.assertIsNone(web_app.quiz_parse_assignment_question_plan(rows[0][1]))
        self.assertIsNotNone(web_app.quiz_parse_assignment_question_plan(rows[1][1]))


class AssignmentQuestionPlanHelperTest(unittest.TestCase):
    def test_null_assignment_plan_is_mc_only(self):
        self.assertIsNone(web_app.quiz_parse_assignment_question_plan(None))

    def test_mixed_open_assignment_plan_round_trips(self):
        raw = json.dumps({
            "mixed_open_enabled": True,
            "question_ids": [10, 20],
            "open_question_ids": [20],
            "include_open_answers_in_final_score": False,
        })

        plan = web_app.quiz_parse_assignment_question_plan(raw)

        self.assertIsNotNone(plan)
        self.assertEqual(plan["question_ids"], [10, 20])
        self.assertEqual(plan["open_question_ids"], [20])
        self.assertFalse(plan["include_open_answers_in_final_score"])

    def test_invalid_assignment_plan_returns_none(self):
        self.assertIsNone(web_app.quiz_parse_assignment_question_plan("{bad json"))
        self.assertIsNone(web_app.quiz_parse_assignment_question_plan(json.dumps([1, 2, 3])))
        self.assertIsNone(web_app.quiz_parse_assignment_question_plan(json.dumps({
            "mixed_open_enabled": True,
            "question_ids": [1],
            "open_question_ids": [2],
        })))


if __name__ == "__main__":
    unittest.main()

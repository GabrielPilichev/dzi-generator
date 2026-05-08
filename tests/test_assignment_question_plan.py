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


def _register_dzi_default_denied_test_route(app):
    endpoint = "dzi_test_only_default_denied"
    if endpoint in app.view_functions:
        return

    def view():
        return "test-only DZI endpoint"

    app.add_url_rule("/__test__/dzi-default-denied", endpoint=endpoint, view_func=view)


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
    @classmethod
    def setUpClass(cls):
        cls.app = web_app.app
        _register_dzi_default_denied_test_route(cls.app)
        cls.app.config.update(TESTING=True)
        conn = web_app.quiz_db()
        try:
            cls.section = cls._first_eligible_section(conn)
            cls.mc_question_id = web_app.quiz_section_question_ids(conn, int(cls.section["id"]))[0]
            cls.open_question_id = cls._insert_eligible_open_question(conn)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _first_eligible_section(conn):
        rows = conn.execute("""
            SELECT id, title_bg
            FROM curriculum_sections
            ORDER BY id
        """).fetchall()
        for row in rows:
            if web_app.quiz_section_question_ids(conn, int(row["id"])):
                return row
        raise AssertionError("No section with eligible MC questions found")

    @staticmethod
    def _insert_eligible_open_question(conn):
        cur = conn.execute("""
            INSERT INTO questions (
                source_exam, source_number, question_type, topic, difficulty,
                points, prompt, has_image, is_ai_generated, quality_score
            )
            VALUES (?, ?, 'fill_in', 'test', 'medium', 1, ?, 0, 0, NULL)
        """, (
            "temp-assignment-plan-open",
            16,
            "Попълнете липсващите стойности.",
        ))
        question_id = int(cur.lastrowid)
        for number, answer in ((1, "клиент"), (2, '["jpeg", "jpg"]')):
            conn.execute("""
                INSERT INTO fill_in_subquestions (
                    question_id, subquestion_number, correct_answer, answer_alternatives
                )
                VALUES (?, ?, ?, NULL)
            """, (question_id, number, answer))
        return question_id

    def setUp(self):
        self.client = self.app.test_client()

    def _create_assignment(self, *, question_plan_json=None):
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO quiz_assignments (
                    section_id, title_bg, question_count, time_limit_minutes, question_plan_json
                )
                VALUES (?, ?, 1, NULL, ?)
            """, (self.section["id"], self.section["title_bg"], question_plan_json))
            assignment_id = int(cur.lastrowid)
            conn.commit()
            return assignment_id
        finally:
            conn.close()

    def _attempt_for_assignment(self, assignment_id):
        conn = web_app.quiz_db()
        try:
            return conn.execute("""
                SELECT *
                FROM quiz_attempts
                WHERE assignment_id = ?
                ORDER BY id DESC
                LIMIT 1
            """, (assignment_id,)).fetchone()
        finally:
            conn.close()

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

    def test_quiz_start_null_assignment_plan_keeps_plain_mc_attempt_format(self):
        assignment_id = self._create_assignment()

        response = self.client.post(f"/quiz/{assignment_id}", data={"student_name": "MC Plan Student"})

        self.assertEqual(response.status_code, 302)
        attempt = self._attempt_for_assignment(assignment_id)
        stored = json.loads(attempt["question_ids_json"])
        self.assertIsInstance(stored, list)
        self.assertNotIn("mixed_open_enabled", attempt["question_ids_json"])

    def test_quiz_start_copies_valid_mixed_assignment_plan_to_attempt(self):
        plan = {
            "mixed_open_enabled": True,
            "question_ids": [self.mc_question_id, self.open_question_id],
            "open_question_ids": [self.open_question_id],
        }
        assignment_id = self._create_assignment(question_plan_json=json.dumps(plan))

        response = self.client.post(f"/quiz/{assignment_id}", data={"student_name": "Mixed Plan Student"})

        self.assertEqual(response.status_code, 302)
        attempt = self._attempt_for_assignment(assignment_id)
        stored = json.loads(attempt["question_ids_json"])
        self.assertTrue(stored["mixed_open_enabled"])
        self.assertEqual(stored["question_ids"], [self.mc_question_id, self.open_question_id])
        self.assertEqual(stored["open_question_ids"], [self.open_question_id])
        self.assertFalse(stored["include_open_answers_in_final_score"])
        self.assertEqual(attempt["score_total"], 1)

    def test_copied_mixed_plan_renders_and_records_open_answers(self):
        plan = {
            "mixed_open_enabled": True,
            "question_ids": [self.mc_question_id, self.open_question_id],
            "open_question_ids": [self.open_question_id],
        }
        assignment_id = self._create_assignment(question_plan_json=json.dumps(plan))
        self.client.post(f"/quiz/{assignment_id}", data={"student_name": "Mixed Open Submit"})
        attempt = self._attempt_for_assignment(assignment_id)

        response = self.client.get(f"/quiz/attempt/{attempt['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'name="open_q_{self.open_question_id}_1"'.encode("utf-8"), response.data)

        response = self.client.post(f"/quiz/attempt/{attempt['id']}", data={
            f"open_q_{self.open_question_id}_1": "клиент",
            f"open_q_{self.open_question_id}_2": "jpg",
        })
        self.assertEqual(response.status_code, 302)

        conn = web_app.quiz_db()
        try:
            count = conn.execute("""
                SELECT COUNT(*)
                FROM quiz_text_answers
                WHERE attempt_id = ?
                  AND question_id = ?
            """, (attempt["id"], self.open_question_id)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 2)

    def test_quiz_start_rejects_malformed_assignment_plan_without_attempt(self):
        assignment_id = self._create_assignment(question_plan_json=json.dumps({
            "mixed_open_enabled": True,
            "question_ids": [self.mc_question_id],
            "open_question_ids": [self.open_question_id],
        }))

        response = self.client.post(f"/quiz/{assignment_id}", data={"student_name": "Bad Plan"})

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self._attempt_for_assignment(assignment_id))


if __name__ == "__main__":
    unittest.main()

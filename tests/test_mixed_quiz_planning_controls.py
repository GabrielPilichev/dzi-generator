import atexit
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path


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


def _register_dzi_default_denied_test_route(app):
    endpoint = "dzi_test_only_default_denied"
    if endpoint in app.view_functions:
        return

    def view():
        return "test-only DZI endpoint"

    app.add_url_rule("/__test__/dzi-default-denied", endpoint=endpoint, view_func=view)


class MixedQuizPlanningControlsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = web_app.app
        _register_dzi_default_denied_test_route(cls.app)
        cls.app.config.update(TESTING=True)
        conn = web_app.quiz_db()
        try:
            cls.section = cls._first_eligible_section(conn)
            cls.open_source_slug = cls._first_open_source_slug(conn)
        finally:
            conn.close()

    @staticmethod
    def _first_eligible_section(conn):
        rows = conn.execute("""
            SELECT id, section_slug
            FROM curriculum_sections
            ORDER BY id
        """).fetchall()
        for row in rows:
            if web_app.quiz_section_question_ids(conn, int(row["id"])):
                return row
        raise AssertionError("No section with eligible MC questions found")

    @staticmethod
    def _first_open_source_slug(conn):
        candidates = web_app.fetch_open_question_candidates(conn)
        if not candidates:
            raise AssertionError("No open question candidates found")
        return candidates[0]["source_slug"]

    def setUp(self):
        self.client = self.app.test_client()

    def _login_tester(self):
        with self.client.session_transaction() as sess:
            sess["tester_authenticated"] = True
            sess["ui_profile"] = "tester"

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True
            sess["ui_profile"] = "admin"

    def _assignment_count(self):
        conn = web_app.quiz_db()
        try:
            return conn.execute("SELECT COUNT(*) FROM quiz_assignments").fetchone()[0]
        finally:
            conn.close()

    def _assignment(self, assignment_id):
        conn = web_app.quiz_db()
        try:
            return conn.execute("SELECT * FROM quiz_assignments WHERE id = ?", (assignment_id,)).fetchone()
        finally:
            conn.close()

    @staticmethod
    def _assignment_id_from_location(location):
        return int(str(location).rstrip("/").split("/")[-1])

    def _attempt_count(self):
        conn = web_app.quiz_db()
        try:
            return conn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0]
        finally:
            conn.close()

    def _text_answer_count(self):
        conn = web_app.quiz_db()
        try:
            exists = conn.execute("""
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'quiz_text_answers'
            """).fetchone()
            if not exists:
                return 0
            return conn.execute("SELECT COUNT(*) FROM quiz_text_answers").fetchone()[0]
        finally:
            conn.close()

    def test_get_defaults_open_count_to_zero(self):
        self._login_tester()
        response = self.client.get("/teacher/new")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="include_open_questions"', response.data)
        self.assertIn(b'name="open_count"', response.data)
        self.assertIn(b'value="0"', response.data)
        self.assertIn("MC-only".encode("utf-8"), response.data)

    def test_existing_mc_only_creation_path_still_works_without_new_params(self):
        self._login_tester()
        before_assignments = self._assignment_count()
        before_text_answers = self._text_answer_count()

        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "1",
            "time_limit_minutes": "",
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn("/teacher/assignment/", response.headers["Location"])
        self.assertEqual(self._assignment_count(), before_assignments + 1)
        self.assertEqual(self._text_answer_count(), before_text_answers)
        assignment_id = self._assignment_id_from_location(response.headers["Location"])
        self.assertIsNone(self._assignment(assignment_id)["question_plan_json"])

    def test_include_open_absent_with_open_count_keeps_mc_only_behavior(self):
        self._login_tester()
        before_assignments = self._assignment_count()

        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "1",
            "open_count": "2",
            "time_limit_minutes": "",
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn("/teacher/assignment/", response.headers["Location"])
        self.assertEqual(self._assignment_count(), before_assignments + 1)

    def test_include_open_with_positive_count_reports_plan_without_creating_assignment(self):
        self._login_tester()
        before_assignments = self._assignment_count()
        before_attempts = self._attempt_count()
        before_text_answers = self._text_answer_count()

        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "1",
            "time_limit_minutes": "",
            "include_open_questions": "1",
            "open_count": "1",
            "source_slug": self.open_source_slug,
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn("Планиране само за учител/админ".encode("utf-8"), response.data)
        self.assertIn("Само админ може да създаде експериментален смесен тест".encode("utf-8"), response.data)
        self.assertEqual(self._assignment_count(), before_assignments)
        self.assertEqual(self._attempt_count(), before_attempts)
        self.assertEqual(self._text_answer_count(), before_text_answers)

    def test_admin_mixed_open_assignment_stores_question_plan_json(self):
        self._login_admin()
        before_assignments = self._assignment_count()

        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "1",
            "time_limit_minutes": "",
            "include_open_questions": "1",
            "open_count": "1",
            "source_slug": self.open_source_slug,
            "create_mixed_assignment": "1",
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn("/teacher/assignment/", response.headers["Location"])
        self.assertEqual(self._assignment_count(), before_assignments + 1)
        assignment_id = self._assignment_id_from_location(response.headers["Location"])
        stored = json.loads(self._assignment(assignment_id)["question_plan_json"])
        self.assertTrue(stored["mixed_open_enabled"])
        self.assertEqual(len(stored["open_question_ids"]), 1)
        self.assertIn(stored["open_question_ids"][0], stored["question_ids"])
        self.assertFalse(stored["include_open_answers_in_final_score"])

    def test_tester_cannot_create_mixed_open_assignment(self):
        self._login_tester()
        before_assignments = self._assignment_count()

        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "1",
            "time_limit_minutes": "",
            "include_open_questions": "1",
            "open_count": "1",
            "source_slug": self.open_source_slug,
            "create_mixed_assignment": "1",
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])
        self.assertEqual(self._assignment_count(), before_assignments)

    def test_mixed_open_assignment_starts_attempt_renders_and_records_open_answer(self):
        self._login_admin()
        create_response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "1",
            "time_limit_minutes": "",
            "include_open_questions": "1",
            "open_count": "1",
            "source_slug": self.open_source_slug,
            "create_mixed_assignment": "1",
        })
        assignment_id = self._assignment_id_from_location(create_response.headers["Location"])
        plan = json.loads(self._assignment(assignment_id)["question_plan_json"])
        open_question_id = plan["open_question_ids"][0]

        start_response = self.client.post(f"/quiz/{assignment_id}", data={"student_name": "Mixed Assignment Student"})
        self.assertEqual(start_response.status_code, 302)
        attempt_id = int(start_response.headers["Location"].rstrip("/").split("/")[-1])

        attempt_response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(attempt_response.status_code, 200)
        self.assertIn(f'name="open_q_{open_question_id}_1"'.encode("utf-8"), attempt_response.data)

        submit_response = self.client.post(f"/quiz/attempt/{attempt_id}", data={
            f"open_q_{open_question_id}_1": "клиент",
        })
        self.assertEqual(submit_response.status_code, 302)
        conn = web_app.quiz_db()
        try:
            stored_attempt = conn.execute(
                "SELECT question_ids_json, score_total FROM quiz_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            text_count = conn.execute("""
                SELECT COUNT(*)
                FROM quiz_text_answers
                WHERE attempt_id = ?
                  AND question_id = ?
            """, (attempt_id, open_question_id)).fetchone()[0]
        finally:
            conn.close()
        self.assertFalse(json.loads(stored_attempt["question_ids_json"])["include_open_answers_in_final_score"])
        self.assertEqual(stored_attempt["score_total"], 1)
        self.assertGreaterEqual(text_count, 1)

    def test_mixed_plan_reports_shortfall_when_open_count_exceeds_available(self):
        self._login_tester()
        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "1",
            "time_limit_minutes": "",
            "include_open_questions": "1",
            "open_count": "999",
            "source_slug": self.open_source_slug,
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn("Има недостиг".encode("utf-8"), response.data)
        self.assertIn("отворени".encode("utf-8"), response.data)

    def test_admin_can_use_planning_controls(self):
        self._login_admin()
        response = self.client.get("/teacher/new")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="include_open_questions"', response.data)

    def test_sessionless_user_still_needs_tester_login_for_teacher_new(self):
        response = self.client.get("/teacher/new")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/tester/login", response.headers["Location"])

    def test_tester_does_not_gain_admin_candidates_page_access(self):
        self._login_tester()
        response = self.client.get("/admin/open-question-candidates")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()

from unittest.mock import patch
import unittest


from tests.auth_helpers import sign_in_admin
from tests.support import IsolatedAppTestCase, web_app
from web.blueprints import teacher as teacher_module
from web.services import quiz as quiz_service

class HardenAssignmentInputsTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        cls.app.config.update(TESTING=True)
        conn = web_app.quiz_db()
        try:
            cls.section = cls._first_eligible_section(conn)
        finally:
            conn.close()

    @staticmethod
    def _first_eligible_section(conn):
        rows = conn.execute("SELECT id FROM curriculum_sections ORDER BY id").fetchall()
        for row in rows:
            if web_app.quiz_section_question_ids(conn, int(row["id"])):
                return row
        raise AssertionError("No section with eligible MC questions found")

    def setUp(self):
        self.client = self.app.test_client()
        # Clean up assignments for each test if needed, but let's just use unique titles
    
    def _login_admin(self):
        sign_in_admin(self.client, web_app.app.config["DB_PATH"])

    def test_teacher_new_malformed_numeric_input(self):
        self._login_admin()
        # Case 1: non-numeric section_id
        resp = self.client.post("/teacher/new", data={
            "section_id": "abc",
            "question_count": "10",
            "time_limit_minutes": "15"
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Провери числовите стойности", resp.get_data(as_text=True))

        # Case 2: non-numeric question_count
        resp = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "xyz",
            "time_limit_minutes": "15"
        })
        self.assertEqual(resp.status_code, 400)

        # Case 3: non-numeric open_count (if mixed enabled)
        resp = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "10",
            "include_open_questions": "on",
            "open_count": "bad"
        })
        self.assertEqual(resp.status_code, 400)

    def test_teacher_new_time_limit_out_of_range(self):
        self._login_admin()
        # Too low
        resp = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "10",
            "time_limit_minutes": "0"
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Времето трябва да е между", resp.get_data(as_text=True))
        self.assertIn("1 и 600 минути", resp.get_data(as_text=True))

        # Too high
        resp = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "10",
            "time_limit_minutes": "601"
        })
        self.assertEqual(resp.status_code, 400)

    def test_teacher_new_valid_creation(self):
        self._login_admin()
        resp = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "5",
            "time_limit_minutes": "20"
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.location.startswith("/teacher/assignment/"))

    def test_teacher_new_preserves_large_valid_time_limits(self):
        self._login_admin()
        for minutes in (400, 600):
            with self.subTest(minutes=minutes):
                resp = self.client.post("/teacher/new", data={
                    "section_id": str(self.section["id"]),
                    "question_count": "5",
                    "time_limit_minutes": str(minutes),
                }, follow_redirects=False)
                self.assertEqual(resp.status_code, 302)
                assignment_id = int(resp.location.rstrip("/").rsplit("/", 1)[-1])

                conn = web_app.quiz_db()
                try:
                    row = conn.execute(
                        "SELECT time_limit_minutes FROM quiz_assignments WHERE id = ?",
                        (assignment_id,),
                    ).fetchone()
                finally:
                    conn.close()

                self.assertIsNotNone(row)
                self.assertEqual(row["time_limit_minutes"], minutes)

    def test_duplicate_title_capped(self):
        self._login_admin()
        # Create an assignment with a long title
        long_title = "A" * 200
        conn = web_app.quiz_db()
        cur = conn.execute("""
            INSERT INTO quiz_assignments (section_id, title_bg, question_count)
            VALUES (?, ?, ?)
        """, (self.section["id"], long_title, 5))
        assignment_id = cur.lastrowid
        conn.commit()
        conn.close()

        # Duplicate it
        resp = self.client.post(f"/teacher/assignment/{assignment_id}/duplicate", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        
        # Check the new assignment's title
        conn = web_app.quiz_db()
        new_row = conn.execute("""
            SELECT title_bg FROM quiz_assignments 
            WHERE id > ? 
            ORDER BY id DESC LIMIT 1
        """, (assignment_id,)).fetchone()
        conn.close()
        
        self.assertIsNotNone(new_row)
        self.assertLessEqual(len(new_row["title_bg"]), teacher_module.QUIZ_TITLE_MAX_LENGTH)
        self.assertTrue(new_row["title_bg"].endswith(teacher_module.QUIZ_DUPLICATE_TITLE_SUFFIX))
        
    def test_quiz_start_integrity_error_does_not_claim_attempt(self):
        # Create an assignment
        conn = web_app.quiz_db()
        cur = conn.execute("""
            INSERT INTO quiz_assignments (section_id, title_bg, question_count)
            VALUES (?, ?, ?)
        """, (self.section["id"], "Integrity Test", 5))
        assignment_id = cur.lastrowid
        conn.commit()
        conn.close()

        student_name = "Concurrent Student"
        inserted_attempt_id = None

        def insert_competing_attempt(conn, assignment, name):
            nonlocal inserted_attempt_id
            cur = conn.execute("""
                INSERT INTO quiz_attempts (
                    assignment_id, student_name, seed, question_ids_json, score_total
                )
                VALUES (?, ?, ?, ?, ?)
            """, (int(assignment["id"]), name, 12345, "[]", 0))
            inserted_attempt_id = int(cur.lastrowid)
            conn.commit()
            return 12345, []

        with patch.object(quiz_service, "quiz_pick_questions", side_effect=insert_competing_attempt):
            resp = self.client.post(
                f"/quiz/{assignment_id}",
                data={"student_name": student_name},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 404)
        with self.client.session_transaction() as sess:
            self.assertNotIn(web_app.quiz_attempt_owner_session_key(inserted_attempt_id), sess)
        self.assertEqual(
            self.client.get(f"/quiz/attempt/{inserted_attempt_id}").status_code, 404
        )

if __name__ == "__main__":
    unittest.main()

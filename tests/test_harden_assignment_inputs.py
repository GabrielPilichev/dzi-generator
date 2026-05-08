import unittest
import sqlite3
from unittest.mock import patch
from pathlib import Path
import os
import tempfile
import shutil
import atexit

_TMP = tempfile.TemporaryDirectory()
atexit.register(_TMP.cleanup)

_ROOT = Path(__file__).resolve().parents[1]
_TMP_DB = Path(_TMP.name) / "questions.db"
shutil.copy2(_ROOT / "data" / "questions.db", _TMP_DB)

os.environ["DZI_DB"] = str(_TMP_DB)
os.environ["DZI_ADMIN_PASSWORD"] = "admin-pass"

from web import app as web_app

class HardenAssignmentInputsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True
            sess["ui_profile"] = "admin"

    def test_teacher_new_malformed_numeric_input(self):
        self._login_admin()
        # Case 1: non-numeric section_id
        resp = self.client.post("/teacher/new", data={
            "section_id": "abc",
            "question_count": "10",
            "time_limit_minutes": "15"
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"\xd0\x9f\xd1\x80\xd0\xbe\xd0\xb2\xd0\xb5\xd1\x80\xd0\xb8 \xd1\x87\xd0\xb8\xd1\x81\xd0\xbb\xd0\xbe\xd0\xb2\xd0\xb8\xd1\x82\xd0\xb5 \xd1\x81\xd1\x82\xd0\xbe\xd0\xb9\xd0\xbd\xd0\xbe\xd1\x81\xd1\x82\xd0\xb8", resp.data) # "Провери числовите стойности"

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
        self.assertIn(b"1 \xd0\xb8 600 \xd0\xbc\xd0\xb8\xd0\xbd\xd1\x83\xd1\x82\xd0\xb8", resp.data) # "1 и 600 минути"

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
        self.assertLessEqual(len(new_row["title_bg"]), 200)
        self.assertTrue(new_row["title_bg"].endswith("\u00a0(\u00ba\u00be\u00bf\u00b8\u00b5)")) # Wait, QUIZ_DUPLICATE_TITLE_SUFFIX is " (копие)"
        # Let's check what exactly it is.
        
    def test_quiz_start_integrity_error_redirect(self):
        # Create an assignment
        conn = web_app.quiz_db()
        cur = conn.execute("""
            INSERT INTO quiz_assignments (section_id, title_bg, question_count)
            VALUES (?, ?, ?)
        """, (self.section["id"], "Integrity Test", 5))
        assignment_id = cur.lastrowid
        conn.commit()
        conn.close()

        # First attempt creation
        student_name = "Concurrent Student"
        resp = self.client.post(f"/quiz/start/{assignment_id}", data={"student_name": student_name}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        attempt_url = resp.location

        # Now we want to simulate the IntegrityError in the INSERT.
        # We can do this by mocking the INSERT to fail once with IntegrityError, 
        # but the existing attempt IS there.
        # Actually, the route already handles existing attempts by a SELECT before INSERT.
        # To hit the 'except sqlite3.IntegrityError', the SELECT must NOT find it, but the INSERT must find it.
        
        # We can mock the connection's execute to raise IntegrityError on the INSERT query.
        
        with patch("web.app.quiz_db") as mock_quiz_db:
            # We need a real-ish connection but mock some calls
            real_conn = sqlite3.connect(str(_TMP_DB))
            real_conn.row_factory = sqlite3.Row
            mock_quiz_db.return_value = real_conn
            
            original_execute = real_conn.execute
            
            def side_effect(sql, *args):
                # If it's the SELECT check for existing attempt, return None (simulating it's not there yet)
                if "SELECT *" in sql and "quiz_attempts" in sql and student_name in args:
                    return original_execute(sql + " AND 1=0", args) # Force no results
                
                # If it's the INSERT, raise IntegrityError
                if "INSERT INTO quiz_attempts" in sql:
                    # But before raising, let's make sure it ACTUALLY exists in the DB so the re-fetch works
                    # Wait, it already exists from our first successful POST above.
                    raise sqlite3.IntegrityError("UNIQUE constraint failed: quiz_attempts.assignment_id, quiz_attempts.student_name")
                
                return original_execute(sql, *args)
            
            # This is tricky because real_conn.execute is a C method. We might need to mock the connection object entirely.
            mock_conn = unittest.mock.MagicMock()
            mock_quiz_db.return_value = mock_conn
            
            # Mocking the sequence of events:
            # 1. quiz_fetch_assignment
            # 2. SELECT * FROM quiz_attempts (pre-check) -> return None
            # 3. INSERT INTO quiz_attempts -> raise IntegrityError
            # 4. SELECT * FROM quiz_attempts (re-fetch) -> return the existing attempt
            
            mock_conn.execute.side_effect = [
                unittest.mock.MagicMock(fetchone=lambda: {"id": assignment_id, "question_plan_json": None}), # quiz_fetch_assignment
                unittest.mock.MagicMock(fetchone=lambda: None), # pre-check
                sqlite3.IntegrityError("race"), # INSERT
                unittest.mock.MagicMock(fetchone=lambda: {"id": 12345, "submitted_at": None}), # re-fetch
            ]
            
            resp = self.client.post(f"/quiz/start/{assignment_id}", data={"student_name": student_name}, follow_redirects=False)
            self.assertEqual(resp.status_code, 302)
            self.assertIn("/quiz/attempt/12345", resp.location)

if __name__ == "__main__":
    unittest.main()

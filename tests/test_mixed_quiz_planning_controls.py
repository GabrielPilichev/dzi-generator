import json
import sqlite3
import unittest
from unittest.mock import patch


from tests.auth_helpers import sign_in_admin, sign_in_teacher, student_client
from tests.support import IsolatedAppTestCase, web_app
from web.blueprints import teacher as teacher_module
from web.services import quiz as quiz_service


class MixedQuizPlanningControlsTest(IsolatedAppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = web_app.app
        cls.app.config.update(TESTING=True)
        conn = web_app.quiz_db()
        try:
            cls.section, cls.open_source_slug = cls._first_mixed_eligible_section(conn)
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

    @classmethod
    def _first_mixed_eligible_section(cls, conn):
        rows = conn.execute("""
            SELECT id, section_slug
            FROM curriculum_sections
            ORDER BY id
        """).fetchall()
        for row in rows:
            if not web_app.quiz_section_question_ids(conn, int(row["id"])):
                continue
            candidates = web_app.fetch_open_question_candidates(conn, section_id=int(row["id"]))
            if candidates:
                return row, candidates[0]["source_slug"]
        return cls._first_eligible_section(conn), cls._first_open_source_slug(conn)

    def setUp(self):
        self.client = self.app.test_client()

    def _login_tester(self):
        sign_in_teacher(self.client, web_app.app.config["DB_PATH"])

    def _login_admin(self):
        sign_in_admin(self.client, web_app.app.config["DB_PATH"])

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
        self.assertIn(b'class="open-question-panel"', response.data)
        self.assertIn("ръчен/учителски преглед".encode("utf-8"), response.data)
        self.assertIn("Автоматичната проверка е само ориентир".encode("utf-8"), response.data)
        self.assertIn(b'name="open_count"', response.data)
        self.assertIn(b'value="0"', response.data)
        self.assertIn("MC-only".encode("utf-8"), response.data)

    def test_get_shows_practical_task_links_as_manual_review_option(self):
        self._login_tester()
        response = self.client.get("/teacher/new", query_string={"section": self.section["section_slug"]})

        self.assertEqual(response.status_code, 200)
        self.assertIn("Практически задачи 26–28".encode("utf-8"), response.data)
        self.assertIn("Практическите задачи се проверяват ръчно от учител.".encode("utf-8"), response.data)
        self.assertIn("практическите точки не се включват".encode("utf-8"), response.data)
        self.assertIn(b"/dzi/source/", response.data)
        self.assertIn(b"/practical", response.data)

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

    def test_open_count_zero_creates_mc_only_assignment(self):
        self._login_tester()
        before_assignments = self._assignment_count()

        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "2",
            "open_count": "0",
            "time_limit_minutes": "",
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn("/teacher/assignment/", response.headers["Location"])
        self.assertEqual(self._assignment_count(), before_assignments + 1)
        assignment_id = self._assignment_id_from_location(response.headers["Location"])
        self.assertIsNone(self._assignment(assignment_id)["question_plan_json"])

    def test_new_assignment_rejects_malformed_numeric_inputs(self):
        self._login_tester()
        before_assignments = self._assignment_count()

        response = self.client.post("/teacher/new", data={
            "section_id": "not-a-section",
            "question_count": "many",
            "time_limit_minutes": "soon",
            "open_count": "some",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("Провери числовите стойности".encode("utf-8"), response.data)
        self.assertEqual(self._assignment_count(), before_assignments)

    def test_new_assignment_rejects_out_of_range_time_limit(self):
        self._login_tester()
        before_assignments = self._assignment_count()

        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "1",
            "time_limit_minutes": str(teacher_module.QUIZ_TIME_LIMIT_MAX_MINUTES + 1),
            "open_count": "0",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("Времето трябва да е между".encode("utf-8"), response.data)
        self.assertEqual(self._assignment_count(), before_assignments)

    def test_open_count_positive_without_checkbox_creates_mixed_assignment(self):
        self._login_tester()
        before_assignments = self._assignment_count()

        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "2",
            "open_count": "1",
            "source_slug": self.open_source_slug,
            "time_limit_minutes": "",
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn("/teacher/assignment/", response.headers["Location"])
        self.assertEqual(self._assignment_count(), before_assignments + 1)
        assignment_id = self._assignment_id_from_location(response.headers["Location"])
        stored = json.loads(self._assignment(assignment_id)["question_plan_json"])
        self.assertTrue(stored["mixed_open_enabled"])
        self.assertEqual(len(stored["open_question_ids"]), 1)
        self.assertEqual(len(stored["question_ids"]), 2)

    def test_include_open_with_positive_count_creates_mixed_assignment(self):
        self._login_tester()
        before_assignments = self._assignment_count()
        before_attempts = self._attempt_count()
        before_text_answers = self._text_answer_count()

        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "2",
            "time_limit_minutes": "",
            "include_open_questions": "1",
            "open_count": "1",
            "source_slug": self.open_source_slug,
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn("/teacher/assignment/", response.headers["Location"])
        self.assertEqual(self._assignment_count(), before_assignments + 1)
        self.assertEqual(self._attempt_count(), before_attempts)
        self.assertEqual(self._text_answer_count(), before_text_answers)
        assignment_id = self._assignment_id_from_location(response.headers["Location"])
        stored = json.loads(self._assignment(assignment_id)["question_plan_json"])
        self.assertTrue(stored["mixed_open_enabled"])
        self.assertEqual(len(stored["open_question_ids"]), 1)
        self.assertEqual(len(stored["question_ids"]), 2)

    def test_admin_mixed_open_assignment_stores_question_plan_json(self):
        self._login_admin()
        before_assignments = self._assignment_count()

        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "2",
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

    def test_admin_mixed_open_assignment_can_enable_display_only_combined_score(self):
        self._login_admin()

        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "2",
            "time_limit_minutes": "",
            "include_open_questions": "1",
            "open_count": "1",
            "source_slug": self.open_source_slug,
            "create_mixed_assignment": "1",
            "include_open_answers_in_final_score": "1",
        })

        self.assertEqual(response.status_code, 302)
        assignment_id = self._assignment_id_from_location(response.headers["Location"])
        stored = json.loads(self._assignment(assignment_id)["question_plan_json"])
        self.assertTrue(stored["include_open_answers_in_final_score"])

    def test_tester_can_create_mixed_open_assignment(self):
        self._login_tester()
        before_assignments = self._assignment_count()

        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "2",
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

    def test_tester_cannot_enable_display_only_combined_score(self):
        self._login_tester()
        before_assignments = self._assignment_count()

        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "2",
            "time_limit_minutes": "",
            "include_open_questions": "1",
            "open_count": "1",
            "source_slug": self.open_source_slug,
            "create_mixed_assignment": "1",
            "include_open_answers_in_final_score": "1",
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn("/teacher/assignment/", response.headers["Location"])
        self.assertEqual(self._assignment_count(), before_assignments + 1)
        assignment_id = self._assignment_id_from_location(response.headers["Location"])
        stored = json.loads(self._assignment(assignment_id)["question_plan_json"])
        self.assertFalse(stored["include_open_answers_in_final_score"])

    def test_mixed_open_assignment_starts_attempt_renders_and_records_open_answer(self):
        self._login_admin()
        create_response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "2",
            "time_limit_minutes": "",
            "include_open_questions": "1",
            "open_count": "1",
            "source_slug": self.open_source_slug,
            "create_mixed_assignment": "1",
        })
        assignment_id = self._assignment_id_from_location(create_response.headers["Location"])
        plan = json.loads(self._assignment(assignment_id)["question_plan_json"])
        open_question_id = plan["open_question_ids"][0]

        student = student_client(self.app, web_app.app.config["DB_PATH"])
        start_response = student.post(f"/quiz/{assignment_id}")
        self.assertEqual(start_response.status_code, 302)
        attempt_id = int(start_response.headers["Location"].rstrip("/").split("/")[-1])

        attempt_response = student.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(attempt_response.status_code, 200)
        self.assertIn(f'name="open_q_{open_question_id}_1"'.encode("utf-8"), attempt_response.data)

        submit_response = student.post(f"/quiz/attempt/{attempt_id}", data={
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
        copied_plan = json.loads(stored_attempt["question_ids_json"])
        self.assertFalse(copied_plan["include_open_answers_in_final_score"])
        self.assertIn(open_question_id, copied_plan["open_question_ids"])
        self.assertEqual(stored_attempt["score_total"], 1)
        self.assertGreaterEqual(text_count, 1)

        result_response = student.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(result_response.status_code, 200)
        self.assertNotIn("Комбиниран резултат".encode("utf-8"), result_response.data)

    def test_checked_mixed_open_assignment_attempt_copies_flag_and_shows_combined_score(self):
        self._login_admin()
        create_response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "2",
            "time_limit_minutes": "",
            "include_open_questions": "1",
            "open_count": "1",
            "source_slug": self.open_source_slug,
            "create_mixed_assignment": "1",
            "include_open_answers_in_final_score": "1",
        })
        assignment_id = self._assignment_id_from_location(create_response.headers["Location"])
        plan = json.loads(self._assignment(assignment_id)["question_plan_json"])
        open_question_id = plan["open_question_ids"][0]

        student = student_client(self.app, web_app.app.config["DB_PATH"])
        start_response = student.post(f"/quiz/{assignment_id}")
        self.assertEqual(start_response.status_code, 302)
        attempt_id = int(start_response.headers["Location"].rstrip("/").split("/")[-1])

        submit_response = student.post(f"/quiz/attempt/{attempt_id}", data={
            f"open_q_{open_question_id}_1": "клиент",
        })
        self.assertEqual(submit_response.status_code, 302)

        conn = web_app.quiz_db()
        try:
            stored_attempt = conn.execute("""
                SELECT question_ids_json, score_correct, score_total
                FROM quiz_attempts
                WHERE id = ?
            """, (attempt_id,)).fetchone()
        finally:
            conn.close()
        self.assertTrue(json.loads(stored_attempt["question_ids_json"])["include_open_answers_in_final_score"])
        self.assertEqual(stored_attempt["score_total"], 1)

        result_response = student.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(result_response.status_code, 200)
        self.assertIn("Комбиниран резултат".encode("utf-8"), result_response.data)
        self.assertIn(b'<span class="result-score">0/1</span>', result_response.data)

    def test_open_count_with_no_available_candidates_does_not_create_assignment(self):
        self._login_tester()
        before_assignments = self._assignment_count()

        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "1",
            "time_limit_minutes": "",
            "include_open_questions": "1",
            "open_count": "1",
            "source_slug": "__no_open_candidates__",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("Няма достатъчно отворени въпроси".encode("utf-8"), response.data)
        self.assertEqual(self._assignment_count(), before_assignments)

    def test_open_count_greater_than_question_count_is_rejected(self):
        self._login_tester()
        before_assignments = self._assignment_count()

        response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "1",
            "time_limit_minutes": "",
            "open_count": "2",
            "source_slug": self.open_source_slug,
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("Броят отворени въпроси не може да надвишава".encode("utf-8"), response.data)
        self.assertEqual(self._assignment_count(), before_assignments)

    def test_admin_can_use_planning_controls(self):
        self._login_admin()
        response = self.client.get("/teacher/new")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="include_open_questions"', response.data)
        self.assertIn("Включи отворени въпроси".encode("utf-8"), response.data)
        self.assertIn("автоматичната проверка е ориентир".encode("utf-8"), response.data)

    def test_sessionless_user_still_needs_to_sign_in_for_teacher_new(self):
        response = self.client.get("/teacher/new")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login?next=/teacher/new")

    def test_teacher_does_not_gain_admin_candidates_page_access(self):
        self._login_tester()
        response = self.client.get("/admin/open-question-candidates")

        self.assertEqual(response.status_code, 403)

    def test_quiz_start_never_claims_a_historical_attempt_with_the_same_name(self):
        self._login_tester()
        create_response = self.client.post("/teacher/new", data={
            "section_id": str(self.section["id"]),
            "question_count": "1",
            "time_limit_minutes": "",
        })
        assignment_id = self._assignment_id_from_location(create_response.headers["Location"])
        student_name = "Concurrent Student"
        inserted_attempt_id = None

        def insert_competing_attempt(conn, assignment, name):
            nonlocal inserted_attempt_id
            cur = conn.execute("""
                INSERT INTO quiz_attempts (
                    assignment_id, student_name, seed, question_ids_json, score_total
                )
                VALUES (?, ?, ?, ?, ?)
            """, (int(assignment["id"]), student_name, 12345, "[]", 0))
            inserted_attempt_id = int(cur.lastrowid)
            conn.commit()
            return 12345, []

        # A historical (no-account) attempt under the account's display name
        # appears while the account starts: it is neither a conflict nor claimed.
        student = student_client(self.app, web_app.app.config["DB_PATH"], display_name=student_name)
        with patch.object(quiz_service, "quiz_pick_questions", side_effect=insert_competing_attempt):
            response = student.post(f"/quiz/{assignment_id}")

        self.assertEqual(response.status_code, 302)
        own_attempt_id = int(response.headers["Location"].rsplit("/", 1)[-1])
        self.assertNotEqual(own_attempt_id, inserted_attempt_id)
        with student.session_transaction() as sess:
            self.assertNotIn(web_app.quiz_attempt_owner_session_key(inserted_attempt_id), sess)
        self.assertEqual(student.get(f"/quiz/attempt/{inserted_attempt_id}").status_code, 404)
        conn = web_app.quiz_db()
        try:
            owners = dict(conn.execute(
                "SELECT id, user_id FROM quiz_attempts WHERE assignment_id = ?", (assignment_id,)
            ).fetchall())
        finally:
            conn.close()
        self.assertIsNone(owners[inserted_attempt_id])
        self.assertIsNotNone(owners[own_attempt_id])


if __name__ == "__main__":
    unittest.main()

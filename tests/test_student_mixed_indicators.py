import atexit
import json
import os
import shutil
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


class StudentMixedIndicatorsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = web_app.app
        _register_dzi_default_denied_test_route(cls.app)
        cls.app.config.update(TESTING=True)
        conn = web_app.quiz_db()
        try:
            cls.section = cls._first_eligible_section(conn)
            cls.mc_question_id = web_app.quiz_section_question_ids(
                conn, int(cls.section["id"])
            )[0]
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
            VALUES (?, ?, 'fill_in', 'student-mixed', 'medium', 1, ?, 0, 0, NULL)
        """, (
            "temp-student-mixed",
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

    def _create_assignment(self, *, question_plan_json=None, title="Student Mixed"):
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO quiz_assignments (
                    section_id, title_bg, question_count, time_limit_minutes, question_plan_json
                )
                VALUES (?, ?, ?, ?, ?)
            """, (self.section["id"], title, 2, None, question_plan_json))
            assignment_id = int(cur.lastrowid)
            conn.commit()
            return assignment_id
        finally:
            conn.close()

    def _attempt_count(self, assignment_id):
        conn = web_app.quiz_db()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM quiz_attempts WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()[0]
        finally:
            conn.close()

    def _delete_assignment(self, assignment_id):
        conn = web_app.quiz_db()
        try:
            conn.execute("""
                DELETE FROM quiz_text_answers
                WHERE attempt_id IN (SELECT id FROM quiz_attempts WHERE assignment_id = ?)
            """, (assignment_id,))
            conn.execute("""
                DELETE FROM quiz_answers
                WHERE attempt_id IN (SELECT id FROM quiz_attempts WHERE assignment_id = ?)
            """, (assignment_id,))
            conn.execute("DELETE FROM quiz_attempts WHERE assignment_id = ?", (assignment_id,))
            conn.execute("DELETE FROM quiz_assignments WHERE id = ?", (assignment_id,))
            conn.commit()
        finally:
            conn.close()

    def _mixed_plan(self, *, combined_score=False):
        return json.dumps({
            "mixed_open_enabled": True,
            "question_ids": [self.mc_question_id, self.open_question_id],
            "open_question_ids": [self.open_question_id],
            "include_open_answers_in_final_score": combined_score,
        })

    # --- quiz_start ----------------------------------------------------

    def test_quiz_start_mc_only_has_no_mixed_badge(self):
        assignment_id = self._create_assignment(title="MC Only Start")
        try:
            response = self.client.get(f"/quiz/{assignment_id}")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertNotIn("отворени въпроса", body)
            self.assertNotIn("сборен резултат", body)
            self.assertNotIn("pill--mixed", body)
        finally:
            self._delete_assignment(assignment_id)

    def test_quiz_start_mixed_shows_open_count_pill(self):
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_plan(combined_score=False),
            title="Mixed Start",
        )
        try:
            response = self.client.get(f"/quiz/{assignment_id}")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("1 отворени въпроса", body)
            self.assertIn("Този тест съдържа 1 отворени въпроса.", body)
            self.assertIn("Стандартният MC резултат се записва отделно.", body)
            self.assertNotIn("сборен резултат", body)
            self.assertNotIn("pill--combined", body)
        finally:
            self._delete_assignment(assignment_id)

    def test_quiz_start_mixed_combined_shows_combined_pill_and_hint(self):
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_plan(combined_score=True),
            title="Mixed Combined Start",
        )
        try:
            response = self.client.get(f"/quiz/{assignment_id}")
            body = response.data.decode("utf-8")
            self.assertIn("pill pill--combined", body)
            self.assertIn("сборен резултат", body)
            self.assertIn("Ще виждаш и сборен резултат заедно с MC точките", body)
        finally:
            self._delete_assignment(assignment_id)

    def test_quiz_start_get_does_not_create_attempt(self):
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_plan(combined_score=True),
            title="No Attempt On GET",
        )
        try:
            self.assertEqual(self._attempt_count(assignment_id), 0)
            response = self.client.get(f"/quiz/{assignment_id}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self._attempt_count(assignment_id), 0)
        finally:
            self._delete_assignment(assignment_id)

    def test_quiz_start_post_still_creates_attempt(self):
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_plan(combined_score=False),
            title="Start POST Still Works",
        )
        try:
            response = self.client.post(
                f"/quiz/{assignment_id}",
                data={"student_name": "Student Indicator"},
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self._attempt_count(assignment_id), 1)
        finally:
            self._delete_assignment(assignment_id)

    def test_quiz_start_malformed_plan_does_not_crash_get(self):
        assignment_id = self._create_assignment(
            question_plan_json="{not valid",
            title="Broken Plan Start",
        )
        try:
            response = self.client.get(f"/quiz/{assignment_id}")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertNotIn("отворени въпроса", body)
        finally:
            self._delete_assignment(assignment_id)

    # --- quiz_attempt --------------------------------------------------

    def _start_attempt(self, assignment_id, *, student_name):
        response = self.client.post(
            f"/quiz/{assignment_id}",
            data={"student_name": student_name},
        )
        self.assertEqual(response.status_code, 302)
        location = response.headers.get("Location", "")
        return int(location.rstrip("/").rsplit("/", 1)[-1])

    def test_quiz_attempt_mc_only_has_no_mixed_banner(self):
        assignment_id = self._create_assignment(title="MC Only Attempt")
        try:
            attempt_id = self._start_attempt(assignment_id, student_name="MC Student")
            response = self.client.get(f"/quiz/attempt/{attempt_id}")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertNotIn("quiz-mixed-banner", body)
            self.assertNotIn("отворени въпроса", body)
        finally:
            self._delete_assignment(assignment_id)

    def test_quiz_attempt_mixed_shows_top_banner_and_per_question_warning(self):
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_plan(combined_score=False),
            title="Mixed Attempt",
        )
        try:
            attempt_id = self._start_attempt(
                assignment_id,
                student_name="Mixed Student",
            )
            response = self.client.get(f"/quiz/attempt/{attempt_id}")
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8")
            self.assertIn("quiz-mixed-banner", body)
            self.assertIn("1 отворени въпроса", body)
            self.assertIn(
                "Отворените отговори се записват и преглеждат отделно.",
                body,
            )
            self.assertIn("Съхраненият MC резултат не се променя.", body)
            self.assertNotIn("сборен резултат", body)
        finally:
            self._delete_assignment(assignment_id)

    def test_quiz_attempt_mixed_combined_uses_combined_wording(self):
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_plan(combined_score=True),
            title="Mixed Combined Attempt",
        )
        try:
            attempt_id = self._start_attempt(
                assignment_id,
                student_name="Combined Student",
            )
            response = self.client.get(f"/quiz/attempt/{attempt_id}")
            body = response.data.decode("utf-8")
            self.assertIn("pill pill--combined", body)
            self.assertIn("ще видиш и сборен резултат", body)
            self.assertIn("Съхраненият MC резултат остава отделен.", body)
            # Per-question fill_in warning also reflects combined-on wording
            self.assertIn("ще видиш сборен резултат", body)
        finally:
            self._delete_assignment(assignment_id)

    def test_quiz_attempt_post_submission_unchanged(self):
        assignment_id = self._create_assignment(
            question_plan_json=self._mixed_plan(combined_score=False),
            title="Submit Unchanged",
        )
        try:
            attempt_id = self._start_attempt(
                assignment_id,
                student_name="Submitter",
            )
            response = self.client.post(
                f"/quiz/attempt/{attempt_id}",
                data={
                    f"open_q_{self.open_question_id}_1": "клиент",
                    f"open_q_{self.open_question_id}_2": "jpg",
                },
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn(
                f"/quiz/attempt/{attempt_id}/result",
                response.headers.get("Location", ""),
            )
            conn = web_app.quiz_db()
            try:
                row = conn.execute(
                    "SELECT submitted_at, score_correct, score_total FROM quiz_attempts WHERE id = ?",
                    (attempt_id,),
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(row["submitted_at"])
            self.assertEqual(row["score_total"], 1)
        finally:
            self._delete_assignment(assignment_id)


if __name__ == "__main__":
    unittest.main()

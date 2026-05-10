import atexit
import html
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

from web import app as web_app  # noqa: E402


class SectionReviewAnswersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web_app.QUIZ_DB_PATH = _TMP_DB
        web_app.QUIZ_VAULT_PATH = _TMP_VAULT
        cls.app = web_app.app
        cls.app.config.update(TESTING=True)
        cls.app.config["DB_PATH"] = str(_TMP_DB)

        conn = web_app.quiz_db()
        try:
            row = cls._first_section_question_with_correct_option(conn)
            cls.section_id = int(row["section_id"])
            cls.section_slug = row["section_slug"]
            cls.question_id = int(row["question_id"])
            cls.correct_text = row["correct_text"]
            cls.any_option_text = row["any_option_text"]
        finally:
            conn.close()

    @staticmethod
    def _first_section_question_with_correct_option(conn):
        row = conn.execute("""
            SELECT
                cs.id AS section_id,
                cs.section_slug,
                q.id AS question_id,
                correct.option_text AS correct_text,
                any_opt.option_text AS any_option_text
            FROM curriculum_sections cs
            JOIN topic_section_assignments tsa ON tsa.section_id = cs.id
            JOIN questions q ON q.topic_id = tsa.topic_id
            JOIN multiple_choice_options correct
              ON correct.question_id = q.id
             AND correct.is_correct = 1
            JOIN multiple_choice_options any_opt
              ON any_opt.question_id = q.id
            WHERE q.question_type = 'multiple_choice'
              AND (q.is_ai_generated = 0 OR q.quality_score >= 1.0)
            ORDER BY cs.id, q.id, any_opt.option_letter
            LIMIT 1
        """).fetchone()
        if row is None:
            raise AssertionError("No section question with a correct option found")
        return row

    def setUp(self):
        self.client = self.app.test_client()

    def test_section_review_hides_answers_behind_details(self):
        response = self.client.get(f"/section/{self.section_slug}")
        self.assertEqual(response.status_code, 200)

        body = response.data.decode("utf-8")
        escaped_correct = html.escape(self.correct_text, quote=True)
        self.assertIn("<summary>Покажи отговорите</summary>", body)
        details_start = body.rfind('<details class="answer-details">', 0, body.find(escaped_correct))
        self.assertNotEqual(details_start, -1)
        details_end = body.find("</details>", details_start)
        self.assertNotEqual(details_end, -1)
        self.assertNotIn("open", body[details_start:body.find(">", details_start)])
        self.assertIn(escaped_correct, body[details_start:details_end])

    def test_active_quiz_attempt_still_shows_answer_options_normally(self):
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO quiz_assignments (
                    section_id, title_bg, question_count, time_limit_minutes
                )
                VALUES (?, 'Review Answers Regression', 1, NULL)
            """, (self.section_id,))
            assignment_id = int(cur.lastrowid)
            cur = conn.execute("""
                INSERT INTO quiz_attempts (
                    assignment_id, student_name, seed, question_ids_json, score_total
                )
                VALUES (?, 'Active Options Student', 'review-answer-seed', ?, 1)
            """, (assignment_id, json.dumps([self.question_id])))
            attempt_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()

        response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertIn(html.escape(self.any_option_text, quote=True), body)
        self.assertIn('class="quiz-option-card"', body)
        self.assertNotIn("answer-details", body)


if __name__ == "__main__":
    unittest.main()

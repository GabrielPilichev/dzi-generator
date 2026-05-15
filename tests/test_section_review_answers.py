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
            cls.open_answer_question_id = cls._insert_open_answer_fixture(conn)
            cls.empty_answer_question_id = cls._insert_empty_open_answer_fixture(conn)
            cls.xss_answer_question_id = cls._insert_xss_open_answer_fixture(conn)
            cls.dzi_q23 = cls._fetch_dzi_task_23_answer_fixture(conn)
            conn.commit()
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

    @classmethod
    def _insert_section_question(cls, conn, *, prompt, correct_answer, alternatives=None):
        cur = conn.execute("""
            INSERT INTO questions (
                source_exam, source_number, question_type, topic, topic_id,
                difficulty, points, prompt, has_image, is_ai_generated, quality_score
            )
            VALUES (?, ?, 'fill_in', 'section-review-open-test', ?, 'medium', 1, ?, 0, 0, NULL)
        """, (
            "temp-section-review-open-test",
            9000 + conn.execute("""
                SELECT COUNT(*)
                FROM questions
                WHERE source_exam = 'temp-section-review-open-test'
            """).fetchone()[0],
            cls._section_topic_id(conn),
            prompt,
        ))
        question_id = int(cur.lastrowid)
        conn.execute("""
            INSERT INTO fill_in_subquestions (
                question_id, subquestion_number, subquestion_text, correct_answer, answer_alternatives
            )
            VALUES (?, 1, ?, ?, ?)
        """, (
            question_id,
            "Попълнете стойността",
            correct_answer,
            alternatives,
        ))
        return question_id

    @classmethod
    def _section_topic_id(cls, conn):
        row = conn.execute("""
            SELECT topic_id
            FROM topic_section_assignments
            WHERE section_id = ?
            ORDER BY is_primary DESC, topic_id
            LIMIT 1
        """, (cls.section_id,)).fetchone()
        if row is None:
            raise AssertionError("Section has no assigned topic")
        return int(row["topic_id"])

    @classmethod
    def _insert_open_answer_fixture(cls, conn):
        return cls._insert_section_question(
            conn,
            prompt="Отворен въпрос с алтернативи за преглед.",
            correct_answer='["клиент", "потребител"]',
            alternatives='["user", "client"]',
        )

    @classmethod
    def _insert_empty_open_answer_fixture(cls, conn):
        return cls._insert_section_question(
            conn,
            prompt="Отворен въпрос без въведен приет отговор.",
            correct_answer="",
            alternatives=None,
        )

    @classmethod
    def _insert_xss_open_answer_fixture(cls, conn):
        return cls._insert_section_question(
            conn,
            prompt="Отворен въпрос с HTML в отговора.",
            correct_answer='<script>alert("x")</script>',
            alternatives=None,
        )

    @staticmethod
    def _fetch_dzi_task_23_answer_fixture(conn):
        row = conn.execute("""
            SELECT
                e.year,
                e.session,
                e.variant,
                q.id AS question_id,
                fis.correct_answer,
                fis.answer_alternatives
            FROM exams e
            JOIN exam_tasks et ON et.exam_id = e.id
            JOIN exam_task_questions etq
              ON etq.task_id = et.id
             AND etq.role = 'primary'
            JOIN questions q ON q.id = etq.question_id
            JOIN fill_in_subquestions fis ON fis.question_id = q.id
            WHERE e.format_version = ?
              AND et.task_number = 23
              AND q.question_type IN ('fill_in', 'short_answer')
              AND TRIM(COALESCE(fis.correct_answer, '')) <> ''
            ORDER BY e.year DESC, e.session, e.variant, fis.subquestion_number
            LIMIT 1
        """, (web_app.DZI_FORMAT_VERSION,)).fetchone()
        if row is None:
            raise AssertionError("No DZI task 23 accepted answer found in DB")
        return dict(row)

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

    def test_section_review_renders_visible_show_all_control(self):
        response = self.client.get(f"/section/{self.section_slug}")
        self.assertEqual(response.status_code, 200)

        body = response.data.decode("utf-8")
        self.assertIn('id="toggle-correct"', body)
        self.assertIn('class="segmented-button reveal-all-button"', body)
        self.assertIn('aria-pressed="false"', body)
        self.assertIn('aria-keyshortcuts="h"', body)
        self.assertIn("Покажи всички отговори", body)

    def test_section_tools_script_is_included_and_keeps_answer_shortcut(self):
        response = self.client.get(f"/section/{self.section_slug}")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertIn('src="/static/js/section-tools.js"', body)

        asset = self.client.get("/static/js/section-tools.js")
        self.assertEqual(asset.status_code, 200)
        script = asset.get_data(as_text=True)
        self.assertIn('event.key.toLowerCase() === "h"', script)
        self.assertIn("Покажи всички отговори", script)
        self.assertIn("Скрий всички отговори", script)
        self.assertIn("syncAnswerToggleFromDetails", script)

    def test_section_review_mc_correct_answer_stays_inside_reveal_block(self):
        response = self.client.get(f"/section/{self.section_slug}")
        self.assertEqual(response.status_code, 200)

        body = response.data.decode("utf-8")
        escaped_correct = html.escape(self.correct_text, quote=True)
        details_start = body.rfind('<details class="answer-details">', 0, body.find(escaped_correct))
        self.assertNotEqual(details_start, -1)
        details_end = body.find("</details>", details_start)
        self.assertNotEqual(details_end, -1)
        details = body[details_start:details_end]

        self.assertIn(escaped_correct, details)
        self.assertIn("верен", details)

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

    def test_section_review_open_question_reveals_accepted_answers_and_alternatives(self):
        response = self.client.get(f"/section/{self.section_slug}")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")

        prompt = "Отворен въпрос с алтернативи за преглед."
        details_start = body.find(prompt)
        self.assertNotEqual(details_start, -1)
        details_start = body.find('<details class="answer-details">', details_start)
        self.assertNotEqual(details_start, -1)
        details_end = body.find("</details>", details_start)
        self.assertNotEqual(details_end, -1)
        details = body[details_start:details_end]

        self.assertIn("<summary>Покажи отговорите</summary>", details)
        self.assertIn("клиент", details)
        self.assertIn("потребител", details)
        self.assertIn("user", details)
        self.assertIn("client", details)

    def test_section_review_open_question_without_answer_shows_fallback(self):
        response = self.client.get(f"/section/{self.section_slug}")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")

        prompt = "Отворен въпрос без въведен приет отговор."
        details_start = body.find(prompt)
        self.assertNotEqual(details_start, -1)
        details_start = body.find('<details class="answer-details">', details_start)
        details_end = body.find("</details>", details_start)
        details = body[details_start:details_end]

        self.assertIn("Няма въведен приет отговор", details)

    def test_section_review_open_answers_are_escaped(self):
        response = self.client.get(f"/section/{self.section_slug}")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")

        self.assertIn("&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;", body)
        self.assertNotIn('<script>alert("x")</script>', body)

    def test_dzi_preparation_task_23_displays_correct_answer_when_present(self):
        slug = web_app.dzi_source_slug(self.dzi_q23)
        expected_answers = web_app.dzi_json_text_list(self.dzi_q23["correct_answer"])
        self.assertTrue(expected_answers)

        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True
            sess["ui_profile"] = "admin"
        response = self.client.get(f"/dzi/source/{slug}")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")

        task_start = body.find("Задача 23")
        self.assertNotEqual(task_start, -1)
        next_task = body.find("Задача 24", task_start)
        task_html = body[task_start:next_task if next_task != -1 else len(body)]
        self.assertIn(html.escape(expected_answers[0], quote=True), task_html)


if __name__ == "__main__":
    unittest.main()

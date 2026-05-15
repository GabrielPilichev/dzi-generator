import atexit
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


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


class TesterFlowSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web_app.QUIZ_DB_PATH = _TMP_DB
        web_app.QUIZ_VAULT_PATH = _TMP_VAULT
        cls.app = web_app.app
        cls.app.config.update(TESTING=True)
        cls.app.config["DB_PATH"] = str(_TMP_DB)

        conn = web_app.quiz_db()
        try:
            cls.section = cls._first_eligible_section(conn)
            cls.section_question_id = web_app.quiz_section_question_ids(
                conn,
                int(cls.section["id"]),
            )[0]
            cls.smoke_question_id = cls._insert_smoke_mc_question(conn)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _first_eligible_section(conn):
        rows = conn.execute("""
            SELECT id, section_slug, title_bg, class
            FROM curriculum_sections
            WHERE class BETWEEN 8 AND 12
            ORDER BY id
        """).fetchall()
        for row in rows:
            if web_app.quiz_section_question_ids(conn, int(row["id"])):
                return row
        raise AssertionError("No section with eligible quiz questions found")

    @classmethod
    def _section_topic_id(cls, conn):
        row = conn.execute("""
            SELECT topic_id
            FROM topic_section_assignments
            WHERE section_id = ?
            ORDER BY is_primary DESC, topic_id
            LIMIT 1
        """, (cls.section["id"],)).fetchone()
        if row is None:
            raise AssertionError("Smoke section has no assigned topic")
        return int(row["topic_id"])

    @classmethod
    def _insert_smoke_mc_question(cls, conn):
        cur = conn.execute("""
            INSERT INTO questions (
                source_exam, source_number, question_type, topic, topic_id,
                difficulty, points, prompt, has_image, image_path,
                is_ai_generated, quality_score
            )
            VALUES (?, 99001, 'multiple_choice', 'tester-flow-smoke', ?, 'hard', 1, ?, 0, NULL, 0, NULL)
        """, (
            "temp-tester-flow-smoke",
            cls._section_topic_id(conn),
            "Smoke въпрос за резултат и обратна връзка.",
        ))
        question_id = int(cur.lastrowid)
        for letter, text, is_correct in (
            ("А", "Грешен smoke отговор", 0),
            ("Б", "Верен smoke отговор", 1),
            ("В", "Друг грешен smoke отговор", 0),
            ("Г", "Още един грешен smoke отговор", 0),
        ):
            conn.execute("""
                INSERT INTO multiple_choice_options (question_id, option_letter, option_text, is_correct)
                VALUES (?, ?, ?, ?)
            """, (question_id, letter, text, is_correct))
        return question_id

    def setUp(self):
        self.client = self.app.test_client()

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True
            sess["ui_profile"] = "admin"

    def _create_assignment(self, *, question_count=1, time_limit_minutes=None):
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO quiz_assignments (section_id, title_bg, question_count, time_limit_minutes)
                VALUES (?, ?, ?, ?)
            """, (
                self.section["id"],
                "Tester Flow Smoke",
                question_count,
                time_limit_minutes,
            ))
            assignment_id = int(cur.lastrowid)
            conn.commit()
            return assignment_id
        finally:
            conn.close()

    def _create_attempt(
        self,
        question_id,
        *,
        submitted,
        time_limit_minutes=None,
        started_at="2026-05-15 10:00:00",
        submitted_at="2026-05-15 10:12:34",
    ):
        assignment_id = self._create_assignment(time_limit_minutes=time_limit_minutes)
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO quiz_attempts (
                    assignment_id, student_name, seed, question_ids_json,
                    started_at, submitted_at, score_correct, score_total
                )
                VALUES (?, 'Smoke Student', 'tester-flow-smoke', ?, ?, ?, ?, 1)
            """, (
                assignment_id,
                json.dumps([question_id]),
                started_at,
                submitted_at if submitted else None,
                0 if submitted else None,
            ))
            attempt_id = int(cur.lastrowid)
            if submitted:
                conn.execute("""
                    INSERT INTO quiz_answers (attempt_id, question_id, chosen_letter, is_correct)
                    VALUES (?, ?, 'А', 0)
                """, (attempt_id, question_id))
            conn.commit()
            return assignment_id, attempt_id
        finally:
            conn.close()

    def test_homepage_renders_main_tester_feedback_markers(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="home-search-input"', html)
        self.assertIn('class="home-section home-dzi-section"', html)
        self.assertIn("Подготовка за ДЗИ", html)
        self.assertIn('class="mobile-profile-menu"', html)
        self.assertIn("Вход за тестер", html)
        self.assertIn("Вход за админ", html)

    def test_grade_page_renders_content_cards_and_primary_links(self):
        response = self.client.get(f"/grade/{self.section['class']}")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('class="section-item"', html)
        self.assertIn('class="stretched-link"', html)
        self.assertIn(f'href="/section/{self.section["section_slug"]}"', html)

    def test_section_review_page_renders_reveal_copy_and_filter_controls(self):
        response = self.client.get(f"/section/{self.section['section_slug']}")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<details class="answer-details">', html)
        self.assertIn('id="toggle-correct"', html)
        self.assertIn("Покажи всички отговори", html)
        self.assertIn('class="copy-question-button"', html)
        self.assertIn('id="question-type-filter"', html)
        self.assertIn('id="question-difficulty-filter"', html)
        self.assertIn('src="/static/js/section-tools.js"', html)

    def test_cyrillic_wrong_passwords_return_normal_login_errors(self):
        for path in ("/tester/login", "/admin/login"):
            with self.subTest(path=path):
                response = self.client.post(path, data={"password": "грешна-парола"})
                self.assertEqual(response.status_code, 200)
                self.assertIn("Грешна парола.".encode("utf-8"), response.data)
                self.assertNotIn("грешна-парола".encode("utf-8"), response.data)

    def test_practical_task_page_and_download_failures_are_safe(self):
        self._login_admin()

        page = self.client.get("/dzi/source/may_2025_v2/practical")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Практически задачи", html)
        self.assertIn("Задача 26", html)
        self.assertIn("/dzi/practical/resource/", html)
        self.assertIn("/download", html)

        missing = self.client.get("/dzi/practical/resource/999999/download")
        self.assertEqual(missing.status_code, 404)

    def test_active_quiz_attempt_renders_timer_progress_and_autosave_markers(self):
        _assignment_id, attempt_id = self._create_attempt(
            self.smoke_question_id,
            submitted=False,
            time_limit_minutes=30,
        )

        with patch.object(web_app, "quiz_current_timestamp", return_value=datetime(2026, 5, 15, 10, 0, 0)):
            response = self.client.get(f"/quiz/attempt/{attempt_id}")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="countdown"', html)
        self.assertIn('data-remaining="1800"', html)
        self.assertIn('id="timer-warning"', html)
        self.assertIn('data-warning-threshold="300"', html)
        self.assertIn('id="quiz-progress"', html)
        self.assertIn('id="quiz-progress-bar"', html)
        self.assertIn('src="/static/js/quiz-draft-autosave.js"', html)
        self.assertIn('src="/static/js/quiz-progress-indicator.js"', html)

    def test_quiz_result_renders_analytics_and_wrong_answer_feedback(self):
        _assignment_id, attempt_id = self._create_attempt(
            self.smoke_question_id,
            submitted=True,
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Време за решаване: 12 мин. 34 сек.", html)
        self.assertIn("Успеваемост: 0%", html)
        self.assertIn("Разбивка по трудност", html)
        self.assertIn("Трудни", html)
        self.assertIn("0/1 верни", html)
        self.assertIn("Правилен отговор", html)
        self.assertIn("Обяснение", html)
        self.assertIn("Няма въведено обяснение за този въпрос.", html)


if __name__ == "__main__":
    unittest.main()

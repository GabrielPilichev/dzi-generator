import atexit
from datetime import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch
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


STALE_MESSAGE = (
    "Този тест съдържа стари или непълни въпроси и не може да бъде показан коректно. "
    "Моля, създайте нов тест."
)


class QuizAttemptRenderTest(unittest.TestCase):
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
            cls.valid_question_id = web_app.quiz_section_question_ids(conn, int(cls.section["id"]))[0]
            cls.valid_prompt = conn.execute(
                "SELECT prompt FROM questions WHERE id = ?",
                (cls.valid_question_id,),
            ).fetchone()["prompt"]
            cls.invalid_question_id = cls._insert_invalid_question(conn)
            (
                cls.visual_filter_section_id,
                cls.visual_filter_question_id,
                cls.visual_filter_text_question_id,
            ) = cls._insert_visual_filter_fixture(conn)
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
        raise AssertionError("No section with eligible quiz questions found")

    @staticmethod
    def _insert_invalid_question(conn):
        cur = conn.execute("""
            INSERT INTO questions (
                source_exam, source_number, question_type, topic, difficulty,
                points, prompt, has_image, is_ai_generated, quality_score
            )
            VALUES (?, ?, 'multiple_choice', 'test', 'medium', 1, ?, 0, 0, NULL)
        """, (
            "temp-stale-render-test",
            1,
            "Invalid stale render question",
        ))
        return int(cur.lastrowid)

    @staticmethod
    def _insert_eligible_mc_question(conn, *, source_number, topic_id=None, prompt, difficulty="medium"):
        cur = conn.execute("""
            INSERT INTO questions (
                source_exam, source_number, question_type, topic, topic_id,
                difficulty, points, prompt, has_image, image_path,
                is_ai_generated, quality_score
            )
            VALUES (?, ?, 'multiple_choice', 'visual-filter-test', ?, ?, 1, ?, 0, NULL, 0, NULL)
        """, (
            "temp-visual-filter-test",
            source_number,
            topic_id,
            difficulty,
            prompt,
        ))
        question_id = int(cur.lastrowid)
        for letter, text, is_correct in (
            ("А", "Първа възможност", 0),
            ("Б", "Втора възможност", 1),
            ("В", "Трета възможност", 0),
            ("Г", "Четвърта възможност", 0),
        ):
            conn.execute("""
                INSERT INTO multiple_choice_options (question_id, option_letter, option_text, is_correct)
                VALUES (?, ?, ?, ?)
            """, (question_id, letter, text, is_correct))
        return question_id

    @staticmethod
    def _insert_eligible_open_question(conn, *, source_exam=None, source_number=16, difficulty="medium"):
        if source_exam is None:
            count = conn.execute("""
                SELECT COUNT(*)
                FROM questions
                WHERE source_exam LIKE 'temp-mixed-open-render-%'
            """).fetchone()[0]
            source_exam = f"temp-mixed-open-render-{count + 1}"

        cur = conn.execute("""
            INSERT INTO questions (
                source_exam, source_number, question_type, topic, difficulty,
                points, prompt, has_image, is_ai_generated, quality_score
            )
            VALUES (?, ?, 'fill_in', 'test', ?, 1, ?, 0, 0, NULL)
        """, (
            source_exam,
            source_number,
            difficulty,
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

    @classmethod
    def _insert_visual_filter_fixture(cls, conn):
        section_cur = conn.execute("""
            INSERT INTO curriculum_sections (
                section_slug, title_bg, class, section_type, display_order,
                has_section_test, is_dzi_relevant
            )
            VALUES (?, ?, 12, 'test', 9999, 1, 1)
        """, (
            "temp-visual-filter-section",
            "Temp visual filter section",
        ))
        section_id = int(section_cur.lastrowid)

        topic_cur = conn.execute("""
            INSERT INTO curriculum_topics (
                topic_slug, title_bg, difficulty, exam_relevance
            )
            VALUES (?, ?, 'medium', ?)
        """, (
            "temp-visual-filter-topic",
            "Temp visual filter topic",
            json.dumps(["DZI"]),
        ))
        topic_id = int(topic_cur.lastrowid)

        conn.execute("""
            INSERT INTO topic_section_assignments (topic_id, section_id, relationship_type, is_primary)
            VALUES (?, ?, 'covers', 1)
        """, (topic_id, section_id))

        visual_question_id = cls._insert_eligible_mc_question(
            conn,
            source_number=1,
            topic_id=topic_id,
            prompt=(
                "Даденото изображение представлява визуално представяне на уеб страница. "
                "Кой е правилният извод?"
            ),
        )
        text_question_id = cls._insert_eligible_mc_question(
            conn,
            source_number=2,
            topic_id=topic_id,
            prompt="Кое твърдение за електронна таблица е вярно?",
        )
        return section_id, visual_question_id, text_question_id

    def setUp(self):
        self.client = self.app.test_client()

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True
            sess["ui_profile"] = "admin"

    def _login_tester(self):
        with self.client.session_transaction() as sess:
            sess["tester_authenticated"] = True
            sess["ui_profile"] = "tester"

    def test_dzi_pool_health_counts_may_2025_v2(self):
        with self.app.app_context():
            health = web_app.fetch_dzi_pool_health("may_2025_v2")

        self.assertIsNotNone(health)
        self.assertEqual(health["source_slug"], "may_2025_v2")
        self.assertEqual(health["imported_count"], 25)
        self.assertEqual(health["usable_count"], 15)
        self.assertEqual(health["filtered_count"], 10)
        self.assertEqual(health["not_yet_supported_count"], 10)
        self.assertEqual(health["invalid_mc_count"], 0)

    def test_generated_quiz_excludes_visual_dependent_question_without_asset(self):
        conn = web_app.quiz_db()
        try:
            question_ids = web_app.quiz_section_question_ids(conn, self.visual_filter_section_id)
        finally:
            conn.close()

        self.assertIn(self.visual_filter_text_question_id, question_ids)
        self.assertNotIn(self.visual_filter_question_id, question_ids)

    def test_visual_eligibility_helper_rejects_missing_visual_but_allows_text_only(self):
        conn = web_app.quiz_db()
        try:
            rows = conn.execute("""
                SELECT id, prompt, question_type, has_image, image_path
                FROM questions
                WHERE id IN (?, ?)
                ORDER BY id
            """, (
                self.visual_filter_question_id,
                self.visual_filter_text_question_id,
            )).fetchall()
            eligibility = {
                int(row["id"]): web_app.is_quiz_question_eligible(conn, row)
                for row in rows
            }
        finally:
            conn.close()

        self.assertFalse(eligibility[self.visual_filter_question_id])
        self.assertTrue(eligibility[self.visual_filter_text_question_id])

    def test_visual_prompt_heuristic_is_conservative(self):
        self.assertTrue(web_app.quiz_prompt_needs_visual("Даденото изображение показва уеб страница."))
        self.assertTrue(web_app.quiz_prompt_needs_visual("В показана диаграма са дадени стойности."))
        self.assertTrue(web_app.quiz_prompt_needs_visual("Коя стойност се вижда в диаграмата?"))
        self.assertTrue(web_app.quiz_prompt_needs_visual("Какво е отбелязано на изображението?"))
        self.assertTrue(web_app.quiz_prompt_needs_visual("Използвайте показаната таблица."))
        self.assertFalse(web_app.quiz_prompt_needs_visual("Кое твърдение за електронна таблица е вярно?"))

    def _create_assignment(self, *, time_limit_minutes=None):
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO quiz_assignments (section_id, title_bg, question_count, time_limit_minutes)
                VALUES (?, ?, 2, ?)
            """, (self.section["id"], self.section["title_bg"], time_limit_minutes))
            assignment_id = int(cur.lastrowid)
            conn.commit()
            return assignment_id
        finally:
            conn.close()

    def _create_attempt(
        self,
        question_ids,
        *,
        submitted=True,
        student_name="Stale Student",
        time_limit_minutes=None,
        started_at=None,
    ):
        assignment_id = self._create_assignment(time_limit_minutes=time_limit_minutes)
        conn = web_app.quiz_db()
        try:
            renderable_question_ids, _skipped_count = web_app.filter_renderable_attempt_question_ids(conn, question_ids)
            cur = conn.execute("""
                INSERT INTO quiz_attempts (
                    assignment_id, student_name, seed, question_ids_json,
                    submitted_at, score_correct, score_total
                )
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, 0, ?)
            """, (
                assignment_id,
                student_name,
                "stale-seed",
                json.dumps(question_ids),
                len(renderable_question_ids),
            ))
            attempt_id = int(cur.lastrowid)

            if not submitted:
                conn.execute("""
                    UPDATE quiz_attempts
                    SET submitted_at = NULL, score_correct = NULL, score_total = ?
                    WHERE id = ?
                """, (len(renderable_question_ids), attempt_id))
            if started_at is not None:
                conn.execute("""
                    UPDATE quiz_attempts
                    SET started_at = ?
                    WHERE id = ?
                """, (started_at, attempt_id))
            elif self.valid_question_id in question_ids:
                wrong_letter = self._wrong_letter(conn, self.valid_question_id)
                conn.execute("""
                    INSERT INTO quiz_answers (attempt_id, question_id, chosen_letter, is_correct)
                    VALUES (?, ?, ?, 0)
                """, (attempt_id, self.valid_question_id, wrong_letter))

            conn.commit()
            return assignment_id, attempt_id
        finally:
            conn.close()

    @staticmethod
    def _set_attempt_timestamps(attempt_id, *, started_at=None, submitted_at=None):
        conn = web_app.quiz_db()
        try:
            conn.execute("""
                UPDATE quiz_attempts
                SET started_at = COALESCE(?, started_at),
                    submitted_at = ?
                WHERE id = ?
            """, (started_at, submitted_at, attempt_id))
            conn.commit()
        finally:
            conn.close()

    def _create_mixed_planned_attempt(
        self,
        *,
        submitted=False,
        student_name="Mixed Open Render",
        include_open_answers_in_final_score=False,
    ):
        assignment_id = self._create_assignment()
        conn = web_app.quiz_db()
        try:
            open_question_id = self._insert_eligible_open_question(conn)
            question_plan = {
                "mixed_open_enabled": True,
                "question_ids": [self.valid_question_id, open_question_id],
                "open_question_ids": [open_question_id],
            }
            if include_open_answers_in_final_score:
                question_plan["include_open_answers_in_final_score"] = True
            cur = conn.execute("""
                INSERT INTO quiz_attempts (
                    assignment_id, student_name, seed, question_ids_json,
                    submitted_at, score_correct, score_total
                )
                VALUES (?, ?, ?, ?, NULL, NULL, 1)
            """, (
                assignment_id,
                student_name,
                "mixed-open-seed",
                json.dumps(question_plan),
            ))
            attempt_id = int(cur.lastrowid)
            if submitted:
                conn.execute("""
                    UPDATE quiz_attempts
                    SET submitted_at = CURRENT_TIMESTAMP, score_correct = 0, score_total = 1
                    WHERE id = ?
                """, (attempt_id,))
            conn.commit()
            return assignment_id, attempt_id, open_question_id
        finally:
            conn.close()

    @staticmethod
    def _ensure_question_explanation_column(conn):
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(questions)").fetchall()
        }
        if "explanation" not in columns:
            conn.execute("ALTER TABLE questions ADD COLUMN explanation TEXT")

    def _quiz_text_answer_count(self):
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

    @staticmethod
    def _attempt_row(attempt_id):
        conn = web_app.quiz_db()
        try:
            return conn.execute(
                "SELECT * FROM quiz_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
        finally:
            conn.close()

    @staticmethod
    def _clear_attempt_answers(attempt_id):
        conn = web_app.quiz_db()
        try:
            conn.execute("DELETE FROM quiz_answers WHERE attempt_id = ?", (attempt_id,))
            conn.execute("DELETE FROM quiz_text_answers WHERE attempt_id = ?", (attempt_id,))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _quiz_text_answer_rows(attempt_id):
        conn = web_app.quiz_db()
        try:
            return conn.execute("""
                SELECT
                    id, question_id, subquestion_number, raw_answer, normalized_answer,
                    is_correct, teacher_override, teacher_note
                FROM quiz_text_answers
                WHERE attempt_id = ?
                ORDER BY question_id, subquestion_number
            """, (attempt_id,)).fetchall()
        finally:
            conn.close()

    @staticmethod
    def _wrong_letter(conn, question_id):
        row = conn.execute("""
            SELECT option_letter
            FROM multiple_choice_options
            WHERE question_id = ?
              AND is_correct = 0
            ORDER BY option_letter
            LIMIT 1
        """, (question_id,)).fetchone()
        if row is None:
            raise AssertionError("Valid test question has no wrong option")
        return row["option_letter"]

    def test_mixed_stale_result_filters_invalid_question(self):
        assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id, self.invalid_question_id],
            student_name="Mixed Result",
        )

        response = self.client.post(f"/quiz/{assignment_id}", data={"student_name": "Mixed Result"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/quiz/attempt/{attempt_id}/result", response.headers["Location"])

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.valid_prompt.encode("utf-8"), response.data)
        self.assertNotIn("Invalid stale render question".encode("utf-8"), response.data)
        self.assertNotIn("Правилен отговор: —".encode("utf-8"), response.data)
        self.assertNotIn(STALE_MESSAGE.encode("utf-8"), response.data)
        self.assertIn("Първоначално зададени: 2".encode("utf-8"), response.data)
        self.assertIn("Пропуснати: 1 невалидни въпроса".encode("utf-8"), response.data)

    def test_student_name_one_character_is_rejected(self):
        assignment_id = self._create_assignment()

        response = self.client.post(f"/quiz/{assignment_id}", data={"student_name": "А"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Името трябва да съдържа поне 2 символа.".encode("utf-8"), response.data)
        conn = web_app.quiz_db()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM quiz_attempts WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_student_name_whitespace_only_is_rejected(self):
        assignment_id = self._create_assignment()

        response = self.client.post(f"/quiz/{assignment_id}", data={"student_name": "   \n\t  "})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Името трябва да съдържа поне 2 символа.".encode("utf-8"), response.data)

    def test_normal_bulgarian_student_name_is_accepted(self):
        assignment_id = self._create_assignment()

        response = self.client.post(f"/quiz/{assignment_id}", data={"student_name": "Иван"})

        self.assertEqual(response.status_code, 302)
        self.assertIn("/quiz/attempt/", response.headers["Location"])
        conn = web_app.quiz_db()
        try:
            row = conn.execute(
                "SELECT student_name FROM quiz_attempts WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["student_name"], "Иван")

    def test_all_invalid_result_shows_stale_message(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.invalid_question_id],
            student_name="Invalid Result",
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertIn(STALE_MESSAGE.encode("utf-8"), response.data)
        self.assertNotIn("Правилен отговор: —".encode("utf-8"), response.data)
        self.assertIn("Първоначално зададени: 1".encode("utf-8"), response.data)
        self.assertIn("Пропуснати: 1 невалидни въпроса".encode("utf-8"), response.data)

    def test_result_hides_skipped_count_when_all_questions_render(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            student_name="Valid Result",
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Първоначално зададени:".encode("utf-8"), response.data)
        self.assertNotIn("Пропуснати:".encode("utf-8"), response.data)

    def test_mixed_stale_active_attempt_filters_invalid_question(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id, self.invalid_question_id],
            submitted=False,
            student_name="Mixed Active",
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.valid_prompt.encode("utf-8"), response.data)
        self.assertNotIn("Invalid stale render question".encode("utf-8"), response.data)
        self.assertNotIn(STALE_MESSAGE.encode("utf-8"), response.data)

    def test_mc_only_active_attempt_does_not_render_open_text_inputs(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="MC Only Active",
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.valid_prompt.encode("utf-8"), response.data)
        self.assertNotIn(b'name="open_q_', response.data)
        self.assertNotIn("Отворените отговори".encode("utf-8"), response.data)

    def test_30_minute_assignment_renders_non_expired_timer(self):
        fixed_now = datetime(2026, 5, 13, 12, 0, 0)
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="Timer 30",
            time_limit_minutes=30,
            started_at="2026-05-13 12:00:00",
        )

        with patch.object(web_app, "quiz_current_timestamp", return_value=fixed_now):
            response = self.client.get(f"/quiz/attempt/{attempt_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-remaining="1800"', response.data)
        self.assertIn(b'data-timer-expired="0"', response.data)

    def test_active_attempt_renders_timer_warning_markup(self):
        fixed_now = datetime(2026, 5, 13, 12, 0, 0)
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="Timer Warning Markup",
            time_limit_minutes=30,
            started_at="2026-05-13 12:00:00",
        )

        with patch.object(web_app, "quiz_current_timestamp", return_value=fixed_now):
            response = self.client.get(f"/quiz/attempt/{attempt_id}")

        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertIn('id="timer-warning"', body)
        self.assertIn('class="timer-warning"', body)
        self.assertIn('role="status"', body)
        self.assertIn('aria-live="polite"', body)
        self.assertIn("hidden", body)

    def test_timer_warning_thresholds_are_rendered_for_js(self):
        fixed_now = datetime(2026, 5, 13, 12, 29, 30)
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="Timer Warning Thresholds",
            time_limit_minutes=30,
            started_at="2026-05-13 12:00:00",
        )

        with patch.object(web_app, "quiz_current_timestamp", return_value=fixed_now):
            response = self.client.get(f"/quiz/attempt/{attempt_id}")

        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertIn('data-remaining="30"', body)
        self.assertIn('data-warning-threshold="300"', body)
        self.assertIn('data-urgent-threshold="60"', body)
        self.assertIn("Остават по-малко от 5 минути.", body)
        self.assertIn("Остава по-малко от 1 минута. Проверете отговорите си.", body)
        self.assertIn("syncWarning(remaining)", body)

    def test_60_minute_assignment_renders_non_expired_timer(self):
        fixed_now = datetime(2026, 5, 13, 12, 0, 0)
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="Timer 60",
            time_limit_minutes=60,
            started_at="2026-05-13 12:00:00",
        )

        with patch.object(web_app, "quiz_current_timestamp", return_value=fixed_now):
            response = self.client.get(f"/quiz/attempt/{attempt_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-remaining="3600"', response.data)
        self.assertIn(b'data-timer-expired="0"', response.data)

    def test_400_minute_assignment_preserves_full_duration(self):
        fixed_now = datetime(2026, 5, 13, 12, 0, 0)
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="Timer 400",
            time_limit_minutes=400,
            started_at="2026-05-13 12:00:00",
        )

        with patch.object(web_app, "quiz_current_timestamp", return_value=fixed_now):
            response = self.client.get(f"/quiz/attempt/{attempt_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-remaining="24000"', response.data)
        self.assertIn(b'data-timer-expired="0"', response.data)

    def test_600_minute_assignment_preserves_full_duration(self):
        fixed_now = datetime(2026, 5, 13, 12, 0, 0)
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="Timer 600",
            time_limit_minutes=600,
            started_at="2026-05-13 12:00:00",
        )

        with patch.object(web_app, "quiz_current_timestamp", return_value=fixed_now):
            response = self.client.get(f"/quiz/attempt/{attempt_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-remaining="36000"', response.data)
        self.assertIn(b'data-timer-expired="0"', response.data)

    def test_quiz_attempt_initial_render_does_not_submit_non_expired_timer(self):
        fixed_now = datetime(2026, 5, 13, 12, 0, 0)
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="Timer No Auto Submit",
            time_limit_minutes=30,
            started_at="2026-05-13 12:00:00",
        )

        with patch.object(web_app, "quiz_current_timestamp", return_value=fixed_now):
            response = self.client.get(f"/quiz/attempt/{attempt_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-timer-expired="0"', response.data)
        conn = web_app.quiz_db()
        try:
            attempt = conn.execute(
                "SELECT submitted_at FROM quiz_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(attempt["submitted_at"])

    def test_no_timer_assignment_omits_countdown(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="No Timer",
            time_limit_minutes=None,
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'id="countdown"', response.data)
        self.assertNotIn(b"data-remaining", response.data)
        self.assertNotIn(b'id="timer-warning"', response.data)

    def test_result_page_does_not_include_timer_warning_controls(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=True,
            student_name="Timer Warning Result",
            time_limit_minutes=30,
            started_at="2026-05-13 12:00:00",
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")

        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertNotIn('id="timer-warning"', body)
        self.assertNotIn("data-warning-threshold", body)
        self.assertNotIn("data-urgent-threshold", body)
        self.assertNotIn("syncWarning(remaining)", body)

    def test_quiz_duration_format_handles_seconds_minutes_and_hours(self):
        self.assertEqual(web_app.quiz_format_duration(34), "34 сек.")
        self.assertEqual(web_app.quiz_format_duration(754), "12 мин. 34 сек.")
        self.assertEqual(web_app.quiz_format_duration(3723), "1 ч. 2 мин. 3 сек.")

    def test_quiz_success_rate_format_handles_common_and_missing_values(self):
        self.assertEqual(web_app.quiz_success_rate_display(3, 4), "75%")
        self.assertEqual(web_app.quiz_success_rate_display(1, 3), "33.3%")
        self.assertEqual(web_app.quiz_success_rate_display(0, 0), "Няма данни за успеваемост")
        self.assertEqual(web_app.quiz_success_rate_display(None, None), "Няма данни за успеваемост")

    def test_completed_result_page_shows_success_rate(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=True,
            student_name="Success Result",
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Успеваемост: 0%".encode("utf-8"), response.data)

    def test_completed_result_page_shows_success_rate_fallback_for_zero_total(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=True,
            student_name="Success Missing Total",
        )
        conn = web_app.quiz_db()
        try:
            conn.execute("""
                UPDATE quiz_attempts
                SET score_correct = 0, score_total = 0
                WHERE id = ?
            """, (attempt_id,))
            conn.commit()
        finally:
            conn.close()

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Успеваемост: Няма данни за успеваемост".encode("utf-8"), response.data)

    def test_completed_result_page_shows_attempt_duration(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=True,
            student_name="Duration Result",
        )
        self._set_attempt_timestamps(
            attempt_id,
            started_at="2026-05-13 12:00:00",
            submitted_at="2026-05-13 12:12:34",
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Време за решаване: 12 мин. 34 сек.".encode("utf-8"), response.data)

    def test_completed_result_page_shows_duration_fallback_for_bad_timestamps(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=True,
            student_name="Duration Bad",
        )
        self._set_attempt_timestamps(
            attempt_id,
            started_at="2026-05-13 12:10:00",
            submitted_at="2026-05-13 12:00:00",
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Време за решаване: Няма данни за време".encode("utf-8"), response.data)

    def test_teacher_results_show_success_rate(self):
        assignment_id, _attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=True,
            student_name="Success Teacher",
        )
        self._login_admin()

        response = self.client.get(f"/teacher/assignment/{assignment_id}/results")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Успеваемост".encode("utf-8"), response.data)
        self.assertIn(">0%</td>".encode("utf-8"), response.data)

    def test_teacher_results_show_attempt_duration(self):
        assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=True,
            student_name="Duration Teacher",
        )
        self._set_attempt_timestamps(
            attempt_id,
            started_at="2026-05-13 12:00:00",
            submitted_at="2026-05-13 13:02:03",
        )
        self._login_admin()

        response = self.client.get(f"/teacher/assignment/{assignment_id}/results")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Време за решаване".encode("utf-8"), response.data)
        self.assertIn("1 ч. 2 мин. 3 сек.".encode("utf-8"), response.data)

    def test_teacher_results_show_duration_fallback_for_missing_end_timestamp(self):
        assignment_id, _attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="Duration Missing End",
            started_at="2026-05-13 12:00:00",
        )
        self._login_admin()

        response = self.client.get(f"/teacher/assignment/{assignment_id}/results")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Няма данни за време".encode("utf-8"), response.data)

    def test_active_attempt_shows_question_difficulty_when_available(self):
        conn = web_app.quiz_db()
        try:
            question_id = self._insert_eligible_mc_question(
                conn,
                source_number=901,
                prompt="Въпрос с показана трудност.",
                difficulty="hard",
            )
            conn.commit()
        finally:
            conn.close()
        _assignment_id, attempt_id = self._create_attempt(
            [question_id],
            submitted=False,
            student_name="Difficulty Active",
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Въпрос с показана трудност.".encode("utf-8"), response.data)
        self.assertIn("Трудност: труден".encode("utf-8"), response.data)
        self.assertNotIn("Трудност: hard".encode("utf-8"), response.data)

    def test_active_attempt_shows_numeric_question_difficulty_label(self):
        conn = web_app.quiz_db()
        try:
            question_id = self._insert_eligible_mc_question(
                conn,
                source_number=903,
                prompt="Въпрос с числова трудност.",
                difficulty=1,
            )
            conn.commit()
        finally:
            conn.close()
        _assignment_id, attempt_id = self._create_attempt(
            [question_id],
            submitted=False,
            student_name="Numeric Difficulty Active",
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Въпрос с числова трудност.".encode("utf-8"), response.data)
        self.assertIn("Трудност: много лесен".encode("utf-8"), response.data)

    def test_result_shows_question_difficulty_label_when_available(self):
        conn = web_app.quiz_db()
        try:
            question_id = self._insert_eligible_mc_question(
                conn,
                source_number=904,
                prompt="Въпрос с трудност в резултата.",
                difficulty="hard",
            )
            conn.commit()
        finally:
            conn.close()
        _assignment_id, attempt_id = self._create_attempt(
            [question_id],
            student_name="Difficulty Result",
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Въпрос с трудност в резултата.".encode("utf-8"), response.data)
        self.assertIn("Трудност: труден".encode("utf-8"), response.data)
        self.assertNotIn("Трудност: hard".encode("utf-8"), response.data)

    def test_active_attempt_omits_difficulty_when_missing(self):
        conn = web_app.quiz_db()
        try:
            question_id = self._insert_eligible_mc_question(
                conn,
                source_number=902,
                prompt="Въпрос без трудност.",
                difficulty=None,
            )
            conn.commit()
        finally:
            conn.close()
        _assignment_id, attempt_id = self._create_attempt(
            [question_id],
            submitted=False,
            student_name="No Difficulty Active",
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Въпрос без трудност.".encode("utf-8"), response.data)
        self.assertNotIn("Трудност:".encode("utf-8"), response.data)

    def test_mixed_planned_attempt_renders_open_text_inputs(self):
        _assignment_id, attempt_id, open_question_id = self._create_mixed_planned_attempt()

        response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.valid_prompt.encode("utf-8"), response.data)
        self.assertIn("Попълнете липсващите стойности.".encode("utf-8"), response.data)
        self.assertIn("Трудност: среден".encode("utf-8"), response.data)
        self.assertNotIn("Трудност: medium".encode("utf-8"), response.data)
        self.assertIn(f'name="open_q_{open_question_id}_1"'.encode("utf-8"), response.data)
        self.assertIn(f'name="open_q_{open_question_id}_2"'.encode("utf-8"), response.data)
        # Per-question warning now reflects combined-score state honestly.
        # Default (combined off) wording.
        self.assertIn(
            "Съхраненият MC резултат не се променя.".encode("utf-8"),
            response.data,
        )

    def test_mc_only_result_does_not_show_open_answer_section(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            student_name="MC Only Result",
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Отворени отговори".encode("utf-8"), response.data)
        self.assertNotIn("не са включени в точния резултат".encode("utf-8"), response.data)
        self.assertNotIn("Информационен сбор за отворени отговори".encode("utf-8"), response.data)
        self.assertNotIn("Интеграцията на отворените отговори".encode("utf-8"), response.data)

    def test_wrong_mc_result_shows_correct_answer_and_explanation_fallback(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            student_name="MC Wrong Feedback",
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Правилен отговор".encode("utf-8"), response.data)
        self.assertIn("Обяснение".encode("utf-8"), response.data)
        self.assertIn("Няма въведено обяснение за този въпрос.".encode("utf-8"), response.data)

    def test_wrong_mc_result_shows_existing_question_explanation(self):
        conn = web_app.quiz_db()
        try:
            self._ensure_question_explanation_column(conn)
            question_id = self._insert_eligible_mc_question(
                conn,
                source_number=905,
                prompt="Въпрос с готово обяснение.",
            )
            conn.execute(
                "UPDATE questions SET explanation = ? WHERE id = ?",
                ("Това е налично обяснение от базата.", question_id),
            )
            conn.commit()
        finally:
            conn.close()

        _assignment_id, attempt_id = self._create_attempt(
            [question_id],
            student_name="MC Existing Explanation",
        )
        conn = web_app.quiz_db()
        try:
            wrong_letter = self._wrong_letter(conn, question_id)
            conn.execute("""
                INSERT INTO quiz_answers (attempt_id, question_id, chosen_letter, is_correct)
                VALUES (?, ?, ?, 0)
            """, (attempt_id, question_id, wrong_letter))
            conn.commit()
        finally:
            conn.close()

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Това е налично обяснение от базата.".encode("utf-8"), response.data)
        self.assertNotIn("Няма въведено обяснение за този въпрос.".encode("utf-8"), response.data)

    def test_wrong_mc_result_escapes_explanation_and_answer_text(self):
        conn = web_app.quiz_db()
        try:
            self._ensure_question_explanation_column(conn)
            question_id = self._insert_eligible_mc_question(
                conn,
                source_number=906,
                prompt="Въпрос с HTML в обратната връзка.",
            )
            conn.execute(
                "UPDATE questions SET explanation = ? WHERE id = ?",
                ('<script>alert("feedback")</script>', question_id),
            )
            conn.execute(
                """
                UPDATE multiple_choice_options
                SET option_text = ?
                WHERE question_id = ?
                  AND is_correct = 1
                """,
                ('<script>alert("answer")</script>', question_id),
            )
            conn.commit()
        finally:
            conn.close()

        _assignment_id, attempt_id = self._create_attempt(
            [question_id],
            student_name="MC Escaped Feedback",
        )
        conn = web_app.quiz_db()
        try:
            wrong_letter = self._wrong_letter(conn, question_id)
            conn.execute("""
                INSERT INTO quiz_answers (attempt_id, question_id, chosen_letter, is_correct)
                VALUES (?, ?, ?, 0)
            """, (attempt_id, question_id, wrong_letter))
            conn.commit()
        finally:
            conn.close()

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertIn("&lt;script&gt;alert(&#34;feedback&#34;)&lt;/script&gt;", body)
        self.assertIn("&lt;script&gt;alert(&#34;answer&#34;)&lt;/script&gt;", body)
        self.assertNotIn('<script>alert("feedback")</script>', body)
        self.assertNotIn('<script>alert("answer")</script>', body)

    def test_mc_only_post_does_not_write_quiz_text_answers_and_keeps_quiz_answers(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="MC Only Submit",
        )
        unplanned_open_id = None
        conn = web_app.quiz_db()
        try:
            wrong_letter = self._wrong_letter(conn, self.valid_question_id)
            unplanned_open_id = self._insert_eligible_open_question(conn)
            conn.commit()
        finally:
            conn.close()

        response = self.client.post(f"/quiz/attempt/{attempt_id}", data={
            f"q_{self.valid_question_id}": wrong_letter,
            f"open_q_{unplanned_open_id}_1": "клиент",
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._quiz_text_answer_rows(attempt_id), [])

        conn = web_app.quiz_db()
        try:
            answer = conn.execute("""
                SELECT chosen_letter, is_correct
                FROM quiz_answers
                WHERE attempt_id = ?
                  AND question_id = ?
            """, (attempt_id, self.valid_question_id)).fetchone()
            attempt = conn.execute("""
                SELECT score_correct, score_total
                FROM quiz_attempts
                WHERE id = ?
            """, (attempt_id,)).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(answer)
        self.assertEqual(answer["chosen_letter"], wrong_letter)
        self.assertEqual(answer["is_correct"], 0)
        self.assertEqual(attempt["score_correct"], 0)
        self.assertEqual(attempt["score_total"], 1)

    def test_empty_mc_only_submission_is_rejected_without_completing_attempt(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="Empty MC Submit",
        )
        self._clear_attempt_answers(attempt_id)

        response = self.client.post(f"/quiz/attempt/{attempt_id}", data={})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Попълни поне един отговор, преди да предадеш теста.".encode("utf-8"), response.data)
        attempt = self._attempt_row(attempt_id)
        self.assertIsNone(attempt["submitted_at"])
        conn = web_app.quiz_db()
        try:
            answer_count = conn.execute(
                "SELECT COUNT(*) FROM quiz_answers WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(answer_count, 0)

    def test_partial_mc_submission_is_still_allowed(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="Partial MC Submit",
        )
        self._clear_attempt_answers(attempt_id)
        conn = web_app.quiz_db()
        try:
            wrong_letter = self._wrong_letter(conn, self.valid_question_id)
        finally:
            conn.close()

        response = self.client.post(f"/quiz/attempt/{attempt_id}", data={
            f"q_{self.valid_question_id}": wrong_letter,
        })

        self.assertEqual(response.status_code, 302)
        attempt = self._attempt_row(attempt_id)
        self.assertIsNotNone(attempt["submitted_at"])
        self.assertEqual(attempt["score_total"], 1)

    def test_submitting_mixed_open_text_writes_planned_quiz_text_answers(self):
        _assignment_id, attempt_id, open_question_id = self._create_mixed_planned_attempt(
            student_name="Mixed Submit",
        )
        conn = web_app.quiz_db()
        try:
            wrong_letter = self._wrong_letter(conn, self.valid_question_id)
        finally:
            conn.close()

        response = self.client.post(f"/quiz/attempt/{attempt_id}", data={
            f"q_{self.valid_question_id}": wrong_letter,
            f"open_q_{open_question_id}_1": "клиент",
            f"open_q_{open_question_id}_2": "jpg",
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/quiz/attempt/{attempt_id}/result", response.headers["Location"])
        text_answer_rows = self._quiz_text_answer_rows(attempt_id)
        self.assertEqual(len(text_answer_rows), 2)
        self.assertEqual([row["question_id"] for row in text_answer_rows], [open_question_id, open_question_id])
        self.assertEqual([row["subquestion_number"] for row in text_answer_rows], [1, 2])
        self.assertEqual([row["raw_answer"] for row in text_answer_rows], ["клиент", "jpg"])
        self.assertEqual([row["is_correct"] for row in text_answer_rows], [1, 1])

        conn = web_app.quiz_db()
        try:
            text_answer_rows = conn.execute("""
                SELECT COUNT(*)
                FROM quiz_answers
                WHERE attempt_id = ?
                  AND question_id = ?
            """, (attempt_id, open_question_id)).fetchone()[0]
            attempt = conn.execute("""
                SELECT score_total
                FROM quiz_attempts
                WHERE id = ?
            """, (attempt_id,)).fetchone()
        finally:
            conn.close()

        self.assertEqual(text_answer_rows, 0)
        self.assertEqual(attempt["score_total"], 1)

    def test_empty_mixed_open_submission_is_rejected(self):
        _assignment_id, attempt_id, _open_question_id = self._create_mixed_planned_attempt(
            student_name="Empty Mixed Submit",
        )

        response = self.client.post(f"/quiz/attempt/{attempt_id}", data={})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Попълни поне един отговор, преди да предадеш теста.".encode("utf-8"), response.data)
        attempt = self._attempt_row(attempt_id)
        self.assertIsNone(attempt["submitted_at"])
        self.assertEqual(self._quiz_text_answer_rows(attempt_id), [])

    def test_mixed_open_submission_with_only_open_text_is_allowed(self):
        _assignment_id, attempt_id, open_question_id = self._create_mixed_planned_attempt(
            student_name="Open Only Submit",
        )

        response = self.client.post(f"/quiz/attempt/{attempt_id}", data={
            f"open_q_{open_question_id}_1": "клиент",
        })

        self.assertEqual(response.status_code, 302)
        attempt = self._attempt_row(attempt_id)
        self.assertIsNotNone(attempt["submitted_at"])
        rows = self._quiz_text_answer_rows(attempt_id)
        self.assertEqual([row["raw_answer"] for row in rows], ["клиент", ""])

    def test_timer_expired_blank_submission_is_preserved(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="Expired Timer Blank Submit",
            time_limit_minutes=1,
            started_at="2020-01-01 00:00:00",
        )
        self._clear_attempt_answers(attempt_id)

        response = self.client.post(f"/quiz/attempt/{attempt_id}", data={
            "submission_reason": "timer_expired",
        })

        self.assertEqual(response.status_code, 302)
        attempt = self._attempt_row(attempt_id)
        self.assertIsNotNone(attempt["submitted_at"])
        self.assertEqual(attempt["score_correct"], 0)
        self.assertEqual(attempt["score_total"], 1)

    def test_mixed_open_result_shows_recorded_answers_outside_score(self):
        _assignment_id, attempt_id, open_question_id = self._create_mixed_planned_attempt(
            student_name="Mixed Result Review",
        )
        conn = web_app.quiz_db()
        try:
            wrong_letter = self._wrong_letter(conn, self.valid_question_id)
        finally:
            conn.close()

        post_response = self.client.post(f"/quiz/attempt/{attempt_id}", data={
            f"q_{self.valid_question_id}": wrong_letter,
            f"open_q_{open_question_id}_1": "неверен",
            f"open_q_{open_question_id}_2": "jpg",
        })
        self.assertEqual(post_response.status_code, 302)

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Отворени отговори".encode("utf-8"), response.data)
        self.assertIn("неверен".encode("utf-8"), response.data)
        self.assertIn("jpg".encode("utf-8"), response.data)
        self.assertIn("Автоматично съвпадение".encode("utf-8"), response.data)
        self.assertIn("Няма автоматично съвпадение".encode("utf-8"), response.data)
        self.assertIn("Автоматичните точки са само информативни".encode("utf-8"), response.data)
        self.assertIn("не са включени в точния резултат".encode("utf-8"), response.data)
        self.assertIn("Интеграцията на отворените отговори във финалния резултат още не е включена".encode("utf-8"), response.data)
        self.assertIn("1.0/1.0 т.".encode("utf-8"), response.data)
        self.assertIn("0.0/1.0 т.".encode("utf-8"), response.data)
        self.assertIn("Информационен сбор за отворени отговори".encode("utf-8"), response.data)
        self.assertIn("1.0/2.0 т.".encode("utf-8"), response.data)
        self.assertIn("не е включен в точния резултат".encode("utf-8"), response.data)
        self.assertIn("режим: ordered".encode("utf-8"), response.data)
        self.assertIn("Прегледът и оценяването от учител ще бъдат добавени по-късно".encode("utf-8"), response.data)
        self.assertNotIn("Комбиниран резултат".encode("utf-8"), response.data)
        self.assertIn(b'<span class="result-score">0/1</span>', response.data)
        self.assertNotIn(b"accepted_answers_json", response.data)
        self.assertNotIn(b"[&#34;jpeg&#34;, &#34;jpg&#34;]", response.data)
        self.assertIn("Приет отговор".encode("utf-8"), response.data)
        self.assertIn("клиент".encode("utf-8"), response.data)
        self.assertIn("Обяснение".encode("utf-8"), response.data)
        self.assertIn("Няма въведено обяснение за този въпрос.".encode("utf-8"), response.data)

    def test_wrong_open_result_shows_accepted_alternatives(self):
        _assignment_id, attempt_id, open_question_id = self._create_mixed_planned_attempt(
            student_name="Mixed Wrong Alternatives",
        )
        conn = web_app.quiz_db()
        try:
            wrong_letter = self._wrong_letter(conn, self.valid_question_id)
        finally:
            conn.close()

        post_response = self.client.post(f"/quiz/attempt/{attempt_id}", data={
            f"q_{self.valid_question_id}": wrong_letter,
            f"open_q_{open_question_id}_1": "грешно",
            f"open_q_{open_question_id}_2": "png",
        })
        self.assertEqual(post_response.status_code, 302)

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertIn("Приет отговор", body)
        self.assertIn("клиент", body)
        self.assertIn("jpeg, jpg", body)
        self.assertIn("Няма въведено обяснение за този въпрос.", body)
        self.assertNotIn("accepted_answers_json", body)

    def test_wrong_open_result_without_accepted_answer_shows_fallback(self):
        _assignment_id, attempt_id, open_question_id = self._create_mixed_planned_attempt(
            submitted=True,
            student_name="Mixed Missing Accepted",
        )
        conn = web_app.quiz_db()
        try:
            web_app.insert_quiz_text_answer(
                conn,
                attempt_id=attempt_id,
                question_id=open_question_id,
                subquestion_number=9,
                raw_answer="няма съвпадение",
                normalized_answer="няма съвпадение",
                accepted_answers_json="[]",
                is_correct=False,
                points_awarded=0,
                points_possible=1,
            )
            conn.commit()
        finally:
            conn.close()

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Няма въведен приет отговор.".encode("utf-8"), response.data)

    def test_mixed_open_with_score_integration_flag_shows_combined_score(self):
        _assignment_id, attempt_id, open_question_id = self._create_mixed_planned_attempt(
            student_name="Mixed Combined",
            include_open_answers_in_final_score=True,
        )
        conn = web_app.quiz_db()
        try:
            wrong_letter = self._wrong_letter(conn, self.valid_question_id)
        finally:
            conn.close()

        post_response = self.client.post(f"/quiz/attempt/{attempt_id}", data={
            f"q_{self.valid_question_id}": wrong_letter,
            f"open_q_{open_question_id}_1": "неверен",
            f"open_q_{open_question_id}_2": "jpg",
        })
        self.assertEqual(post_response.status_code, 302)

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertIn("MC резултат".encode("utf-8"), response.data)
        self.assertIn(b'<span class="result-score">0/1</span>', response.data)
        self.assertIn("Успеваемост: 0%".encode("utf-8"), response.data)
        self.assertIn("Комбиниран резултат".encode("utf-8"), response.data)
        self.assertIn("1.0/3.0 т.".encode("utf-8"), response.data)
        self.assertIn("MC: 0.0/1.0 ·".encode("utf-8"), response.data)
        self.assertIn("отворени: 1.0/2.0".encode("utf-8"), response.data)
        self.assertIn("1.0/2.0 т.".encode("utf-8"), response.data)
        self.assertIn("включен е в комбинирания резултат".encode("utf-8"), response.data)
        self.assertNotIn(b"accepted_answers_json", response.data)

    def test_admin_teacher_results_show_recorded_open_answers_read_only(self):
        assignment_id, attempt_id, open_question_id = self._create_mixed_planned_attempt(
            student_name="Admin Review",
        )
        conn = web_app.quiz_db()
        try:
            wrong_letter = self._wrong_letter(conn, self.valid_question_id)
        finally:
            conn.close()

        post_response = self.client.post(f"/quiz/attempt/{attempt_id}", data={
            f"q_{self.valid_question_id}": wrong_letter,
            f"open_q_{open_question_id}_1": "неверен",
            f"open_q_{open_question_id}_2": "jpg",
        })
        self.assertEqual(post_response.status_code, 302)

        self._login_admin()
        response = self.client.get(f"/teacher/assignment/{assignment_id}/results")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Отворени отговори".encode("utf-8"), response.data)
        self.assertIn("Admin Review".encode("utf-8"), response.data)
        self.assertIn("неверен".encode("utf-8"), response.data)
        self.assertIn("jpg".encode("utf-8"), response.data)
        self.assertIn("авто-съвпадение".encode("utf-8"), response.data)
        self.assertIn("няма съвпадение".encode("utf-8"), response.data)
        self.assertIn("Инф. точки".encode("utf-8"), response.data)
        self.assertIn("ordered".encode("utf-8"), response.data)
        self.assertIn("не са включени в крайния резултат".encode("utf-8"), response.data)
        self.assertIn("Интеграцията на отворените отговори във финалния резултат още не е включена".encode("utf-8"), response.data)
        self.assertIn("Успеваемост".encode("utf-8"), response.data)
        self.assertIn(">0%</td>".encode("utf-8"), response.data)
        self.assertIn(b"<fieldset disabled", response.data)
        self.assertIn(b"Include open answers in final score", response.data)
        self.assertIn(b'name="include_open_answers_in_final_score_preview"', response.data)
        self.assertNotIn(b'name="include_open_answers_in_final_score"', response.data)
        self.assertIn("Учителският override".encode("utf-8"), response.data)
        self.assertIn(b"<form", response.data)
        self.assertIn(b'name="text_answer_id"', response.data)
        self.assertIn(b'name="teacher_override"', response.data)
        self.assertIn(b'name="teacher_note"', response.data)
        self.assertNotIn(b"teacher_override_points", response.data)
        self.assertIn("MC резултат: 0/1".encode("utf-8"), response.data)
        self.assertIn("Информационен сбор, не е включен в крайния резултат.".encode("utf-8"), response.data)
        self.assertNotIn(b"accepted_answers_json", response.data)
        self.assertNotIn(b"[&#34;jpeg&#34;, &#34;jpg&#34;]", response.data)

        conn = web_app.quiz_db()
        try:
            attempt = conn.execute("""
                SELECT score_correct, score_total
                FROM quiz_attempts
                WHERE id = ?
            """, (attempt_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(attempt["score_correct"], 0)
        self.assertEqual(attempt["score_total"], 1)

    def test_tester_cannot_access_teacher_open_answer_review(self):
        assignment_id, _attempt_id, _open_question_id = self._create_mixed_planned_attempt(
            student_name="Tester Blocked Review",
        )
        self._login_tester()

        response = self.client.get(f"/teacher/assignment/{assignment_id}/results")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_admin_teacher_results_hides_open_answer_section_for_mc_only_attempts(self):
        assignment_id, _attempt_id = self._create_attempt(
            [self.valid_question_id],
            student_name="Admin MC Only",
        )
        self._login_admin()

        response = self.client.get(f"/teacher/assignment/{assignment_id}/results")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Отворени отговори".encode("utf-8"), response.data)
        self.assertNotIn("Инф. точки".encode("utf-8"), response.data)
        self.assertNotIn("Информационен сбор".encode("utf-8"), response.data)
        self.assertNotIn("Include open answers in final score".encode("utf-8"), response.data)
        self.assertNotIn("Интеграцията на отворените отговори".encode("utf-8"), response.data)

    def test_admin_can_save_teacher_open_answer_override_without_score_change(self):
        assignment_id, attempt_id, open_question_id = self._create_mixed_planned_attempt(
            student_name="Save Override",
            include_open_answers_in_final_score=True,
        )
        conn = web_app.quiz_db()
        try:
            wrong_letter = self._wrong_letter(conn, self.valid_question_id)
        finally:
            conn.close()
        self.client.post(f"/quiz/attempt/{attempt_id}", data={
            f"q_{self.valid_question_id}": wrong_letter,
            f"open_q_{open_question_id}_1": "неверен",
            f"open_q_{open_question_id}_2": "jpg",
        })
        text_answer_id = self._quiz_text_answer_rows(attempt_id)[0]["id"]
        self._login_admin()

        response = self.client.post(f"/teacher/assignment/{assignment_id}/results", data={
            "text_answer_id": str(text_answer_id),
            "teacher_override": "1",
            "teacher_note": "Reviewed manually",
        })
        self.assertEqual(response.status_code, 302)

        conn = web_app.quiz_db()
        try:
            row = conn.execute("""
                SELECT teacher_override, teacher_note
                FROM quiz_text_answers
                WHERE id = ?
            """, (text_answer_id,)).fetchone()
            attempt = conn.execute("""
                SELECT score_correct, score_total
                FROM quiz_attempts
                WHERE id = ?
            """, (attempt_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["teacher_override"], 1)
        self.assertEqual(row["teacher_note"], "Reviewed manually")
        self.assertEqual(attempt["score_correct"], 0)
        self.assertEqual(attempt["score_total"], 1)

        response = self.client.get(f"/teacher/assignment/{assignment_id}/results")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Reviewed manually", response.data)
        self.assertIn("Приет от учител".encode("utf-8"), response.data)
        self.assertIn(b"2.0/2.0", response.data)
        self.assertIn("комбиниран резултат".encode("utf-8"), response.data)
        self.assertIn(b"2.0/3.0", response.data)
        self.assertIn("Включен е в комбинирания резултат".encode("utf-8"), response.data)

    def test_tester_cannot_save_teacher_open_answer_override(self):
        assignment_id, attempt_id, open_question_id = self._create_mixed_planned_attempt(
            student_name="Tester Save Blocked",
        )
        conn = web_app.quiz_db()
        try:
            wrong_letter = self._wrong_letter(conn, self.valid_question_id)
        finally:
            conn.close()
        self.client.post(f"/quiz/attempt/{attempt_id}", data={
            f"q_{self.valid_question_id}": wrong_letter,
            f"open_q_{open_question_id}_1": "неверен",
        })
        text_answer_id = self._quiz_text_answer_rows(attempt_id)[0]["id"]
        self._login_tester()

        response = self.client.post(f"/teacher/assignment/{assignment_id}/results", data={
            "text_answer_id": str(text_answer_id),
            "teacher_override": "1",
            "teacher_note": "should not save",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])
        row = self._quiz_text_answer_rows(attempt_id)[0]
        self.assertEqual(row["teacher_override"], 0)
        self.assertIsNone(row["teacher_note"])

    def test_teacher_open_answer_override_rejects_row_outside_assignment(self):
        assignment_id, _attempt_id, _open_question_id = self._create_mixed_planned_attempt(
            student_name="Reject Other Assignment",
        )
        other_assignment_id, other_attempt_id, other_open_question_id = self._create_mixed_planned_attempt(
            student_name="Other Assignment Row",
        )
        conn = web_app.quiz_db()
        try:
            wrong_letter = self._wrong_letter(conn, self.valid_question_id)
        finally:
            conn.close()
        self.client.post(f"/quiz/attempt/{other_attempt_id}", data={
            f"q_{self.valid_question_id}": wrong_letter,
            f"open_q_{other_open_question_id}_1": "неверен",
        })
        other_text_answer_id = self._quiz_text_answer_rows(other_attempt_id)[0]["id"]
        self._login_admin()

        response = self.client.post(f"/teacher/assignment/{assignment_id}/results", data={
            "text_answer_id": str(other_text_answer_id),
            "teacher_override": "1",
            "teacher_note": "wrong assignment",
        })
        self.assertEqual(response.status_code, 404)
        row = self._quiz_text_answer_rows(other_attempt_id)[0]
        self.assertEqual(row["teacher_override"], 0)
        self.assertIsNone(row["teacher_note"])

        response = self.client.post(f"/teacher/assignment/{other_assignment_id}/results", data={
            "text_answer_id": str(other_text_answer_id),
            "teacher_override": "maybe",
            "teacher_note": "invalid override",
        })
        self.assertEqual(response.status_code, 400)

    def test_unexpected_open_text_for_unplanned_question_is_ignored(self):
        _assignment_id, attempt_id, planned_open_id = self._create_mixed_planned_attempt(
            student_name="Mixed Ignore Unexpected",
        )
        conn = web_app.quiz_db()
        try:
            wrong_letter = self._wrong_letter(conn, self.valid_question_id)
            unplanned_open_id = self._insert_eligible_open_question(conn)
            conn.commit()
        finally:
            conn.close()

        response = self.client.post(f"/quiz/attempt/{attempt_id}", data={
            f"q_{self.valid_question_id}": wrong_letter,
            f"open_q_{planned_open_id}_1": "клиент",
            f"open_q_{unplanned_open_id}_1": "unexpected",
        })

        self.assertEqual(response.status_code, 302)
        rows = self._quiz_text_answer_rows(attempt_id)
        self.assertEqual({row["question_id"] for row in rows}, {planned_open_id})
        self.assertEqual([row["subquestion_number"] for row in rows], [1, 2])
        self.assertEqual([row["raw_answer"] for row in rows], ["клиент", ""])

    def test_all_invalid_active_attempt_shows_stale_message(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.invalid_question_id],
            submitted=False,
            student_name="Invalid Active",
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(STALE_MESSAGE.encode("utf-8"), response.data)

    def test_stored_question_ids_json_is_unchanged(self):
        original_ids = [self.valid_question_id, self.invalid_question_id]
        _assignment_id, attempt_id = self._create_attempt(original_ids, student_name="Unchanged JSON")

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)

        conn = web_app.quiz_db()
        try:
            stored = conn.execute(
                "SELECT question_ids_json FROM quiz_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()["question_ids_json"]
        finally:
            conn.close()
        self.assertEqual(json.loads(stored), original_ids)

    # ------------------------------------------------------------------
    # XSS regression: open-answer raw_answer + teacher_note must be escaped
    # wherever they're rendered. Stored verbatim, escaped at render time.
    # ------------------------------------------------------------------

    _OPEN_SCRIPT_PAYLOAD = '<script>alert("open")</script>'
    _OPEN_SCRIPT_ESCAPED = "&lt;script&gt;alert(&#34;open&#34;)&lt;/script&gt;"
    _TEACHER_NOTE_PAYLOAD = '<script>alert("note")</script>'
    _TEACHER_NOTE_ESCAPED = "&lt;script&gt;alert(&#34;note&#34;)&lt;/script&gt;"

    def _submit_open_answer(self, attempt_id, open_question_id, raw_text):
        conn = web_app.quiz_db()
        try:
            wrong_letter = self._wrong_letter(conn, self.valid_question_id)
        finally:
            conn.close()
        response = self.client.post(f"/quiz/attempt/{attempt_id}", data={
            f"q_{self.valid_question_id}": wrong_letter,
            f"open_q_{open_question_id}_1": raw_text,
            f"open_q_{open_question_id}_2": "jpg",
        })
        self.assertEqual(response.status_code, 302)

    def test_open_answer_raw_text_with_script_is_escaped_on_quiz_result(self):
        _assignment_id, attempt_id, open_question_id = self._create_mixed_planned_attempt(
            student_name="Result Page XSS",
        )
        self._submit_open_answer(attempt_id, open_question_id, self._OPEN_SCRIPT_PAYLOAD)

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        # Stored verbatim in DB...
        rows = self._quiz_text_answer_rows(attempt_id)
        self.assertEqual(rows[0]["raw_answer"], self._OPEN_SCRIPT_PAYLOAD)
        # ...but escaped at render time.
        self.assertIn(self._OPEN_SCRIPT_ESCAPED, body)
        self.assertNotIn(self._OPEN_SCRIPT_PAYLOAD, body)

    def test_open_answer_raw_text_with_script_is_escaped_on_teacher_results(self):
        assignment_id, attempt_id, open_question_id = self._create_mixed_planned_attempt(
            student_name="Teacher Page XSS",
        )
        self._submit_open_answer(attempt_id, open_question_id, self._OPEN_SCRIPT_PAYLOAD)
        self._login_admin()

        response = self.client.get(f"/teacher/assignment/{assignment_id}/results")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertIn(self._OPEN_SCRIPT_ESCAPED, body)
        self.assertNotIn(self._OPEN_SCRIPT_PAYLOAD, body)

    def test_teacher_note_with_script_is_escaped_on_teacher_results(self):
        assignment_id, attempt_id, open_question_id = self._create_mixed_planned_attempt(
            student_name="Note XSS",
        )
        self._submit_open_answer(attempt_id, open_question_id, "клиент")
        text_answer_id = self._quiz_text_answer_rows(attempt_id)[0]["id"]
        self._login_admin()

        save = self.client.post(f"/teacher/assignment/{assignment_id}/results", data={
            "text_answer_id": str(text_answer_id),
            "teacher_override": "1",
            "teacher_note": self._TEACHER_NOTE_PAYLOAD,
        })
        self.assertEqual(save.status_code, 302)

        # Stored verbatim in DB...
        stored_note = self._quiz_text_answer_rows(attempt_id)[0]["teacher_note"]
        self.assertEqual(stored_note, self._TEACHER_NOTE_PAYLOAD)

        # ...but escaped at render time. Note rendering happens inside a
        # <textarea> so the escaped form is what must appear.
        response = self.client.get(f"/teacher/assignment/{assignment_id}/results")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertIn(self._TEACHER_NOTE_ESCAPED, body)
        self.assertNotIn(self._TEACHER_NOTE_PAYLOAD, body)

    # ------------------------------------------------------------------
    # Client-side draft autosave / restore (template-level coverage)
    # ------------------------------------------------------------------

    def test_active_attempt_page_includes_autosave_script(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="Autosave Active",
        )
        response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertIn('src="/static/js/quiz-draft-autosave.js"', body)
        # Form has the attempt-specific marker that drives the storage key.
        self.assertIn(f'data-attempt-id="{attempt_id}"', body)
        # Hidden status target for the "Черновата е запазена" message.
        self.assertIn('id="draft-status"', body)

    def test_active_attempt_inputs_have_stable_names_for_autosave(self):
        _assignment_id, attempt_id, open_question_id = self._create_mixed_planned_attempt(
            student_name="Autosave Names",
        )
        response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        # MC inputs are named q_<question_id>; open inputs are
        # open_q_<question_id>_<subquestion_number>. The autosave JS keys on
        # these exact prefixes — locking them in protects the contract.
        self.assertRegex(body, r'name="q_\d+"')
        self.assertIn(f'name="open_q_{open_question_id}_1"', body)
        self.assertIn(f'name="open_q_{open_question_id}_2"', body)

    def test_active_autosave_script_is_not_served_on_result_page(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            student_name="Autosave Result",
        )
        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertNotIn("quiz-draft-autosave.js", body)

    def test_result_page_does_not_include_active_autosave_wiring(self):
        # The active-page autosave script restores drafts; the result page
        # must not run it, so an answered/submitted attempt does not get
        # answers re-populated from localStorage.
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            student_name="Autosave Result Clean",
        )
        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertNotIn("quiz-draft-autosave.js", body)
        self.assertNotIn('data-attempt-id=', body)
        self.assertNotIn('id="quiz-form"', body)

    def test_stale_attempt_page_does_not_include_autosave_script(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.invalid_question_id],
            submitted=False,
            student_name="Autosave Stale",
        )
        response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        # Stale-attempt branch renders only the warning, no form / no script.
        self.assertIn(STALE_MESSAGE, body)
        self.assertNotIn("quiz-draft-autosave.js", body)
        self.assertNotIn('id="quiz-form"', body)

    def test_autosave_static_asset_is_served_and_uses_attempt_key(self):
        asset = self.client.get("/static/js/quiz-draft-autosave.js")
        self.assertEqual(asset.status_code, 200)
        body = asset.get_data(as_text=True)
        # The key is attempt-scoped: drafts cannot leak across attempts.
        self.assertIn('"learnpilot:quiz-draft:" + attemptId', body)
        # Restore looks for q_/open_q_ names; malformed JSON is guarded.
        self.assertIn('JSON.parse', body)
        self.assertIn('open_q_', body)
        self.assertIn('q_', body)

    # ------------------------------------------------------------------
    # Live progress indicator (template-level coverage)
    # ------------------------------------------------------------------

    def test_active_attempt_page_renders_progress_container(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="Progress Active",
        )
        response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertIn('id="quiz-progress"', body)
        self.assertIn('data-total="1"', body)
        self.assertIn("Отговорени:", body)
        self.assertIn('id="quiz-progress-count">0</span>', body)
        self.assertIn('id="quiz-progress-total">1</span>', body)
        self.assertIn('id="quiz-progress-bar"', body)
        self.assertIn('max="1"', body)
        self.assertIn('value="0"', body)
        self.assertIn('src="/static/js/quiz-progress-indicator.js"', body)

    def test_active_question_cards_carry_progress_data_attributes(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            submitted=False,
            student_name="Progress Attributes",
        )
        response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        # The progress JS counts answered cards by these attributes.
        self.assertRegex(body, r'data-question-id="\d+"')
        self.assertIn('data-question-type="multiple_choice"', body)

    def test_mixed_attempt_progress_total_includes_open_questions(self):
        _assignment_id, attempt_id, open_question_id = self._create_mixed_planned_attempt(
            student_name="Progress Mixed",
        )
        response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        # Mixed attempt has 1 MC + 1 open question → total = 2.
        self.assertIn('data-total="2"', body)
        self.assertIn('id="quiz-progress-total">2</span>', body)
        self.assertIn('max="2"', body)
        # Both question types are marked so the script can count both kinds.
        self.assertIn('data-question-type="multiple_choice"', body)
        self.assertIn('data-question-type="fill_in"', body)
        self.assertIn(f'data-question-id="{open_question_id}"', body)

    def test_result_page_does_not_include_progress_indicator(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            student_name="Progress Result",
        )
        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertNotIn("quiz-progress-indicator.js", body)
        self.assertNotIn('id="quiz-progress"', body)
        self.assertNotIn('id="quiz-progress-bar"', body)

    def test_stale_attempt_page_does_not_include_progress_indicator(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.invalid_question_id],
            submitted=False,
            student_name="Progress Stale",
        )
        response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertIn(STALE_MESSAGE, body)
        self.assertNotIn("quiz-progress-indicator.js", body)
        self.assertNotIn('id="quiz-progress"', body)

    def test_progress_static_asset_is_served_and_counts_both_types(self):
        asset = self.client.get("/static/js/quiz-progress-indicator.js")
        self.assertEqual(asset.status_code, 200)
        body = asset.get_data(as_text=True)
        # The progress script reads cards via data-question-id and decides
        # answered-ness using the data-question-type marker.
        self.assertIn("data-question-id", body)
        self.assertIn("data-question-type", body)
        self.assertIn("multiple_choice", body)
        self.assertIn('input[type="radio"]:checked', body)
        # Open-answer detection: any non-blank open_q_ field counts.
        self.assertIn('open_q_', body)


if __name__ == "__main__":
    unittest.main()

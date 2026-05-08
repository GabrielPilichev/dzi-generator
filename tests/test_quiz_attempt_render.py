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
    def _insert_eligible_mc_question(conn, *, source_number, topic_id, prompt):
        cur = conn.execute("""
            INSERT INTO questions (
                source_exam, source_number, question_type, topic, topic_id,
                difficulty, points, prompt, has_image, image_path,
                is_ai_generated, quality_score
            )
            VALUES (?, ?, 'multiple_choice', 'visual-filter-test', ?, 'medium', 1, ?, 0, NULL, 0, NULL)
        """, (
            "temp-visual-filter-test",
            source_number,
            topic_id,
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
    def _insert_eligible_open_question(conn, *, source_exam=None, source_number=16):
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
            VALUES (?, ?, 'fill_in', 'test', 'medium', 1, ?, 0, 0, NULL)
        """, (
            source_exam,
            source_number,
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

    def _create_assignment(self):
        conn = web_app.quiz_db()
        try:
            cur = conn.execute("""
                INSERT INTO quiz_assignments (section_id, title_bg, question_count, time_limit_minutes)
                VALUES (?, ?, 2, NULL)
            """, (self.section["id"], self.section["title_bg"]))
            assignment_id = int(cur.lastrowid)
            conn.commit()
            return assignment_id
        finally:
            conn.close()

    def _create_attempt(self, question_ids, *, submitted=True, student_name="Stale Student"):
        assignment_id = self._create_assignment()
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

    def _create_mixed_planned_attempt(self, *, submitted=False, student_name="Mixed Open Render"):
        assignment_id = self._create_assignment()
        conn = web_app.quiz_db()
        try:
            open_question_id = self._insert_eligible_open_question(conn)
            question_plan = {
                "mixed_open_enabled": True,
                "question_ids": [self.valid_question_id, open_question_id],
                "open_question_ids": [open_question_id],
            }
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
    def _quiz_text_answer_rows(attempt_id):
        conn = web_app.quiz_db()
        try:
            return conn.execute("""
                SELECT question_id, subquestion_number, raw_answer, normalized_answer, is_correct
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

    def test_mixed_planned_attempt_renders_open_text_inputs(self):
        _assignment_id, attempt_id, open_question_id = self._create_mixed_planned_attempt()

        response = self.client.get(f"/quiz/attempt/{attempt_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.valid_prompt.encode("utf-8"), response.data)
        self.assertIn("Попълнете липсващите стойности.".encode("utf-8"), response.data)
        self.assertIn(f'name="open_q_{open_question_id}_1"'.encode("utf-8"), response.data)
        self.assertIn(f'name="open_q_{open_question_id}_2"'.encode("utf-8"), response.data)
        self.assertIn("няма да бъдат включени в точния резултат".encode("utf-8"), response.data)

    def test_mc_only_result_does_not_show_open_answer_section(self):
        _assignment_id, attempt_id = self._create_attempt(
            [self.valid_question_id],
            student_name="MC Only Result",
        )

        response = self.client.get(f"/quiz/attempt/{attempt_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Отворени отговори".encode("utf-8"), response.data)
        self.assertNotIn("не са включени в точния резултат".encode("utf-8"), response.data)

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
        self.assertIn("1.0/1.0 т.".encode("utf-8"), response.data)
        self.assertIn("0.0/1.0 т.".encode("utf-8"), response.data)
        self.assertIn("режим: ordered".encode("utf-8"), response.data)
        self.assertIn("Прегледът и оценяването от учител ще бъдат добавени по-късно".encode("utf-8"), response.data)
        self.assertIn(b'<span class="result-score">0/1</span>', response.data)
        self.assertNotIn(b"accepted_answers_json", response.data)
        self.assertNotIn(b"[&#34;jpeg&#34;, &#34;jpg&#34;]", response.data)

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
        self.assertIn("само за четене".encode("utf-8"), response.data)
        self.assertIn("не са включени в крайния резултат".encode("utf-8"), response.data)
        self.assertIn("override не са активни още".encode("utf-8"), response.data)
        self.assertIn(b"<fieldset disabled", response.data)
        self.assertIn(b"Teacher override coming soon", response.data)
        self.assertIn(b'name="teacher_override_status_preview"', response.data)
        self.assertIn(b'name="teacher_override_points_preview"', response.data)
        self.assertIn(b'name="teacher_note_preview"', response.data)
        self.assertNotIn(b"<form", response.data)
        self.assertIn("MC резултат: 0/1".encode("utf-8"), response.data)
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

    def test_teacher_open_answer_review_has_no_post_edit_behavior(self):
        assignment_id, attempt_id, _open_question_id = self._create_mixed_planned_attempt(
            student_name="No Edit Review",
        )
        self._login_admin()

        response = self.client.post(f"/teacher/assignment/{assignment_id}/results", data={
            "teacher_override": "1",
            "teacher_override_status_preview": "Приеми като верен",
            "teacher_override_points_preview": "1",
            "teacher_note_preview": "manual note",
        })
        self.assertEqual(response.status_code, 405)

        conn = web_app.quiz_db()
        try:
            count = conn.execute("""
                SELECT COUNT(*)
                FROM quiz_text_answers
                WHERE attempt_id = ?
                  AND (teacher_override != 0 OR teacher_note IS NOT NULL)
            """, (attempt_id,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

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


if __name__ == "__main__":
    unittest.main()

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

    def setUp(self):
        self.client = self.app.test_client()

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

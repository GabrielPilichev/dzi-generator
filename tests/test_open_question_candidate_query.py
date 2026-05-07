import atexit
import os
import shutil
import sqlite3
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

from web.app import fetch_open_question_candidates  # noqa: E402


class OpenQuestionCandidateQueryTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE questions (
                id INTEGER PRIMARY KEY,
                source_exam TEXT NOT NULL,
                source_number INTEGER,
                question_type TEXT NOT NULL,
                prompt TEXT NOT NULL,
                has_image INTEGER DEFAULT 0,
                image_path TEXT
            );
            CREATE TABLE fill_in_subquestions (
                id INTEGER PRIMARY KEY,
                question_id INTEGER NOT NULL,
                subquestion_number INTEGER NOT NULL,
                correct_answer TEXT NOT NULL,
                answer_alternatives TEXT
            );
        """)
        self.insert_question(1, "source_a", 1, "multiple_choice", "MC question")
        self.insert_question(2, "source_a", 2, "fill_in", "Ordered fill-in")
        self.insert_subquestion(2, 1, "300")
        self.insert_subquestion(2, 2, '["540", "540 лв."]')
        self.insert_question(3, "source_a", 3, "fill_in", "Order-independent fill-in")
        accepted = '["клиент", "рецепционист"]'
        self.insert_subquestion(3, 1, accepted)
        self.insert_subquestion(3, 2, accepted)
        self.insert_question(4, "source_a", 4, "fill_in", "Missing accepted answer")
        self.insert_subquestion(4, 1, "")
        self.insert_question(5, "source_a", 5, "fill_in", "Според показаната диаграма попълнете стойността.")
        self.insert_subquestion(5, 1, "42")
        self.insert_question(6, "source_a", 26, "fill_in", "Practical task")
        self.insert_subquestion(6, 1, "42")
        self.insert_question(7, "source_b", 1, "fill_in", "Other source")
        self.insert_subquestion(7, 1, "да")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def insert_question(self, question_id, source_exam, source_number, question_type, prompt):
        self.conn.execute("""
            INSERT INTO questions (
                id, source_exam, source_number, question_type, prompt, has_image, image_path
            )
            VALUES (?, ?, ?, ?, ?, 0, NULL)
        """, (question_id, source_exam, source_number, question_type, prompt))

    def insert_subquestion(self, question_id, subquestion_number, correct_answer):
        self.conn.execute("""
            INSERT INTO fill_in_subquestions (
                question_id, subquestion_number, correct_answer, answer_alternatives
            )
            VALUES (?, ?, ?, NULL)
        """, (question_id, subquestion_number, correct_answer))

    def test_mc_is_excluded_and_eligible_fill_in_is_included(self):
        candidates = fetch_open_question_candidates(self.conn, source_slug="source_a")
        ids = [candidate["question_id"] for candidate in candidates]

        self.assertNotIn(1, ids)
        self.assertIn(2, ids)

    def test_ordered_fill_in_candidate_shape(self):
        candidates = fetch_open_question_candidates(self.conn, source_slug="source_a")
        ordered = next(candidate for candidate in candidates if candidate["question_id"] == 2)

        self.assertEqual(ordered["source_slug"], "source_a")
        self.assertEqual(ordered["task_number"], 2)
        self.assertEqual(ordered["grading_mode"], "ordered")
        self.assertEqual(ordered["subquestion_count"], 2)

    def test_order_independent_fill_in_is_included(self):
        candidates = fetch_open_question_candidates(self.conn, source_slug="source_a")
        candidate = next(candidate for candidate in candidates if candidate["question_id"] == 3)

        self.assertEqual(candidate["grading_mode"], "order_independent")
        self.assertEqual(candidate["subquestion_count"], 2)

    def test_missing_accepted_answer_is_excluded(self):
        ids = [candidate["question_id"] for candidate in fetch_open_question_candidates(self.conn, source_slug="source_a")]
        self.assertNotIn(4, ids)

    def test_visual_dependent_fill_in_is_excluded(self):
        ids = [candidate["question_id"] for candidate in fetch_open_question_candidates(self.conn, source_slug="source_a")]
        self.assertNotIn(5, ids)

    def test_practical_task_26_is_excluded(self):
        ids = [candidate["question_id"] for candidate in fetch_open_question_candidates(self.conn, source_slug="source_a")]
        self.assertNotIn(6, ids)

    def test_source_slug_filter(self):
        candidates = fetch_open_question_candidates(self.conn, source_slug="source_b")
        self.assertEqual([candidate["question_id"] for candidate in candidates], [7])

    def test_limit(self):
        candidates = fetch_open_question_candidates(self.conn, limit=1)
        self.assertEqual(len(candidates), 1)

    def test_fetch_does_not_write(self):
        before = self.conn.total_changes
        fetch_open_question_candidates(self.conn)
        after = self.conn.total_changes

        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()

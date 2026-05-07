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

from web.app import fetch_quiz_text_answers_for_attempt  # noqa: E402


MIGRATION_SQL = (_ROOT / "web" / "migrations" / "005_quiz_text_answers.sql").read_text(encoding="utf-8")


class QuizTextAnswerQueryTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript("""
            CREATE TABLE quiz_attempts (id INTEGER PRIMARY KEY);
            CREATE TABLE questions (id INTEGER PRIMARY KEY);
            CREATE TABLE fill_in_subquestions (id INTEGER PRIMARY KEY);
        """)
        self.conn.executescript(MIGRATION_SQL)
        self.conn.executemany("INSERT INTO quiz_attempts (id) VALUES (?)", [(1,), (2,)])
        self.conn.executemany("INSERT INTO questions (id) VALUES (?)", [(10,), (20,)])
        self.conn.executemany("INSERT INTO fill_in_subquestions (id) VALUES (?)", [(100,), (200,), (300,)])
        self.conn.executescript("""
            INSERT INTO quiz_text_answers (
                id, attempt_id, question_id, subquestion_id, subquestion_number,
                raw_answer, normalized_answer, grading_mode, accepted_answers_json,
                matched_answer, is_correct, points_awarded, points_possible,
                grader_version, teacher_override, teacher_note
            )
            VALUES
                (1, 1, 20, 200, 2, 'B raw', 'b raw', 'ordered', '["b"]', NULL, 0, 0, 1, 'v1', 0, NULL),
                (2, 1, 10, 100, 1, 'A raw', 'a raw', 'ordered', '["a"]', 'a', 1, 1, 1, 'v1', 1, 'reviewed'),
                (3, 1, 20, 300, 1, 'C raw', 'c raw', 'order_independent', '["c"]', 'c', 1, 1, 1, 'v2', 0, NULL),
                (4, 2, 10, 100, 1, 'Other', 'other', 'ordered', '["other"]', 'other', 1, 1, 1, NULL, 0, NULL);
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_fetch_by_attempt(self):
        rows = fetch_quiz_text_answers_for_attempt(self.conn, 1)

        self.assertEqual(len(rows), 3)
        self.assertIsInstance(rows[0], dict)
        self.assertEqual(rows[0]["id"], 2)
        self.assertEqual(rows[0]["attempt_id"], 1)
        self.assertEqual(rows[0]["question_id"], 10)
        self.assertEqual(rows[0]["subquestion_id"], 100)
        self.assertEqual(rows[0]["subquestion_number"], 1)
        self.assertEqual(rows[0]["raw_answer"], "A raw")
        self.assertEqual(rows[0]["normalized_answer"], "a raw")
        self.assertEqual(rows[0]["grading_mode"], "ordered")
        self.assertEqual(rows[0]["accepted_answers_json"], '["a"]')
        self.assertEqual(rows[0]["matched_answer"], "a")
        self.assertEqual(rows[0]["is_correct"], 1)
        self.assertEqual(rows[0]["points_awarded"], 1)
        self.assertEqual(rows[0]["points_possible"], 1)
        self.assertEqual(rows[0]["grader_version"], "v1")
        self.assertEqual(rows[0]["teacher_override"], 1)
        self.assertEqual(rows[0]["teacher_note"], "reviewed")

    def test_optional_question_filter(self):
        rows = fetch_quiz_text_answers_for_attempt(self.conn, 1, question_id=20)

        self.assertEqual([row["id"] for row in rows], [3, 1])
        self.assertTrue(all(row["question_id"] == 20 for row in rows))

    def test_ordering_by_question_id_then_subquestion_number(self):
        rows = fetch_quiz_text_answers_for_attempt(self.conn, 1)

        self.assertEqual(
            [(row["question_id"], row["subquestion_number"]) for row in rows],
            [(10, 1), (20, 1), (20, 2)],
        )

    def test_empty_attempt_returns_empty_list(self):
        self.assertEqual(fetch_quiz_text_answers_for_attempt(self.conn, 999), [])

    def test_fetch_does_not_write(self):
        before = self.conn.total_changes
        fetch_quiz_text_answers_for_attempt(self.conn, 1)
        after = self.conn.total_changes

        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()

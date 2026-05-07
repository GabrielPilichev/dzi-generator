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

from web.app import insert_quiz_text_answer  # noqa: E402


MIGRATION_SQL = (_ROOT / "web" / "migrations" / "005_quiz_text_answers.sql").read_text(encoding="utf-8")


class QuizTextAnswerInsertTest(unittest.TestCase):
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
        self.conn.execute("INSERT INTO quiz_attempts (id) VALUES (1)")
        self.conn.execute("INSERT INTO questions (id) VALUES (2)")
        self.conn.execute("INSERT INTO fill_in_subquestions (id) VALUES (3)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def fetch_text_answer(self, row_id):
        return self.conn.execute(
            "SELECT * FROM quiz_text_answers WHERE id = ?",
            (row_id,),
        ).fetchone()

    def test_insert_returns_id_and_stores_values(self):
        row_id = insert_quiz_text_answer(
            self.conn,
            attempt_id=1,
            question_id=2,
            subquestion_id=3,
            subquestion_number=1,
            response_order=1,
            raw_answer=" Да ",
            normalized_answer="да",
            grading_mode="order_independent",
            accepted_answers_json='["да"]',
            matched_answer="да",
            is_correct=True,
            points_awarded=1,
            points_possible=1,
            grader_version="v1",
        )

        self.assertIsInstance(row_id, int)
        row = self.fetch_text_answer(row_id)
        self.assertEqual(row["attempt_id"], 1)
        self.assertEqual(row["question_id"], 2)
        self.assertEqual(row["subquestion_id"], 3)
        self.assertEqual(row["subquestion_number"], 1)
        self.assertEqual(row["response_order"], 1)
        self.assertEqual(row["raw_answer"], " Да ")
        self.assertEqual(row["normalized_answer"], "да")
        self.assertEqual(row["grading_mode"], "order_independent")
        self.assertEqual(row["accepted_answers_json"], '["да"]')
        self.assertEqual(row["matched_answer"], "да")
        self.assertEqual(row["is_correct"], 1)
        self.assertEqual(row["points_awarded"], 1)
        self.assertEqual(row["points_possible"], 1)
        self.assertEqual(row["grader_version"], "v1")

    def test_none_raw_answer_becomes_empty_string(self):
        row_id = insert_quiz_text_answer(
            self.conn,
            attempt_id=1,
            question_id=2,
            subquestion_number=1,
            raw_answer=None,
            normalized_answer="",
        )

        row = self.fetch_text_answer(row_id)
        self.assertEqual(row["raw_answer"], "")

    def test_is_correct_boolean_is_stored_as_integer(self):
        true_id = insert_quiz_text_answer(
            self.conn,
            attempt_id=1,
            question_id=2,
            subquestion_number=1,
            raw_answer="x",
            normalized_answer="x",
            is_correct=True,
        )
        false_id = insert_quiz_text_answer(
            self.conn,
            attempt_id=1,
            question_id=2,
            subquestion_number=2,
            raw_answer="y",
            normalized_answer="y",
            is_correct=False,
        )

        self.assertEqual(self.fetch_text_answer(true_id)["is_correct"], 1)
        self.assertEqual(self.fetch_text_answer(false_id)["is_correct"], 0)

    def test_invalid_grading_mode_raises_before_insert(self):
        with self.assertRaises(ValueError):
            insert_quiz_text_answer(
                self.conn,
                attempt_id=1,
                question_id=2,
                subquestion_number=1,
                raw_answer="x",
                normalized_answer="x",
                grading_mode="regex",
            )

        count = self.conn.execute("SELECT COUNT(*) FROM quiz_text_answers").fetchone()[0]
        self.assertEqual(count, 0)

    def test_foreign_key_constraints_work(self):
        with self.assertRaises(sqlite3.IntegrityError):
            insert_quiz_text_answer(
                self.conn,
                attempt_id=999,
                question_id=2,
                subquestion_id=3,
                subquestion_number=1,
                raw_answer="x",
                normalized_answer="x",
            )


if __name__ == "__main__":
    unittest.main()

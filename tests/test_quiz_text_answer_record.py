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

from web.app import record_quiz_text_answers  # noqa: E402


MIGRATION_SQL = (_ROOT / "web" / "migrations" / "005_quiz_text_answers.sql").read_text(encoding="utf-8")


class QuizTextAnswerRecordTest(unittest.TestCase):
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
        self.conn.execute("INSERT INTO fill_in_subquestions (id) VALUES (4)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def rows(self):
        return self.conn.execute("""
            SELECT *
            FROM quiz_text_answers
            ORDER BY subquestion_number
        """).fetchall()

    def test_ordered_answers_record_correct_rows(self):
        results = record_quiz_text_answers(
            self.conn,
            attempt_id=1,
            question_id=2,
            submitted_answers={1: " Да ", 2: "не"},
            accepted_answers_by_slot={1: ["да"], 2: ["да"]},
        )

        self.assertEqual(len(results), 2)
        self.assertTrue(all("id" in result for result in results))
        rows = self.rows()
        self.assertEqual(rows[0]["raw_answer"], " Да ")
        self.assertEqual(rows[0]["normalized_answer"], "да")
        self.assertEqual(rows[0]["accepted_answers_json"], '["да"]')
        self.assertEqual(rows[0]["matched_answer"], "да")
        self.assertEqual(rows[0]["is_correct"], 1)
        self.assertEqual(rows[0]["points_awarded"], 1)
        self.assertEqual(rows[0]["points_possible"], 1)
        self.assertEqual(rows[1]["raw_answer"], "не")
        self.assertEqual(rows[1]["is_correct"], 0)

    def test_order_independent_duplicate_submitted_answer_gets_credit_once(self):
        results = record_quiz_text_answers(
            self.conn,
            attempt_id=1,
            question_id=2,
            submitted_answers={1: "клиент", 2: "клиент"},
            accepted_answers_by_slot={1: ["клиент"], 2: ["рецепционист"]},
            grading_mode="order_independent",
        )

        self.assertEqual([result["is_correct"] for result in results], [True, False])
        rows = self.rows()
        self.assertEqual([row["is_correct"] for row in rows], [1, 0])

    def test_subquestion_id_is_stored_when_provided(self):
        record_quiz_text_answers(
            self.conn,
            attempt_id=1,
            question_id=2,
            submitted_answers={1: "да", 2: "не"},
            accepted_answers_by_slot={1: ["да"], 2: ["не"]},
            subquestion_ids_by_slot={1: 3, 2: 4},
        )

        rows = self.rows()
        self.assertEqual([row["subquestion_id"] for row in rows], [3, 4])

    def test_grader_version_is_stored(self):
        record_quiz_text_answers(
            self.conn,
            attempt_id=1,
            question_id=2,
            submitted_answers={1: "да"},
            accepted_answers_by_slot={1: ["да"]},
            grader_version="text-v1",
        )

        row = self.rows()[0]
        self.assertEqual(row["grader_version"], "text-v1")

    def test_invalid_grading_mode_raises_and_inserts_no_rows(self):
        with self.assertRaises(ValueError):
            record_quiz_text_answers(
                self.conn,
                attempt_id=1,
                question_id=2,
                submitted_answers={1: "да"},
                accepted_answers_by_slot={1: ["да"]},
                grading_mode="regex",
            )

        count = self.conn.execute("SELECT COUNT(*) FROM quiz_text_answers").fetchone()[0]
        self.assertEqual(count, 0)

    def test_foreign_key_constraints_still_work(self):
        with self.assertRaises(sqlite3.IntegrityError):
            record_quiz_text_answers(
                self.conn,
                attempt_id=999,
                question_id=2,
                submitted_answers={1: "да"},
                accepted_answers_by_slot={1: ["да"]},
                subquestion_ids_by_slot={1: 3},
            )


if __name__ == "__main__":
    unittest.main()

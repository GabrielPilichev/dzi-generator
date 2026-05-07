import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "web" / "migrations" / "005_quiz_text_answers.sql"


class QuizTextAnswersMigrationSqlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")

    def assertSqlContains(self, text):
        self.assertIn(text, self.sql)

    def test_creates_quiz_text_answers_table(self):
        self.assertSqlContains("CREATE TABLE IF NOT EXISTS quiz_text_answers")
        self.assertSqlContains("id INTEGER PRIMARY KEY AUTOINCREMENT")
        self.assertSqlContains("attempt_id INTEGER NOT NULL")
        self.assertSqlContains("question_id INTEGER NOT NULL")
        self.assertSqlContains("subquestion_id INTEGER")
        self.assertSqlContains("subquestion_number INTEGER NOT NULL")

    def test_contains_answer_storage_columns(self):
        self.assertSqlContains("raw_answer TEXT NOT NULL DEFAULT ''")
        self.assertSqlContains("normalized_answer TEXT NOT NULL DEFAULT ''")
        self.assertSqlContains("accepted_answers_json TEXT NOT NULL DEFAULT '[]'")
        self.assertSqlContains("matched_answer TEXT")
        self.assertSqlContains("points_awarded REAL NOT NULL DEFAULT 0")
        self.assertSqlContains("points_possible REAL NOT NULL DEFAULT 1")
        self.assertSqlContains("teacher_note TEXT")

    def test_contains_checks_and_unique_constraint(self):
        self.assertSqlContains("CHECK (grading_mode IN ('ordered', 'order_independent'))")
        self.assertSqlContains("is_correct INTEGER NOT NULL DEFAULT 0 CHECK (is_correct IN (0, 1))")
        self.assertSqlContains("teacher_override INTEGER NOT NULL DEFAULT 0 CHECK (teacher_override IN (0, 1))")
        self.assertSqlContains("UNIQUE (attempt_id, question_id, subquestion_number)")

    def test_contains_foreign_keys(self):
        self.assertSqlContains("FOREIGN KEY (attempt_id) REFERENCES quiz_attempts(id) ON DELETE CASCADE")
        self.assertSqlContains("FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE")
        self.assertSqlContains("FOREIGN KEY (subquestion_id) REFERENCES fill_in_subquestions(id) ON DELETE SET NULL")

    def test_contains_indexes(self):
        self.assertSqlContains("idx_quiz_text_answers_attempt")
        self.assertSqlContains("ON quiz_text_answers(attempt_id)")
        self.assertSqlContains("idx_quiz_text_answers_question")
        self.assertSqlContains("ON quiz_text_answers(question_id)")
        self.assertSqlContains("idx_quiz_text_answers_attempt_question")
        self.assertSqlContains("ON quiz_text_answers(attempt_id, question_id)")
        self.assertSqlContains("idx_quiz_text_answers_correct")
        self.assertSqlContains("ON quiz_text_answers(attempt_id, is_correct)")


if __name__ == "__main__":
    unittest.main()

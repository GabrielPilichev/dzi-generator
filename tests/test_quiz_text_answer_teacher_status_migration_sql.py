import sqlite3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_005 = ROOT / "web" / "migrations" / "005_quiz_text_answers.sql"
MIGRATION_009 = ROOT / "web" / "migrations" / "009_quiz_text_answer_teacher_status.sql"


class QuizTextAnswerTeacherStatusMigrationSqlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_009.read_text(encoding="utf-8")

    def test_adds_teacher_override_status_column(self):
        self.assertIn("ALTER TABLE quiz_text_answers", self.sql)
        self.assertIn("ADD COLUMN teacher_override_status TEXT", self.sql)
        self.assertIn("'correct'", self.sql)
        self.assertIn("'partially_correct'", self.sql)
        self.assertIn("'incorrect'", self.sql)

    def test_migration_executes_after_quiz_text_answers(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript("""
                CREATE TABLE quiz_attempts (id INTEGER PRIMARY KEY);
                CREATE TABLE questions (id INTEGER PRIMARY KEY);
                CREATE TABLE fill_in_subquestions (id INTEGER PRIMARY KEY);
            """)
            conn.executescript(MIGRATION_005.read_text(encoding="utf-8"))
            conn.executescript(self.sql)

            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(quiz_text_answers)").fetchall()
            }
            self.assertIn("teacher_override_status", columns)

            conn.execute("INSERT INTO quiz_attempts (id) VALUES (1)")
            conn.execute("INSERT INTO questions (id) VALUES (2)")
            conn.execute("""
                INSERT INTO quiz_text_answers (
                    attempt_id, question_id, subquestion_number,
                    raw_answer, normalized_answer, teacher_override_status
                )
                VALUES (1, 2, 1, 'x', 'x', 'partially_correct')
            """)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("""
                    INSERT INTO quiz_text_answers (
                        attempt_id, question_id, subquestion_number,
                        raw_answer, normalized_answer, teacher_override_status
                    )
                    VALUES (1, 2, 2, 'x', 'x', 'maybe')
                """)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

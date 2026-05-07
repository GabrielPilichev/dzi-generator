import sqlite3
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

    def test_migration_executes_against_in_memory_stub_schema(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript("""
                CREATE TABLE quiz_attempts (id INTEGER PRIMARY KEY);
                CREATE TABLE questions (id INTEGER PRIMARY KEY);
                CREATE TABLE fill_in_subquestions (id INTEGER PRIMARY KEY);
            """)
            conn.executescript(self.sql)

            table = conn.execute("""
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'quiz_text_answers'
            """).fetchone()
            self.assertIsNotNone(table)

            index_rows = conn.execute("""
                SELECT name
                FROM sqlite_master
                WHERE type = 'index'
                  AND tbl_name = 'quiz_text_answers'
            """).fetchall()
            index_names = {row[0] for row in index_rows}
            self.assertIn("idx_quiz_text_answers_attempt", index_names)
            self.assertIn("idx_quiz_text_answers_question", index_names)
            self.assertIn("idx_quiz_text_answers_attempt_question", index_names)
            self.assertIn("idx_quiz_text_answers_correct", index_names)

            foreign_keys = conn.execute("PRAGMA foreign_key_list(quiz_text_answers)").fetchall()
            foreign_key_tables = {row[2] for row in foreign_keys}
            self.assertIn("quiz_attempts", foreign_key_tables)
            self.assertIn("questions", foreign_key_tables)
            self.assertIn("fill_in_subquestions", foreign_key_tables)

            unique_indexes = [
                row
                for row in conn.execute("PRAGMA index_list(quiz_text_answers)").fetchall()
                if row[2]
            ]
            unique_columns = []
            for row in unique_indexes:
                columns = [
                    column_row[2]
                    for column_row in conn.execute(f"PRAGMA index_info({row[1]})").fetchall()
                ]
                unique_columns.append(columns)
            self.assertIn(["attempt_id", "question_id", "subquestion_number"], unique_columns)

            conn.execute("INSERT INTO quiz_attempts (id) VALUES (1)")
            conn.execute("INSERT INTO questions (id) VALUES (2)")
            conn.execute("INSERT INTO fill_in_subquestions (id) VALUES (3)")
            conn.execute("""
                INSERT INTO quiz_text_answers (
                    attempt_id, question_id, subquestion_id, subquestion_number,
                    raw_answer, normalized_answer
                )
                VALUES (1, 2, 3, 1, ' Да ', 'да')
            """)
            stored = conn.execute("""
                SELECT raw_answer, normalized_answer
                FROM quiz_text_answers
            """).fetchone()
            self.assertEqual(stored, (" Да ", "да"))

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("""
                    INSERT INTO quiz_text_answers (
                        attempt_id, question_id, subquestion_id, subquestion_number,
                        raw_answer, normalized_answer
                    )
                    VALUES (999, 2, 3, 2, 'x', 'x')
                """)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

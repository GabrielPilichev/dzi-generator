import sqlite3
import unittest

from src.audit_open_question_readiness import audit_open_question_readiness


class AuditOpenQuestionReadinessTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE questions (
                id INTEGER PRIMARY KEY,
                source_exam TEXT NOT NULL,
                source_number INTEGER,
                question_type TEXT NOT NULL,
                prompt TEXT NOT NULL
            );
            CREATE TABLE fill_in_subquestions (
                id INTEGER PRIMARY KEY,
                question_id INTEGER NOT NULL,
                subquestion_number INTEGER NOT NULL,
                correct_answer TEXT NOT NULL,
                answer_alternatives TEXT
            );
        """)
        self.insert_question(1, "source_a", 1, "fill_in", "Ready open question")
        self.insert_subquestion(1, 1, "да")
        self.insert_question(2, "source_a", 26, "fill_in", "Practical task")
        self.insert_subquestion(2, 1, "да")
        self.insert_question(3, "source_a", 3, "fill_in", "Според показаната диаграма попълнете стойността.")
        self.insert_subquestion(3, 1, "42")
        self.insert_question(4, "source_b", 4, "fill_in", "Missing answer")
        self.insert_subquestion(4, 1, "")
        self.insert_question(5, "source_b", 5, "short_answer", "Ready short answer")
        self.insert_subquestion(5, 1, '["jpeg", "jpg"]')
        self.insert_question(6, "source_b", 6, "multiple_choice", "MC ignored")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def insert_question(self, question_id, source_exam, source_number, question_type, prompt):
        self.conn.execute("""
            INSERT INTO questions (id, source_exam, source_number, question_type, prompt)
            VALUES (?, ?, ?, ?, ?)
        """, (question_id, source_exam, source_number, question_type, prompt))

    def insert_subquestion(self, question_id, subquestion_number, correct_answer):
        self.conn.execute("""
            INSERT INTO fill_in_subquestions (
                question_id, subquestion_number, correct_answer, answer_alternatives
            )
            VALUES (?, ?, ?, NULL)
        """, (question_id, subquestion_number, correct_answer))

    def test_audit_counts_open_question_readiness(self):
        summary = audit_open_question_readiness(self.conn)

        self.assertEqual(summary["total_inspected"], 5)
        self.assertEqual(summary["auto_gradable_count"], 2)
        self.assertEqual(summary["excluded_practical_count"], 1)
        self.assertEqual(summary["excluded_visual_dependent_count"], 1)
        self.assertEqual(summary["excluded_missing_accepted_answers_count"], 1)
        self.assertEqual(summary["candidates_by_source_slug"], {"source_a": 1, "source_b": 1})


if __name__ == "__main__":
    unittest.main()

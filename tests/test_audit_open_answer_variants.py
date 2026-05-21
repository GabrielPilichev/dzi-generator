import sqlite3
import unittest

from src.audit_open_answer_variants import (
    answer_values,
    audit_open_answer_variants,
    separator_flags,
)


class AuditOpenAnswerVariantsTest(unittest.TestCase):
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
                subquestion_text TEXT,
                correct_answer TEXT NOT NULL,
                answer_alternatives TEXT
            );
        """)

    def tearDown(self):
        self.conn.close()

    def insert_question(self, question_id, source_slug, task_number, question_type="fill_in"):
        self.conn.execute("""
            INSERT INTO questions (id, source_exam, source_number, question_type, prompt)
            VALUES (?, ?, ?, ?, ?)
        """, (question_id, source_slug, task_number, question_type, f"Task {task_number}"))

    def insert_subquestion(self, question_id, subquestion_number, correct_answer, alternatives=None):
        self.conn.execute("""
            INSERT INTO fill_in_subquestions (
                question_id, subquestion_number, correct_answer, answer_alternatives
            )
            VALUES (?, ?, ?, ?)
        """, (question_id, subquestion_number, correct_answer, alternatives))

    def test_answer_values_accepts_json_lists_and_plain_text(self):
        self.assertEqual(answer_values('["вектор", "векторна"]'), ["вектор", "векторна"])
        self.assertEqual(answer_values("клиент"), ["клиент"])
        self.assertEqual(answer_values("[]"), [])
        self.assertEqual(answer_values(None), [])

    def test_separator_flags_detects_common_variant_patterns(self):
        self.assertIn("slash", separator_flags("едно към много / 1:M"))
        self.assertIn("semicolon", separator_flags("count; брой"))
        self.assertIn("comma", separator_flags("count, брой"))
        self.assertIn("ili", separator_flags("count или брой"))
        self.assertIn("mixed_latin_cyrillic", separator_flags("count / брой"))

    def test_audit_reports_expected_categories(self):
        self.insert_question(1, "source_a", 19)
        self.insert_subquestion(1, 1, "")

        self.insert_question(2, "source_a", 20)
        self.insert_subquestion(2, 1, "вектор")
        self.insert_subquestion(2, 2, "")

        self.insert_question(3, "source_b", 21)
        self.insert_subquestion(3, 1, "пациент: ЕГН", '["едно към много / 1:M"]')

        self.insert_question(4, "source_b", 22)
        shared = '["а", "б"]'
        self.insert_subquestion(4, 1, "x", shared)
        self.insert_subquestion(4, 2, "y", shared)

        summary = audit_open_answer_variants(self.conn)
        categories = summary["category_counts"]

        self.assertEqual(summary["total_inspected"], 4)
        self.assertGreaterEqual(categories["missing_accepted_answers"], 2)
        self.assertEqual(categories["weak_variant_coverage"], 1)
        self.assertEqual(categories["likely_global_alternatives"], 1)
        self.assertGreaterEqual(categories["accepted_without_alternatives"], 1)
        self.assertGreaterEqual(categories["separator_patterns"], 1)
        self.assertGreaterEqual(summary["issues_by_source_slug"]["source_a"], 3)
        self.assertGreaterEqual(summary["likely_needs_manual_review"], 4)


if __name__ == "__main__":
    unittest.main()

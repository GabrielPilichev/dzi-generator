import random
import sqlite3
import unittest


from tests.support import web_app

build_mixed_quiz_plan = web_app.build_mixed_quiz_plan


class MixedQuizPlanHelperTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE exams (id INTEGER PRIMARY KEY, subject TEXT, level TEXT, year INTEGER);
            INSERT INTO exams VALUES (1, 'informatika_it', 'DZI', 2090);
            CREATE TABLE questions (
                id INTEGER PRIMARY KEY,
                exam_id INTEGER DEFAULT 1,
                subject TEXT DEFAULT 'informatika_it',
                level TEXT DEFAULT 'DZI',
                year INTEGER DEFAULT 2090,
                source_exam TEXT NOT NULL,
                source_number INTEGER,
                question_type TEXT NOT NULL,
                prompt TEXT NOT NULL,
                has_image INTEGER DEFAULT 0,
                image_path TEXT,
                is_ai_generated INTEGER DEFAULT 0,
                quality_score REAL
            );
            CREATE TABLE multiple_choice_options (
                id INTEGER PRIMARY KEY,
                question_id INTEGER NOT NULL,
                option_letter TEXT NOT NULL,
                option_text TEXT NOT NULL,
                is_correct INTEGER DEFAULT 0
            );
            CREATE TABLE fill_in_subquestions (
                id INTEGER PRIMARY KEY,
                question_id INTEGER NOT NULL,
                subquestion_number INTEGER NOT NULL,
                correct_answer TEXT NOT NULL,
                answer_alternatives TEXT
            );
        """)
        self.insert_question(1, "source_a", 1, "multiple_choice", "Closed one")
        self.insert_mc_options(1)
        self.insert_question(2, "source_a", 2, "multiple_choice", "Closed two")
        self.insert_mc_options(2)
        self.insert_question(3, "source_a", 16, "fill_in", "Open one")
        self.insert_subquestion(3, 1, "300")
        self.insert_question(4, "source_a", 17, "fill_in", "Open two")
        self.insert_subquestion(4, 1, '["jpeg", "jpg"]')
        self.insert_question(5, "source_b", 1, "multiple_choice", "Other source closed")
        self.insert_mc_options(5)
        self.insert_question(6, "source_b", 16, "fill_in", "Other source open")
        self.insert_subquestion(6, 1, "да")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def insert_question(self, question_id, source_exam, source_number, question_type, prompt):
        self.conn.execute("""
            INSERT INTO questions (
                id, source_exam, source_number, question_type, prompt, has_image, image_path,
                is_ai_generated, quality_score
            )
            VALUES (?, ?, ?, ?, ?, 0, NULL, 0, NULL)
        """, (question_id, source_exam, source_number, question_type, prompt))

    def insert_mc_options(self, question_id):
        for letter, text, is_correct in (
            ("А", "Първа възможност", 1),
            ("Б", "Втора възможност", 0),
            ("В", "Трета възможност", 0),
            ("Г", "Четвърта възможност", 0),
        ):
            self.conn.execute("""
                INSERT INTO multiple_choice_options (question_id, option_letter, option_text, is_correct)
                VALUES (?, ?, ?, ?)
            """, (question_id, letter, text, is_correct))

    def insert_subquestion(self, question_id, subquestion_number, correct_answer):
        self.conn.execute("""
            INSERT INTO fill_in_subquestions (
                question_id, subquestion_number, correct_answer, answer_alternatives
            )
            VALUES (?, ?, ?, NULL)
        """, (question_id, subquestion_number, correct_answer))

    def test_returns_requested_counts_when_available(self):
        plan = build_mixed_quiz_plan(
            self.conn,
            closed_count=2,
            open_count=2,
            source_slug="source_a",
        )

        # When the pool size equals the requested count, the full pool must
        # be returned. The internal order is randomized (see fix for
        # repeated-test bug), so compare as sets.
        self.assertEqual(
            {q["question_id"] for q in plan["closed_questions"]},
            {1, 2},
        )
        self.assertEqual(
            {q["question_id"] for q in plan["open_questions"]},
            {3, 4},
        )
        self.assertEqual(plan["requested_closed_count"], 2)
        self.assertEqual(plan["requested_open_count"], 2)
        self.assertEqual(plan["available_closed_count"], 2)
        self.assertEqual(plan["available_open_count"], 2)

    def test_reports_shortfall_without_writing(self):
        before = self.conn.total_changes
        plan = build_mixed_quiz_plan(
            self.conn,
            closed_count=3,
            open_count=3,
            source_slug="source_a",
        )
        after = self.conn.total_changes

        self.assertEqual(len(plan["closed_questions"]), 2)
        self.assertEqual(len(plan["open_questions"]), 2)
        self.assertEqual(plan["available_closed_count"], 2)
        self.assertEqual(plan["available_open_count"], 2)
        self.assertEqual(after, before)

    def test_source_slug_filter(self):
        plan = build_mixed_quiz_plan(
            self.conn,
            closed_count=2,
            open_count=2,
            source_slug="source_b",
        )

        self.assertEqual([q["question_id"] for q in plan["closed_questions"]], [5])
        self.assertEqual([q["question_id"] for q in plan["open_questions"]], [6])

    def test_rejects_negative_counts(self):
        with self.assertRaises(ValueError):
            build_mixed_quiz_plan(self.conn, closed_count=0, open_count=-1)


if __name__ == "__main__":
    unittest.main()

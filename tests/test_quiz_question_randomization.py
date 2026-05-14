"""Regression tests for repeated-test variation.

Tester report: "Дава едни и същи въпроси, независимо от име, брой, време,
когато са на една и съща тема." — when two new assignments were created for
the same section/count, students always saw the same question set in the
same order.

Root cause: ``build_mixed_quiz_plan`` selected the first N candidates from a
deterministic ORDER BY without any randomization. Two new mixed/open
assignments for the same section therefore always picked an identical
question set.

These tests lock in the fix while staying fully deterministic — randomness
is exercised through seeded ``random.Random`` instances passed via the
helper's new ``rng`` parameter, so the suite is non-flaky.
"""

import atexit
import os
import random
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

from web.app import build_mixed_quiz_plan, quiz_pick_questions  # noqa: E402


def _build_pool_conn():
    """In-memory pool with 12 closed + 6 open eligible candidates."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE questions (
            id INTEGER PRIMARY KEY,
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
    closed_ids = list(range(100, 112))  # 12 closed
    for qid in closed_ids:
        conn.execute("""
            INSERT INTO questions (
                id, source_exam, source_number, question_type, prompt,
                has_image, image_path, is_ai_generated, quality_score
            )
            VALUES (?, 'source_a', ?, 'multiple_choice', ?, 0, NULL, 0, NULL)
        """, (qid, qid - 99, f"Closed Q{qid}"))
        for letter, text, is_correct in (
            ("А", "Първа", 1), ("Б", "Втора", 0),
            ("В", "Трета", 0), ("Г", "Четвърта", 0),
        ):
            conn.execute("""
                INSERT INTO multiple_choice_options (question_id, option_letter, option_text, is_correct)
                VALUES (?, ?, ?, ?)
            """, (qid, letter, text, is_correct))
    open_ids = list(range(200, 206))  # 6 open
    for qid in open_ids:
        conn.execute("""
            INSERT INTO questions (
                id, source_exam, source_number, question_type, prompt,
                has_image, image_path, is_ai_generated, quality_score
            )
            VALUES (?, 'source_a', ?, 'fill_in', ?, 0, NULL, 0, NULL)
        """, (qid, qid - 184, f"Open Q{qid}"))
        conn.execute("""
            INSERT INTO fill_in_subquestions (
                question_id, subquestion_number, correct_answer, answer_alternatives
            )
            VALUES (?, 1, 'answer', NULL)
        """, (qid,))
    conn.commit()
    return conn, closed_ids, open_ids


class BuildMixedQuizPlanRandomizationTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.closed_ids, self.open_ids = _build_pool_conn()

    def tearDown(self):
        self.conn.close()

    def _plan(self, rng, *, closed_count=5, open_count=2):
        plan = build_mixed_quiz_plan(
            self.conn,
            closed_count=closed_count,
            open_count=open_count,
            source_slug="source_a",
            rng=rng,
        )
        return (
            [q["question_id"] for q in plan["closed_questions"]],
            [q["question_id"] for q in plan["open_questions"]],
        )

    # Pool-large case: two new assignments should differ -------------------

    def test_two_calls_with_distinct_seeded_rngs_produce_different_question_sets(self):
        closed_a, open_a = self._plan(random.Random(1), closed_count=5, open_count=2)
        closed_b, open_b = self._plan(random.Random(2), closed_count=5, open_count=2)
        # Sets differ (or at least orders differ) — the bug guaranteed
        # both sets *and* orders were identical. Either differing is fine.
        self.assertTrue(
            set(closed_a) != set(closed_b) or closed_a != closed_b,
            "closed selections should differ across distinct seeds",
        )
        self.assertTrue(
            set(open_a) != set(open_b) or open_a != open_b,
            "open selections should differ across distinct seeds",
        )

    def test_seeded_rng_is_deterministic_for_same_seed(self):
        first = self._plan(random.Random("stable-seed"))
        second = self._plan(random.Random("stable-seed"))
        self.assertEqual(first, second)

    # Pool-small case: deterministic reuse ---------------------------------

    def test_count_at_pool_size_returns_full_pool_in_some_order(self):
        closed, open_picks = self._plan(random.Random(7), closed_count=12, open_count=6)
        self.assertEqual(set(closed), set(self.closed_ids))
        self.assertEqual(set(open_picks), set(self.open_ids))
        # Order may be randomized but length is exact.
        self.assertEqual(len(closed), 12)
        self.assertEqual(len(open_picks), 6)

    def test_count_greater_than_pool_returns_full_pool_safely(self):
        closed, open_picks = self._plan(random.Random(11), closed_count=99, open_count=99)
        self.assertEqual(set(closed), set(self.closed_ids))
        self.assertEqual(set(open_picks), set(self.open_ids))

    def test_zero_counts_return_empty_lists(self):
        closed, open_picks = self._plan(random.Random(13), closed_count=0, open_count=0)
        self.assertEqual(closed, [])
        self.assertEqual(open_picks, [])

    # Existing shape invariants -------------------------------------------

    def test_question_count_is_preserved(self):
        closed, open_picks = self._plan(random.Random(0), closed_count=7, open_count=3)
        self.assertEqual(len(closed), 7)
        self.assertEqual(len(open_picks), 3)

    def test_open_questions_disjoint_from_closed(self):
        closed, open_picks = self._plan(random.Random(0), closed_count=5, open_count=4)
        self.assertEqual(set(closed) & set(open_picks), set())

    def test_default_rng_is_used_when_none_passed(self):
        # When rng is omitted, the helper picks a system-random selection.
        # This test just verifies the call doesn't crash and returns the
        # right shape — it does NOT assert anything about randomness, to
        # keep the test deterministic.
        plan = build_mixed_quiz_plan(
            self.conn,
            closed_count=3,
            open_count=2,
            source_slug="source_a",
        )
        self.assertEqual(len(plan["closed_questions"]), 3)
        self.assertEqual(len(plan["open_questions"]), 2)


class McOnlyPickerStabilityTest(unittest.TestCase):
    """``quiz_pick_questions`` already varies via assignment_id-based seed.

    These tests lock that contract in: same (assignment_id, student_name)
    is stable across calls (so re-renders are deterministic) while
    different assignment_ids produce different orders even with the
    same student name.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE topic_section_assignments (
                topic_id INTEGER, section_id INTEGER,
                relationship_type TEXT, is_primary INTEGER
            );
            CREATE TABLE questions (
                id INTEGER PRIMARY KEY,
                topic_id INTEGER,
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
        for qid in range(1, 21):
            self.conn.execute("""
                INSERT INTO questions (
                    id, topic_id, source_exam, source_number, question_type,
                    prompt, has_image, image_path, is_ai_generated, quality_score
                )
                VALUES (?, 1, 'source_a', ?, 'multiple_choice', ?, 0, NULL, 0, NULL)
            """, (qid, qid, f"Q{qid}"))
            for letter, text, is_correct in (
                ("А", "Първа", 1), ("Б", "Втора", 0),
                ("В", "Трета", 0), ("Г", "Четвърта", 0),
            ):
                self.conn.execute("""
                    INSERT INTO multiple_choice_options (
                        question_id, option_letter, option_text, is_correct
                    )
                    VALUES (?, ?, ?, ?)
                """, (qid, letter, text, is_correct))
        self.conn.execute("""
            INSERT INTO topic_section_assignments
                (topic_id, section_id, relationship_type, is_primary)
            VALUES (1, 555, 'covers', 1)
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _assignment(self, assignment_id):
        return {
            "id": assignment_id,
            "section_id": 555,
            "question_count": 8,
        }

    def test_same_assignment_same_name_is_stable(self):
        _seed_a, ids_a = quiz_pick_questions(self.conn, self._assignment(42), "Иван")
        _seed_b, ids_b = quiz_pick_questions(self.conn, self._assignment(42), "Иван")
        self.assertEqual(ids_a, ids_b)
        self.assertEqual(len(ids_a), 8)

    def test_different_assignments_same_name_pick_different_orders(self):
        _seed_a, ids_a = quiz_pick_questions(self.conn, self._assignment(42), "Иван")
        _seed_b, ids_b = quiz_pick_questions(self.conn, self._assignment(43), "Иван")
        # Different assignment_id -> different seed -> different shuffle.
        # We assert the lists are not identical; even when the SET ends up
        # equal at small pool sizes, the order must vary.
        self.assertNotEqual(ids_a, ids_b)

    def test_count_is_clamped_to_available_pool(self):
        assignment = self._assignment(99)
        assignment["question_count"] = 999  # way more than the 20 available
        _seed, ids = quiz_pick_questions(self.conn, assignment, "Малък пул")
        self.assertEqual(len(ids), 20)
        self.assertEqual(set(ids), set(range(1, 21)))


if __name__ == "__main__":
    unittest.main()

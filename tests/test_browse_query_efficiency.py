"""Browse decoration batches options/subquestions; questions(topic_id) is indexed.

Databases are temporary clones of the CANONICAL template; nothing touches a repository database.
Statement budgets are ceilings with headroom, never exact counts.
"""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import src.subject_browse_cache as cache_module
from src.subject_browse_cache import BrowseResultCache
from src.subject_content import SQL_CHUNK_SIZE
from tests.support import CANONICAL, IsolatedAppTestCase, build_test_database, web_app
from web.services.browsing import _decorate_browse_questions
from web.services.dzi_exams import dzi_json_text_list, fetch_fill_in_answer_rows
from web.services.question_visuals import attach_question_visual_assets
from web.text_values import unique_text_values

MIGRATION = Path(__file__).resolve().parents[1] / "web" / "migrations" / "011_questions_topic_id_index.sql"
# Measured on the canonical dataset: DZI section 820 -> 21 statements.
OLD_DZI_SECTION_STATEMENTS = 820
DZI_SECTION_CEILING = 60
OTHER_SECTION_CEILING = 40


def reference_decorate(db, rows):
    """The previous per-question implementation, kept as the semantic oracle."""
    questions = []
    for r in rows:
        q = dict(r)
        if q["question_type"] in ("multiple_choice", "true_false"):
            q["options"] = [dict(o) for o in db.execute(
                "SELECT option_letter, option_text, is_correct FROM multiple_choice_options "
                "WHERE question_id = ? ORDER BY option_letter", (q["question_id"],))]
        else:
            q["options"] = []
        if q["question_type"] in {"fill_in", "short_answer"}:
            q["subquestions"] = fetch_fill_in_answer_rows(db, int(q["question_id"]))
        else:
            q["subquestions"] = []
        questions.append(q)
    return attach_question_visual_assets(db, questions, id_key="question_id")


def traced_get(path):
    statements = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    with patch.object(sqlite3, "connect", traced):
        response = web_app.app.test_client().get(path)
    return response, statements


class DecorationOnCanonicalDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix="learnpilot-browse-")
        cls.addClassCleanup(directory.cleanup)
        cls.db = build_test_database(Path(directory.name) / "canonical.db")
        cls.conn = sqlite3.connect(cls.db)
        cls.conn.row_factory = sqlite3.Row
        cls.addClassCleanup(cls.conn.close)
        cls.rows = [
            {"question_id": r["id"], "question_type": r["question_type"], "prompt": r["prompt"]}
            for r in cls.conn.execute("SELECT id, question_type, prompt FROM questions ORDER BY id")
        ]

    def test_every_question_type_is_present(self):
        types = {r["question_type"] for r in self.rows}
        self.assertTrue({"multiple_choice", "fill_in"} <= types, types)
        self.assertGreater(len(self.rows), SQL_CHUNK_SIZE)

    def test_batched_output_equals_the_per_question_reference(self):
        self.assertEqual(_decorate_browse_questions(self.conn, self.rows),
                         reference_decorate(self.conn, self.rows))

    def test_input_order_is_preserved_and_shuffle_safe(self):
        shuffled = self.rows[::-1]
        out = _decorate_browse_questions(self.conn, shuffled)
        self.assertEqual([q["question_id"] for q in out], [r["question_id"] for r in shuffled])
        self.assertEqual(out, reference_decorate(self.conn, shuffled))

    def test_option_letters_and_subquestion_numbers_stay_sorted(self):
        for q in _decorate_browse_questions(self.conn, self.rows):
            letters = [o["option_letter"] for o in q["options"]]
            self.assertEqual(letters, sorted(letters))
            numbers = [s["subquestion_number"] for s in q["subquestions"]]
            self.assertEqual(numbers, sorted(numbers))
            self.assertNotIn("question_id", q["subquestions"][0] if q["subquestions"] else {})

    def test_empty_input(self):
        self.assertEqual(_decorate_browse_questions(self.conn, []), [])

    def test_questions_without_options_or_subquestions(self):
        mc = next(r for r in self.rows if r["question_type"] == "multiple_choice")
        self.addCleanup(self.conn.rollback)    # DELETEs open an implicit transaction
        self.conn.execute("DELETE FROM multiple_choice_options WHERE question_id = ?", (mc["question_id"],))
        fill = next(r for r in self.rows if r["question_type"] in {"fill_in", "short_answer"})
        self.conn.execute("DELETE FROM fill_in_subquestions WHERE question_id = ?", (fill["question_id"],))
        out = {q["question_id"]: q for q in _decorate_browse_questions(self.conn, [mc, fill])}
        self.assertEqual(out[mc["question_id"]]["options"], [])
        self.assertEqual(out[fill["question_id"]]["subquestions"], [])

    def test_unrelated_types_get_no_options_or_subquestions(self):
        other = [r for r in self.rows if r["question_type"] not in
                 ("multiple_choice", "true_false", "fill_in", "short_answer")]
        for q in _decorate_browse_questions(self.conn, other):
            self.assertEqual((q["options"], q["subquestions"]), ([], []))

    def test_statements_grow_with_chunks_not_questions(self):
        def count(rows):
            statements = []
            self.conn.set_trace_callback(statements.append)
            try:
                _decorate_browse_questions(self.conn, rows)
            finally:
                self.conn.set_trace_callback(None)
            return len(statements)

        small, large = count(self.rows[:50]), count(self.rows)
        self.assertGreater(len(self.rows), 1500)
        self.assertLess(small, 10)
        # Past one chunk, each extra chunk adds a bounded handful of statements.
        chunks = -(-len(self.rows) // SQL_CHUNK_SIZE)
        self.assertLessEqual(large, small + 6 * chunks)
        self.assertLess(large, len(self.rows) / 20)

    def test_no_statement_exceeds_the_parameter_chunk(self):
        sizes = []
        self.conn.set_trace_callback(lambda sql: sizes.append(sql.count("?")))
        try:
            _decorate_browse_questions(self.conn, self.rows)
        finally:
            self.conn.set_trace_callback(None)
        self.assertLessEqual(max(sizes, default=0), SQL_CHUNK_SIZE)

    def test_accepted_answers_match_the_text_helpers(self):
        for q in _decorate_browse_questions(self.conn, self.rows):
            for sub in q["subquestions"]:
                correct = dzi_json_text_list(sub["correct_answer"])
                self.assertEqual(sub["accepted_answers"], unique_text_values(
                    correct + dzi_json_text_list(sub["answer_alternatives"])))


class SectionPageBudgetTest(IsolatedAppTestCase):
    content_profile = CANONICAL

    def setUp(self):
        stack = patch.object(cache_module, "browse_cache", BrowseResultCache())
        stack.start()
        self.addCleanup(stack.stop)

    def test_dzi_section_is_far_below_the_per_question_baseline(self):
        response, statements = traced_get("/section/grade12-dzi-preparation")
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(statements), DZI_SECTION_CEILING)
        self.assertLess(len(statements), OLD_DZI_SECTION_STATEMENTS / 10)

    def test_physics_and_it_sections_stay_bounded(self):
        for path in ("/subjects/physics/8/mechanics", "/section/grade8-computer-systems",
                     "/section/grade12-m3-summary"):
            with self.subTest(path=path):
                response, statements = traced_get(path)
                self.assertEqual(response.status_code, 200)
                self.assertLessEqual(len(statements), OTHER_SECTION_CEILING, len(statements))


class TopicIdIndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix="learnpilot-topic-index-")
        cls.addClassCleanup(directory.cleanup)
        cls.db = build_test_database(Path(directory.name) / "canonical.db")

    def connect(self):
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn

    def test_index_is_on_topic_id_only_and_the_legacy_one_is_kept(self):
        conn = self.connect()
        self.assertEqual([r[2] for r in conn.execute("PRAGMA index_info(idx_questions_topic_id)")], ["topic_id"])
        self.assertEqual([r[2] for r in conn.execute("PRAGMA index_info(idx_questions_topic)")], ["topic"])
        self.assertTrue(MIGRATION.read_text(encoding="utf-8").strip())

    def plan(self, conn, sql, *params):
        return " | ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))

    def test_section_and_grade_count_queries_use_the_index(self):
        conn = self.connect()
        in_section = self.plan(conn, """
            SELECT DISTINCT q.id FROM topic_section_assignments tsa
            JOIN curriculum_topics t ON t.id = tsa.topic_id
            JOIN questions q ON q.topic_id = t.id
            WHERE tsa.section_id = ? AND (q.is_ai_generated = 0 OR q.quality_score >= 1.0)
            ORDER BY t.topic_slug, q.id""", 1)
        grade_counts = self.plan(conn, """
            SELECT tc.class, tc.topic_id, q.id FROM topic_classes tc
            JOIN curriculum_topics t ON t.id = tc.topic_id
            LEFT JOIN questions q ON q.topic_id = t.id
                AND (q.is_ai_generated = 0 OR q.quality_score >= 1.0)
            WHERE tc.class IN (8, 9) ORDER BY tc.class""")
        for plan in (in_section, grade_counts):
            self.assertIn("idx_questions_topic_id", plan)
            self.assertNotIn("AUTOMATIC", plan)

    def test_migration_is_idempotent_and_changes_no_rows(self):
        conn = self.connect()
        # WITHOUT ROWID tables (login_throttle) have no rowid to total.
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "AND sql NOT LIKE '%WITHOUT ROWID%'")]

        def digest():
            return {t: conn.execute(f'SELECT COUNT(*), TOTAL(rowid) FROM "{t}"').fetchone()[:] for t in tables}

        before = digest()
        conn.executescript(MIGRATION.read_text(encoding="utf-8"))
        conn.executescript(MIGRATION.read_text(encoding="utf-8"))
        self.assertEqual(digest(), before)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'idx_questions_topic_id'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()

"""Question Bank Assistant tools (design doc section 3.2) against a small fixture database.

The fixture is built in a temporary directory per test class (``assistant_fakes``); the
runtime ``data/questions.db`` is never opened. Handlers are called directly with a
``ToolContext`` whose connection comes from ``open_read_only``.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.assistant import tools
from src.assistant.tools import TOOLS, ToolContext, ToolError, open_read_only
from tests.assistant_fakes import (
    FIXED_NOW,
    build_fixture_db,
    file_sha256,
    fixed_now,
    read_jsonl,
)

ALL_TOOLS = {
    "search_questions", "get_question", "topic_coverage",
    "find_similar_questions", "flag_question", "propose_topic_change",
}


class AssistantToolsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls.db_path = build_fixture_db(Path(cls._dir.name) / "fixture.db")

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def setUp(self):
        self.conn = open_read_only(self.db_path)
        self.addCleanup(self.conn.close)
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.proposals_path = Path(work.name) / "proposals.jsonl"
        self.ctx = ToolContext(conn=self.conn, proposals_path=self.proposals_path, now=fixed_now)

    def call(self, name, **tool_input):
        return TOOLS[name].handler(self.ctx, tool_input)


class RegistryTest(AssistantToolsTestCase):
    def test_registry_has_exactly_the_documented_tools(self):
        self.assertEqual(set(TOOLS), ALL_TOOLS)
        for name, spec in TOOLS.items():
            self.assertEqual(spec.name, name)
            self.assertTrue(spec.description.strip())
            self.assertTrue(callable(spec.handler))

    def test_every_schema_is_strict_compatible(self):
        for name, spec in TOOLS.items():
            with self.subTest(tool=name):
                schema = spec.input_schema
                self.assertEqual(schema.get("type"), "object")
                self.assertIs(schema.get("additionalProperties"), False)
                self.assertEqual(set(schema.get("required", [])), set(schema["properties"]))


class ReadOnlyConnectionTest(AssistantToolsTestCase):
    def test_rows_are_sqlite_rows(self):
        self.assertIs(self.conn.row_factory, sqlite3.Row)

    def test_writes_are_rejected(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute("UPDATE questions SET prompt = 'x' WHERE id = 101")
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute(
                "INSERT INTO questions (id, prompt, question_type) VALUES (999, 'x', 'y')"
            )
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute("CREATE TABLE sneaky (id INTEGER)")


class SearchQuestionsTest(AssistantToolsTestCase):
    def test_output_shape(self):
        out = self.call("search_questions", query="устройство", grade=None, limit=25)
        self.assertEqual(set(out), {"results"})
        self.assertEqual({r["id"] for r in out["results"]}, {101, 102, 105})
        for row in out["results"]:
            self.assertEqual(set(row), {"id", "prompt", "question_type", "grade", "topic_slug"})
        by_id = {r["id"]: r for r in out["results"]}
        self.assertEqual(by_id[101]["grade"], 9)
        self.assertEqual(by_id[101]["topic_slug"], "input-output-devices")
        self.assertEqual(by_id[101]["question_type"], "multiple_choice")
        self.assertEqual(by_id[105]["grade"], 11)

    def test_grade_filter(self):
        out = self.call("search_questions", query="устройство", grade=9, limit=25)
        self.assertEqual({r["id"] for r in out["results"]}, {101, 102})
        out = self.call("search_questions", query="устройство", grade=11, limit=25)
        self.assertEqual({r["id"] for r in out["results"]}, {105})

    def test_like_is_case_insensitive(self):
        out = self.call("search_questions", query="excel", grade=None, limit=25)
        self.assertEqual([r["id"] for r in out["results"]], [106])

    def test_limit_caps_results(self):
        out = self.call("search_questions", query="устройство", grade=None, limit=1)
        self.assertEqual(len(out["results"]), 1)

    def test_question_without_topic_is_found_with_null_grade(self):
        out = self.call("search_questions", query="алгоритъм", grade=None, limit=5)
        self.assertEqual(len(out["results"]), 1)
        self.assertEqual(out["results"][0]["id"], 107)
        self.assertIsNone(out["results"][0]["grade"])
        self.assertIsNone(out["results"][0]["topic_slug"])

    def test_input_ranges(self):
        for bad in (
            {"query": "x", "grade": 7, "limit": 5},
            {"query": "x", "grade": 13, "limit": 5},
            {"query": "x", "grade": None, "limit": 0},
            {"query": "x", "grade": None, "limit": 26},
        ):
            with self.subTest(input=bad), self.assertRaises(ToolError):
                self.call("search_questions", **bad)


class GetQuestionTest(AssistantToolsTestCase):
    def test_output_shape(self):
        out = self.call("get_question", question_id=101)
        self.assertEqual(
            set(out),
            {"id", "prompt", "question_type", "grade", "topic_slug", "topic_title",
             "options", "is_ai_generated"},
        )
        self.assertEqual(out["id"], 101)
        self.assertEqual(out["prompt"], "Кой от следните е входно устройство")
        self.assertEqual(out["grade"], 9)
        self.assertEqual(out["topic_slug"], "input-output-devices")
        self.assertEqual(out["topic_title"], "Входно-изходни устройства")
        self.assertFalse(out["is_ai_generated"])
        self.assertEqual([o["letter"] for o in out["options"]], ["А", "Б", "В"])
        for option in out["options"]:
            self.assertEqual(set(option), {"letter", "text", "is_correct"})
        self.assertEqual([bool(o["is_correct"]) for o in out["options"]], [True, False, False])
        self.assertEqual(out["options"][0]["text"], "Клавиатура")

    def test_ai_generated_flag(self):
        self.assertTrue(self.call("get_question", question_id=102)["is_ai_generated"])

    def test_question_without_options_or_topic(self):
        out = self.call("get_question", question_id=107)
        self.assertEqual(out["options"], [])
        self.assertIsNone(out["grade"])
        self.assertIsNone(out["topic_slug"])

    def test_missing_question_is_a_tool_error(self):
        with self.assertRaises(ToolError):
            self.call("get_question", question_id=99999)


class TopicCoverageTest(AssistantToolsTestCase):
    def test_threshold_and_sort(self):
        out = self.call("topic_coverage", grade=None, max_questions=1)
        self.assertEqual(set(out), {"topics"})
        for row in out["topics"]:
            self.assertEqual(set(row), {"topic_slug", "title", "grade", "question_count"})
        counts = {r["topic_slug"]: r["question_count"] for r in out["topics"]}
        self.assertEqual(counts, {
            "operating-systems": 1, "binary-numbers": 0, "databases-intro": 0, "html-basics": 0,
        })
        ordered = [r["question_count"] for r in out["topics"]]
        self.assertEqual(ordered, sorted(ordered))

    def test_grade_filter(self):
        out = self.call("topic_coverage", grade=9, max_questions=10)
        rows = {r["topic_slug"]: r for r in out["topics"]}
        self.assertEqual(set(rows), {"input-output-devices", "operating-systems", "binary-numbers"})
        self.assertEqual(rows["input-output-devices"]["question_count"], 3)
        self.assertEqual(rows["binary-numbers"]["title"], "Двоична бройна система")
        self.assertTrue(all(r["grade"] == 9 for r in out["topics"]))
        self.assertEqual([r["topic_slug"] for r in out["topics"]][-1], "input-output-devices")

    def test_zero_threshold_lists_only_empty_topics(self):
        out = self.call("topic_coverage", grade=11, max_questions=0)
        self.assertEqual([r["topic_slug"] for r in out["topics"]], ["databases-intro"])

    def test_input_ranges(self):
        for bad in ({"grade": 7, "max_questions": 1}, {"grade": 13, "max_questions": 1},
                    {"grade": None, "max_questions": -1}):
            with self.subTest(input=bad), self.assertRaises(ToolError):
                self.call("topic_coverage", **bad)


class FindSimilarQuestionsTest(AssistantToolsTestCase):
    def test_same_grade_only_and_threshold(self):
        out = self.call("find_similar_questions", question_id=101, limit=10)
        self.assertEqual(set(out), {"similar"})
        ids = [r["id"] for r in out["similar"]]
        self.assertEqual(ids, [102])  # 105 has identical text but is grade 11
        row = out["similar"][0]
        self.assertEqual(set(row), {"id", "prompt", "score"})
        self.assertAlmostEqual(row["score"], 5 / 7, places=2)
        self.assertGreaterEqual(row["score"], 0.5)

    def test_question_never_matches_itself(self):
        out = self.call("find_similar_questions", question_id=102, limit=10)
        self.assertNotIn(102, [r["id"] for r in out["similar"]])
        self.assertEqual([r["id"] for r in out["similar"]], [101])

    def test_no_matches_below_threshold(self):
        out = self.call("find_similar_questions", question_id=106, limit=10)
        self.assertEqual(out["similar"], [])

    def test_missing_question_is_a_tool_error(self):
        with self.assertRaises(ToolError):
            self.call("find_similar_questions", question_id=99999, limit=5)

    def test_input_ranges(self):
        for limit in (0, 11):
            with self.subTest(limit=limit), self.assertRaises(ToolError):
                self.call("find_similar_questions", question_id=101, limit=limit)


class FlagQuestionTest(AssistantToolsTestCase):
    def test_appends_exactly_one_proposal_line(self):
        out = self.call("flag_question", question_id=102, reason="Почти същото като въпрос 101.")
        self.assertEqual(out, {"status": "proposed"})
        lines = read_jsonl(self.proposals_path)
        self.assertEqual(len(lines), 1)
        self.assertEqual(set(lines[0]), {"kind", "question_id", "reason", "created_at"})
        self.assertEqual(lines[0]["kind"], "flag")
        self.assertEqual(lines[0]["question_id"], 102)
        self.assertEqual(lines[0]["reason"], "Почти същото като въпрос 101.")
        self.assertEqual(lines[0]["created_at"], FIXED_NOW)

    def test_second_call_appends(self):
        self.call("flag_question", question_id=102, reason="Първа причина за флаг.")
        self.call("flag_question", question_id=103, reason="Втора причина за флаг.")
        self.assertEqual([l["question_id"] for l in read_jsonl(self.proposals_path)], [102, 103])

    def test_reason_length_is_validated(self):
        for reason in ("short", "x" * 501):
            with self.subTest(length=len(reason)), self.assertRaises(ToolError):
                self.call("flag_question", question_id=102, reason=reason)
        self.assertEqual(read_jsonl(self.proposals_path), [])


class ProposeTopicChangeTest(AssistantToolsTestCase):
    RATIONALE = "Въпросът е за формули, не за бази данни."

    def test_same_grade_move_appends_one_line(self):
        out = self.call("propose_topic_change", question_id=106, topic_slug="databases-intro",
                        rationale=self.RATIONALE)
        self.assertEqual(out, {"status": "proposed"})
        lines = read_jsonl(self.proposals_path)
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            set(lines[0]),
            {"kind", "question_id", "from_topic", "to_topic", "rationale", "created_at"},
        )
        self.assertEqual(lines[0]["kind"], "topic_change")
        self.assertEqual(lines[0]["question_id"], 106)
        self.assertEqual(lines[0]["from_topic"], "excel-formulas")
        self.assertEqual(lines[0]["to_topic"], "databases-intro")
        self.assertEqual(lines[0]["rationale"], self.RATIONALE)
        self.assertEqual(lines[0]["created_at"], FIXED_NOW)

    def test_higher_grade_move_is_allowed(self):
        out = self.call("propose_topic_change", question_id=106, topic_slug="html-basics",
                        rationale=self.RATIONALE)
        self.assertEqual(out, {"status": "proposed"})
        self.assertEqual(len(read_jsonl(self.proposals_path)), 1)

    def test_lower_grade_move_is_rejected(self):
        with self.assertRaises(ToolError):
            self.call("propose_topic_change", question_id=106, topic_slug="binary-numbers",
                      rationale=self.RATIONALE)
        self.assertEqual(read_jsonl(self.proposals_path), [])

    def test_unknown_topic_is_rejected(self):
        with self.assertRaises(ToolError):
            self.call("propose_topic_change", question_id=106, topic_slug="no-such-topic",
                      rationale=self.RATIONALE)
        self.assertEqual(read_jsonl(self.proposals_path), [])

    def test_rationale_length_is_validated(self):
        for rationale in ("short", "x" * 501):
            with self.subTest(length=len(rationale)), self.assertRaises(ToolError):
                self.call("propose_topic_change", question_id=106, topic_slug="databases-intro",
                          rationale=rationale)
        self.assertEqual(read_jsonl(self.proposals_path), [])


class DatabaseUnchangedTest(AssistantToolsTestCase):
    def test_db_bytes_unchanged_after_every_tool(self):
        before = file_sha256(self.db_path)
        self.call("search_questions", query="устройство", grade=None, limit=25)
        self.call("get_question", question_id=101)
        self.call("topic_coverage", grade=None, max_questions=5)
        self.call("find_similar_questions", question_id=101, limit=10)
        self.call("flag_question", question_id=102, reason="Почти същото като 101.")
        self.call("propose_topic_change", question_id=106, topic_slug="html-basics",
                  rationale="По-подходяща тема за въпроса.")
        for bad in (
            lambda: self.call("get_question", question_id=99999),
            lambda: self.call("propose_topic_change", question_id=106,
                              topic_slug="binary-numbers", rationale="Грешен клас за темата."),
        ):
            with self.assertRaises(ToolError):
                bad()
        self.conn.close()
        self.assertEqual(file_sha256(self.db_path), before)
        self.assertEqual(len(read_jsonl(self.proposals_path)), 2)

    def test_tool_error_is_an_exception(self):
        self.assertTrue(issubclass(tools.ToolError, Exception))


if __name__ == "__main__":
    unittest.main()

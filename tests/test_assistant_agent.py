"""Question Bank Assistant agent loop (design doc section 3.5) with a scripted fake client.

Every test scripts the router reply first, then the agent's model replies. No network; the
fixture database lives in a temporary directory and must stay byte-for-byte unchanged.
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.assistant import agent
from src.assistant.agent import AgentResult, run_agent
from src.assistant.skills import SKILL_LIBRARY
from src.assistant.tools import TOOLS, ToolContext, open_read_only
from tests.assistant_fakes import (
    FakeClient,
    build_fixture_db,
    file_sha256,
    fixed_now,
    read_jsonl,
    response,
    router_response,
    text_block,
    thinking_block,
    tool_use_block,
)


def result_text(tool_result):
    content = tool_result.get("content")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content)
    return content or ""


class AgentTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls.db_path = build_fixture_db(Path(cls._dir.name) / "fixture.db")
        cls.db_hash = file_sha256(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.proposals_path = self.work / "proposals.jsonl"
        self.audit_path = self.work / "audit.jsonl"
        self.conn = open_read_only(self.db_path)
        self.addCleanup(self.conn.close)
        self.ctx = ToolContext(conn=self.conn, proposals_path=self.proposals_path, now=fixed_now)
        self.confirm_calls = []
        self.confirm_answer = True

    def tearDown(self):
        self.conn.close()
        self.assertEqual(file_sha256(self.db_path), self.db_hash, "fixture DB changed")

    def confirm(self, name, tool_input):
        self.confirm_calls.append((name, tool_input))
        return self.confirm_answer

    def run_script(self, *responses, request="Помогни с банката с въпроси.", **kwargs):
        self.client = FakeClient(responses)
        result = run_agent(request, client=self.client, ctx=self.ctx, confirm=self.confirm,
                           audit_path=self.audit_path, **kwargs)
        self.assertIsInstance(result, AgentResult)
        self.assertEqual(self.client.script, [], "not every scripted response was used")
        return result

    def tool_results(self, call_index):
        """The tool_result blocks in the last user message of the n-th agent request."""
        messages = self.client.agent_calls[call_index]["messages"]
        last = messages[-1]
        self.assertEqual(last["role"], "user")
        self.assertIsInstance(last["content"], list)
        for block in last["content"]:
            self.assertEqual(block["type"], "tool_result")
        return last["content"]


class ConstantsTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(agent.MODEL, "claude-opus-5-5")
        self.assertEqual(agent.MAX_STEPS, 12)


class HappyPathTest(AgentTestCase):
    def test_read_tool_then_answer(self):
        tool_input = {"grade": 9, "max_questions": 1}
        result = self.run_script(
            router_response("coverage_audit", 0.9, "Coverage question."),
            response([tool_use_block("tu_1", "topic_coverage", tool_input)], "tool_use"),
            response([text_block("Темите с малко въпроси са "), text_block("две.")], "end_turn"),
            request="Кои теми за 9. клас имат най-много 1 въпрос?",
        )
        self.assertEqual(result.status, "done")
        self.assertEqual(result.skill, "coverage_audit")
        self.assertIn("Темите с малко въпроси са", result.answer)
        self.assertIn("две.", result.answer)
        self.assertEqual(result.proposals, 0)
        self.assertEqual(result.steps, 2)
        self.assertEqual(len(self.client.agent_calls), 2)
        self.assertEqual(self.confirm_calls, [])

        (tool_result,) = self.tool_results(1)
        self.assertEqual(tool_result["tool_use_id"], "tu_1")
        self.assertFalse(tool_result.get("is_error", False))
        expected = TOOLS["topic_coverage"].handler(self.ctx, tool_input)
        self.assertEqual(json.loads(result_text(tool_result)), expected)

    def test_offers_exactly_the_skill_tools_strict(self):
        for skill_name in SKILL_LIBRARY:
            with self.subTest(skill=skill_name):
                self.run_script(
                    router_response(skill_name, 0.9, "fits"),
                    response([text_block("Готово.")], "end_turn"),
                )
                (call,) = self.client.agent_calls
                offered = SKILL_LIBRARY[skill_name].offered_tools()
                self.assertEqual([t["name"] for t in call["tools"]], offered)
                for tool in call["tools"]:
                    self.assertIs(tool["strict"], True)
                    self.assertEqual(tool["input_schema"], TOOLS[tool["name"]].input_schema)
                    self.assertEqual(tool["description"], TOOLS[tool["name"]].description)
                system = call["system"]
                if not isinstance(system, str):
                    system = " ".join(block.get("text", "") for block in system)
                self.assertIn(SKILL_LIBRARY[skill_name].instructions, system)

    def test_topic_review_request_never_offers_flag_question(self):
        self.run_script(
            router_response("topic_review", 0.9, "topic"),
            response([text_block("ok")], "end_turn"),
        )
        names = [t["name"] for t in self.client.agent_calls[0]["tools"]]
        self.assertNotIn("flag_question", names)
        self.assertIn("propose_topic_change", names)

    def test_request_parameters(self):
        self.run_script(
            router_response("coverage_audit", 0.9, "Coverage."),
            response([tool_use_block("tu_1", "topic_coverage",
                                     {"grade": None, "max_questions": 0})], "tool_use"),
            response([text_block("ok")], "end_turn"),
        )
        for call in self.client.agent_calls:
            self.assertEqual(call["model"], "claude-opus-5-5")
            self.assertEqual(call["max_tokens"], 16000)
            self.assertEqual(call["thinking"], {"type": "adaptive"})
            self.assertEqual(call["output_config"], {"effort": "medium"})
            self.assertIn("server-side-fallback-2026-07-01", call["betas"])
            self.assertEqual(call["fallbacks"], "default")
            self.assertNotIn("tool_choice", call)
            self.assertNotIn("temperature", call)
        first_messages = self.client.agent_calls[0]["messages"]
        self.assertEqual(first_messages[0]["role"], "user")

    def test_assistant_content_goes_back_unchanged(self):
        thinking = thinking_block("Трябва да видя покритието.", "sig-xyz")
        first = response(
            [thinking, text_block("Ще проверя."),
             tool_use_block("tu_1", "topic_coverage", {"grade": 11, "max_questions": 0})],
            "tool_use",
        )
        original_content = list(first.content)
        self.run_script(
            router_response("coverage_audit", 0.9, "Coverage."),
            first,
            response([text_block("ok")], "end_turn"),
        )
        messages = self.client.agent_calls[1]["messages"]
        assistant_turns = [m for m in messages if m["role"] == "assistant"]
        self.assertEqual(len(assistant_turns), 1)
        self.assertEqual(list(assistant_turns[0]["content"]), original_content)
        self.assertEqual(assistant_turns[0]["content"][0].type, "thinking")
        self.assertEqual(assistant_turns[0]["content"][0].signature, "sig-xyz")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIs(messages[-2], assistant_turns[0])

    def test_pause_turn_continues(self):
        paused = response([thinking_block(), text_block("Работя...")], "pause_turn")
        result = self.run_script(
            router_response("coverage_audit", 0.9, "Coverage."),
            paused,
            response([text_block("Готово.")], "end_turn"),
        )
        self.assertEqual(result.status, "done")
        self.assertEqual(result.answer.strip(), "Готово.")
        second = self.client.agent_calls[1]["messages"]
        self.assertEqual(second[-1]["role"], "assistant")
        self.assertEqual(list(second[-1]["content"]), list(paused.content))

    def test_max_tokens_returns_partial_text(self):
        result = self.run_script(
            router_response("coverage_audit", 0.9, "Coverage."),
            response([text_block("Частичен отговор")], "max_tokens"),
        )
        self.assertEqual(result.status, "done")
        self.assertIn("Частичен отговор", result.answer)
        self.assertNotEqual(result.answer.strip(), "Частичен отговор")  # plus a note


class MultipleToolUsesTest(AgentTestCase):
    def test_two_tool_uses_return_in_one_user_message_in_order(self):
        result = self.run_script(
            router_response("duplicate_hunt", 0.9, "Duplicates."),
            response([
                tool_use_block("tu_a", "get_question", {"question_id": 101}),
                tool_use_block("tu_b", "find_similar_questions",
                               {"question_id": 101, "limit": 5}),
            ], "tool_use"),
            response([text_block("Въпрос 102 е почти дубликат.")], "end_turn"),
        )
        self.assertEqual(result.status, "done")
        results = self.tool_results(1)
        self.assertEqual([r["tool_use_id"] for r in results], ["tu_a", "tu_b"])
        self.assertEqual(json.loads(result_text(results[0]))["id"], 101)
        self.assertEqual([s["id"] for s in json.loads(result_text(results[1]))["similar"]], [102])
        user_turns = [m for m in self.client.agent_calls[1]["messages"] if m["role"] == "user"]
        self.assertEqual(len(user_turns), 2)  # the request, then one message with both results


class ConfirmTest(AgentTestCase):
    FLAG = {"question_id": 102, "reason": "Почти дубликат на въпрос 101."}

    def script(self):
        return (
            router_response("duplicate_hunt", 0.9, "Duplicates."),
            response([tool_use_block("tu_flag", "flag_question", dict(self.FLAG))], "tool_use"),
            response([text_block("Предложих флаг.")], "end_turn"),
        )

    def test_approved_proposal_is_written(self):
        self.confirm_answer = True
        result = self.run_script(*self.script())
        self.assertEqual(result.status, "done")
        self.assertEqual(result.proposals, 1)
        self.assertEqual(self.confirm_calls, [("flag_question", self.FLAG)])
        lines = read_jsonl(self.proposals_path)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["kind"], "flag")
        self.assertEqual(lines[0]["question_id"], 102)
        (tool_result,) = self.tool_results(1)
        self.assertEqual(tool_result["tool_use_id"], "tu_flag")
        self.assertFalse(tool_result.get("is_error", False))
        self.assertEqual(json.loads(result_text(tool_result)), {"status": "proposed"})

    def test_declined_proposal_is_not_written(self):
        self.confirm_answer = False
        result = self.run_script(*self.script())
        self.assertEqual(result.status, "done")
        self.assertEqual(result.proposals, 0)
        self.assertEqual(self.confirm_calls, [("flag_question", self.FLAG)])
        self.assertEqual(read_jsonl(self.proposals_path), [])
        (tool_result,) = self.tool_results(1)
        self.assertEqual(tool_result["tool_use_id"], "tu_flag")
        self.assertFalse(tool_result.get("is_error", False))
        self.assertIn("declined_by_operator", result_text(tool_result))
        self.assertEqual(json.loads(result_text(tool_result)), {"status": "declined_by_operator"})
        declined = [e for e in read_jsonl(self.audit_path)
                    if e["event"] == "tool_result" and e.get("tool") == "flag_question"]
        self.assertEqual(len(declined), 1)
        self.assertTrue(declined[0]["declined"])


class TrustEnforcementTest(AgentTestCase):
    def assert_refused(self, skill_name, tool_name, tool_input):
        result = self.run_script(
            router_response(skill_name, 0.9, "fits"),
            response([tool_use_block("tu_x", tool_name, tool_input)], "tool_use"),
            response([text_block("Разбрах.")], "end_turn"),
        )
        self.assertEqual(result.status, "done")
        self.assertEqual(result.proposals, 0)
        self.assertEqual(self.confirm_calls, [])
        self.assertEqual(read_jsonl(self.proposals_path), [])
        (tool_result,) = self.tool_results(1)
        self.assertEqual(tool_result["tool_use_id"], "tu_x")
        self.assertIs(tool_result.get("is_error"), True)
        self.assertIn("not available", result_text(tool_result))

    def test_forbidden_tool_is_refused(self):
        self.assert_refused("topic_review", "flag_question",
                            {"question_id": 102, "reason": "Опит за забранен флаг."})

    def test_unbound_tool_is_refused(self):
        self.assert_refused("coverage_audit", "flag_question",
                            {"question_id": 102, "reason": "Опит за несвързан инструмент."})

    def test_unbound_write_tool_is_refused(self):
        self.assert_refused("answer_check", "propose_topic_change",
                            {"question_id": 106, "topic_slug": "html-basics",
                             "rationale": "Опит за несвързан инструмент."})

    def test_unknown_tool_is_refused(self):
        self.assert_refused("coverage_audit", "drop_everything", {})


class ToolErrorTest(AgentTestCase):
    def test_tool_error_becomes_is_error_and_loop_continues(self):
        result = self.run_script(
            router_response("answer_check", 0.9, "Answer check."),
            response([tool_use_block("tu_1", "get_question", {"question_id": 99999})],
                     "tool_use"),
            response([tool_use_block("tu_2", "get_question", {"question_id": 101})],
                     "tool_use"),
            response([text_block("Отговорът е верен.")], "end_turn"),
        )
        self.assertEqual(result.status, "done")
        self.assertEqual(result.steps, 3)
        (err,) = self.tool_results(1)
        self.assertEqual(err["tool_use_id"], "tu_1")
        self.assertIs(err.get("is_error"), True)
        self.assertTrue(result_text(err).strip())
        (ok,) = self.tool_results(2)
        self.assertFalse(ok.get("is_error", False))

    def test_grade_rule_violation_reaches_model_as_error(self):
        self.confirm_answer = True
        result = self.run_script(
            router_response("topic_review", 0.9, "Topic."),
            response([tool_use_block("tu_1", "propose_topic_change",
                                     {"question_id": 106, "topic_slug": "binary-numbers",
                                      "rationale": "Опит за преместване в по-нисък клас."})],
                     "tool_use"),
            response([text_block("Не може.")], "end_turn"),
        )
        self.assertEqual(result.proposals, 0)
        self.assertEqual(read_jsonl(self.proposals_path), [])
        (err,) = self.tool_results(1)
        self.assertIs(err.get("is_error"), True)


class StopConditionsTest(AgentTestCase):
    def test_refusal(self):
        result = self.run_script(
            router_response("coverage_audit", 0.9, "Coverage."),
            response([text_block("Не мога да помогна.")], "refusal",
                     stop_details={"type": "refusal"}),
        )
        self.assertEqual(result.status, "refused")
        self.assertEqual(result.skill, "coverage_audit")
        self.assertEqual(len(self.client.agent_calls), 1)

    def test_step_limit(self):
        steps = 3
        loop = [
            response([tool_use_block(f"tu_{i}", "topic_coverage",
                                     {"grade": 9, "max_questions": i})], "tool_use")
            for i in range(steps)
        ]
        result = self.run_script(
            router_response("coverage_audit", 0.9, "Coverage."), *loop, max_steps=steps,
        )
        self.assertEqual(result.status, "step_limit")
        self.assertEqual(result.steps, steps)
        self.assertEqual(len(self.client.agent_calls), steps)
        finish = [e for e in read_jsonl(self.audit_path) if e["event"] == "finish"]
        self.assertEqual(finish[-1]["status"], "step_limit")

    def test_router_none_needs_clarification_without_agent_call(self):
        result = self.run_script(router_response("none", 0.9, "Не е ясно какво се иска."))
        self.assertEqual(result.status, "needs_clarification")
        self.assertIsNone(result.skill)
        self.assertEqual(result.proposals, 0)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.agent_calls, [])
        self.assertIn("Не е ясно какво се иска.", result.answer)
        for name in SKILL_LIBRARY:
            self.assertIn(name, result.answer)

    def test_low_confidence_needs_clarification(self):
        result = self.run_script(router_response("answer_check", 0.3, "Може би."))
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(len(self.client.calls), 1)


class AuditLogTest(AgentTestCase):
    def test_events(self):
        self.confirm_answer = True
        self.run_script(
            router_response("duplicate_hunt", 0.9, "Duplicates."),
            response([tool_use_block("tu_1", "find_similar_questions",
                                     {"question_id": 101, "limit": 5}),
                      tool_use_block("tu_2", "flag_question",
                                     {"question_id": 102, "reason": "Почти дубликат на 101."})],
                     "tool_use", input_tokens=123, output_tokens=45),
            response([text_block("Готово.")], "end_turn"),
        )
        events = read_jsonl(self.audit_path)
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds[0], "route")
        self.assertEqual(kinds[-1], "finish")
        self.assertTrue({"route", "model_call", "tool_call", "tool_result", "finish"} <= set(kinds))
        for event in events:
            self.assertIn("ts", event)

        model_calls = [e for e in events if e["event"] == "model_call"]
        self.assertEqual(len(model_calls), 2)
        self.assertEqual(model_calls[0]["stop_reason"], "tool_use")
        self.assertEqual(model_calls[0]["usage"]["input_tokens"], 123)
        self.assertEqual(model_calls[0]["usage"]["output_tokens"], 45)
        self.assertEqual(model_calls[1]["stop_reason"], "end_turn")

        tool_calls = [e for e in events if e["event"] == "tool_call"]
        self.assertEqual([e["tool"] for e in tool_calls],
                         ["find_similar_questions", "flag_question"])
        self.assertEqual([e["trust"] for e in tool_calls], ["read", "confirm"])
        self.assertEqual(tool_calls[0]["input"], {"question_id": 101, "limit": 5})

        tool_results = [e for e in events if e["event"] == "tool_result"]
        self.assertEqual([e["tool"] for e in tool_results],
                         ["find_similar_questions", "flag_question"])
        self.assertTrue(all(e["ok"] for e in tool_results))
        self.assertFalse(any(e["declined"] for e in tool_results))

        finish = events[-1]
        self.assertEqual(finish["status"], "done")
        self.assertEqual(finish["proposals"], 1)
        self.assertEqual(finish["steps"], 2)

    def test_refused_tool_is_logged(self):
        self.run_script(
            router_response("topic_review", 0.9, "Topic."),
            response([tool_use_block("tu_1", "flag_question",
                                     {"question_id": 102, "reason": "Забранен инструмент."})],
                     "tool_use"),
            response([text_block("ok")], "end_turn"),
        )
        events = read_jsonl(self.audit_path)
        results = [e for e in events if e["event"] == "tool_result" and e.get("tool") == "flag_question"]
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])


if __name__ == "__main__":
    unittest.main()

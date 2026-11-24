"""Question Bank Assistant skill library and router (design doc sections 3.3 and 3.4)."""

import unittest

from src.assistant.router import ROUTER_THRESHOLD, RouteDecision, route_request
from src.assistant.skills import (
    SKILL_LIBRARY,
    WRITE_TOOLS,
    Skill,
    ToolBinding,
    validate_library,
)
from src.assistant.tools import TOOLS
from src.assistant.trust import TrustLevel
from tests.assistant_fakes import (
    FakeClient,
    response,
    router_response,
    text_block,
)

READ, CONFIRM, FORBIDDEN = TrustLevel.READ, TrustLevel.CONFIRM, TrustLevel.FORBIDDEN

EXPECTED_BINDINGS = {
    "coverage_audit": [("topic_coverage", READ), ("search_questions", READ)],
    "answer_check": [("get_question", READ), ("search_questions", READ),
                     ("flag_question", CONFIRM)],
    "duplicate_hunt": [("search_questions", READ), ("get_question", READ),
                       ("find_similar_questions", READ), ("flag_question", CONFIRM)],
    "topic_review": [("get_question", READ), ("search_questions", READ),
                     ("topic_coverage", READ), ("propose_topic_change", CONFIRM),
                     ("flag_question", FORBIDDEN)],
}


def skill(name="probe", bindings=(ToolBinding("search_questions", READ),)):
    return Skill(name=name, description="Probe skill.", instructions="Probe.",
                 bindings=tuple(bindings))


class TrustLevelTest(unittest.TestCase):
    def test_values(self):
        self.assertEqual(READ.value, "read")
        self.assertEqual(CONFIRM.value, "confirm")
        self.assertEqual(FORBIDDEN.value, "forbidden")
        self.assertIsInstance(READ, str)


class SkillLibraryTest(unittest.TestCase):
    def test_library_validates(self):
        validate_library(SKILL_LIBRARY)  # does not raise

    def test_write_tools(self):
        self.assertEqual(WRITE_TOOLS, frozenset({"flag_question", "propose_topic_change"}))

    def test_library_matches_the_design(self):
        self.assertEqual(set(SKILL_LIBRARY), set(EXPECTED_BINDINGS))
        for name, expected in EXPECTED_BINDINGS.items():
            with self.subTest(skill=name):
                sk = SKILL_LIBRARY[name]
                self.assertEqual(sk.name, name)
                self.assertTrue(sk.description.strip())
                self.assertTrue(sk.instructions.strip())
                self.assertEqual([(b.tool, b.trust) for b in sk.bindings], expected)

    def test_offered_tools_exclude_forbidden_in_binding_order(self):
        for name, expected in EXPECTED_BINDINGS.items():
            with self.subTest(skill=name):
                self.assertEqual(
                    SKILL_LIBRARY[name].offered_tools(),
                    [tool for tool, trust in expected if trust is not FORBIDDEN],
                )

    def test_topic_review_does_not_offer_flag_question(self):
        topic_review = SKILL_LIBRARY["topic_review"]
        self.assertNotIn("flag_question", topic_review.offered_tools())
        self.assertIs(topic_review.trust_for("flag_question"), FORBIDDEN)

    def test_trust_for(self):
        answer_check = SKILL_LIBRARY["answer_check"]
        self.assertIs(answer_check.trust_for("get_question"), READ)
        self.assertIs(answer_check.trust_for("flag_question"), CONFIRM)
        self.assertIs(answer_check.trust_for("propose_topic_change"), FORBIDDEN)  # unbound
        self.assertIs(answer_check.trust_for("no_such_tool"), FORBIDDEN)

    def test_write_tools_are_never_bound_as_read(self):
        for sk in SKILL_LIBRARY.values():
            for binding in sk.bindings:
                if binding.tool in WRITE_TOOLS:
                    self.assertIsNot(binding.trust, READ, (sk.name, binding.tool))

    def test_every_offered_tool_schema_is_strict_compatible(self):
        for sk in SKILL_LIBRARY.values():
            for tool in sk.offered_tools():
                with self.subTest(skill=sk.name, tool=tool):
                    schema = TOOLS[tool].input_schema
                    self.assertIs(schema.get("additionalProperties"), False)
                    self.assertEqual(set(schema.get("required", [])), set(schema["properties"]))


class ValidateLibraryTest(unittest.TestCase):
    def test_accepts_a_minimal_valid_library(self):
        validate_library({"probe": skill()})

    def test_rejects_unknown_tool(self):
        bad = skill(bindings=[ToolBinding("search_questions", READ),
                              ToolBinding("drop_table", READ)])
        with self.assertRaises(ValueError):
            validate_library({"probe": bad})

    def test_rejects_read_binding_on_a_write_tool(self):
        for tool in ("flag_question", "propose_topic_change"):
            with self.subTest(tool=tool), self.assertRaises(ValueError):
                validate_library({"probe": skill(bindings=[ToolBinding(tool, READ)])})

    def test_rejects_duplicate_binding(self):
        bad = skill(bindings=[ToolBinding("get_question", READ),
                              ToolBinding("get_question", READ)])
        with self.assertRaises(ValueError):
            validate_library({"probe": bad})
        bad = skill(bindings=[ToolBinding("flag_question", CONFIRM),
                              ToolBinding("flag_question", FORBIDDEN)])
        with self.assertRaises(ValueError):
            validate_library({"probe": bad})

    def test_rejects_skill_with_no_offered_tools(self):
        with self.assertRaises(ValueError):
            validate_library({"probe": skill(bindings=[])})
        with self.assertRaises(ValueError):
            validate_library({"probe": skill(bindings=[ToolBinding("flag_question", FORBIDDEN)])})

    def test_trust_for_on_constructed_skill(self):
        sk = skill(bindings=[ToolBinding("search_questions", READ),
                             ToolBinding("flag_question", FORBIDDEN)])
        self.assertEqual(sk.offered_tools(), ["search_questions"])
        self.assertIs(sk.trust_for("flag_question"), FORBIDDEN)
        self.assertIs(sk.trust_for("get_question"), FORBIDDEN)


class RouterTest(unittest.TestCase):
    MODEL = "claude-opus-5-5"

    def route(self, *responses, request="Кои теми за 9. клас имат под 5 въпроса?"):
        client = FakeClient(responses)
        decision = route_request(client, request, SKILL_LIBRARY, model=self.MODEL)
        self.assertEqual(len(client.calls), 1)
        return decision, client.calls[0]

    def test_threshold(self):
        self.assertEqual(ROUTER_THRESHOLD, 0.6)

    def test_clear_request_routes_to_skill(self):
        decision, _ = self.route(router_response("coverage_audit", 0.92, "Coverage question."))
        self.assertIsInstance(decision, RouteDecision)
        self.assertEqual(decision.skill, "coverage_audit")
        self.assertAlmostEqual(decision.confidence, 0.92)
        self.assertEqual(decision.reason, "Coverage question.")

    def test_each_skill_can_be_routed(self):
        for name in SKILL_LIBRARY:
            with self.subTest(skill=name):
                decision, _ = self.route(router_response(name, 0.8, "fits"))
                self.assertEqual(decision.skill, name)

    def test_none_means_unclear(self):
        decision, _ = self.route(router_response("none", 0.95, "Not about the bank."))
        self.assertIsNone(decision.skill)

    def test_low_confidence_means_unclear(self):
        decision, _ = self.route(router_response("answer_check", ROUTER_THRESHOLD - 0.01, "unsure"))
        self.assertIsNone(decision.skill)

    def test_confidence_at_threshold_is_accepted(self):
        decision, _ = self.route(router_response("answer_check", ROUTER_THRESHOLD, "borderline"))
        self.assertEqual(decision.skill, "answer_check")

    def test_refusal_means_unclear(self):
        decision, _ = self.route(response([text_block("I can't help with that.")], "refusal"))
        self.assertIsNone(decision.skill)
        self.assertIsInstance(decision.reason, str)

    def test_request_uses_structured_output_schema(self):
        _, call = self.route(router_response("coverage_audit", 0.9, "ok"))
        self.assertEqual(call["model"], self.MODEL)
        fmt = call["output_config"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        schema = fmt["schema"]
        self.assertIs(schema.get("additionalProperties"), False)
        self.assertEqual(set(schema["required"]), {"skill", "confidence", "reason"})
        self.assertEqual(set(schema["properties"]), {"skill", "confidence", "reason"})
        self.assertEqual(set(schema["properties"]["skill"]["enum"]),
                         set(SKILL_LIBRARY) | {"none"})
        self.assertEqual(len(schema["properties"]["skill"]["enum"]), len(SKILL_LIBRARY) + 1)
        self.assertNotIn("tools", call)

    def test_system_prompt_lists_every_skill(self):
        _, call = self.route(router_response("coverage_audit", 0.9, "ok"))
        system = call["system"]
        if not isinstance(system, str):
            system = " ".join(block.get("text", "") for block in system)
        for sk in SKILL_LIBRARY.values():
            self.assertIn(sk.name, system)
            self.assertIn(sk.description, system)

    def test_request_text_is_sent(self):
        request = "Има ли дубликати на въпрос 101?"
        _, call = self.route(router_response("duplicate_hunt", 0.9, "ok"), request=request)
        self.assertIn(request, repr(call["messages"]))


if __name__ == "__main__":
    unittest.main()

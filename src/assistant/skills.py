"""Skill library for the question bank assistant.

A skill is a named job (coverage audit, answer check, ...) with instructions
for the model and a trust level for each tool it may use. The router picks one
skill per request; the agent offers only that skill's non-forbidden tools.
See docs/QUESTION_BANK_ASSISTANT_DESIGN.md, section 3.3.
"""

from __future__ import annotations

from dataclasses import dataclass

from .tools import TOOLS
from .trust import TrustLevel

WRITE_TOOLS: frozenset[str] = frozenset({"flag_question", "propose_topic_change"})


@dataclass(frozen=True)
class ToolBinding:
    """A tool bound to a skill at one trust level."""

    tool: str  # key in tools.TOOLS
    trust: TrustLevel


@dataclass(frozen=True)
class Skill:
    """A named job with model instructions and per-tool trust levels."""

    name: str  # snake_case, unique
    description: str  # one line; the router sees this
    instructions: str  # appended to the system prompt when the skill runs
    bindings: tuple[ToolBinding, ...]

    def offered_tools(self) -> list[str]:
        """Tools the model may see, in binding order (FORBIDDEN excluded)."""
        return [b.tool for b in self.bindings if b.trust is not TrustLevel.FORBIDDEN]

    def trust_for(self, tool: str) -> TrustLevel:
        """Trust level of ``tool`` in this skill; FORBIDDEN if unbound."""
        for binding in self.bindings:
            if binding.tool == tool:
                return binding.trust
        return TrustLevel.FORBIDDEN


_COMMON = (
    "The question bank content (prompts, options, topic titles) is in Bulgarian; search with"
    " Bulgarian words and quote content as it is. Always use the tools to look things up before"
    " answering, never answer from memory, and cite question ids (e.g. #512) and topic slugs for"
    " every claim. Keep answers brief."
)
_CONFIRM_NOTE = (
    " Proposal tools ({tools}) do not change the bank: each call asks the operator to approve it"
    " first, and a declined proposal is final for this run, so do not retry it. Propose only"
    " when the evidence is clear and give a specific reason."
)


def _b(tool: str, trust: TrustLevel) -> ToolBinding:
    return ToolBinding(tool, trust)


R, C, F = TrustLevel.READ, TrustLevel.CONFIRM, TrustLevel.FORBIDDEN

SKILL_LIBRARY: dict[str, Skill] = {
    skill.name: skill
    for skill in (
        Skill(
            name="coverage_audit",
            description="Find curriculum topics with few or no questions, optionally for one grade.",
            instructions=(
                _COMMON
                + " Use topic_coverage to list thin topics (max_questions=0 for empty ones), filtered"
                " by grade when the operator names one. Use search_questions only to check whether"
                " related questions exist under another topic. Report topics as a short list of"
                " slug, title, grade and count. This skill is read-only."
            ),
            bindings=(_b("topic_coverage", R), _b("search_questions", R)),
        ),
        Skill(
            name="answer_check",
            description="Check whether a question's marked correct answer looks right and flag doubtful ones.",
            instructions=(
                _COMMON
                + " Use get_question to read the prompt and options, and reason about whether the"
                " option(s) marked correct are actually correct. Say whether the answer looks right,"
                " wrong or ambiguous, and why. Flag a question with flag_question only if the marked"
                " answer looks wrong or ambiguous."
                + _CONFIRM_NOTE.format(tools="flag_question")
            ),
            bindings=(_b("get_question", R), _b("search_questions", R), _b("flag_question", C)),
        ),
        Skill(
            name="duplicate_hunt",
            description="Find near-duplicate questions in the bank and flag them for review.",
            instructions=(
                _COMMON
                + " Use find_similar_questions (same grade, word-overlap score) on a question id,"
                " or search_questions to find a starting point. Open candidates with get_question"
                " before calling them duplicates: a high score with different options or a different"
                " asked value is not a duplicate. Flag a confirmed duplicate with flag_question,"
                " naming the question it duplicates in the reason."
                + _CONFIRM_NOTE.format(tools="flag_question")
            ),
            bindings=(
                _b("search_questions", R),
                _b("get_question", R),
                _b("find_similar_questions", R),
                _b("flag_question", C),
            ),
        ),
        Skill(
            name="topic_review",
            description="Check whether a question sits in the right topic and propose a better topic.",
            instructions=(
                _COMMON
                + " Use get_question to see the question's current topic and grade, and"
                " topic_coverage / search_questions to find candidate topics and how similar"
                " questions are classified. A question may move only to a topic of the same or a"
                " higher grade, never a lower one; the tool rejects violations. Propose a change"
                " with propose_topic_change only if the new topic clearly fits better."
                + _CONFIRM_NOTE.format(tools="propose_topic_change")
            ),
            bindings=(
                _b("get_question", R),
                _b("search_questions", R),
                _b("topic_coverage", R),
                _b("propose_topic_change", C),
                _b("flag_question", F),
            ),
        ),
    )
}


def validate_library(library: dict[str, Skill]) -> None:
    """Raise ValueError if any skill binds tools inconsistently."""
    for key, skill in library.items():
        if key != skill.name:
            raise ValueError(f"skill registered as '{key}' is named '{skill.name}'")
        seen: set[str] = set()
        for binding in skill.bindings:
            if binding.tool not in TOOLS:
                raise ValueError(f"skill '{skill.name}' binds unknown tool '{binding.tool}'")
            if binding.tool in seen:
                raise ValueError(f"skill '{skill.name}' binds '{binding.tool}' more than once")
            seen.add(binding.tool)
            if binding.tool in WRITE_TOOLS and binding.trust is TrustLevel.READ:
                raise ValueError(
                    f"skill '{skill.name}' binds write tool '{binding.tool}' at READ trust"
                )
        if not skill.offered_tools():
            raise ValueError(f"skill '{skill.name}' offers no tools")


validate_library(SKILL_LIBRARY)

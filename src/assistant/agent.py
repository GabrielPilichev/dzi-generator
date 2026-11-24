"""Manual agent loop: route, offer the skill's tools, enforce trust, audit every step."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.assistant.router import route_request
from src.assistant.skills import SKILL_LIBRARY, Skill
from src.assistant.tools import TOOLS, ToolContext, ToolError
from src.assistant.trust import TrustLevel

MODEL = "claude-opus-5-5"
MAX_STEPS = 12
MAX_TOKENS = 16000

BASE_SYSTEM_PROMPT = (
    "You are an assistant for the operator of the LearnPilot question bank, a database of "
    "school IT curriculum questions (grades 8-12); the question content is mostly Bulgarian. "
    "Answer the operator in the language they write in. Use the tools to look things up "
    "instead of guessing, and cite question ids for every question you mention. You cannot "
    "change the database: a write tool only records a proposal for human review, and only "
    "after the operator approves it. Never claim a change was made; say it was proposed, or "
    "that the operator declined it."
)

Confirm = Callable[[str, dict], bool]


@dataclass
class AgentResult:
    """Outcome of one ``run_agent`` call."""

    status: str  # "done" | "needs_clarification" | "step_limit" | "refused"
    skill: str | None
    answer: str
    proposals: int
    steps: int


class _Audit:
    """Appends one JSON line per event to the audit log."""

    def __init__(self, path: Path, now: Callable[[], str]) -> None:
        self.path = Path(path)
        self.now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **fields: Any) -> None:
        record = {"ts": self.now(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _text_of(content: Any) -> str:
    return "\n\n".join(b.text for b in content if getattr(b, "type", None) == "text" and b.text).strip()


def _usage(response: Any) -> dict[str, int | None]:
    usage = getattr(response, "usage", None)
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }


def _refusal_answer(response: Any) -> str:
    details = getattr(response, "stop_details", None)
    explanation = getattr(details, "explanation", None) if details is not None else None
    category = getattr(details, "category", None) if details is not None else None
    text = _text_of(response.content)
    parts = [p for p in (text, f"Refusal ({category}): {explanation}" if (category or explanation) else "") if p]
    return "\n\n".join(parts) or "The model declined this request."


def _skill_list(library: dict[str, Skill]) -> str:
    return "\n".join(f"- {s.name}: {s.description}" for s in library.values())


def build_tools(skill: Skill) -> list[dict[str, Any]]:
    """Tool definitions for the skill's offered (non-forbidden) tools, all strict."""
    tools = []
    for name in skill.offered_tools():
        spec = TOOLS[name]
        tools.append(
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_schema,
                "strict": True,
            }
        )
    return tools


def run_agent(
    request: str,
    *,
    client: Any,
    ctx: ToolContext,
    confirm: Confirm,
    audit_path: Path,
    library: dict[str, Skill] = SKILL_LIBRARY,
    model: str = MODEL,
    max_steps: int = MAX_STEPS,
) -> AgentResult:
    """Route ``request`` to a skill and run the tool loop until done, refused or the step cap."""
    audit = _Audit(audit_path, ctx.now)
    proposals = 0
    steps = 0

    def finish(status: str, skill: str | None, answer: str) -> AgentResult:
        audit.log("finish", status=status, proposals=proposals, steps=steps)
        return AgentResult(status=status, skill=skill, answer=answer, proposals=proposals, steps=steps)

    decision = route_request(client, request, library, model=model)
    audit.log("route", skill=decision.skill, confidence=decision.confidence, reason=decision.reason)
    if decision.skill is None:
        answer = (
            f"{decision.reason}\n\nPlease rephrase the request so it matches one of these skills:\n"
            f"{_skill_list(library)}"
        )
        return finish("needs_clarification", None, answer)

    skill = library[decision.skill]
    system = f"{BASE_SYSTEM_PROMPT}\n\n## Skill: {skill.name}\n{skill.instructions}"
    tools = build_tools(skill)
    messages: list[dict[str, Any]] = [{"role": "user", "content": request}]
    last_text = ""

    while steps < max_steps:
        response = client.beta.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=system,
            tools=tools,
            messages=messages,
        )
        steps += 1
        audit.log("model_call", stop_reason=response.stop_reason, usage=_usage(response))
        messages.append({"role": "assistant", "content": response.content})
        text = _text_of(response.content)
        if text:
            last_text = text

        if response.stop_reason == "refusal":
            return finish("refused", skill.name, _refusal_answer(response))
        if response.stop_reason == "end_turn":
            return finish("done", skill.name, text)
        if response.stop_reason == "max_tokens":
            note = "[Note: the answer was cut off at the output token limit.]"
            return finish("done", skill.name, f"{text}\n\n{note}".strip())
        if response.stop_reason == "pause_turn":
            continue

        tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            return finish("done", skill.name, text)
        if steps >= max_steps:
            break  # the model would never see these results; do not run them

        results = []
        for block in tool_uses:
            output, is_error, ran_ok = _run_tool(block, skill, ctx, confirm, audit)
            if ran_ok and skill.trust_for(block.name) is TrustLevel.CONFIRM:
                proposals += 1
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(output, ensure_ascii=False),
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": results})

    note = f"[Stopped after {max_steps} model calls without a final answer.]"
    return finish("step_limit", skill.name, f"{last_text}\n\n{note}".strip())


def _run_tool(
    block: Any, skill: Skill, ctx: ToolContext, confirm: Confirm, audit: _Audit
) -> tuple[dict[str, Any], bool, bool]:
    """Enforce trust for one tool_use block. Returns (output, is_error, handler_succeeded)."""
    name = block.name
    tool_input = dict(block.input or {})
    spec = TOOLS.get(name)
    trust = skill.trust_for(name) if spec is not None else TrustLevel.FORBIDDEN
    audit.log("tool_call", tool=name, trust=trust.value, input=tool_input)

    if trust is TrustLevel.FORBIDDEN:
        audit.log("tool_result", tool=name, ok=False, declined=False)
        return {"error": f"tool not available for this skill: {name}"}, True, False

    if trust is TrustLevel.CONFIRM and not confirm(name, tool_input):
        audit.log("tool_result", tool=name, ok=False, declined=True)
        return {"status": "declined_by_operator"}, False, False

    try:
        output = spec.handler(ctx, tool_input)
    except ToolError as exc:
        audit.log("tool_result", tool=name, ok=False, declined=False, error=str(exc))
        return {"error": str(exc)}, True, False
    audit.log("tool_result", tool=name, ok=True, declined=False)
    return output, False, True

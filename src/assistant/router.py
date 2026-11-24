"""Pick one skill per operator request with a structured-output model call."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.assistant.skills import Skill

ROUTER_THRESHOLD = 0.6
ROUTER_MAX_TOKENS = 2000

_ROUTER_PROMPT = (
    "You route operator requests for the LearnPilot question bank assistant "
    "(question content is mostly Bulgarian). Pick exactly one skill that fits the "
    "request, or \"none\" if no skill fits or the request is unclear. Give a "
    "confidence between 0 and 1 and a one-sentence reason in the operator's language.\n\n"
    "Skills:\n"
)


@dataclass(frozen=True)
class RouteDecision:
    """The router's choice; ``skill is None`` means ask the operator."""

    skill: str | None
    confidence: float
    reason: str


def router_schema(library: dict[str, Skill]) -> dict[str, Any]:
    """JSON Schema for the router's structured output."""
    return {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "enum": [*library.keys(), "none"]},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["skill", "confidence", "reason"],
        "additionalProperties": False,
    }


def router_system_prompt(library: dict[str, Skill]) -> str:
    """System prompt listing every skill's name and description."""
    lines = [f"- {skill.name}: {skill.description}" for skill in library.values()]
    return _ROUTER_PROMPT + "\n".join(lines)


def _refusal_reason(response: Any) -> str:
    details = getattr(response, "stop_details", None)
    parts = [
        str(value)
        for value in (
            getattr(details, "category", None) if details is not None else None,
            getattr(details, "explanation", None) if details is not None else None,
        )
        if value
    ]
    return "Request refused by the model" + (f": {' - '.join(parts)}" if parts else ".")


def route_request(client: Any, request: str, library: dict[str, Skill], *, model: str) -> RouteDecision:
    """Ask the model which skill fits ``request``; below threshold returns ``skill=None``."""
    response = client.beta.messages.create(
        model=model,
        max_tokens=ROUTER_MAX_TOKENS,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": router_schema(library)},
        },
        system=router_system_prompt(library),
        messages=[{"role": "user", "content": request}],
    )
    if response.stop_reason == "refusal":
        return RouteDecision(skill=None, confidence=0.0, reason=_refusal_reason(response))

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        return RouteDecision(skill=None, confidence=0.0, reason="Router returned no decision.")
    try:
        data = json.loads(text)
        skill = str(data["skill"])
        confidence = max(0.0, min(1.0, float(data["confidence"])))
        reason = str(data["reason"])
    except (ValueError, KeyError, TypeError):
        return RouteDecision(skill=None, confidence=0.0, reason="Router returned an unreadable decision.")

    if skill == "none" or skill not in library or confidence < ROUTER_THRESHOLD:
        return RouteDecision(skill=None, confidence=confidence, reason=reason)
    return RouteDecision(skill=skill, confidence=confidence, reason=reason)

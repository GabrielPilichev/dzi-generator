"""Tools the question bank assistant can call.

READ tools query the bank through a read-only SQLite connection. Write tools
never touch the database: they append one proposal per call to a JSONL review
file, which a separate, reviewed step may apply later.

Every ``input_schema`` satisfies strict tool use: ``additionalProperties`` is
false and ``required`` lists every property. Numeric and length limits are
checked in the handlers and reported as ``ToolError``.
See docs/QUESTION_BANK_ASSISTANT_DESIGN.md, section 3.2.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

MIN_GRADE = 8
MAX_GRADE = 12
MIN_TEXT = 10
MAX_TEXT = 500
SIMILARITY_THRESHOLD = 0.5

# Grade of a question: class of the section of its topic (NULL without a topic).
_QUESTION_SELECT = """
    SELECT q.id, q.prompt, q.question_type, q.is_ai_generated,
           t.topic_slug, t.title_bg AS topic_title, s.class AS grade
    FROM questions q
    LEFT JOIN curriculum_topics t ON t.id = q.topic_id
    LEFT JOIN curriculum_sections s ON s.id = t.section_id
"""


class ToolError(Exception):
    """Bad input or missing record; becomes an ``is_error`` tool result."""


@dataclass
class ToolContext:
    """What a handler may use: a read-only connection and the proposals file."""

    conn: sqlite3.Connection  # opened read-only: file:<db>?mode=ro
    proposals_path: Path  # JSONL file proposals are appended to
    now: Callable[[], str]  # ISO timestamp provider (tests inject a fixed clock)


@dataclass(frozen=True)
class ToolSpec:
    """One tool: its name, model-facing description, schema and handler."""

    name: str
    description: str
    input_schema: dict
    handler: Callable[[ToolContext, dict], dict]


def open_read_only(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` in SQLite read-only URI mode; any write raises."""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Input validation


def _int(args: dict, key: str, *, low: int | None = None, high: int | None = None) -> int:
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"'{key}' must be an integer")
    if low is not None and value < low:
        raise ToolError(f"'{key}' must be at least {low}, got {value}")
    if high is not None and value > high:
        raise ToolError(f"'{key}' must be at most {high}, got {value}")
    return value


def _grade(args: dict, key: str = "grade") -> int | None:
    if args.get(key) is None:
        return None
    return _int(args, key, low=MIN_GRADE, high=MAX_GRADE)


def _text(args: dict, key: str, *, low: int = 1, high: int | None = None) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise ToolError(f"'{key}' must be a string")
    value = value.strip()
    if len(value) < low:
        raise ToolError(
            f"'{key}' must not be empty" if low == 1 else f"'{key}' must be at least {low} characters"
        )
    if high is not None and len(value) > high:
        raise ToolError(f"'{key}' must be at most {high} characters, got {len(value)}")
    return value


def _schema(properties: dict[str, Any]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_NULLABLE_GRADE = {
    "type": ["integer", "null"],
    "description": f"School grade {MIN_GRADE}-{MAX_GRADE}, or null for all grades.",
}
_QUESTION_ID = {"type": "integer", "description": "Question id."}


def _load_question(conn: sqlite3.Connection, question_id: int) -> sqlite3.Row:
    row = conn.execute(_QUESTION_SELECT + " WHERE q.id = ?", (question_id,)).fetchone()
    if row is None:
        raise ToolError(f"question {question_id} does not exist")
    return row


def _append_proposal(ctx: ToolContext, record: dict) -> None:
    path = Path(ctx.proposals_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _tokens(text: str | None) -> set[str]:
    return set(re.findall(r"\w+", (text or "").lower()))


# ---------------------------------------------------------------------------
# READ handlers


def search_questions(ctx: ToolContext, args: dict) -> dict:
    """Case-insensitive substring search over question prompts."""
    query = _text(args, "query")
    grade = _grade(args)
    limit = _int(args, "limit", low=1, high=25)
    sql = _QUESTION_SELECT
    params: list[Any] = []
    if grade is not None:
        sql += " WHERE s.class = ?"
        params.append(grade)
    sql += " ORDER BY q.id"
    # SQLite LIKE folds ASCII only; the bank is Bulgarian, so fold in Python.
    needle = query.casefold()
    results = []
    for row in ctx.conn.execute(sql, params):
        if needle in (row["prompt"] or "").casefold():
            results.append(
                {
                    "id": row["id"],
                    "prompt": row["prompt"],
                    "question_type": row["question_type"],
                    "grade": row["grade"],
                    "topic_slug": row["topic_slug"],
                }
            )
            if len(results) >= limit:
                break
    return {"results": results}


def get_question(ctx: ToolContext, args: dict) -> dict:
    """One question with its topic, grade and answer options."""
    question_id = _int(args, "question_id")
    row = _load_question(ctx.conn, question_id)
    options = [
        {"letter": opt["option_letter"], "text": opt["option_text"], "is_correct": bool(opt["is_correct"])}
        for opt in ctx.conn.execute(
            "SELECT option_letter, option_text, is_correct FROM multiple_choice_options"
            " WHERE question_id = ? ORDER BY option_letter",
            (question_id,),
        )
    ]
    return {
        "id": row["id"],
        "prompt": row["prompt"],
        "question_type": row["question_type"],
        "grade": row["grade"],
        "topic_slug": row["topic_slug"],
        "topic_title": row["topic_title"],
        "options": options,
        "is_ai_generated": bool(row["is_ai_generated"]),
    }


def topic_coverage(ctx: ToolContext, args: dict) -> dict:
    """Topics with at most ``max_questions`` questions, thinnest first."""
    grade = _grade(args)
    max_questions = _int(args, "max_questions", low=0)
    sql = """
        SELECT t.topic_slug, t.title_bg AS title, s.class AS grade,
               COUNT(q.id) AS question_count
        FROM curriculum_topics t
        LEFT JOIN curriculum_sections s ON s.id = t.section_id
        LEFT JOIN questions q ON q.topic_id = t.id
    """
    params: list[Any] = []
    if grade is not None:
        sql += " WHERE s.class = ?"
        params.append(grade)
    sql += """
        GROUP BY t.id
        HAVING COUNT(q.id) <= ?
        ORDER BY question_count, t.topic_slug
    """
    params.append(max_questions)
    topics = [dict(row) for row in ctx.conn.execute(sql, params)]
    return {"topics": topics}


def find_similar_questions(ctx: ToolContext, args: dict) -> dict:
    """Near-duplicates of a question by token Jaccard within the same grade."""
    question_id = _int(args, "question_id")
    limit = _int(args, "limit", low=1, high=10)
    source = _load_question(ctx.conn, question_id)
    source_tokens = _tokens(source["prompt"])
    if source["grade"] is None:
        where, params = " WHERE s.class IS NULL AND q.id != ?", [question_id]
    else:
        where, params = " WHERE s.class = ? AND q.id != ?", [source["grade"], question_id]
    similar = []
    if source_tokens:
        for row in ctx.conn.execute(_QUESTION_SELECT + where, params):
            tokens = _tokens(row["prompt"])
            if not tokens:
                continue
            score = len(source_tokens & tokens) / len(source_tokens | tokens)
            if score >= SIMILARITY_THRESHOLD:
                similar.append({"id": row["id"], "prompt": row["prompt"], "score": round(score, 3)})
    similar.sort(key=lambda item: (-item["score"], item["id"]))
    return {"similar": similar[:limit]}


# ---------------------------------------------------------------------------
# CONFIRM handlers (proposals only; never the database)


def flag_question(ctx: ToolContext, args: dict) -> dict:
    """Propose that a question be reviewed by a human."""
    question_id = _int(args, "question_id")
    reason = _text(args, "reason", low=MIN_TEXT, high=MAX_TEXT)
    _load_question(ctx.conn, question_id)
    _append_proposal(
        ctx,
        {"kind": "flag", "question_id": question_id, "reason": reason, "created_at": ctx.now()},
    )
    return {"status": "proposed"}


def propose_topic_change(ctx: ToolContext, args: dict) -> dict:
    """Propose moving a question to a topic of the same or a higher grade."""
    question_id = _int(args, "question_id")
    topic_slug = _text(args, "topic_slug")
    rationale = _text(args, "rationale", low=MIN_TEXT, high=MAX_TEXT)
    question = _load_question(ctx.conn, question_id)
    target = ctx.conn.execute(
        "SELECT t.topic_slug, s.class AS grade FROM curriculum_topics t"
        " LEFT JOIN curriculum_sections s ON s.id = t.section_id WHERE t.topic_slug = ?",
        (topic_slug,),
    ).fetchone()
    if target is None:
        raise ToolError(f"topic '{topic_slug}' does not exist")
    if question["topic_slug"] == topic_slug:
        raise ToolError(f"question {question_id} is already in topic '{topic_slug}'")
    current_grade = question["grade"]
    if current_grade is not None:
        if target["grade"] is None:
            raise ToolError(
                f"topic '{topic_slug}' has no grade, so the grade rule cannot be checked"
                f" for question {question_id} (grade {current_grade})"
            )
        if target["grade"] < current_grade:
            raise ToolError(
                f"grade rule: question {question_id} is grade {current_grade} and cannot move"
                f" to topic '{topic_slug}' of lower grade {target['grade']};"
                " only the same or a higher grade is allowed"
            )
    _append_proposal(
        ctx,
        {
            "kind": "topic_change",
            "question_id": question_id,
            "from_topic": question["topic_slug"],
            "to_topic": topic_slug,
            "rationale": rationale,
            "created_at": ctx.now(),
        },
    )
    return {"status": "proposed"}


# ---------------------------------------------------------------------------
# Registry

_SPECS = (
    ToolSpec(
        name="search_questions",
        description=(
            "Search question prompts for a substring (case-insensitive). The bank is in Bulgarian,"
            " so search with Bulgarian words. Optionally filter by grade (8-12). limit is 1-25."
            " Returns id, prompt, question_type, grade and topic_slug for each match."
        ),
        input_schema=_schema(
            {
                "query": {"type": "string", "description": "Text to look for in the prompt."},
                "grade": _NULLABLE_GRADE,
                "limit": {"type": "integer", "description": "Maximum results, 1-25."},
            }
        ),
        handler=search_questions,
    ),
    ToolSpec(
        name="get_question",
        description=(
            "Get one question by id: prompt, type, grade, topic, answer options with the one(s)"
            " marked correct, and whether it was AI-generated."
        ),
        input_schema=_schema({"question_id": _QUESTION_ID}),
        handler=get_question,
    ),
    ToolSpec(
        name="topic_coverage",
        description=(
            "List curriculum topics that have at most max_questions questions, thinnest first."
            " Use max_questions=0 for empty topics. Optionally filter by grade (8-12)."
        ),
        input_schema=_schema(
            {
                "grade": _NULLABLE_GRADE,
                "max_questions": {"type": "integer", "description": "Upper bound on question count, >= 0."},
            }
        ),
        handler=topic_coverage,
    ),
    ToolSpec(
        name="find_similar_questions",
        description=(
            "Find near-duplicates of a question in the same grade by word overlap (Jaccard >= 0.5)."
            " limit is 1-10. Returns id, prompt and score, best first."
        ),
        input_schema=_schema(
            {
                "question_id": _QUESTION_ID,
                "limit": {"type": "integer", "description": "Maximum results, 1-10."},
            }
        ),
        handler=find_similar_questions,
    ),
    ToolSpec(
        name="flag_question",
        description=(
            "Propose that a human review a question (for example a doubtful marked answer or a"
            " duplicate). Writes a proposal only after the operator approves; never edits the bank."
            f" reason is {MIN_TEXT}-{MAX_TEXT} characters."
        ),
        input_schema=_schema(
            {
                "question_id": _QUESTION_ID,
                "reason": {"type": "string", "description": f"Why to flag it, {MIN_TEXT}-{MAX_TEXT} characters."},
            }
        ),
        handler=flag_question,
    ),
    ToolSpec(
        name="propose_topic_change",
        description=(
            "Propose moving a question to another topic. The target topic must exist and be of the"
            " same or a higher grade than the question's current grade. Writes a proposal only after"
            f" the operator approves; never edits the bank. rationale is {MIN_TEXT}-{MAX_TEXT} characters."
        ),
        input_schema=_schema(
            {
                "question_id": _QUESTION_ID,
                "topic_slug": {"type": "string", "description": "Slug of the target topic."},
                "rationale": {
                    "type": "string",
                    "description": f"Why the new topic fits better, {MIN_TEXT}-{MAX_TEXT} characters.",
                },
            }
        ),
        handler=propose_topic_change,
    ),
)

TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in _SPECS}

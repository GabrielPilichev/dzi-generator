"""DZI open-question ownership and generation readiness, without application state.

Curriculum topic memberships describe content, not exam-pool ownership. Display
queries deliberately do not use this generation-only filter.
"""

from __future__ import annotations

import json
import sqlite3

from src.subject_content import IT, canonical_subject


VISUAL_DEPENDENT_PATTERNS = (
    "изображението", "на изображението", "даденото изображение",
    "следното изображение", "показаното изображение", "диаграмата",
    "диаграма е представена", "графиката", "разгледайте графиката",
    "таблицата по-долу", "фигурата", "в диаграмата", "показана диаграма",
    "показаната таблица", "показаната диаграма", "дадената таблица",
    "даден е фрагмент от електронна таблица",
)


def _get(row, key, default=None):
    if row is None:
        return default
    if hasattr(row, "keys"):
        try:
            return row[key]
        except (IndexError, KeyError):
            return default
    return getattr(row, key, default)


def _real_answer(value) -> bool:
    text = str(value or "").strip()
    return bool(text) and text not in {"-", "—", "[]"}


def answer_values(value) -> list[str]:
    """Preserve the quiz's existing accepted-answer parsing semantics."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if _real_answer(item)]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return [text] if _real_answer(text) else []
    if isinstance(parsed, (list, tuple, set)):
        return [str(item) for item in parsed if _real_answer(item)]
    return [str(parsed)] if _real_answer(parsed) else []


def has_accepted_answers(subquestions) -> bool:
    rows = list(subquestions or [])
    return bool(rows) and all(
        any(answer_values(_get(row, field)) for field in (
            "accepted_answers", "correct_answers", "correct_answer", "answer_alternatives"
        )) for row in rows
    )


def is_open_question_generation_ready(question, subquestions) -> bool:
    """Existing auto-gradable safeguards; ownership is checked separately."""
    if _get(question, "question_type") not in {"fill_in", "short_answer"}:
        return False
    for field in ("source_number", "task_number"):
        try:
            number = int(_get(question, field))
        except (TypeError, ValueError):
            continue
        if number in {26, 27, 28}:
            return False
    prompt = (_get(question, "prompt", "") or "").lower()
    if any(pattern in prompt for pattern in VISUAL_DEPENDENT_PATTERNS):
        return False
    return has_accepted_answers(subquestions)


def fetch_dzi_open_questions(conn: sqlite3.Connection, *, source_slug=None) -> list[dict]:
    """Generation-ready IT DZI pool; source can only narrow the owned pool.

    The linked exam is the canonical exam identity. Legacy records need not
    have an exam-task link; task skeleton kinds are not question ownership.
    No topic or school-grade assignment is required.
    """
    rows = conn.execute("""
        SELECT q.*, e.subject AS exam_subject, e.year AS exam_year
        FROM questions q
        JOIN exams e ON e.id = q.exam_id
        WHERE q.question_type IN ('fill_in', 'short_answer')
          AND q.level = 'DZI' AND e.level = 'DZI'
        ORDER BY q.source_exam, q.source_number, q.id
    """).fetchall()
    candidates = []
    for row in rows:
        if (canonical_subject(row["subject"]) != IT
                or canonical_subject(row["exam_subject"]) != IT
                or row["year"] != row["exam_year"]):
            continue
        subquestions = [dict(sub) for sub in conn.execute("""
            SELECT id, subquestion_number, correct_answer, answer_alternatives
            FROM fill_in_subquestions WHERE question_id = ?
            ORDER BY subquestion_number
        """, (row["id"],))]
        if not is_open_question_generation_ready(row, subquestions):
            continue
        if source_slug is not None and row["source_exam"] != source_slug:
            continue
        candidates.append({**dict(row), "subquestions": subquestions})
    return candidates

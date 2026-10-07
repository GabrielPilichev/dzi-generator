"""Quiz domain services: assignment/attempt data access, question selection,
eligibility, grading, result shaping and vault notes.

This module never imports ``web.app`` or a blueprint (the dependency points
``web.app`` / ``web.blueprints.quiz`` -> here). Connections come from the shared
``web.db`` policy and callers close them; this module owns no transaction
boundary beyond what each function already did in ``web.app``.

Runtime locations (database, vault, project root) are not resolved here:
the composition root registers them with :func:`configure_runtime` as zero-argument
callables that are evaluated on every use, so the module globals that tests and
deployments reconfigure in ``web.app`` keep controlling where quiz code reads
and writes.
"""

from __future__ import annotations

import hashlib as _quiz_hashlib
import json as _quiz_json
import random as _quiz_random
import re as _quiz_re
import sqlite3
import sys as _quiz_sys
import unicodedata as _quiz_unicodedata
from datetime import datetime as _quiz_datetime, timezone as _quiz_timezone
from pathlib import Path as _QuizPath
from contextlib import contextmanager
from typing import Callable, NamedTuple

from flask import url_for as _quiz_url_for

from src.dzi_open_questions import (
    VISUAL_DEPENDENT_PATTERNS as QUIZ_VISUAL_DEPENDENT_PATTERNS,
    answer_values as _quiz_answer_values,
    fetch_dzi_open_questions,
    is_open_question_generation_ready as is_fill_in_question_auto_gradable,
)
from src.subject_assessment import AssessmentScope
from src.subject_content import (
    IT, canonical_subject, question_ownerships, section_ownership, sql_chunks,
)
from web import db as web_db
from web.access import Principal
from web.services import dzi_exams
from web.services.question_visuals import (
    attach_question_visual_assets,
    fetch_question_visual_assets,
)
from web.db_introspect import first_existing_column, sqlite_table_columns
from web.text_values import unique_text_values


# ---------------------------------------------------------------------------
# Runtime configuration (registered by the composition root)
# ---------------------------------------------------------------------------

_runtime: dict[str, Callable[[], object]] = {}


def configure_runtime(
    *,
    db_path: Callable[[], object],
    vault_path: Callable[[], object],
    project_root: Callable[[], object],
) -> None:
    """Register how to resolve the quiz database, vault and project root.

    Each argument is a zero-argument callable evaluated at every use, so a later
    change to the underlying setting (a test fixture, a deployment) is honoured.
    """
    _runtime["db_path"] = db_path
    _runtime["vault_path"] = vault_path
    _runtime["project_root"] = project_root


def _runtime_value(name: str):
    try:
        provider = _runtime[name]
    except KeyError:
        raise RuntimeError(
            "web.services.quiz is not configured; call configure_runtime() first"
        ) from None
    return provider()


def quiz_vault_path() -> _QuizPath:
    return _QuizPath(_runtime_value("vault_path"))


def quiz_project_root() -> _QuizPath:
    return _QuizPath(_runtime_value("project_root"))


QUIZ_APPROVED_FILTER = "(q.is_ai_generated = 0 OR q.quality_score >= 1.0)"
QUESTION_DIFFICULTY_BUCKETS = {
    "1": ("very_easy", "Много лесни", "diff-easy"),
    "2": ("easy", "Лесни", "diff-easy"),
    "3": ("medium", "Средни", "diff-medium"),
    "4": ("hard", "Трудни", "diff-hard"),
    "5": ("very_hard", "Много трудни", "diff-hard"),
    "easy": ("easy", "Лесни", "diff-easy"),
    "medium": ("medium", "Средни", "diff-medium"),
    "hard": ("hard", "Трудни", "diff-hard"),
}
QUESTION_DIFFICULTY_BUCKET_ORDER = [
    "very_easy",
    "easy",
    "medium",
    "hard",
    "very_hard",
    "unknown",
]


def quiz_db():
    """One short-lived connection per call; the caller closes it."""
    return web_db.connect_db(_runtime_value("db_path"))


@contextmanager
def quiz_connection():
    """A :func:`quiz_db` connection that is closed when the block exits, even on error.

    Leaving the block does not commit: uncommitted writes are rolled back by the
    close, exactly as when a caller closed the connection by hand.
    """
    conn = quiz_db()
    try:
        yield conn
    finally:
        conn.close()

def quiz_slugify(value: str) -> str:
    bg = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
        "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
        "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sht", "ъ": "a",
        "ь": "", "ю": "yu", "я": "ya",
    }
    out = []
    for ch in (value or "").lower():
        out.append(bg.get(ch, ch))
    value = "".join(out)
    value = _quiz_re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "quiz"


def quiz_markdown_escape(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n")


def quiz_frontmatter(data: dict) -> str:
    lines = ["---"]
    for key, value in data.items():
        if isinstance(value, list):
            inner = ", ".join(str(v) for v in value)
            lines.append(f"{key}: [{inner}]")
        elif value is None:
            lines.append(f"{key}: null")
        elif isinstance(value, str):
            safe = value.replace('"', '\\"')
            lines.append(f'{key}: "{safe}"')
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def quiz_assignment_note_path(assignment_id: int, section_slug: str, created_at: str | None = None) -> _QuizPath:
    date = (created_at or _quiz_datetime.now().isoformat())[:10]
    filename = f"{date}-assignment-{assignment_id}-{quiz_slugify(section_slug)}.md"
    return quiz_vault_path() / "Generated" / "Quizzes" / filename


def quiz_attempt_note_path(assignment_id: int, student_name: str, submitted_at: str | None = None) -> _QuizPath:
    date = (submitted_at or _quiz_datetime.now().isoformat())[:10]
    filename = f"{date}-assignment-{assignment_id}-{quiz_slugify(student_name)}.md"
    return quiz_vault_path() / "Generated" / "Quizzes" / "attempts" / filename


def quiz_create_assignment(
    conn, *, principal: Principal, section_id: int, title_bg: str,
    question_count: int, time_limit_minutes: int | None = None,
    question_plan_json: str | None = None,
) -> int:
    """Insert with the creator's stable account id; legacy creators stay unowned.

    The caller supplies the current Principal and owns the transaction. An
    admin's authorization bypass does not change who owns the new row.
    """
    if not principal.is_teacher:
        raise ValueError("Assignment creation requires teacher capability")
    cursor = conn.execute("""
        INSERT INTO quiz_assignments (
            section_id, title_bg, question_count, time_limit_minutes,
            question_plan_json, owner_user_id
        ) VALUES (?, ?, ?, ?, ?, ?)
    """, (section_id, title_bg, question_count, time_limit_minutes,
          question_plan_json, principal.user_id))
    return int(cursor.lastrowid)


def quiz_can_access_assignment(assignment, principal: Principal) -> bool:
    """Detail access: admin, account owner, or legacy teacher on an unowned row.

    Takes the already loaded row and Principal; no per-row user lookup. Other
    teacher routes retain their admin policy until the PR7 rollout.
    """
    if assignment is None:
        return False
    if principal.is_admin:
        return True
    return principal.is_teacher and assignment["owner_user_id"] == principal.user_id


def quiz_fetch_assignment(conn, assignment_id: int):
    return conn.execute("""
        SELECT
            qa.*,
            cs.section_slug,
            cs.title_bg AS section_title,
            cs.class AS section_class
        FROM quiz_assignments qa
        JOIN curriculum_sections cs ON cs.id = qa.section_id
        WHERE qa.id = ?
    """, (assignment_id,)).fetchone()


def quiz_clean_answer_text(value: object) -> str:
    return str(value or "").strip()


_QUIZ_SMART_QUOTE_TRANSLATION = str.maketrans({
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
})


def quiz_normalize_text_answer(value: object) -> str:
    if value is None:
        return ""

    text = _quiz_unicodedata.normalize("NFKC", str(value))
    text = text.translate(_QUIZ_SMART_QUOTE_TRANSLATION)
    text = " ".join(text.strip().split())
    return text.casefold()


def _quiz_mapping_get(value: object, key: str, default: object = None) -> object:
    if value is None:
        return default
    if hasattr(value, "keys"):
        try:
            return value[key]  # sqlite3.Row supports mapping-style access without get().
        except (IndexError, KeyError):
            return default
    return getattr(value, key, default)


def _quiz_int_value(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quiz_subquestion_accepted_answers(subquestion) -> list[str]:
    accepted = []
    accepted.extend(_quiz_answer_values(_quiz_mapping_get(subquestion, "accepted_answers")))
    accepted.extend(_quiz_answer_values(_quiz_mapping_get(subquestion, "correct_answers")))
    accepted.extend(_quiz_answer_values(_quiz_mapping_get(subquestion, "correct_answer")))
    accepted.extend(_quiz_answer_values(_quiz_mapping_get(subquestion, "answer_alternatives")))
    return accepted


def _quiz_candidate_grading_mode(subquestions: list[dict]) -> str:
    accepted_sets = [
        {quiz_normalize_text_answer(value) for value in _quiz_subquestion_accepted_answers(row)}
        for row in subquestions
    ]
    if len(accepted_sets) > 1 and all(values and values == accepted_sets[0] for values in accepted_sets):
        return "order_independent"
    return "ordered"


def fetch_open_question_candidates(
    conn,
    *,
    source_slug: str | None = None,
    section_id: int | None = None,
    limit: int | None = None,
    subject: str | None = None,
    scope: AssessmentScope | None = None,
) -> list[dict]:
    """DZI-ready candidates; a curriculum caller needs a stored DZI section.

    Calls without a section are for explicit DZI generation/audit contexts.
    They still enforce the same exam ownership and readiness boundary.
    """
    if scope is not None and section_id != scope.section_id:
        raise ValueError("Open-question scope must match its section")
    if section_id is not None:
        scope = scope or AssessmentScope(conn, section_id)
        if not scope.allows_dzi_open_questions:
            return []
    if subject is not None and canonical_subject(subject) != IT:
        return []
    candidates = []
    for question in fetch_dzi_open_questions(conn, source_slug=source_slug):
        candidates.append({
            "question_id": int(question["id"]),
            "source_slug": question["source_exam"],
            "task_number": question["source_number"],
            "grading_mode": _quiz_candidate_grading_mode(question["subquestions"]),
            "subquestion_count": len(question["subquestions"]),
        })
        if limit is not None and len(candidates) >= limit:
            break
    attach_question_image_indicators(conn, candidates)
    return candidates


def open_question_scope_summary(conn, section_id: int, scope: AssessmentScope) -> dict:
    """DZI-ready count and sources; ordinary curriculum sections return zero."""
    source_counts: dict[str, int] = {}
    candidates = fetch_open_question_candidates(conn, section_id=section_id, scope=scope)
    for candidate in candidates:
        source_slug = candidate.get("source_slug") or "unknown"
        source_counts[source_slug] = source_counts.get(source_slug, 0) + 1
    return {
        "count": len(candidates),
        "sources": [
            {"source_slug": source_slug, "count": count}
            for source_slug, count in sorted(source_counts.items())
        ],
    }


def build_mixed_quiz_plan(
    conn,
    *,
    closed_count: int,
    open_count: int,
    source_slug: str | None = None,
    open_source_slug: str | None = None,
    section_id: int | None = None,
    rng: "_quiz_random.Random | None" = None,
    scope: AssessmentScope | None = None,
) -> dict:
    if closed_count < 0 or open_count < 0:
        raise ValueError("closed_count and open_count must be non-negative")
    if scope is not None and section_id != scope.section_id:
        raise ValueError("Mixed-question scope must match its section")
    # Randomization happens at plan-creation time so that two new assignments
    # for the same section/count pick different question sets when the pool
    # is large enough. Callers can pass a seeded ``rng`` for deterministic
    # tests; the persisted ``question_plan_json`` keeps a *single* fixed
    # selection per assignment, so already-started attempts never reshuffle.
    if rng is None:
        rng = _quiz_random.Random()

    params = []
    source_filter = ""
    if source_slug is not None:
        source_filter = "AND q.source_exam = ?"
        params.append(source_slug)
    section_join = ""
    section_filter = ""
    if section_id is not None:
        section_join = "JOIN topic_section_assignments tsa ON tsa.topic_id = q.topic_id"
        section_filter = "AND tsa.section_id = ?"
        params.append(section_id)

    # SAFE: optional SQL fragments are static; values still use ? params.
    closed_rows = conn.execute(f"""
        SELECT DISTINCT
            q.id,
            q.source_exam,
            q.source_number,
            q.prompt,
            q.question_type,
            q.has_image,
            q.image_path
        FROM questions q
        {section_join}
        WHERE q.question_type = 'multiple_choice'
          AND {QUIZ_APPROVED_FILTER}
          {source_filter}
          {section_filter}
        ORDER BY q.source_exam, q.source_number, q.id
    """, params).fetchall()

    closed_candidates = []
    for row in closed_rows:
        if scope is not None and not scope.accepts(int(row["id"])):
            continue
        if not is_quiz_question_eligible(conn, row):
            continue
        closed_candidates.append({
            "question_id": int(row["id"]),
            "source_slug": row["source_exam"],
            "task_number": row["source_number"],
            "question_type": "multiple_choice",
        })

    open_candidates = fetch_open_question_candidates(
        conn,
        source_slug=open_source_slug if open_source_slug is not None else source_slug,
        section_id=section_id,
        scope=scope,
    )

    return {
        "closed_questions": _quiz_random_sample(rng, closed_candidates, closed_count),
        "open_questions": _quiz_random_sample(rng, open_candidates, open_count),
        "requested_closed_count": closed_count,
        "requested_open_count": open_count,
        "available_closed_count": len(closed_candidates),
        "available_open_count": len(open_candidates),
    }


def _quiz_random_sample(rng, population, k):
    """Sample up to k items from population without replacement.

    Returns the full population (in random order) when k >= len(population).
    Returns [] for empty input or non-positive k. The order of the returned
    items is randomized so that the persisted question_plan_json varies
    across new assignments even when the pool is small.
    """
    if k <= 0 or not population:
        return []
    pool = list(population)
    if k >= len(pool):
        rng.shuffle(pool)
        return pool
    return rng.sample(pool, k)


def insert_quiz_text_answer(
    conn,
    *,
    attempt_id: int,
    question_id: int,
    subquestion_number: int,
    raw_answer: object,
    normalized_answer: str,
    subquestion_id: int | None = None,
    response_order: int | None = None,
    grading_mode: str = "ordered",
    accepted_answers_json: str = "[]",
    matched_answer: str | None = None,
    is_correct: bool = False,
    points_awarded: float = 0,
    points_possible: float = 1,
    grader_version: str | None = None,
) -> int:
    if grading_mode not in {"ordered", "order_independent"}:
        raise ValueError("grading_mode must be 'ordered' or 'order_independent'")
    if normalized_answer is None:
        raise ValueError("normalized_answer is required")

    cur = conn.execute("""
        INSERT INTO quiz_text_answers (
            attempt_id, question_id, subquestion_id, subquestion_number,
            response_order, raw_answer, normalized_answer, grading_mode,
            accepted_answers_json, matched_answer, is_correct,
            points_awarded, points_possible, grader_version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        attempt_id,
        question_id,
        subquestion_id,
        subquestion_number,
        response_order,
        "" if raw_answer is None else str(raw_answer),
        normalized_answer,
        grading_mode,
        accepted_answers_json,
        matched_answer,
        1 if is_correct else 0,
        points_awarded,
        points_possible,
        grader_version,
    ))
    return int(cur.lastrowid)


def _quiz_slot_items(values) -> list[tuple[int, object]]:
    if hasattr(values, "items"):
        return sorted((int(slot), value) for slot, value in values.items())
    return [(index, value) for index, value in enumerate(values or [], start=1)]


def _quiz_accepted_by_slot(values) -> dict[int, list[str]]:
    accepted = {}
    for slot, slot_values in _quiz_slot_items(values):
        if slot_values is None:
            accepted[slot] = []
        elif isinstance(slot_values, (list, tuple, set)):
            accepted[slot] = [str(value) for value in slot_values if value is not None]
        else:
            accepted[slot] = [str(slot_values)]
    return accepted


def _quiz_text_grade_result(
    subquestion_number: int,
    raw_answer: object,
    accepted_answers: list[str],
    *,
    matched_answer: str | None,
    is_correct: bool,
) -> dict:
    return {
        "subquestion_number": subquestion_number,
        "raw_answer": "" if raw_answer is None else str(raw_answer),
        "normalized_answer": quiz_normalize_text_answer(raw_answer),
        "accepted_answers": accepted_answers,
        "accepted_answers_json": _quiz_json.dumps(accepted_answers, ensure_ascii=False),
        "matched_answer": matched_answer,
        "is_correct": is_correct,
        "points_awarded": 1 if is_correct else 0,
        "points_possible": 1,
    }


def grade_quiz_text_answers(submitted_answers, accepted_answers_by_slot, *, grading_mode: str = "ordered") -> list[dict]:
    if grading_mode not in {"ordered", "order_independent"}:
        raise ValueError("grading_mode must be 'ordered' or 'order_independent'")

    submitted = dict(_quiz_slot_items(submitted_answers))
    accepted_by_slot = _quiz_accepted_by_slot(accepted_answers_by_slot)
    slots = sorted(set(submitted) | set(accepted_by_slot))

    if grading_mode == "ordered":
        results = []
        for slot in slots:
            raw_answer = submitted.get(slot, "")
            accepted_answers = accepted_by_slot.get(slot, [])
            normalized_answer = quiz_normalize_text_answer(raw_answer)
            matched_answer = None
            for accepted_answer in accepted_answers:
                if normalized_answer and normalized_answer == quiz_normalize_text_answer(accepted_answer):
                    matched_answer = accepted_answer
                    break
            results.append(_quiz_text_grade_result(
                slot,
                raw_answer,
                accepted_answers,
                matched_answer=matched_answer,
                is_correct=matched_answer is not None,
            ))
        return results

    remaining = {}
    for accepted_answers in accepted_by_slot.values():
        for accepted_answer in accepted_answers:
            normalized = quiz_normalize_text_answer(accepted_answer)
            if not normalized:
                continue
            remaining.setdefault(normalized, []).append(accepted_answer)

    results = []
    for slot in slots:
        raw_answer = submitted.get(slot, "")
        accepted_answers = accepted_by_slot.get(slot, [])
        normalized_answer = quiz_normalize_text_answer(raw_answer)
        matched_answer = None
        if normalized_answer and remaining.get(normalized_answer):
            matched_answer = remaining[normalized_answer].pop(0)
        results.append(_quiz_text_grade_result(
            slot,
            raw_answer,
            accepted_answers,
            matched_answer=matched_answer,
            is_correct=matched_answer is not None,
        ))
    return results


def record_quiz_text_answers(
    conn,
    *,
    attempt_id: int,
    question_id: int,
    submitted_answers,
    accepted_answers_by_slot,
    grading_mode: str = "ordered",
    subquestion_ids_by_slot=None,
    grader_version: str | None = None,
) -> list[dict]:
    graded_results = grade_quiz_text_answers(
        submitted_answers,
        accepted_answers_by_slot,
        grading_mode=grading_mode,
    )
    subquestion_ids = dict(_quiz_slot_items(subquestion_ids_by_slot)) if subquestion_ids_by_slot else {}

    recorded = []
    for result in graded_results:
        subquestion_number = int(result["subquestion_number"])
        row_id = insert_quiz_text_answer(
            conn,
            attempt_id=attempt_id,
            question_id=question_id,
            subquestion_id=subquestion_ids.get(subquestion_number),
            subquestion_number=subquestion_number,
            raw_answer=result["raw_answer"],
            normalized_answer=result["normalized_answer"],
            grading_mode=grading_mode,
            accepted_answers_json=result["accepted_answers_json"],
            matched_answer=result["matched_answer"],
            is_correct=result["is_correct"],
            points_awarded=result["points_awarded"],
            points_possible=result["points_possible"],
            grader_version=grader_version,
        )
        recorded.append({**result, "id": row_id})
    return recorded


def fetch_quiz_text_answers_for_attempt(conn, attempt_id: int, *, question_id: int | None = None) -> list[dict]:
    params = [attempt_id]
    question_filter = ""
    if question_id is not None:
        question_filter = "AND question_id = ?"
        params.append(question_id)
    columns = sqlite_table_columns(conn, "quiz_text_answers")
    teacher_status_expr = (
        "teacher_override_status"
        if "teacher_override_status" in columns
        else "NULL AS teacher_override_status"
    )

    # SAFE: question_filter is a static fragment and teacher_status_expr is
    # selected from a known column name or a static NULL expression.
    rows = conn.execute(f"""
        SELECT
            id,
            attempt_id,
            question_id,
            subquestion_id,
            subquestion_number,
            raw_answer,
            normalized_answer,
            grading_mode,
            accepted_answers_json,
            matched_answer,
            is_correct,
            points_awarded,
            points_possible,
            grader_version,
            teacher_override,
            {teacher_status_expr},
            teacher_note
        FROM quiz_text_answers
        WHERE attempt_id = ?
          {question_filter}
        ORDER BY question_id, subquestion_number
    """, params).fetchall()
    answers = []
    for row in rows:
        answer = dict(row)
        answer["accepted_answers"] = quiz_display_answer_list(answer.get("accepted_answers_json"))
        answer["feedback_explanation"] = QUIZ_NO_EXPLANATION_MESSAGE
        prepare_quiz_text_answer_review(answer)
        answers.append(answer)
    return answers


QUIZ_NO_EXPLANATION_MESSAGE = "Няма въведено обяснение за този въпрос."
QUIZ_NO_ACCEPTED_ANSWER_MESSAGE = "Няма въведен приет отговор."
QUESTION_EXPLANATION_COLUMNS = (
    "explanation",
    "explanation_bg",
    "answer_explanation",
    "answer_explanation_bg",
    "rationale",
    "rationale_bg",
    "feedback",
    "feedback_bg",
)
OPTION_EXPLANATION_COLUMNS = (
    "explanation",
    "explanation_bg",
    "rationale",
    "rationale_bg",
    "feedback",
    "feedback_bg",
)


def quiz_display_answer_list(raw_value: object) -> list[str]:
    return unique_text_values(_quiz_answer_values(raw_value))


def fetch_question_explanations(conn, question_ids: list[int]) -> dict[int, str]:
    if not question_ids:
        return {}
    column = first_existing_column(sqlite_table_columns(conn, "questions"), QUESTION_EXPLANATION_COLUMNS)
    if column is None:
        return {}

    # SAFE: column is selected from QUESTION_EXPLANATION_COLUMNS; placeholders
    # are generated from trusted question IDs.
    placeholders = ",".join("?" for _ in question_ids)
    rows = conn.execute(f"""
        SELECT id, {column} AS explanation
        FROM questions
        WHERE id IN ({placeholders})
    """, question_ids).fetchall()
    return {
        int(row["id"]): str(row["explanation"]).strip()
        for row in rows
        if row["explanation"] is not None and str(row["explanation"]).strip()
    }


def fetch_option_explanations(conn, question_ids: list[int]) -> dict[tuple[int, str], str]:
    if not question_ids:
        return {}
    column = first_existing_column(
        sqlite_table_columns(conn, "multiple_choice_options"),
        OPTION_EXPLANATION_COLUMNS,
    )
    if column is None:
        return {}

    # SAFE: column is selected from OPTION_EXPLANATION_COLUMNS; placeholders
    # are generated from trusted question IDs.
    placeholders = ",".join("?" for _ in question_ids)
    rows = conn.execute(f"""
        SELECT question_id, option_letter, {column} AS explanation
        FROM multiple_choice_options
        WHERE question_id IN ({placeholders})
    """, question_ids).fetchall()
    return {
        (int(row["question_id"]), row["option_letter"]): str(row["explanation"]).strip()
        for row in rows
        if row["explanation"] is not None and str(row["explanation"]).strip()
    }


def quiz_text_answer_informational_subtotal(rows) -> dict:
    awarded = 0.0
    possible = 0.0
    count = 0
    for row in rows or []:
        count += 1
        try:
            row_possible = float(row.get("points_possible") or 0)
        except (TypeError, ValueError):
            row_possible = 0.0
        possible += row_possible

        try:
            awarded += float(row.get("points_awarded") or 0)
        except (TypeError, ValueError):
            pass

    return {
        "awarded": awarded,
        "possible": possible,
        "count": count,
    }


def quiz_combined_score_summary(attempt, open_subtotal: dict | None, *, enabled: bool) -> dict | None:
    if not enabled or not open_subtotal:
        return None
    return {
        "mc_awarded": float(attempt["score_correct"] or 0),
        "mc_possible": float(attempt["score_total"] or 0),
        "open_awarded": float(open_subtotal.get("awarded") or 0),
        "open_possible": float(open_subtotal.get("possible") or 0),
        "combined_awarded": float(attempt["score_correct"] or 0) + float(open_subtotal.get("awarded") or 0),
        "combined_possible": float(attempt["score_total"] or 0) + float(open_subtotal.get("possible") or 0),
    }


def quiz_record_planned_open_text_answers(
    conn,
    *,
    attempt_id: int,
    question_ids: list[int],
    open_question_ids: list[int],
    form,
) -> None:
    planned_open_ids = set()
    renderable_ids = {int(qid) for qid in question_ids}
    for qid in open_question_ids or []:
        try:
            if isinstance(qid, bool):
                raise ValueError
            planned_qid = int(qid)
        except (TypeError, ValueError):
            continue
        if planned_qid in renderable_ids:
            planned_open_ids.add(planned_qid)

    for qid in question_ids:
        if int(qid) not in planned_open_ids:
            continue

        subquestions = conn.execute("""
            SELECT id, subquestion_number, correct_answer, answer_alternatives
            FROM fill_in_subquestions
            WHERE question_id = ?
            ORDER BY subquestion_number
        """, (qid,)).fetchall()
        submitted_answers = {}
        accepted_answers_by_slot = {}
        subquestion_ids_by_slot = {}

        for row in subquestions:
            slot = int(row["subquestion_number"])
            field_name = f"open_q_{qid}_{slot}"
            if field_name in form:
                submitted_answers[slot] = form.get(field_name, "")
            accepted_answers_by_slot[slot] = _quiz_subquestion_accepted_answers(row)
            subquestion_ids_by_slot[slot] = int(row["id"])

        if submitted_answers:
            record_quiz_text_answers(
                conn,
                attempt_id=attempt_id,
                question_id=int(qid),
                submitted_answers=submitted_answers,
                accepted_answers_by_slot=accepted_answers_by_slot,
                grading_mode=_quiz_candidate_grading_mode([dict(row) for row in subquestions]),
                subquestion_ids_by_slot=subquestion_ids_by_slot,
            )


def quiz_answer_text_is_real(value: object) -> bool:
    text = quiz_clean_answer_text(value)
    return bool(text) and text not in {"-", "—", "[]"}


def quiz_prompt_needs_visual(prompt: str | None) -> bool:
    text = (prompt or "").lower()
    return any(pattern in text for pattern in QUIZ_VISUAL_DEPENDENT_PATTERNS)


def quiz_path_exists(path_value: object) -> bool:
    raw_path = quiz_clean_answer_text(path_value)
    if not raw_path or raw_path in {"-", "—"}:
        return False

    path = _QuizPath(raw_path)
    if path.is_absolute():
        return path.exists()
    return (quiz_project_root() / path).exists()


def question_has_usable_visual(conn, question) -> bool:
    if quiz_path_exists(question["image_path"]):
        return True

    if int(question["has_image"] or 0) and quiz_path_exists(question["image_path"]):
        return True

    asset_rows = conn.execute("""
        SELECT a.local_path
        FROM asset_links al
        JOIN assets a ON a.id = al.asset_id
        WHERE (
            (al.owner_type IN ('question', 'questions') AND al.owner_id = ?)
            OR (
                al.owner_type = 'exam_task'
                AND al.owner_id IN (
                    SELECT task_id
                    FROM exam_task_questions
                    WHERE question_id = ?
                )
            )
          )
          AND a.asset_type IN ('image', 'pdf_crop')
    """, (question["id"], question["id"])).fetchall()

    return any(quiz_path_exists(row["local_path"]) for row in asset_rows)


def multiple_choice_options_are_eligible(options) -> bool:
    """Pure rule over option rows; shared by the per-question and batched paths."""
    if len(options) != 4:
        return False

    if any(not quiz_answer_text_is_real(row["option_text"]) for row in options):
        return False

    correct_options = [row for row in options if int(row["is_correct"] or 0) == 1]
    if len(correct_options) != 1:
        return False

    return quiz_answer_text_is_real(correct_options[0]["option_text"])


def quiz_multiple_choice_is_eligible(conn, question_id: int) -> bool:
    options = conn.execute("""
        SELECT option_text, is_correct
        FROM multiple_choice_options
        WHERE question_id = ?
        ORDER BY option_letter
    """, (question_id,)).fetchall()

    return multiple_choice_options_are_eligible(options)


def quiz_fill_in_is_eligible(conn, question_id: int) -> bool:
    rows = conn.execute("""
        SELECT correct_answer
        FROM fill_in_subquestions
        WHERE question_id = ?
        ORDER BY subquestion_number
    """, (question_id,)).fetchall()

    if not rows:
        return False

    return all(quiz_answer_text_is_real(row["correct_answer"]) for row in rows)


def is_quiz_question_eligible(conn, question) -> bool:
    if quiz_prompt_needs_visual(question["prompt"]) and not question_has_usable_visual(conn, question):
        return False

    question_type = question["question_type"]
    if question_type == "multiple_choice":
        return quiz_multiple_choice_is_eligible(conn, int(question["id"]))

    if question_type in {"fill_in", "short_answer"}:
        return quiz_fill_in_is_eligible(conn, int(question["id"]))

    return False


def quiz_section_question_ids(
    conn, section_id: int, *, scope: AssessmentScope | None = None
) -> list[int]:
    if scope is not None and section_id != scope.section_id:
        raise ValueError("MC scope must match its section")
    if scope is not None:
        # Batched ownership/options/visual checks: a bounded number of queries however many
        # candidates the section has (same ids, same order as the reference below).
        return quiz_section_question_ids_by_section(conn, {section_id: scope})[section_id]
    return _quiz_section_question_ids_reference(conn, section_id, scope=None)


def _quiz_section_question_ids_reference(
    conn, section_id: int, *, scope: AssessmentScope | None = None
) -> list[int]:
    """Per-question reference implementation (one lookup set per candidate).

    Kept for the scope-less legacy picker and as the oracle the batched path is tested against.
    """
    # SAFE: QUIZ_APPROVED_FILTER is a static SQL fragment; section_id uses ?.
    rows = conn.execute(f"""
        SELECT DISTINCT q.id, q.prompt, q.question_type, q.has_image, q.image_path
        FROM questions q
        JOIN topic_section_assignments tsa ON tsa.topic_id = q.topic_id
        WHERE tsa.section_id = ?
          AND {QUIZ_APPROVED_FILTER}
          AND q.question_type = 'multiple_choice'
        ORDER BY q.id
    """, (section_id,)).fetchall()
    return [
        int(r["id"]) for r in rows
        if (scope is None or scope.accepts(int(r["id"])))
        and is_quiz_question_eligible(conn, r)
    ]


def quiz_section_question_ids_by_section(
    conn, scopes: dict[int, AssessmentScope]
) -> dict[int, list[int]]:
    """``quiz_section_question_ids`` for many sections in a bounded set of queries.

    ``is_quiz_question_eligible`` and ``quiz_section_question_ids`` stay the
    reference implementation (attempt rendering and quiz start use them). This
    returns, for every section in ``scopes``, the same ids in the same order.
    Ownership, option and visual checks run over batch-fetched rows; the
    image-reference phrase rule stays in Python (``quiz_prompt_needs_visual``).
    """
    result: dict[int, list[int]] = {section_id: [] for section_id in scopes}
    if not scopes:
        return result

    # Candidate (section, question) pairs, exactly as the per-section query selects them.
    rows_by_section: dict[int, list] = {}
    questions: dict[int, object] = {}
    for chunk in sql_chunks(scopes):
        marks = ",".join("?" for _ in chunk)
        # SAFE: QUIZ_APPROVED_FILTER is static and placeholders are generated.
        for row in conn.execute(f"""
            SELECT DISTINCT tsa.section_id AS section_id, q.id, q.prompt, q.question_type,
                            q.has_image, q.image_path
            FROM questions q
            JOIN topic_section_assignments tsa ON tsa.topic_id = q.topic_id
            WHERE tsa.section_id IN ({marks})
              AND {QUIZ_APPROVED_FILTER}
              AND q.question_type = 'multiple_choice'
            ORDER BY tsa.section_id, q.id
        """, chunk):
            rows_by_section.setdefault(int(row["section_id"]), []).append(row)
            questions[int(row["id"])] = row

    owners = question_ownerships(conn, questions)
    candidate_ids = sorted(questions)

    options_by_question: dict[int, list] = {}
    for chunk in sql_chunks(candidate_ids):
        marks = ",".join("?" for _ in chunk)
        # SAFE: placeholders only; values use ? params.
        for option in conn.execute(f"""
            SELECT question_id, option_text, is_correct
            FROM multiple_choice_options
            WHERE question_id IN ({marks})
            ORDER BY question_id, option_letter
        """, chunk):
            options_by_question.setdefault(int(option["question_id"]), []).append(option)

    usable_visual = _usable_visual_question_ids(conn, questions)
    eligible = {}
    for question_id, question in questions.items():
        if quiz_prompt_needs_visual(question["prompt"]) and question_id not in usable_visual:
            eligible[question_id] = False
        else:
            eligible[question_id] = multiple_choice_options_are_eligible(
                options_by_question.get(question_id, [])
            )

    for section_id, scope in scopes.items():
        for row in rows_by_section.get(section_id, []):
            question_id = int(row["id"])
            owner = owners[question_id]
            if owner.is_consistent and owner.subject == scope.subject and eligible[question_id]:
                result[section_id].append(question_id)
    return result


def _usable_visual_question_ids(conn, questions: dict[int, object]) -> set[int]:
    """Ids among phrase-flagged questions that ``question_has_usable_visual`` accepts."""
    usable: set[int] = set()
    pending = []
    for question_id, question in questions.items():
        if not quiz_prompt_needs_visual(question["prompt"]):
            continue
        if quiz_path_exists(question["image_path"]):
            usable.add(question_id)
        else:
            pending.append(question_id)

    for chunk in sql_chunks(pending):
        marks = ",".join("?" for _ in chunk)
        # SAFE: placeholders only; values use ? params.
        for row in conn.execute(f"""
            SELECT al.owner_id AS question_id, a.local_path
            FROM asset_links al
            JOIN assets a ON a.id = al.asset_id
            WHERE al.owner_type IN ('question', 'questions')
              AND al.owner_id IN ({marks})
              AND a.asset_type IN ('image', 'pdf_crop')
            UNION ALL
            SELECT etq.question_id, a.local_path
            FROM asset_links al
            JOIN assets a ON a.id = al.asset_id
            JOIN exam_task_questions etq ON etq.task_id = al.owner_id
            WHERE al.owner_type = 'exam_task'
              AND etq.question_id IN ({marks})
              AND a.asset_type IN ('image', 'pdf_crop')
        """, chunk + chunk):
            if int(row["question_id"]) not in usable and quiz_path_exists(row["local_path"]):
                usable.add(int(row["question_id"]))
    return usable


def quiz_seed(assignment_id: int, student_name: str) -> str:
    raw = f"{assignment_id}|{student_name.strip().lower()}".encode("utf-8")
    return _quiz_hashlib.sha256(raw).hexdigest()


def quiz_pick_questions(
    conn, assignment, student_name: str, *, scope: AssessmentScope | None = None
) -> tuple[str, list[int]]:
    seed = quiz_seed(int(assignment["id"]), student_name)
    if scope is None:
        try:
            owner = section_ownership(conn, int(assignment["section_id"]))
        except sqlite3.OperationalError as exc:
            if "no such table: curriculum_sections" not in str(exc):
                raise
            # Legacy in-memory picker unit fixtures have no curriculum tables.
            owner = None
        if owner is not None and owner.is_consistent:
            scope = AssessmentScope(conn, int(assignment["section_id"]))
    ids = quiz_section_question_ids(conn, int(assignment["section_id"]), scope=scope)
    rng = _quiz_random.Random(seed)
    rng.shuffle(ids)
    count = min(int(assignment["question_count"]), len(ids))
    return seed, ids[:count]


def quiz_shuffle_options(seed: str, question_id: int, options: list[dict]) -> list[dict]:
    shuffled = [dict(o) for o in options]
    rng = _quiz_random.Random(f"{seed}|options|{question_id}")
    rng.shuffle(shuffled)

    display_letters = ["А", "Б", "В", "Г", "Д", "Е"]
    for i, opt in enumerate(shuffled):
        opt["display_letter"] = display_letters[i] if i < len(display_letters) else str(i + 1)

    return shuffled


def quiz_load_questions(conn, question_ids: list[int], seed: str, include_correct: bool = False) -> list[dict]:
    if not question_ids:
        return []

    # SAFE: placeholders are generated from trusted question IDs.
    placeholders = ",".join("?" for _ in question_ids)
    q_rows = conn.execute(f"""
        SELECT id, prompt, question_type, difficulty
        FROM questions
        WHERE id IN ({placeholders})
    """, question_ids).fetchall()

    by_id = {int(r["id"]): dict(r) for r in q_rows}
    result = []

    for qid in question_ids:
        q = by_id.get(int(qid))
        if not q:
            continue

        if q["question_type"] in {"fill_in", "short_answer"}:
            sub_rows = conn.execute("""
                SELECT id, subquestion_number
                FROM fill_in_subquestions
                WHERE question_id = ?
                ORDER BY subquestion_number
            """, (qid,)).fetchall()
            q["options"] = []
            q["subquestions"] = [dict(row) for row in sub_rows]
            result.append(q)
            continue

        if include_correct:
            opt_rows = conn.execute("""
                SELECT id, option_letter, option_text, is_correct
                FROM multiple_choice_options
                WHERE question_id = ?
                ORDER BY option_letter
            """, (qid,)).fetchall()
        else:
            opt_rows = conn.execute("""
                SELECT id, option_letter, option_text
                FROM multiple_choice_options
                WHERE question_id = ?
                ORDER BY option_letter
            """, (qid,)).fetchall()

        q["options"] = quiz_shuffle_options(seed, int(qid), [dict(o) for o in opt_rows])
        result.append(q)

    return attach_question_visual_assets(conn, result)


def quiz_question_types_by_id(conn, question_ids: list[int]) -> dict[int, str]:
    if not question_ids:
        return {}

    unique_ids = list(dict.fromkeys(int(qid) for qid in question_ids))
    # SAFE: placeholders are generated from trusted question IDs.
    placeholders = ",".join("?" for _ in unique_ids)
    rows = conn.execute(f"""
        SELECT id, question_type
        FROM questions
        WHERE id IN ({placeholders})
    """, unique_ids).fetchall()
    return {int(row["id"]): row["question_type"] for row in rows}


def quiz_correct_mc_letters_by_id(conn, question_ids: list[int]) -> dict[int, str | None]:
    if not question_ids:
        return {}

    unique_ids = list(dict.fromkeys(int(qid) for qid in question_ids))
    # SAFE: placeholders are generated from trusted question IDs.
    placeholders = ",".join("?" for _ in unique_ids)
    rows = conn.execute(f"""
        SELECT question_id, option_letter
        FROM multiple_choice_options
        WHERE question_id IN ({placeholders})
          AND is_correct = 1
    """, unique_ids).fetchall()

    correct_by_qid: dict[int, str | None] = {}
    for row in rows:
        qid = int(row["question_id"])
        if qid not in correct_by_qid:
            correct_by_qid[qid] = row["option_letter"]
    return correct_by_qid


STALE_ATTEMPT_MESSAGE = (
    "Този тест съдържа стари или непълни въпроси и не може да бъде показан коректно. "
    "Моля, създайте нов тест."
)
QUIZ_STUDENT_NAME_TOO_SHORT_MESSAGE = "Името трябва да съдържа поне 2 символа."
QUIZ_STUDENT_NAME_TOO_LONG_MESSAGE = "Името трябва да е най-много 80 символа."
QUIZ_EMPTY_SUBMISSION_MESSAGE = "Попълни поне един отговор, преди да предадеш теста."


def quiz_student_name_error(student_name: str) -> str | None:
    if len(student_name) < 2:
        return QUIZ_STUDENT_NAME_TOO_SHORT_MESSAGE
    if len(student_name) > 80:
        return QUIZ_STUDENT_NAME_TOO_LONG_MESSAGE
    return None


def quiz_has_any_submitted_answer(form, *, mc_question_ids: list[int], question_ids: list[int], open_question_ids: list[int]) -> bool:
    for qid in mc_question_ids:
        if form.get(f"q_{qid}"):
            return True

    renderable_ids = set()
    for qid in question_ids:
        try:
            renderable_ids.add(int(qid))
        except (TypeError, ValueError):
            continue
    planned_open_ids = set()
    for qid in open_question_ids or []:
        try:
            parsed = int(qid)
        except (TypeError, ValueError):
            continue
        if parsed in renderable_ids:
            planned_open_ids.add(parsed)
    for key in form.keys():
        if not key.startswith("open_q_"):
            continue
        parts = key.split("_")
        if len(parts) < 4:
            continue
        try:
            qid = int(parts[2])
        except (TypeError, ValueError):
            continue
        if qid in planned_open_ids and str(form.get(key) or "").strip():
            return True
    return False


def quiz_parse_attempt_question_plan(raw_value) -> dict:
    try:
        parsed = _quiz_json.loads(raw_value or "[]")
    except (_quiz_json.JSONDecodeError, TypeError):
        parsed = []

    if isinstance(parsed, dict):
        raw_question_ids = parsed.get("question_ids", [])
        raw_open_ids = parsed.get("open_question_ids", [])
        mixed_open_enabled = bool(parsed.get("mixed_open_enabled"))
        include_open_answers_in_final_score = bool(parsed.get("include_open_answers_in_final_score"))
    else:
        raw_question_ids = parsed
        raw_open_ids = []
        mixed_open_enabled = False
        include_open_answers_in_final_score = False

    return {
        "question_ids": raw_question_ids if isinstance(raw_question_ids, list) else [],
        "open_question_ids": raw_open_ids if isinstance(raw_open_ids, list) else [],
        "mixed_open_enabled": mixed_open_enabled,
        "include_open_answers_in_final_score": include_open_answers_in_final_score,
    }


def _quiz_clean_question_ids(question_ids) -> list[int]:
    clean_ids = []
    for qid in question_ids or []:
        try:
            if isinstance(qid, bool):
                raise ValueError
            parsed_id = int(qid)
        except (TypeError, ValueError):
            continue
        if parsed_id not in clean_ids:
            clean_ids.append(parsed_id)
    return clean_ids


def question_linked_image_counts(conn, question_ids) -> dict[int, int]:
    clean_ids = _quiz_clean_question_ids(question_ids)
    if not clean_ids:
        return {}
    visual_assets_by_question = fetch_question_visual_assets(conn, clean_ids)
    return {
        question_id: len(visual_assets_by_question.get(question_id, []))
        for question_id in clean_ids
    }


def attach_question_image_indicators(
    conn,
    items: list[dict],
    *,
    question_id_key: str = "question_id",
) -> list[dict]:
    question_ids = _quiz_clean_question_ids(
        item.get(question_id_key)
        for item in items
        if item.get(question_id_key) is not None
    )
    image_counts = question_linked_image_counts(conn, question_ids)
    for item in items:
        try:
            question_id = int(item.get(question_id_key))
        except (TypeError, ValueError):
            image_count = 0
        else:
            image_count = image_counts.get(question_id, 0)
        item["linked_image_count"] = image_count
        item["has_linked_image"] = image_count > 0
    return items


def attach_attempt_question_image_counts(conn, attempts) -> list[dict]:
    annotated_attempts = [dict(attempt) for attempt in attempts]
    question_ids_by_attempt: dict[int, list[int]] = {}
    all_question_ids: list[int] = []

    for attempt in annotated_attempts:
        attempt_id = int(attempt["id"])
        plan = quiz_parse_attempt_question_plan(attempt.get("question_ids_json"))
        question_ids = _quiz_clean_question_ids(plan["question_ids"])
        question_ids_by_attempt[attempt_id] = question_ids
        all_question_ids.extend(question_ids)

    image_counts = question_linked_image_counts(conn, all_question_ids)
    for attempt in annotated_attempts:
        attempt_id = int(attempt["id"])
        image_question_count = sum(
            1
            for question_id in question_ids_by_attempt.get(attempt_id, [])
            if image_counts.get(question_id, 0) > 0
        )
        attempt["image_question_count"] = image_question_count
        attempt["has_linked_image_question"] = image_question_count > 0
    return annotated_attempts


def quiz_parse_assignment_question_plan(raw_value) -> dict | None:
    if raw_value is None or str(raw_value).strip() == "":
        return None

    plan = quiz_parse_attempt_question_plan(raw_value)
    if not plan["mixed_open_enabled"]:
        return None
    if not plan["question_ids"] or not plan["open_question_ids"]:
        return None

    planned_ids = set()
    question_ids = []
    for qid in plan["question_ids"]:
        try:
            if isinstance(qid, bool):
                raise ValueError
            parsed_qid = int(qid)
            planned_ids.add(parsed_qid)
            question_ids.append(parsed_qid)
        except (TypeError, ValueError):
            return None

    open_question_ids = []
    for qid in plan["open_question_ids"]:
        try:
            if isinstance(qid, bool):
                raise ValueError
            open_qid = int(qid)
        except (TypeError, ValueError):
            return None
        if open_qid not in planned_ids:
            return None
        open_question_ids.append(open_qid)

    return {
        "mixed_open_enabled": True,
        "question_ids": question_ids,
        "open_question_ids": open_question_ids,
        "include_open_answers_in_final_score": bool(plan["include_open_answers_in_final_score"]),
    }


def quiz_assignment_mixed_status(raw_value) -> dict:
    plan = quiz_parse_assignment_question_plan(raw_value)
    if plan is not None:
        return {
            "is_mixed": True,
            "open_count": len(plan["open_question_ids"]),
            "combined_score": bool(plan["include_open_answers_in_final_score"]),
            "plan_invalid": False,
        }
    if raw_value is None:
        raw_str = ""
    elif isinstance(raw_value, str):
        raw_str = raw_value.strip()
    else:
        raw_str = str(raw_value).strip()
    return {
        "is_mixed": False,
        "open_count": 0,
        "combined_score": False,
        "plan_invalid": bool(raw_str),
    }


def filter_renderable_attempt_question_ids(
    conn,
    question_ids,
    *,
    open_question_ids: list[int] | None = None,
) -> tuple[list[int], int]:
    allowed_open_ids = set()
    for qid in open_question_ids or []:
        try:
            if isinstance(qid, bool):
                raise ValueError
            allowed_open_ids.add(int(qid))
        except (TypeError, ValueError):
            pass

    parsed_ids = []
    skipped = 0
    for qid in question_ids or []:
        try:
            if isinstance(qid, bool):
                raise ValueError
            parsed_ids.append(int(qid))
        except (TypeError, ValueError):
            skipped += 1

    if not parsed_ids:
        return [], skipped

    unique_ids = list(dict.fromkeys(parsed_ids))
    # SAFE: placeholders are generated from parsed integer question IDs.
    placeholders = ",".join("?" for _ in unique_ids)
    rows = conn.execute(f"""
        SELECT id, prompt, question_type, has_image, image_path
        FROM questions
        WHERE id IN ({placeholders})
    """, unique_ids).fetchall()
    by_id = {int(row["id"]): row for row in rows}

    valid_ids = []
    for qid in parsed_ids:
        question = by_id.get(qid)
        if not question:
            skipped += 1
            continue

        if question["question_type"] == "multiple_choice" and is_quiz_question_eligible(conn, question):
            valid_ids.append(qid)
        elif (
            qid in allowed_open_ids
            and question["question_type"] in {"fill_in", "short_answer"}
            and is_fill_in_question_auto_gradable(
                question,
                conn.execute("""
                    SELECT id, subquestion_number, correct_answer, answer_alternatives
                    FROM fill_in_subquestions
                    WHERE question_id = ?
                    ORDER BY subquestion_number
                """, (qid,)).fetchall(),
            )
        ):
            valid_ids.append(qid)
        else:
            skipped += 1

    return valid_ids, skipped


QUIZ_DURATION_FALLBACK_BG = "Няма данни за време"
QUIZ_SUCCESS_RATE_FALLBACK_BG = "Няма данни за успеваемост"


def quiz_parse_timestamp_utc(raw_value) -> _quiz_datetime | None:
    if not raw_value:
        return None
    try:
        parsed = _quiz_datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(_quiz_timezone.utc).replace(tzinfo=None)
    return parsed


def quiz_time_taken_seconds(attempt) -> int | None:
    started = quiz_parse_timestamp_utc(attempt["started_at"])
    ended = quiz_parse_timestamp_utc(attempt["submitted_at"])
    if started is None or ended is None:
        return None

    seconds = int((ended - started).total_seconds())
    if seconds < 0:
        return None
    return seconds


def quiz_format_duration(seconds: int) -> str:
    if seconds is None:
        return QUIZ_DURATION_FALLBACK_BG
    try:
        total_seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return QUIZ_DURATION_FALLBACK_BG

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    rest = total_seconds % 60

    if hours:
        return f"{hours} ч. {minutes} мин. {rest} сек."
    if minutes:
        return f"{minutes} мин. {rest} сек."
    return f"{rest} сек."


def quiz_attempt_duration_display(attempt) -> str:
    return quiz_format_duration(quiz_time_taken_seconds(attempt))


def quiz_success_rate_percent(score_correct, score_total) -> float | None:
    try:
        total = float(score_total)
        correct = float(score_correct or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    percent = (correct / total) * 100.0
    return max(0.0, min(100.0, percent))


def quiz_success_rate_display(score_correct, score_total) -> str:
    percent = quiz_success_rate_percent(score_correct, score_total)
    if percent is None:
        return QUIZ_SUCCESS_RATE_FALLBACK_BG
    rounded = round(percent, 1)
    if rounded.is_integer():
        return f"{int(rounded)}%"
    return f"{rounded:.1f}%"


def quiz_attempt_success_rate_display(attempt) -> str:
    return quiz_success_rate_display(attempt["score_correct"], attempt["score_total"])


def question_difficulty_bucket(value) -> dict:
    key = str(value or "").strip().lower()
    bucket_key, label, css_class = QUESTION_DIFFICULTY_BUCKETS.get(
        key,
        ("unknown", "Без трудност", "diff-unknown"),
    )
    return {"key": bucket_key, "label": label, "class": css_class}



def quiz_difficulty_breakdown_from_rows(rows) -> list[dict]:
    buckets: dict[str, dict] = {}
    for row in rows:
        bucket = question_difficulty_bucket(row.get("difficulty"))
        key = bucket["key"]
        if key not in buckets:
            buckets[key] = {
                "key": key,
                "label": bucket["label"],
                "class": bucket["class"],
                "correct": 0,
                "total": 0,
                "percent": None,
                "percent_display": QUIZ_SUCCESS_RATE_FALLBACK_BG,
            }
        buckets[key]["total"] += 1
        if row.get("is_correct"):
            buckets[key]["correct"] += 1

    for bucket in buckets.values():
        bucket["percent"] = quiz_success_rate_percent(bucket["correct"], bucket["total"])
        bucket["percent_display"] = quiz_success_rate_display(bucket["correct"], bucket["total"])

    return sorted(
        buckets.values(),
        key=lambda item: QUESTION_DIFFICULTY_BUCKET_ORDER.index(item["key"])
        if item["key"] in QUESTION_DIFFICULTY_BUCKET_ORDER
        else len(QUESTION_DIFFICULTY_BUCKET_ORDER),
    )


def quiz_difficulty_breakdown_from_questions(questions: list[dict]) -> list[dict]:
    return quiz_difficulty_breakdown_from_rows([
        {
            "difficulty": q.get("difficulty"),
            "is_correct": bool(q.get("is_correct")),
        }
        for q in questions
    ])


def quiz_teacher_difficulty_breakdown(conn, attempt_ids: list[int]) -> list[dict]:
    if not attempt_ids:
        return []

    # SAFE: placeholders are generated from trusted attempt IDs.
    placeholders = ",".join("?" for _ in attempt_ids)
    rows = conn.execute(f"""
        SELECT q.difficulty, qa.is_correct
        FROM quiz_answers qa
        JOIN questions q ON q.id = qa.question_id
        WHERE qa.attempt_id IN ({placeholders})
        ORDER BY q.difficulty
    """, attempt_ids).fetchall()
    return quiz_difficulty_breakdown_from_rows([
        {
            "difficulty": row["difficulty"],
            "is_correct": bool(row["is_correct"]),
        }
        for row in rows
    ])


def quiz_current_timestamp() -> _quiz_datetime:
    return _quiz_datetime.utcnow()


def quiz_remaining_seconds(assignment, attempt) -> int | None:
    limit = assignment["time_limit_minutes"]
    if not limit:
        return None

    try:
        started = _quiz_datetime.fromisoformat(attempt["started_at"])
        elapsed = int((quiz_current_timestamp() - started).total_seconds())
        return max(0, int(limit) * 60 - elapsed)
    except Exception:
        return int(limit) * 60


def quiz_write_assignment_note(assignment_id: int, base_url: str) -> None:
    try:
        conn = quiz_db()
        a = quiz_fetch_assignment(conn, assignment_id)
        if not a:
            conn.close()
            return

        attempts = conn.execute("""
            SELECT *
            FROM quiz_attempts
            WHERE assignment_id = ?
            ORDER BY started_at
        """, (assignment_id,)).fetchall()

        submitted = [x for x in attempts if x["submitted_at"]]
        avg = None
        if submitted:
            percentages = [
                (float(x["score_correct"] or 0) / max(1, int(x["score_total"] or 1))) * 100
                for x in submitted
            ]
            avg = round(sum(percentages) / len(percentages), 1)

        quiz_link = base_url.rstrip("/") + _quiz_url_for("quiz.quiz_start", assignment_id=assignment_id)
        path = quiz_assignment_note_path(assignment_id, a["section_slug"], a["created_at"])

        rows = []
        for x in attempts:
            attempt_path = quiz_attempt_note_path(assignment_id, x["student_name"], x["submitted_at"])
            # Use basename without .md so Obsidian wikilink resolution finds the file
            # regardless of where it sits in the vault. Filesystem-relative paths like
            # "attempts/foo.md" don't resolve in Obsidian.
            rel = attempt_path.stem
            score = f'{x["score_correct"]}/{x["score_total"]}' if x["submitted_at"] else "в процес"
            rows.append(
                f'| [[{rel}|{x["student_name"]}]] | {score} | {x["started_at"]} | {x["submitted_at"] or "—"} |'
            )

        fm = quiz_frontmatter({
            "title": a["title_bg"],
            "type": "quiz_assignment",
            "section_slug": a["section_slug"],
            "class": a["section_class"],
            "question_count": a["question_count"],
            "time_limit_minutes": a["time_limit_minutes"],
            "created_at": a["created_at"],
            "attempts_count": len(attempts),
            "average_score": avg,
            "status": "open",
            "tags": ["quiz", "assignment"],
        })

        body = f"""{fm}

# {a["title_bg"]}

**Секция:** {a["section_title"]}
**Брой въпроси:** {a["question_count"]}
**Време:** {str(a["time_limit_minutes"]) + " минути" if a["time_limit_minutes"] else "без ограничение"}
**Споделяем линк:** {quiz_link}

## Опити

| Ученик | Резултат | Начало | Предадено |
|--------|----------|--------|-----------|
{chr(10).join(rows) if rows else "| — | — | — | — |"}
"""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        conn.close()
    except Exception as e:
        print(f"[quiz vault] assignment note failed: {e}", file=_quiz_sys.stderr)


def quiz_write_attempt_note(attempt_id: int) -> None:
    try:
        conn = quiz_db()
        attempt = conn.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone()
        if not attempt:
            conn.close()
            return

        assignment = quiz_fetch_assignment(conn, int(attempt["assignment_id"]))
        if not assignment:
            conn.close()
            return

        seed = attempt["seed"]
        qids = _quiz_json.loads(attempt["question_ids_json"])
        qids, _skipped_count = filter_renderable_attempt_question_ids(conn, qids)
        questions = quiz_load_result_questions(conn, attempt, qids, seed)

        path = quiz_attempt_note_path(int(assignment["id"]), attempt["student_name"], attempt["submitted_at"])
        seconds = quiz_time_taken_seconds(attempt)
        pct = round((float(attempt["score_correct"] or 0) / max(1, int(attempt["score_total"] or 1))) * 100, 1)

        fm = quiz_frontmatter({
            "title": f'{attempt["student_name"]} — {assignment["title_bg"]}',
            "type": "quiz_attempt",
            "assignment_id": assignment["id"],
            "student_name": attempt["student_name"],
            "score_correct": attempt["score_correct"],
            "score_total": attempt["score_total"],
            "time_taken_seconds": seconds,
            "submitted_at": attempt["submitted_at"],
            "tags": ["quiz", "attempt"],
        })

        parts = [fm, "", "# Резултат", ""]
        parts.append(f'**Точен резултат:** {attempt["score_correct"]}/{attempt["score_total"]} ({pct}%)')
        parts.append(f"**Време:** {quiz_format_duration(seconds)}")
        parts.append("")
        parts.append("## Въпроси")
        parts.append("")

        for i, q in enumerate(questions, 1):
            parts.append(f"### Q{i}: {quiz_markdown_escape(q['prompt'])}")
            parts.append("")
            parts.append(f"- Отговор на ученика: {quiz_markdown_escape(q['chosen_text'] or 'няма отговор')}")
            parts.append(f"- Правилен отговор: {quiz_markdown_escape(q['correct_text'])}")
            parts.append(f"- Резултат: {'✓ вярно' if q['is_correct'] else '✗ грешно'}")
            parts.append("")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(parts), encoding="utf-8")
        conn.close()
    except Exception as e:
        print(f"[quiz vault] attempt note failed: {e}", file=_quiz_sys.stderr)


def quiz_load_result_questions(conn, attempt, question_ids: list[int], seed: str) -> list[dict]:
    questions = quiz_load_questions(conn, question_ids, seed, include_correct=True)
    result_question_ids = [int(q["id"]) for q in questions]
    question_explanations = fetch_question_explanations(conn, result_question_ids)
    option_explanations = fetch_option_explanations(conn, result_question_ids)
    answer_rows = conn.execute("""
        SELECT question_id, chosen_letter, is_correct
        FROM quiz_answers
        WHERE attempt_id = ?
    """, (attempt["id"],)).fetchall()
    answers = {int(r["question_id"]): r for r in answer_rows}

    for q in questions:
        ans = answers.get(int(q["id"]))
        chosen_letter = ans["chosen_letter"] if ans else None

        chosen_text = None
        correct_text = None

        for opt in q["options"]:
            if opt["option_letter"] == chosen_letter:
                chosen_text = opt["option_text"]
            if int(opt.get("is_correct") or 0) == 1:
                correct_text = opt["option_text"]

        q["chosen_letter"] = chosen_letter
        q["chosen_text"] = chosen_text
        q["correct_text"] = correct_text or "—"
        q["is_correct"] = bool(ans and ans["is_correct"])
        q["feedback_explanation"] = (
            option_explanations.get((int(q["id"]), chosen_letter))
            or question_explanations.get(int(q["id"]))
            or QUIZ_NO_EXPLANATION_MESSAGE
        )

    return questions


TEACHER_OPEN_ANSWER_STATUSES = {"correct", "partially_correct", "incorrect"}
TEACHER_OPEN_ANSWER_STATUS_LABELS = {
    "correct": "Вярно",
    "partially_correct": "Частично вярно",
    "incorrect": "Грешно",
}
QUIZ_TEXT_AUTO_STATUS_LABELS = {
    "correct": "Автоматично съвпадение",
    "incorrect": "Няма автоматично съвпадение",
    "pending": "Предстои проверка",
}


def quiz_text_auto_points(row) -> float:
    try:
        possible = float(_quiz_mapping_get(row, "points_possible") or 0)
    except (TypeError, ValueError):
        possible = 0.0
    return possible if _quiz_mapping_get(row, "is_correct") == 1 else 0.0



def prepare_quiz_text_answer_review(answer: dict) -> dict:
    auto_status = "pending"
    if answer.get("is_correct") == 1:
        auto_status = "correct"
    elif answer.get("is_correct") is not None:
        auto_status = "incorrect"

    teacher_status = answer.get("teacher_override_status")
    if teacher_status not in TEACHER_OPEN_ANSWER_STATUSES:
        teacher_status = None
    if answer.get("teacher_override") == 1 and teacher_status is None:
        teacher_status = "correct"
        try:
            answer["points_awarded"] = float(answer.get("points_possible") or 0)
        except (TypeError, ValueError):
            answer["points_awarded"] = 0.0

    answer["auto_status"] = auto_status
    answer["auto_status_label"] = QUIZ_TEXT_AUTO_STATUS_LABELS[auto_status]
    answer["auto_points_awarded"] = quiz_text_auto_points(answer)
    answer["teacher_override_status"] = teacher_status
    answer["teacher_override_status_label"] = (
        TEACHER_OPEN_ANSWER_STATUS_LABELS.get(teacher_status)
        if teacher_status
        else "Няма учителски override"
    )
    answer["teacher_reviewed"] = bool(answer.get("teacher_override") and teacher_status)
    return answer


# ---------------------------------------------------------------------------
# Attempt lifecycle (used by web.blueprints.quiz)
# ---------------------------------------------------------------------------


class QuizPlanError(ValueError):
    """An assignment stores a question plan that cannot be parsed."""


class QuizAttemptStart(NamedTuple):
    attempt_id: int
    submitted: bool
    created: bool


def quiz_fetch_attempt(conn, attempt_id: int):
    return conn.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone()


def _quiz_find_attempt(conn, assignment_id: int, student_name: str):
    return conn.execute("""
        SELECT *
        FROM quiz_attempts
        WHERE assignment_id = ?
          AND student_name = ?
    """, (assignment_id, student_name)).fetchone()


def quiz_start_or_resume_attempt(conn, assignment, student_name: str) -> QuizAttemptStart:
    """Return the student's existing attempt, or create and commit a new one.

    A concurrent insert for the same (assignment, student) is resolved to the
    competing attempt through the unique constraint. Raises :class:`QuizPlanError`
    for an unreadable stored plan; the caller decides the HTTP response.
    """
    assignment_id = int(assignment["id"])
    existing = _quiz_find_attempt(conn, assignment_id, student_name)
    if existing:
        return QuizAttemptStart(int(existing["id"]), bool(existing["submitted_at"]), False)

    assignment_question_plan = quiz_parse_assignment_question_plan(assignment["question_plan_json"])
    if (assignment["question_plan_json"] or "").strip() and assignment_question_plan is None:
        raise QuizPlanError("unreadable assignment question plan")

    if assignment_question_plan:
        seed = quiz_seed(assignment_id, student_name)
        question_ids_json = _quiz_json.dumps(assignment_question_plan, ensure_ascii=False)
        score_total = len([
            qid
            for qid in assignment_question_plan["question_ids"]
            if qid not in set(assignment_question_plan["open_question_ids"])
        ])
    else:
        seed, question_ids = quiz_pick_questions(conn, assignment, student_name)
        question_ids_json = _quiz_json.dumps(question_ids)
        score_total = len(question_ids)

    try:
        cur = conn.execute("""
            INSERT INTO quiz_attempts (assignment_id, student_name, seed, question_ids_json, score_total)
            VALUES (?, ?, ?, ?, ?)
        """, (
            assignment_id,
            student_name,
            seed,
            question_ids_json,
            score_total,
        ))
    except sqlite3.IntegrityError:
        existing = _quiz_find_attempt(conn, assignment_id, student_name)
        if existing:
            return QuizAttemptStart(int(existing["id"]), bool(existing["submitted_at"]), False)
        raise
    attempt_id = cur.lastrowid
    conn.commit()
    return QuizAttemptStart(attempt_id, False, True)


def quiz_attempt_open_question_ids(attempt_question_plan: dict) -> list[int]:
    return (
        attempt_question_plan["open_question_ids"]
        if attempt_question_plan["mixed_open_enabled"]
        else []
    )


def quiz_attempt_mixed_status(attempt_question_plan: dict) -> dict:
    return {
        "is_mixed": bool(attempt_question_plan["mixed_open_enabled"]),
        "open_count": (
            len(attempt_question_plan["open_question_ids"])
            if attempt_question_plan["mixed_open_enabled"]
            else 0
        ),
        "combined_score": bool(
            attempt_question_plan["include_open_answers_in_final_score"]
        ),
    }


def quiz_multiple_choice_question_ids(conn, question_ids: list[int]) -> list[int]:
    question_types = quiz_question_types_by_id(conn, question_ids)
    return [
        qid
        for qid in question_ids
        if question_types.get(int(qid)) == "multiple_choice"
    ]


def quiz_submit_attempt(
    conn,
    attempt_id: int,
    *,
    mc_question_ids: list[int],
    question_ids: list[int],
    open_question_ids: list[int],
    form,
) -> tuple[int, int]:
    """Grade the multiple-choice answers, store all answers and mark the attempt submitted.

    Answers, planned open-text answers and the attempt update are one transaction,
    committed at the end; an error before that leaves nothing stored. Returns
    ``(score, total)``.
    """
    correct_by_qid = quiz_correct_mc_letters_by_id(conn, mc_question_ids)

    score = 0
    # Stale invalid IDs and planned open fields are excluded from MC scoring.
    total = len(mc_question_ids)

    for qid in mc_question_ids:
        chosen = form.get(f"q_{qid}")
        correct_letter = correct_by_qid.get(int(qid))
        is_correct = 1 if chosen and correct_letter and chosen == correct_letter else 0
        score += is_correct

        conn.execute("""
            INSERT INTO quiz_answers (attempt_id, question_id, chosen_letter, is_correct)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(attempt_id, question_id)
            DO UPDATE SET chosen_letter = excluded.chosen_letter,
                          is_correct = excluded.is_correct
        """, (attempt_id, qid, chosen, is_correct))

    quiz_record_planned_open_text_answers(
        conn,
        attempt_id=attempt_id,
        question_ids=question_ids,
        open_question_ids=open_question_ids,
        form=form,
    )

    conn.execute("""
        UPDATE quiz_attempts
        SET submitted_at = CURRENT_TIMESTAMP,
            score_correct = ?,
            score_total = ?
        WHERE id = ?
    """, (score, total, attempt_id))

    conn.commit()
    return score, total


def quiz_result_context(conn, attempt, attempt_question_plan: dict) -> dict:
    """Everything the result page shows about a submitted attempt, minus the rows themselves."""
    attempt_id = int(attempt["id"])
    stored_qids = attempt_question_plan["question_ids"]
    # MC questions remain the only scored result items; open answers render as read-only review.
    qids, _skipped_count = filter_renderable_attempt_question_ids(conn, stored_qids)
    original_question_count = len(stored_qids or [])
    renderable_question_count = len(qids)
    skipped_question_count = max(0, original_question_count - renderable_question_count)
    questions = quiz_load_result_questions(conn, attempt, qids, attempt["seed"])
    difficulty_breakdown = quiz_difficulty_breakdown_from_questions(questions)
    open_text_answers = fetch_quiz_text_answers_for_attempt(conn, attempt_id)
    open_text_subtotal = (
        quiz_text_answer_informational_subtotal(open_text_answers)
        if open_text_answers
        else None
    )
    combined_score = quiz_combined_score_summary(
        attempt,
        open_text_subtotal,
        enabled=bool(attempt_question_plan["include_open_answers_in_final_score"]),
    )
    return {
        "questions": questions,
        "time_taken": quiz_attempt_duration_display(attempt),
        "success_rate": quiz_attempt_success_rate_display(attempt),
        "difficulty_breakdown": difficulty_breakdown,
        "open_text_answers": open_text_answers,
        "open_text_subtotal": open_text_subtotal,
        "combined_score": combined_score,
        "stale_attempt_message": STALE_ATTEMPT_MESSAGE if not questions else None,
        "original_question_count": original_question_count,
        "renderable_question_count": renderable_question_count,
        "skipped_question_count": skipped_question_count,
    }


def fetch_dzi_pool_health(conn: sqlite3.Connection, source_slug: str = "may_2025_v2") -> dict | None:
    """Pool health of one official exam source, read on the caller's connection."""
    exam = dzi_exams.dzi_find_exam(conn, source_slug)
    if exam is None:
        return None

    # SAFE: QUIZ_APPROVED_FILTER is a static SQL fragment; exam id uses ?.
    rows = conn.execute(f"""
        SELECT DISTINCT q.id, q.prompt, q.question_type, q.has_image, q.image_path
        FROM exam_tasks et
        JOIN exam_task_questions etq
          ON etq.task_id = et.id
         AND etq.role = 'primary'
        JOIN questions q
          ON q.id = etq.question_id
        WHERE et.exam_id = ?
          AND et.task_number BETWEEN 1 AND 25
          AND {QUIZ_APPROVED_FILTER}
        ORDER BY et.task_number, q.id
    """, (exam["id"],)).fetchall()

    usable_count = 0
    not_yet_supported_count = 0
    invalid_mc_count = 0

    for row in rows:
        if row["question_type"] != "multiple_choice":
            not_yet_supported_count += 1
        elif is_quiz_question_eligible(conn, row):
            usable_count += 1
        else:
            invalid_mc_count += 1

    imported_count = len(rows)
    return {
        "source_slug": source_slug,
        "imported_count": imported_count,
        "usable_count": usable_count,
        "filtered_count": imported_count - usable_count,
        "not_yet_supported_count": not_yet_supported_count,
        "invalid_mc_count": invalid_mc_count,
    }

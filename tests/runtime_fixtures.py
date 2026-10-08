"""Explicit, deterministic runtime rows for tests (G2).

A database built from migrations + canonical content has **no** runtime state:
``quiz_assignments``, ``quiz_attempts``, ``quiz_answers``, ``quiz_text_answers`` and the
practical submission tables start empty. The retired legacy seed happened to carry smoke-test
rows (2 assignments, 4 attempts, 19 answers); tests must not depend on those. A test that
needs runtime state creates exactly the rows it needs with these helpers, on its own
temporary database, and never on the canonical test template.

Every helper takes an open connection to a temporary database, inserts with explicit
fixed values, and returns the new row id. Callers commit.
"""

from __future__ import annotations

import json
import sqlite3

FIXTURE_SEED = "runtime-fixture-seed"
FIXTURE_STARTED_AT = "2026-01-01 10:00:00"
FIXTURE_SUBMITTED_AT = "2026-01-01 10:20:00"


def first_section_id(conn: sqlite3.Connection) -> int:
    """The lowest-id curriculum section (stable: canonical ids never change)."""
    return conn.execute("SELECT MIN(id) FROM curriculum_sections").fetchone()[0]


def first_multiple_choice_question_ids(conn: sqlite3.Connection, count: int = 3) -> list[int]:
    """The lowest-id multiple-choice questions that have options (stable canonical ids)."""
    rows = conn.execute(
        "SELECT q.id FROM questions q WHERE q.question_type = 'multiple_choice' "
        "AND EXISTS (SELECT 1 FROM multiple_choice_options o WHERE o.question_id = q.id) "
        "ORDER BY q.id LIMIT ?", (count,)).fetchall()
    return [row[0] for row in rows]


def add_assignment(conn: sqlite3.Connection, *, section_id: int | None = None,
                   title: str = "Fixture assignment", question_count: int = 3,
                   question_plan: list | None = None, public_token: str | None = None) -> int:
    """A raw assignment row. Without ``public_token`` it has none, like an assignment from
    before migration 017, and therefore no student link; pass one to make it enterable."""
    cursor = conn.execute(
        "INSERT INTO quiz_assignments (section_id, title_bg, question_count, time_limit_minutes, "
        "created_at, question_plan_json, public_token) VALUES (?, ?, ?, NULL, ?, ?, ?)",
        (section_id if section_id is not None else first_section_id(conn), title, question_count,
         FIXTURE_STARTED_AT, None if question_plan is None else json.dumps(question_plan), public_token))
    return int(cursor.lastrowid)


def add_attempt(conn: sqlite3.Connection, assignment_id: int, *, student_name: str = "Fixture student",
                question_ids: list[int] | None = None, submitted: bool = False,
                score_correct: int | None = None, user_id: int | None = None) -> int:
    """An attempt on ``assignment_id``; ``submitted`` stamps a submission time and score.

    Without ``user_id`` it is a historical attempt (no account, browser-bound)."""
    ids = list(question_ids) if question_ids is not None else first_multiple_choice_question_ids(conn)
    if submitted and score_correct is None:
        score_correct = len(ids)
    cursor = conn.execute(
        "INSERT INTO quiz_attempts (assignment_id, user_id, student_name, seed, question_ids_json, started_at, "
        "submitted_at, score_correct, score_total) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (assignment_id, user_id, student_name, FIXTURE_SEED, json.dumps(ids), FIXTURE_STARTED_AT,
         FIXTURE_SUBMITTED_AT if submitted else None,
         score_correct if submitted else None, len(ids) if submitted else None))
    return int(cursor.lastrowid)


def add_answer(conn: sqlite3.Connection, attempt_id: int, question_id: int, *,
               chosen_letter: str | None = None, is_correct: int | None = None) -> int:
    """An answer; by default the question's correct option is chosen."""
    correct = conn.execute(
        "SELECT option_letter FROM multiple_choice_options WHERE question_id = ? AND is_correct = 1 "
        "ORDER BY option_letter LIMIT 1", (question_id,)).fetchone()
    letter = chosen_letter if chosen_letter is not None else (correct[0] if correct else None)
    if is_correct is None:
        is_correct = int(correct is not None and letter == correct[0])
    cursor = conn.execute(
        "INSERT INTO quiz_answers (attempt_id, question_id, chosen_letter, is_correct) VALUES (?, ?, ?, ?)",
        (attempt_id, question_id, letter, is_correct))
    return int(cursor.lastrowid)


def add_submitted_attempt(conn: sqlite3.Connection, *, student_name: str = "Fixture student",
                          question_count: int = 3) -> tuple[int, int]:
    """One assignment with one fully answered, submitted attempt: ``(assignment_id, attempt_id)``."""
    ids = first_multiple_choice_question_ids(conn, question_count)
    assignment_id = add_assignment(conn, question_count=len(ids))
    attempt_id = add_attempt(conn, assignment_id, student_name=student_name, question_ids=ids, submitted=True)
    for question_id in ids:
        add_answer(conn, attempt_id, question_id)
    return assignment_id, attempt_id

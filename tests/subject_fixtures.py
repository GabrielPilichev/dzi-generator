"""Synthetic curriculum rows for subject tests.

Callers pass a connection to a temporary database (``tests.support``); nothing
here opens the repository database or decides a subject's real curriculum.
"""

from __future__ import annotations

import io
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from src import subject_registry
from src.manage_migrations import apply_migrations
from src.subject_registry import SubjectDefinition, validate_registry
from tests.support import ROOT


def apply_pending_migrations(db_path) -> None:
    """Bring a temporary database copy up to the current schema with the real runner.

    The seed copy may predate the newest migration; a no-op once it does not.
    Never call this with the repository database.
    """
    path = Path(db_path).resolve()
    if path == (ROOT / "data" / "questions.db").resolve():
        raise AssertionError("refusing to migrate the repository database from a test")
    if apply_migrations(path, ROOT / "web" / "migrations", out=io.StringIO()) != 0:
        raise AssertionError("migrations failed on the temporary database")


def add_curriculum(conn: sqlite3.Connection, subject: str, grade: int,
                   education_type: str = "general", *,
                   title: str = "Synthetic curriculum") -> int:
    return int(conn.execute(
        "INSERT INTO curricula (grade, subject, education_type, title_bg) VALUES (?, ?, ?, ?)",
        (grade, subject, education_type, title),
    ).lastrowid)


def add_module(conn: sqlite3.Connection, curriculum_id: int, slug: str, title: str, *,
               number: int, grade: int) -> int:
    """``module_number`` is globally unique in the schema; pass an unused one."""
    return int(conn.execute(
        """
        INSERT INTO curriculum_modules (module_number, title_bg, class, curriculum_id, module_slug)
        VALUES (?, ?, ?, ?, ?)
        """,
        (number, title, grade, curriculum_id, slug),
    ).lastrowid)


def add_section(conn: sqlite3.Connection, curriculum_id: int | None, slug: str,
                title: str = "Synthetic section", *, grade: int,
                section_type: str = "content", module_id: int | None = None,
                display_order: int | None = None) -> int:
    return int(conn.execute(
        """
        INSERT INTO curriculum_sections
            (curriculum_id, module_id, section_slug, title_bg, class, section_type, display_order)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (curriculum_id, module_id, slug, title, grade, section_type, display_order),
    ).lastrowid)


def add_membership(conn: sqlite3.Connection, topic_id: int, section_id: int, *,
                   primary: bool = False) -> None:
    conn.execute(
        "INSERT INTO topic_section_assignments (topic_id, section_id, is_primary) VALUES (?, ?, ?)",
        (topic_id, section_id, int(primary)),
    )


def set_topic_order(conn: sqlite3.Connection, topic_id: int, section_id: int,
                    display_order: int | None) -> None:
    """Explicit order of a topic in a section (needs migration 010)."""
    conn.execute(
        "UPDATE topic_section_assignments SET display_order = ? WHERE topic_id = ? AND section_id = ?",
        (display_order, topic_id, section_id),
    )


def add_topic(conn: sqlite3.Connection, section_id: int, slug: str, title: str | None = None, *,
              grade: int | None = None, description: str | None = None) -> int:
    """A topic with its primary section membership; ``grade`` also adds its class."""
    topic_id = int(conn.execute(
        "INSERT INTO curriculum_topics (section_id, topic_slug, title_bg, description) "
        "VALUES (?, ?, ?, ?)",
        (section_id, slug, title if title is not None else slug, description),
    ).lastrowid)
    add_membership(conn, topic_id, section_id, primary=True)
    if grade is not None:
        conn.execute("INSERT INTO topic_classes (topic_id, class) VALUES (?, ?)", (topic_id, grade))
    return topic_id


def add_question(conn: sqlite3.Connection, topic_id: int | None, subject: str | None,
                 number: int, *, source: str, prompt: str | None = None,
                 kind: str = "multiple_choice", options: dict[str, str] | None = None,
                 correct: str = "A", accepted_answer: str | None = None,
                 link_topic: bool = False) -> int:
    """A question row plus the answer rows its ``kind`` needs.

    ``options`` maps letters to texts (``correct`` marks one). ``accepted_answer``
    adds one fill-in subquestion. ``link_topic`` also records the question-topic
    assignment row that importers write.
    """
    question_id = int(conn.execute(
        """
        INSERT INTO questions
            (source_exam, source_number, subject, topic_id, question_type, points, prompt)
        VALUES (?, ?, ?, ?, ?, 1, ?)
        """,
        (source, number, subject, topic_id, kind,
         prompt if prompt is not None else f"Synthetic question {number}?"),
    ).lastrowid)
    for letter, text in (options or {}).items():
        conn.execute(
            """
            INSERT INTO multiple_choice_options (question_id, option_letter, option_text, is_correct)
            VALUES (?, ?, ?, ?)
            """,
            (question_id, letter, text, int(letter == correct)),
        )
    if accepted_answer is not None:
        conn.execute(
            """
            INSERT INTO fill_in_subquestions (question_id, subquestion_number, correct_answer)
            VALUES (?, 1, ?)
            """,
            (question_id, accepted_answer),
        )
    if link_topic:
        conn.execute(
            """
            INSERT INTO question_topic_assignments (question_id, topic_id, assignment_type, is_active)
            VALUES (?, ?, 'test', 1)
            """,
            (question_id, topic_id),
        )
    return question_id


def four_options(label: str = "Synthetic option") -> dict[str, str]:
    return {letter: f"{label} {letter}" for letter in "ABCD"}


@contextmanager
def registered_subject(definition: SubjectDefinition):
    """Temporarily register one more subject, exactly as a registry entry would.

    Patches the registry's lookup maps only; the shipped ``SUBJECTS`` tuple and
    every route, template and stylesheet stay untouched.
    """
    validate_registry((*subject_registry.all_subjects(), definition))
    identifiers = {
        identifier: definition.id
        for identifier in (definition.id, definition.route_slug, *definition.legacy_aliases)
    }
    with patch.dict(subject_registry._SUBJECTS_BY_ID, {definition.id: definition}), \
            patch.dict(subject_registry._IDENTIFIERS, identifiers), \
            patch.dict(subject_registry._ROUTE_SLUGS, {definition.route_slug: definition.id}):
        yield definition

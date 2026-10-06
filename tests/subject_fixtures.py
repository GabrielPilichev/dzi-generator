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
from tests.support import ROOT, adopt_baseline


def _referencing_foreign_keys(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    """parent table -> [(child table, child column)] over every declared foreign key."""
    references: dict[str, list[tuple[str, str]]] = {}
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                 "AND name NOT LIKE 'sqlite_%'").fetchall():
        for row in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
            references.setdefault(row[2], []).append((table, row[3]))
    return references


def _delete_with_dependents(conn: sqlite3.Connection, references, table: str, ids: list[int]) -> None:
    """Delete ``ids`` of ``table`` after recursively deleting the rows that reference them."""
    if not ids:
        return
    marks = ",".join("?" for _ in ids)
    for child, column in references.get(table, []):
        child_ids = [row[0] for row in conn.execute(
            f'SELECT rowid FROM "{child}" WHERE "{column}" IN ({marks})', ids)]
        for start in range(0, len(child_ids), 500):
            _delete_with_dependents_rowid(conn, references, child, child_ids[start:start + 500])
    conn.execute(f'DELETE FROM "{table}" WHERE id IN ({marks})', ids)


def _delete_with_dependents_rowid(conn, references, table: str, rowids: list[int]) -> None:
    marks = ",".join("?" for _ in rowids)
    ids = [row[0] for row in conn.execute(f'SELECT id FROM "{table}" WHERE rowid IN ({marks})', rowids)] \
        if _has_id(conn, table) else None
    if ids is None:
        conn.execute(f'DELETE FROM "{table}" WHERE rowid IN ({marks})', rowids)
    else:
        _delete_with_dependents(conn, references, table, ids)


def _has_id(conn: sqlite3.Connection, table: str) -> bool:
    return any(row[1] == "id" and row[5] == 1 for row in conn.execute(f'PRAGMA table_info("{table}")'))


def remove_subject_content(db_path, subjects=("physics", "biology")) -> None:
    """Delete the curricula, sections, topics and questions of ``subjects`` from a temporary DB.

    This produces the "IT/DZI content only" state that subject-preparation tests pin (Physics
    and Biology absent, so those subjects render as in preparation). It is an explicit,
    visible fixture step on a temporary copy of the canonical test database; dependents are
    removed first through the declared foreign keys, and the result is checked with
    ``PRAGMA foreign_key_check``. Never call it with the repository database.
    """
    path = Path(db_path).resolve()
    if path == (ROOT / "data" / "questions.db").resolve():
        raise AssertionError("refusing to modify the repository database from a test")
    marks = ",".join("?" for _ in subjects)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        references = _referencing_foreign_keys(conn)
        curricula = [row[0] for row in conn.execute(
            f"SELECT id FROM curricula WHERE subject IN ({marks})", subjects)]
        sections = [row[0] for row in conn.execute(
            f"SELECT id FROM curriculum_sections WHERE curriculum_id IN ({','.join('?' for _ in curricula)})",
            curricula)] if curricula else []
        topics = [row[0] for row in conn.execute(
            f"SELECT id FROM curriculum_topics WHERE section_id IN ({','.join('?' for _ in sections)})",
            sections)] if sections else []
        questions = [row[0] for row in conn.execute(
            f"SELECT id FROM questions WHERE subject IN ({marks})", subjects)]
        with conn:
            _delete_with_dependents(conn, references, "questions", questions)
            _delete_with_dependents(conn, references, "curriculum_topics", topics)
            _delete_with_dependents(conn, references, "curriculum_sections", sections)
            _delete_with_dependents(conn, references, "curricula", curricula)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise AssertionError(f"removing {subjects} left foreign key violations: {violations[:3]}")
    finally:
        conn.close()


def apply_pending_migrations(db_path) -> None:
    """Bring a temporary database copy up to the current schema with the real runner.

    The seed copy may predate the newest migration; a no-op once it does not.
    A copy of the seed also predates the baseline (000), which an existing
    database adopts instead of running. Never call this with the repository database.
    """
    path = Path(db_path).resolve()
    if path == (ROOT / "data" / "questions.db").resolve():
        raise AssertionError("refusing to migrate the repository database from a test")
    adopt_baseline(path)
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

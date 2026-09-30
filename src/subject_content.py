"""Subject identity and ownership of stored curriculum/question content.

These helpers read existing relationships only. They do not assign a subject
from a class number or repair incomplete legacy records.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.subject_registry import IT, PHYSICS, SUBJECTS, resolve_subject_identifier

# Content identities come from the canonical registry; being registered does not
# imply available content.
KNOWN_CONTENT_SUBJECTS = frozenset(subject.id for subject in SUBJECTS)
SUBJECT_ALIASES = {
    alias: subject.id
    for subject in SUBJECTS
    for alias in (*subject.legacy_aliases, subject.route_slug)
    if alias != subject.id
}


def canonical_subject(value: object) -> str | None:
    """Resolve an explicit stored value/key/route slug; unknown stays unknown."""
    return resolve_subject_identifier(value)


def is_known_content_subject(value: object) -> bool:
    return canonical_subject(value) is not None


@dataclass(frozen=True)
class Ownership:
    subject: str | None
    status: str  # owned, unclassified, unknown, conflict, or missing
    detail: str = ""

    @property
    def is_consistent(self) -> bool:
        return self.status == "owned"


def _declared_ownership(value: object) -> Ownership:
    if value is None or (isinstance(value, str) and not value.strip()):
        return Ownership(None, "unclassified", "missing_subject")
    subject = canonical_subject(value)
    if subject is None:
        return Ownership(None, "unknown", "unknown_subject")
    return Ownership(subject, "owned")


def curriculum_ownership(conn: sqlite3.Connection, curriculum_id: int) -> Ownership:
    row = conn.execute(
        "SELECT subject FROM curricula WHERE id = ?", (curriculum_id,)
    ).fetchone()
    if row is None:
        return Ownership(None, "missing", "curriculum_not_found")
    return _declared_ownership(row[0])


def section_ownership(conn: sqlite3.Connection, section_id: int) -> Ownership:
    row = conn.execute(
        "SELECT curriculum_id FROM curriculum_sections WHERE id = ?", (section_id,)
    ).fetchone()
    if row is None:
        return Ownership(None, "missing", "section_not_found")
    if row[0] is None:
        return Ownership(None, "unclassified", "section_without_curriculum")
    return curriculum_ownership(conn, int(row[0]))


def topic_ownership(conn: sqlite3.Connection, topic_id: int) -> Ownership:
    row = conn.execute(
        "SELECT section_id FROM curriculum_topics WHERE id = ?", (topic_id,)
    ).fetchone()
    if row is None:
        return Ownership(None, "missing", "topic_not_found")
    if row[0] is None:
        return Ownership(None, "unclassified", "topic_without_primary_section")

    primary_section_id = int(row[0])
    primary = section_ownership(conn, primary_section_id)
    if not primary.is_consistent:
        return primary

    memberships = conn.execute(
        """
        SELECT section_id, is_primary
        FROM topic_section_assignments
        WHERE topic_id = ?
        """,
        (topic_id,),
    ).fetchall()
    if not any(int(member[0]) == primary_section_id for member in memberships):
        return Ownership(None, "conflict", "primary_section_not_assigned")

    for section_id, is_primary in memberships:
        section_id = int(section_id)
        if bool(is_primary) != (section_id == primary_section_id):
            return Ownership(None, "conflict", "primary_assignment_mismatch")
        member = section_ownership(conn, section_id)
        if not member.is_consistent:
            return Ownership(None, "conflict", "unclassified_topic_membership")
        if member.subject != primary.subject:
            return Ownership(None, "conflict", "cross_subject_topic_membership")
    return primary


def question_ownership(conn: sqlite3.Connection, question_id: int) -> Ownership:
    row = conn.execute(
        "SELECT subject, topic_id FROM questions WHERE id = ?", (question_id,)
    ).fetchone()
    if row is None:
        return Ownership(None, "missing", "question_not_found")
    if row[1] is None:
        return Ownership(None, "unclassified", "question_without_topic")

    topic = topic_ownership(conn, int(row[1]))
    if not topic.is_consistent:
        return topic
    declared = _declared_ownership(row[0])
    if not declared.is_consistent:
        return declared
    if declared.subject != topic.subject:
        return Ownership(None, "conflict", "question_subject_mismatch")
    return topic


def require_topic_subject(
    conn: sqlite3.Connection, topic_id: int, expected_subject: str
) -> str:
    """Reject an unowned topic or a topic outside an author's supported subject."""
    expected = canonical_subject(expected_subject)
    if expected is None:
        raise ValueError(f"Unknown content subject: {expected_subject!r}")
    owner = topic_ownership(conn, topic_id)
    if not owner.is_consistent:
        raise ValueError(f"Topic {topic_id} has no consistent subject: {owner.detail}")
    if owner.subject != expected:
        raise ValueError(f"Topic {topic_id} belongs to {owner.subject}, expected {expected}")
    return expected

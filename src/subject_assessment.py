"""Subject ownership checks for newly selected assessment questions.

New selections and assignment copies inherit their context from the stored
curriculum section. Started attempt snapshots are deliberately not reselected.
"""

from __future__ import annotations

import sqlite3

from src.subject_content import (
    IT, canonical_subject, question_ownership, section_ownership, section_ownerships,
)
from src.dzi_open_questions import fetch_dzi_open_questions


def question_belongs_to_subject(conn: sqlite3.Connection, question_id: int, subject: str) -> bool:
    expected = canonical_subject(subject)
    owner = question_ownership(conn, question_id)
    return expected is not None and owner.is_consistent and owner.subject == expected


class AssessmentScope:
    def __init__(self, conn: sqlite3.Connection, section_id: int):
        self.conn = conn
        self.section_id = int(section_id)
        owner = section_ownership(conn, self.section_id)
        if not owner.is_consistent:
            raise ValueError(f"Section {section_id} has no known subject: {owner.detail}")
        self.subject = owner.subject
        self.section_type = conn.execute(
            "SELECT section_type FROM curriculum_sections WHERE id = ?", (self.section_id,)
        ).fetchone()[0]

    @classmethod
    def for_all_sections(cls, conn: sqlite3.Connection) -> dict[int, "AssessmentScope"]:
        """Scopes for every section with a known subject, in one query.

        Equivalent to constructing ``AssessmentScope(conn, id)`` per section and
        skipping the ``ValueError`` ones.
        """
        owners = section_ownerships(conn)
        scopes: dict[int, AssessmentScope] = {}
        for section_id, section_type in conn.execute(
            "SELECT id, section_type FROM curriculum_sections"
        ):
            owner = owners[int(section_id)]
            if not owner.is_consistent:
                continue
            scope = cls.__new__(cls)
            scope.conn = conn
            scope.section_id = int(section_id)
            scope.subject = owner.subject
            scope.section_type = section_type
            scopes[scope.section_id] = scope
        return scopes

    @property
    def allows_dzi_content(self) -> bool:
        return self.section_type == "dzi_training" and self.subject == IT

    @property
    def allows_dzi_open_questions(self) -> bool:
        return self.allows_dzi_content

    def accepts(self, question_id: int) -> bool:
        row = self.conn.execute(
            """
            SELECT q.topic_id
            FROM questions q
            JOIN topic_section_assignments tsa ON tsa.topic_id = q.topic_id
            WHERE q.id = ? AND tsa.section_id = ?
            """,
            (question_id, self.section_id),
        ).fetchone()
        return row is not None and question_belongs_to_subject(
            self.conn, int(question_id), self.subject
        )

    def validate_plan(self, question_ids, open_question_ids, *, open_source_slug=None) -> None:
        """Reject forged, inconsistent, or out-of-section IDs before persistence."""
        if not isinstance(question_ids, list) or not question_ids or not isinstance(open_question_ids, list):
            raise ValueError("Invalid question plan")
        if any(type(qid) is not int or qid <= 0 for qid in question_ids + open_question_ids):
            raise ValueError("Invalid question ID")
        if len(set(question_ids)) != len(question_ids) or len(set(open_question_ids)) != len(open_question_ids):
            raise ValueError("Duplicate question ID")
        open_ids = set(open_question_ids)
        if not open_ids.issubset(question_ids):
            raise ValueError("Open question is not in the plan")
        eligible_open_ids = set()
        if open_ids and self.allows_dzi_open_questions:
            eligible_open_ids = {
                row["id"] for row in fetch_dzi_open_questions(
                    self.conn, source_slug=open_source_slug
                )
            }
        for question_id in question_ids:
            if question_id in open_ids:
                if question_id not in eligible_open_ids:
                    raise ValueError("Open question is not generation-ready in this DZI context")
                continue
            if not self.accepts(question_id):
                raise ValueError(f"Question {question_id} does not belong to this section and subject")
            row = self.conn.execute(
                "SELECT question_type, source_exam FROM questions WHERE id = ?", (question_id,)
            ).fetchone()
            if row[0] != "multiple_choice":
                raise ValueError("Invalid closed question type")

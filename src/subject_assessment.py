"""Subject ownership checks for newly selected assessment questions.

Existing persisted plans and attempt snapshots are deliberately not interpreted
here. A new selection inherits its subject from its chosen curriculum section.
"""

from __future__ import annotations

import sqlite3

from src.subject_content import canonical_subject, question_ownership, section_ownership


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
        for question_id in question_ids:
            if not self.accepts(question_id):
                raise ValueError(f"Question {question_id} does not belong to this section and subject")
            row = self.conn.execute(
                "SELECT question_type, source_exam FROM questions WHERE id = ?", (question_id,)
            ).fetchone()
            if question_id in open_ids:
                if row[0] not in {"fill_in", "short_answer"}:
                    raise ValueError("Invalid open question type")
                if open_source_slug is not None and row[1] != open_source_slug:
                    raise ValueError("Open question does not match the requested source")
            elif row[0] != "multiple_choice":
                raise ValueError("Invalid closed question type")

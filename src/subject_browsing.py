"""Read-only, subject-scoped curriculum browsing queries.

The SQL finds candidates; the F1 ownership contract decides which records may
appear. A class number or a question's declared subject alone is insufficient.
"""

from __future__ import annotations

import sqlite3

from src.subject_content import (
    IT,
    canonical_subject,
    question_ownership,
    section_ownership,
    topic_ownership,
)
from src.subject_registry import all_subjects, get_subject


def subject_content_grades(conn: sqlite3.Connection) -> dict[str, frozenset[int]]:
    """Declared grades in which each registered subject owns at least one section.

    A grade counts when a section of the subject's curriculum has the same class
    and curriculum grade, matching ``SubjectContentBrowser.sections_for_grade``.
    Availability is derived from stored content, never configured.
    """
    found: dict[str, set[int]] = {subject.id: set() for subject in all_subjects()}
    rows = conn.execute(
        """
        SELECT DISTINCT c.subject, cs.class
        FROM curriculum_sections cs
        JOIN curricula c ON c.id = cs.curriculum_id
        WHERE cs.class = c.grade
        """
    ).fetchall()
    for stored_subject, grade in rows:
        subject_id = canonical_subject(stored_subject)
        definition = get_subject(subject_id)
        if definition is not None and grade in definition.grades:
            found[subject_id].add(int(grade))
    return {subject_id: frozenset(grades) for subject_id, grades in found.items()}


class SubjectContentBrowser:
    def __init__(self, conn: sqlite3.Connection, subject: str, *, legacy_it: bool = False):
        resolved = canonical_subject(subject)
        if resolved is None:
            raise ValueError(f"Unknown content subject: {subject!r}")
        self.conn = conn
        self.subject = resolved
        self.legacy_it = legacy_it and resolved == IT
        self._section_owners = {}
        self._topic_owners = {}
        self._question_owners = {}

    def _section_owner(self, section_id: int):
        if section_id not in self._section_owners:
            self._section_owners[section_id] = section_ownership(self.conn, section_id)
        return self._section_owners[section_id]

    def _topic_owner(self, topic_id: int):
        if topic_id not in self._topic_owners:
            self._topic_owners[topic_id] = topic_ownership(self.conn, topic_id)
        return self._topic_owners[topic_id]

    def _question_owner(self, question_id: int):
        if question_id not in self._question_owners:
            self._question_owners[question_id] = question_ownership(self.conn, question_id)
        return self._question_owners[question_id]

    def owns_section(self, section_id: int) -> bool:
        owner = self._section_owner(section_id)
        return owner.is_consistent and owner.subject == self.subject

    def owns_topic(self, topic_id: int) -> bool:
        owner = self._topic_owner(topic_id)
        return owner.is_consistent and owner.subject == self.subject

    def owns_question(self, question_id: int, topic_id: int) -> bool:
        owner = self._question_owner(question_id)
        if owner.is_consistent:
            return owner.subject == self.subject
        # Existing IT/DZI browsing accepted a few questions with no declared
        # subject. Preserve that only when their topic is unequivocally IT.
        return (
            self.legacy_it
            and owner.status == "unclassified"
            and owner.detail == "missing_subject"
            and self.owns_topic(topic_id)
        )

    def grade_counts(self) -> list[dict]:
        """Existing class/topic-count semantics, with subject-owned content only."""
        grades = get_subject(self.subject).grades
        # SAFE: placeholders are generated from the registry's integer grades.
        placeholders = ",".join("?" for _ in grades)
        rows = self.conn.execute(
            f"""
            SELECT tc.class AS grade, tc.topic_id, q.id AS question_id
            FROM topic_classes tc
            JOIN curriculum_topics t ON t.id = tc.topic_id
            LEFT JOIN questions q ON q.topic_id = t.id
                AND (q.is_ai_generated = 0 OR q.quality_score >= 1.0)
            WHERE tc.class IN ({placeholders})
            ORDER BY tc.class
            """,
            grades,
        ).fetchall()
        by_grade: dict[int, dict] = {}
        for grade, topic_id, question_id in rows:
            grade, topic_id = int(grade), int(topic_id)
            if not self.owns_topic(topic_id):
                continue
            entry = by_grade.setdefault(grade, {"topics": set(), "questions": set()})
            entry["topics"].add(topic_id)
            if question_id is not None and self.owns_question(int(question_id), topic_id):
                entry["questions"].add(int(question_id))
        return [
            {"grade": grade, "topic_count": len(entry["topics"]),
             "question_count": len(entry["questions"])}
            for grade, entry in sorted(by_grade.items())
        ]

    def sections_for_grade(self, grade: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT cs.id AS section_id, cs.class AS grade,
                   cs.section_slug, cs.title_bg AS section_title,
                   cs.section_type, cs.has_section_test AS has_test,
                   cm.module_slug, cm.title_bg AS module_title,
                   cm.module_number
            FROM curriculum_sections cs
            JOIN curricula c ON c.id = cs.curriculum_id
            LEFT JOIN curriculum_modules cm ON cm.id = cs.module_id
            WHERE cs.class = ? AND c.grade = ?
            ORDER BY COALESCE(cm.module_number, 99), cs.display_order, cs.title_bg
            """,
            (grade, grade),
        ).fetchall()
        sections = []
        for row in rows:
            section = dict(row)
            if not self.owns_section(int(section["section_id"])):
                continue
            section["question_count"] = len(self.questions_in_section(section["section_id"]))
            sections.append(section)
        return sections

    def section(self, slug: str, *, grade: int | None = None,
                legacy_unclassified: bool = False) -> dict | None:
        row = self.conn.execute(
            """
            SELECT cs.id AS section_id, cs.section_slug, cs.title_bg AS section_title,
                   cs.class AS grade, c.grade AS curriculum_grade,
                   cm.title_bg AS module_title, cm.module_number
            FROM curriculum_sections cs
            LEFT JOIN curricula c ON c.id = cs.curriculum_id
            LEFT JOIN curriculum_modules cm ON cm.id = cs.module_id
            WHERE cs.section_slug = ?
            """,
            (slug,),
        ).fetchone()
        if row is None:
            return None
        section = dict(row)
        if grade is not None and (section["grade"] != grade or section["curriculum_grade"] != grade):
            return None
        owner = self._section_owner(int(section["section_id"]))
        if owner.is_consistent and owner.subject == self.subject:
            return section
        if (self.legacy_it and legacy_unclassified
                and owner.status == "unclassified"
                and owner.detail == "section_without_curriculum"):
            return section
        return None

    def topics_in_section(self, section_id: int) -> list[dict]:
        if not self.owns_section(section_id):
            return []
        rows = self.conn.execute(
            """
            SELECT t.id AS topic_id, t.topic_slug, t.title_bg AS topic_title
            FROM topic_section_assignments tsa
            JOIN curriculum_topics t ON t.id = tsa.topic_id
            WHERE tsa.section_id = ?
            ORDER BY t.title_bg, t.id
            """,
            (section_id,),
        ).fetchall()
        return [dict(row) for row in rows if self.owns_topic(int(row["topic_id"]))]

    def questions_in_section(self, section_id: int) -> list[dict]:
        if not self.owns_section(section_id):
            return []
        rows = self.conn.execute(
            """
            SELECT DISTINCT q.id AS question_id, q.topic_id, q.prompt,
                   q.question_type, q.difficulty,
                   t.topic_slug, t.title_bg AS topic_title
            FROM topic_section_assignments tsa
            JOIN curriculum_topics t ON t.id = tsa.topic_id
            JOIN questions q ON q.topic_id = t.id
            WHERE tsa.section_id = ?
              AND (q.is_ai_generated = 0 OR q.quality_score >= 1.0)
            ORDER BY t.topic_slug, q.id
            """,
            (section_id,),
        ).fetchall()
        return [
            dict(row) for row in rows
            if self.owns_question(int(row["question_id"]), int(row["topic_id"]))
        ]

    def search_titles(self, sections: list[dict]) -> str:
        """Subject-owned section/topic titles for the homepage subject search."""
        titles = set()
        for section in sections:
            titles.add(section["section_title"])
            for topic in self.topics_in_section(section["section_id"]):
                titles.add(topic["topic_title"])
        return " ".join(sorted(titles))

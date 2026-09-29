"""Import a reviewed, text-only MC subject batch without DZI exam records.

Dry run is the default. Both modes exercise the same validation and insertion
path; dry run does so on an in-memory copy of the target database.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

from src.subject_assessment import AssessmentScope
from src.subject_content import (
    canonical_subject, curriculum_ownership, question_ownership,
    section_ownership, topic_ownership,
)


LETTERS = ("A", "B", "C", "D")
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _keys(value, required, label):
    if not isinstance(value, dict) or set(value) != set(required):
        raise ValueError(f"{label} must contain exactly: {', '.join(required)}")


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be non-empty, trimmed text")
    return value


def validate_batch(batch):
    """Validate the complete reviewed format before any database work."""
    _keys(batch, ("format_version", "subject", "grade", "source", "curriculum",
                  "section", "topic", "questions"), "batch")
    if type(batch["format_version"]) is not int or batch["format_version"] != 1:
        raise ValueError("Unsupported format_version")
    subject = canonical_subject(batch["subject"])
    if subject is None:
        raise ValueError(f"Unknown content subject: {batch['subject']!r}")
    grade = batch["grade"]
    if type(grade) is not int or grade not in range(8, 13):
        raise ValueError("grade must be an integer from 8 to 12")
    source = _text(batch["source"], "source")
    if not source.startswith(f"reviewed:{subject}:") or len(source.split(":", 2)[2]) == 0:
        raise ValueError("source must be a stable reviewed:<subject>:<batch> identifier")
    curriculum = batch["curriculum"]
    _keys(curriculum, ("subject", "grade", "education_type", "title"), "curriculum")
    if canonical_subject(curriculum["subject"]) != subject or curriculum["grade"] != grade:
        raise ValueError("Curriculum subject/grade does not match batch")
    _text(curriculum["education_type"], "education_type")
    _text(curriculum["title"], "curriculum title")
    section = batch["section"]
    _keys(section, ("slug", "title", "grade"), "section")
    topic = batch["topic"]
    _keys(topic, ("slug", "title", "grade"), "topic")
    for label, item in (("section", section), ("topic", topic)):
        slug = _text(item["slug"], f"{label} slug")
        if not SLUG.fullmatch(slug) or not slug.startswith(f"{subject}-g{grade}-"):
            raise ValueError(f"{label} slug must be namespaced {subject}-g{grade}-...")
        _text(item["title"], f"{label} title")
        if type(item["grade"]) is not int or item["grade"] != grade:
            raise ValueError(f"{label} grade does not match batch")
    questions = batch["questions"]
    if not isinstance(questions, list) or not questions:
        raise ValueError("questions must be a non-empty list")
    numbers = set()
    for question in questions:
        _keys(question, ("number", "subject", "grade", "topic_slug", "type",
                         "prompt", "options", "correct"), "question")
        number = question["number"]
        if type(number) is not int or number <= 0 or number in numbers:
            raise ValueError("question numbers must be unique positive integers")
        numbers.add(number)
        if (canonical_subject(question["subject"]) != subject
                or type(question["grade"]) is not int or question["grade"] != grade
                or question["topic_slug"] != topic["slug"]):
            raise ValueError("Question subject/grade/topic does not match batch")
        if question["type"] != "multiple_choice":
            raise ValueError("Only multiple_choice questions are supported")
        _text(question["prompt"], "question prompt")
        options = question["options"]
        if not isinstance(options, dict) or set(options) != set(LETTERS):
            raise ValueError("MC options must contain exactly A, B, C, D")
        for letter in LETTERS:
            option_text = _text(options[letter], f"option {letter}")
            if option_text in {"-", "—", "[]"}:
                raise ValueError(f"option {letter} is not an eligible MC answer")
        if question["correct"] not in LETTERS:
            raise ValueError("correct must be one of A, B, C, D")
    return subject


def _owned(owner, subject, label):
    if not owner.is_consistent or owner.subject != subject:
        raise ValueError(f"{label} has conflicting ownership: {owner.status}/{owner.detail}")


def _one(conn, sql, params, label):
    rows = conn.execute(sql, params).fetchall()
    if len(rows) > 1:
        raise ValueError(f"Ambiguous existing {label}")
    return rows[0] if rows else None


def _import_in_transaction(conn, batch, subject):
    """Insert or exactly reuse rows. Caller owns the transaction boundary."""
    grade = batch["grade"]
    summary = {key: {"inserted": 0, "reused": 0} for key in
               ("curricula", "sections", "topics", "questions")}
    existing_numbers = {row[0] for row in conn.execute(
        "SELECT source_number FROM questions WHERE source_exam = ?", (batch["source"],))}
    requested_numbers = {question["number"] for question in batch["questions"]}
    if existing_numbers and existing_numbers != requested_numbers:
        raise ValueError("Existing source has a different set of question numbers")

    curriculum = batch["curriculum"]
    matches = [row for row in conn.execute(
        "SELECT id, subject, title_bg FROM curricula WHERE grade = ? AND education_type = ?",
        (grade, curriculum["education_type"]),
    ) if canonical_subject(row["subject"]) == subject]
    if len(matches) > 1:
        raise ValueError("Ambiguous existing curriculum identity")
    if matches:
        row = matches[0]
        if row["title_bg"] != curriculum["title"]:
            raise ValueError("Conflicting curriculum title")
        curriculum_id = row["id"]
        summary["curricula"]["reused"] += 1
    else:
        curriculum_id = conn.execute(
            "INSERT INTO curricula (grade, subject, education_type, title_bg, source_type) "
            "VALUES (?, ?, ?, ?, 'reviewed_subject_import')",
            (grade, subject, curriculum["education_type"], curriculum["title"]),
        ).lastrowid
        summary["curricula"]["inserted"] += 1
    _owned(curriculum_ownership(conn, curriculum_id), subject, "Curriculum")

    section = batch["section"]
    row = conn.execute("SELECT * FROM curriculum_sections WHERE section_slug = ?",
                       (section["slug"],)).fetchone()
    if row:
        if (row["curriculum_id"] != curriculum_id or row["class"] != grade
                or row["title_bg"] != section["title"] or row["section_type"] != "content"):
            raise ValueError("Conflicting section slug/content/grade")
        section_id = row["id"]
        summary["sections"]["reused"] += 1
    else:
        section_id = conn.execute(
            "INSERT INTO curriculum_sections "
            "(curriculum_id, section_slug, title_bg, class, section_type, source_type) "
            "VALUES (?, ?, ?, ?, 'content', 'reviewed_subject_import')",
            (curriculum_id, section["slug"], section["title"], grade),
        ).lastrowid
        summary["sections"]["inserted"] += 1
    _owned(section_ownership(conn, section_id), subject, "Section")

    topic = batch["topic"]
    row = conn.execute("SELECT * FROM curriculum_topics WHERE topic_slug = ?",
                       (topic["slug"],)).fetchone()
    if row:
        if row["section_id"] != section_id or row["title_bg"] != topic["title"]:
            raise ValueError("Conflicting topic slug/content/section")
        topic_id = row["id"]
        membership = conn.execute(
            "SELECT relationship_type, is_primary FROM topic_section_assignments "
            "WHERE topic_id = ? AND section_id = ?", (topic_id, section_id),
        ).fetchone()
        classes = {r[0] for r in conn.execute(
            "SELECT class FROM topic_classes WHERE topic_id = ?", (topic_id,))}
        if membership is None or tuple(membership) != ("covers", 1) or classes != {grade}:
            raise ValueError("Conflicting topic section/class relationships")
        summary["topics"]["reused"] += 1
    else:
        topic_id = conn.execute(
            "INSERT INTO curriculum_topics (topic_slug, title_bg, section_id) VALUES (?, ?, ?)",
            (topic["slug"], topic["title"], section_id),
        ).lastrowid
        conn.execute(
            "INSERT INTO topic_section_assignments "
            "(topic_id, section_id, relationship_type, is_primary, source_type) "
            "VALUES (?, ?, 'covers', 1, 'reviewed_subject_import')",
            (topic_id, section_id),
        )
        conn.execute("INSERT INTO topic_classes (topic_id, class) VALUES (?, ?)",
                     (topic_id, grade))
        summary["topics"]["inserted"] += 1
    _owned(topic_ownership(conn, topic_id), subject, "Topic")

    scope = AssessmentScope(conn, section_id)
    for question in batch["questions"]:
        row = _one(conn,
                   "SELECT * FROM questions WHERE source_exam = ? AND source_number = ?",
                   (batch["source"], question["number"]), "question source identity")
        if row:
            if (row["exam_id"] is not None or row["subject"] != subject
                    or row["level"] != str(grade) or row["topic_id"] != topic_id
                    or row["question_type"] != "multiple_choice"
                    or row["prompt"] != question["prompt"] or row["points"] != 1
                    or row["has_image"] != 0 or row["image_path"] is not None
                    or row["is_ai_generated"] != 0):
                raise ValueError(f"Conflicting question {question['number']}")
            question_id = row["id"]
            actual_options = {r["option_letter"]: (r["option_text"], r["is_correct"])
                              for r in conn.execute(
                                  "SELECT option_letter, option_text, is_correct "
                                  "FROM multiple_choice_options WHERE question_id = ?",
                                  (question_id,))}
            expected_options = {letter: (question["options"][letter],
                                         int(letter == question["correct"])) for letter in LETTERS}
            if actual_options != expected_options:
                raise ValueError(f"Conflicting options for question {question['number']}")
            assignments = conn.execute(
                "SELECT topic_id, section_id, assignment_type, is_active "
                "FROM question_topic_assignments WHERE question_id = ?", (question_id,),
            ).fetchall()
            if len(assignments) != 1 or tuple(assignments[0]) != (
                    topic_id, section_id, "reviewed_subject_import", 1):
                raise ValueError(f"Conflicting question relationships for {question['number']}")
            summary["questions"]["reused"] += 1
        else:
            question_id = conn.execute(
                "INSERT INTO questions "
                "(exam_id, source_exam, source_number, subject, level, question_type, "
                "topic_id, points, prompt, has_image, is_ai_generated) "
                "VALUES (NULL, ?, ?, ?, ?, 'multiple_choice', ?, 1, ?, 0, 0)",
                (batch["source"], question["number"], subject, str(grade), topic_id,
                 question["prompt"]),
            ).lastrowid
            for letter in LETTERS:
                conn.execute(
                    "INSERT INTO multiple_choice_options "
                    "(question_id, option_letter, option_text, is_correct) VALUES (?, ?, ?, ?)",
                    (question_id, letter, question["options"][letter],
                     int(letter == question["correct"])),
                )
            conn.execute(
                "INSERT INTO question_topic_assignments "
                "(question_id, topic_id, section_id, assignment_type, confidence, method, is_active) "
                "VALUES (?, ?, ?, 'reviewed_subject_import', 1.0, 'reviewed_batch', 1)",
                (question_id, topic_id, section_id),
            )
            summary["questions"]["inserted"] += 1
        _owned(question_ownership(conn, question_id), subject, "Question")
        if not scope.accepts(question_id):
            raise ValueError(f"Question {question['number']} is outside assessment scope")
    return summary


def import_batch(db_path: Path, batch: dict, *, apply: bool = False) -> dict:
    """Validate and plan on an in-memory DB, or apply atomically to a target DB."""
    subject = validate_batch(batch)
    db_path = Path(db_path).resolve()
    if apply:
        conn = sqlite3.connect(f"file:{quote(str(db_path))}?mode=rw", uri=True)
    else:
        source = sqlite3.connect(
            f"file:{quote(str(db_path))}?mode=ro&immutable=1", uri=True)
        try:
            conn = sqlite3.connect(":memory:")
            source.backup(conn)
        finally:
            source.close()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            summary = _import_in_transaction(conn, batch, subject)
            if apply:
                conn.commit()
            else:
                conn.rollback()
            return summary
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="reviewed JSON batch")
    parser.add_argument("--db", type=Path, required=True, help="existing target database")
    parser.add_argument("--apply", action="store_true", help="write atomically; default is dry run")
    args = parser.parse_args(argv)
    try:
        batch = json.loads(args.input.read_text(encoding="utf-8"))
        summary = import_batch(args.db, batch, apply=args.apply)
    except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        parser.exit(1, f"Import rejected: {exc}\n")
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "summary": summary},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

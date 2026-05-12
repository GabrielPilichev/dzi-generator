#!/usr/bin/env python3
"""Import official DZI Part 1 questions from the documented JSON format."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path("data/questions.db")
FORMAT_VERSION = "dzi_it_pp_2025_format"
SUBJECT = "informatika_it"
LEVEL = "DZI"
SESSION_BY_PREFIX = {
    "may": "may",
    "aug": "august",
}
QUESTION_TYPE_BY_TASK_KIND = {
    "multiple_choice": "multiple_choice",
    "short_answer": "fill_in",
}
SOURCE_TASK_LAYOUT_OVERRIDES = {
    "aug_2023_v2": {
        11: ("short_answer", 3),
        12: ("short_answer", 3),
        13: ("short_answer", 3),
        16: ("multiple_choice", 1),
        17: ("multiple_choice", 1),
        18: ("multiple_choice", 1),
    },
    "may_2023_v2": {
        11: ("short_answer", 3),
        12: ("short_answer", 3),
        13: ("short_answer", 3),
        16: ("multiple_choice", 1),
        17: ("multiple_choice", 1),
        18: ("multiple_choice", 1),
    },
    "may_2022_v1": {
        11: ("short_answer", 3),
        12: ("short_answer", 3),
        13: ("short_answer", 3),
        16: ("multiple_choice", 1),
        17: ("multiple_choice", 1),
        18: ("multiple_choice", 1),
    },
    "aug_2022_v2": {
        11: ("short_answer", 3),
        12: ("short_answer", 3),
        13: ("short_answer", 3),
        16: ("multiple_choice", 1),
        17: ("multiple_choice", 1),
        18: ("multiple_choice", 1),
    },
}
VALID_ASSET_TYPES = {"image", "pdf_crop", "spreadsheet", "archive", "other"}


@dataclass
class Summary:
    source_slug: str = ""
    exam_id: int | None = None
    tasks_read: int = 0
    questions_inserted: int = 0
    questions_updated: int = 0
    options_inserted: int = 0
    fill_in_subquestions_inserted: int = 0
    exam_task_links_inserted: int = 0
    assets_linked: int = 0
    unknown_topics: int = 0
    unknown_sections: int = 0
    sample_only_dry_run: bool = False
    skipped: int = 0
    validation_errors: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import official DZI Part 1 questions from JSON."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--json", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-missing-assets", action="store_true")
    parser.add_argument("--allow-unknown-topic", action="store_true")
    parser.add_argument("--allow-unknown-section", action="store_true")
    return parser.parse_args()


def get_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def require_tables(conn: sqlite3.Connection) -> None:
    required = (
        "exams",
        "exam_tasks",
        "questions",
        "multiple_choice_options",
        "fill_in_subquestions",
        "exam_task_questions",
        "assets",
        "asset_links",
    )
    missing = [table for table in required if not table_exists(conn, table)]
    if missing:
        raise ValueError(f"Missing required table(s): {', '.join(missing)}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Top-level JSON value must be an object")
    return payload


def parse_source_slug(source_slug: str) -> tuple[int, str, int]:
    parts = source_slug.split("_")
    if len(parts) != 3:
        raise ValueError(f"Invalid source_slug '{source_slug}'; expected <may|aug>_<year>_v<variant>")

    session = SESSION_BY_PREFIX.get(parts[0])
    if session is None:
        raise ValueError(f"Invalid source_slug session prefix '{parts[0]}'")

    try:
        year = int(parts[1])
        if not parts[2].startswith("v"):
            raise ValueError
        variant = int(parts[2][1:])
    except ValueError as exc:
        raise ValueError(f"Invalid source_slug year/variant in '{source_slug}'") from exc

    return year, session, variant


def resolve_exam(conn: sqlite3.Connection, source_slug: str) -> sqlite3.Row:
    year, session, variant = parse_source_slug(source_slug)
    params = (SUBJECT, LEVEL, year, session, variant)

    row = conn.execute(
        """
        SELECT *
        FROM exams
        WHERE subject = ?
          AND level = ?
          AND year = ?
          AND session = ?
          AND variant = ?
          AND format_version = ?
        ORDER BY id
        LIMIT 1
        """,
        (*params, FORMAT_VERSION),
    ).fetchone()
    if row is not None:
        return row

    row = conn.execute(
        """
        SELECT *
        FROM exams
        WHERE subject = ?
          AND level = ?
          AND year = ?
          AND session = ?
          AND variant = ?
        ORDER BY id
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        raise ValueError(f"No matching exam row found for source_slug '{source_slug}'")
    return row


def resolve_topic_id(
    conn: sqlite3.Connection,
    topic_slug: str | None,
    allow_unknown_topic: bool,
    summary: Summary,
) -> int | None:
    if not topic_slug:
        return None
    row = conn.execute(
        "SELECT id FROM curriculum_topics WHERE topic_slug = ?",
        (topic_slug,),
    ).fetchone()
    if row is None:
        if allow_unknown_topic:
            summary.unknown_topics += 1
            print(f"warning: unknown topic_slug '{topic_slug}'; importing with topic_id=NULL")
            return None
        raise ValueError(f"Unknown topic_slug '{topic_slug}'")
    return int(row["id"])


def resolve_section_id(
    conn: sqlite3.Connection,
    section_slug: str | None,
    allow_unknown_section: bool,
    summary: Summary,
) -> int | None:
    if not section_slug:
        return None
    row = conn.execute(
        "SELECT id FROM curriculum_sections WHERE section_slug = ?",
        (section_slug,),
    ).fetchone()
    if row is None:
        if allow_unknown_section:
            summary.unknown_sections += 1
            print(f"warning: unknown section_slug '{section_slug}'; importing with section_id=NULL")
            return None
        raise ValueError(f"Unknown section_slug '{section_slug}'")
    return int(row["id"])


def validate_grade_for_section(
    conn: sqlite3.Connection,
    section_id: int | None,
    grade: Any,
) -> None:
    if grade is None:
        return
    if not isinstance(grade, int) or not 8 <= grade <= 12:
        raise ValueError("grade must be an integer in 8..12")
    if section_id is None:
        return
    row = conn.execute(
        "SELECT class FROM curriculum_sections WHERE id = ?",
        (section_id,),
    ).fetchone()
    if row is not None and row["class"] is not None and int(row["class"]) != grade:
        raise ValueError(
            f"grade {grade} does not match section class {row['class']}"
        )


def answers_to_text(values: Any) -> str:
    if not isinstance(values, list) or not values:
        raise ValueError("answers must be a non-empty array")
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError("answers must contain non-empty strings")
    return json.dumps(values, ensure_ascii=False)


def alternatives_to_text(values: Any) -> str | None:
    if values is None:
        return None
    if not isinstance(values, list):
        raise ValueError("answer_alternatives must be an array")
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError("answer_alternatives must contain non-empty strings")
    return json.dumps(values, ensure_ascii=False)


def validate_options(task: dict[str, Any]) -> None:
    options = task.get("options")
    if not isinstance(options, list) or len(options) != 4:
        raise ValueError("multiple_choice tasks require exactly 4 options")
    correct_count = 0
    letters: set[str] = set()
    for option in options:
        if not isinstance(option, dict):
            raise ValueError("each multiple_choice option must be an object")
        letter = option.get("letter")
        text = option.get("text")
        is_correct = option.get("is_correct")
        if not isinstance(letter, str) or not letter.strip():
            raise ValueError("each option requires a non-empty letter")
        if letter in letters:
            raise ValueError(f"duplicate option letter '{letter}'")
        letters.add(letter)
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"option {letter} requires non-empty text")
        if not isinstance(is_correct, bool):
            raise ValueError(f"option {letter} is_correct must be boolean")
        correct_count += 1 if is_correct else 0
    if correct_count != 1:
        raise ValueError("multiple_choice tasks require exactly 1 correct option")


def validate_short_answer(task: dict[str, Any]) -> None:
    answers = task.get("answers")
    subquestions = task.get("subquestions")
    has_answers = isinstance(answers, list) and len(answers) > 0
    has_subquestions = isinstance(subquestions, list) and len(subquestions) > 0
    if not has_answers and not has_subquestions:
        raise ValueError("short_answer tasks require answers or subquestions")

    if has_answers:
        answers_to_text(answers)
        alternatives_to_text(task.get("answer_alternatives"))
    if subquestions is not None:
        if not isinstance(subquestions, list):
            raise ValueError("subquestions must be an array")
        for index, subquestion in enumerate(subquestions, start=1):
            if not isinstance(subquestion, dict):
                raise ValueError("each subquestion must be an object")
            if not isinstance(subquestion.get("prompt"), str) or not subquestion["prompt"].strip():
                raise ValueError(f"subquestion {index} requires prompt")
            points = subquestion.get("points", 1)
            if not isinstance(points, int) or points <= 0:
                raise ValueError(f"subquestion {index} points must be a positive integer")
            answers_to_text(subquestion.get("answers"))
            alternatives_to_text(subquestion.get("answer_alternatives"))


def validate_assets(task: dict[str, Any], allow_missing_assets: bool) -> None:
    assets = task.get("assets", [])
    if assets is None:
        return
    if not isinstance(assets, list):
        raise ValueError("assets must be an array")
    for index, asset in enumerate(assets, start=1):
        if not isinstance(asset, dict):
            raise ValueError(f"asset {index} must be an object")
        file_path = asset.get("file_path")
        asset_type = asset.get("asset_type")
        if not isinstance(file_path, str) or not file_path.strip():
            raise ValueError(f"asset {index} requires file_path")
        if asset_type not in VALID_ASSET_TYPES:
            raise ValueError(f"asset {index} has invalid asset_type '{asset_type}'")
        if not Path(file_path).exists():
            if allow_missing_assets:
                print(f"warning: asset file missing, metadata will be NULL: {file_path}")
            else:
                raise ValueError(f"asset file does not exist: {file_path}")


def validate_task(
    conn: sqlite3.Connection,
    source_slug: str,
    exam_id: int,
    task: Any,
    allow_missing_assets: bool,
    allow_unknown_topic: bool,
    allow_unknown_section: bool,
    summary: Summary,
) -> tuple[sqlite3.Row, int | None, int | None]:
    if not isinstance(task, dict):
        raise ValueError("each task must be an object")

    task_number = task.get("task_number")
    if not isinstance(task_number, int) or not 1 <= task_number <= 25:
        raise ValueError("task_number must be an integer in 1..25")

    task_kind = task.get("task_kind")
    if task_kind not in QUESTION_TYPE_BY_TASK_KIND:
        raise ValueError("task_kind must be multiple_choice or short_answer")

    points = task.get("points")
    if not isinstance(points, int):
        raise ValueError("points must be an integer")

    prompt = task.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    exam_task = conn.execute(
        """
        SELECT *
        FROM exam_tasks
        WHERE exam_id = ? AND task_number = ?
        """,
        (exam_id, task_number),
    ).fetchone()
    if exam_task is None:
        raise ValueError(f"task_number {task_number} does not exist in exam_tasks")

    expected_task_kind, expected_points = SOURCE_TASK_LAYOUT_OVERRIDES.get(
        source_slug,
        {},
    ).get(task_number, (exam_task["task_kind"], exam_task["points"]))
    if expected_task_kind != task_kind:
        raise ValueError(
            f"task_number {task_number} task_kind '{task_kind}' does not match expected task_kind '{expected_task_kind}'"
        )
    if expected_points != points:
        raise ValueError(
            f"task_number {task_number} points {points} do not match expected points {expected_points}"
        )

    if task_kind == "multiple_choice":
        validate_options(task)
    else:
        validate_short_answer(task)

    validate_assets(task, allow_missing_assets)
    topic_id = resolve_topic_id(conn, task.get("topic_slug"), allow_unknown_topic, summary)
    section_id = resolve_section_id(conn, task.get("section_slug"), allow_unknown_section, summary)
    validate_grade_for_section(conn, section_id, task.get("grade"))
    return exam_task, topic_id, section_id


def find_question(conn: sqlite3.Connection, source_slug: str, task_number: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM questions
        WHERE source_exam = ? AND source_number = ?
        ORDER BY id
        LIMIT 1
        """,
        (source_slug, task_number),
    ).fetchone()


def upsert_question(
    conn: sqlite3.Connection,
    source_slug: str,
    exam: sqlite3.Row,
    task: dict[str, Any],
    topic_id: int | None,
) -> tuple[int, str]:
    task_number = int(task["task_number"])
    question_type = QUESTION_TYPE_BY_TASK_KIND[task["task_kind"]]
    existing = find_question(conn, source_slug, task_number)
    topic_text = task.get("topic_slug")

    if existing is None:
        conn.execute(
            """
            INSERT INTO questions (
                exam_id, source_exam, source_number, subject, level, year,
                question_type, topic, points, prompt, has_image,
                is_ai_generated, quality_score, topic_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1.0, ?)
            """,
            (
                exam["id"],
                source_slug,
                task_number,
                SUBJECT,
                LEVEL,
                exam["year"],
                question_type,
                topic_text,
                task["points"],
                task["prompt"],
                1 if task.get("assets") else 0,
                topic_id,
            ),
        )
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]), "inserted"

    conn.execute(
        """
        UPDATE questions
        SET exam_id = ?,
            subject = ?,
            level = ?,
            year = ?,
            question_type = ?,
            topic = ?,
            points = ?,
            prompt = ?,
            has_image = ?,
            is_ai_generated = 0,
            quality_score = 1.0,
            topic_id = ?
        WHERE id = ?
        """,
        (
            exam["id"],
            SUBJECT,
            LEVEL,
            exam["year"],
            question_type,
            topic_text,
            task["points"],
            task["prompt"],
            1 if task.get("assets") else 0,
            topic_id,
            existing["id"],
        ),
    )
    return int(existing["id"]), "updated"


def replace_multiple_choice_options(
    conn: sqlite3.Connection,
    question_id: int,
    task: dict[str, Any],
) -> int:
    conn.execute("DELETE FROM multiple_choice_options WHERE question_id = ?", (question_id,))
    for option in task["options"]:
        conn.execute(
            """
            INSERT INTO multiple_choice_options (
                question_id, option_letter, option_text, is_correct
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                question_id,
                option["letter"],
                option["text"],
                1 if option["is_correct"] else 0,
            ),
        )
    return len(task["options"])


def replace_fill_in_subquestions(
    conn: sqlite3.Connection,
    question_id: int,
    task: dict[str, Any],
) -> int:
    conn.execute("DELETE FROM fill_in_subquestions WHERE question_id = ?", (question_id,))
    subquestions = task.get("subquestions") or []

    if subquestions:
        for index, subquestion in enumerate(subquestions, start=1):
            conn.execute(
                """
                INSERT INTO fill_in_subquestions (
                    question_id, subquestion_number, subquestion_text,
                    correct_answer, answer_alternatives, points
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    question_id,
                    index,
                    subquestion["prompt"],
                    answers_to_text(subquestion["answers"]),
                    alternatives_to_text(subquestion.get("answer_alternatives")),
                    subquestion.get("points", 1),
                ),
            )
        return len(subquestions)

    conn.execute(
        """
        INSERT INTO fill_in_subquestions (
            question_id, subquestion_number, subquestion_text,
            correct_answer, answer_alternatives, points
        )
        VALUES (?, 1, ?, ?, ?, ?)
        """,
        (
            question_id,
            task["prompt"],
            answers_to_text(task["answers"]),
            alternatives_to_text(task.get("answer_alternatives")),
            task["points"],
        ),
    )
    return 1


def link_exam_task(
    conn: sqlite3.Connection,
    exam_task_id: int,
    question_id: int,
) -> str:
    existing = conn.execute(
        """
        SELECT 1
        FROM exam_task_questions
        WHERE task_id = ? AND question_id = ? AND role = 'primary'
        """,
        (exam_task_id, question_id),
    ).fetchone()
    if existing is not None:
        return "updated"

    conn.execute(
        """
        INSERT INTO exam_task_questions (task_id, question_id, role)
        VALUES (?, ?, 'primary')
        """,
        (exam_task_id, question_id),
    )
    return "inserted"


def sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upsert_asset(conn: sqlite3.Connection, asset: dict[str, Any]) -> int:
    local_path = asset["file_path"]
    path = Path(local_path)
    existing = conn.execute(
        "SELECT id FROM assets WHERE local_path = ?",
        (local_path,),
    ).fetchone()
    mime_type, _ = mimetypes.guess_type(local_path)
    file_size = path.stat().st_size if path.exists() else None
    sha256 = sha256_file(path)

    if existing is None:
        conn.execute(
            """
            INSERT INTO assets (
                asset_type, original_filename, local_path, sha256, mime_type, file_size
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (asset["asset_type"], path.name, local_path, sha256, mime_type, file_size),
        )
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    conn.execute(
        """
        UPDATE assets
        SET asset_type = ?,
            original_filename = ?,
            sha256 = ?,
            mime_type = ?,
            file_size = ?
        WHERE id = ?
        """,
        (asset["asset_type"], path.name, sha256, mime_type, file_size, existing["id"]),
    )
    return int(existing["id"])


def link_asset(
    conn: sqlite3.Connection,
    asset_id: int,
    owner_type: str,
    owner_id: int,
    asset: dict[str, Any],
) -> str:
    role = asset["asset_type"]
    existing = conn.execute(
        """
        SELECT id
        FROM asset_links
        WHERE asset_id = ?
          AND owner_type = ?
          AND owner_id = ?
          AND role = ?
          AND display_order = 0
        """,
        (asset_id, owner_type, owner_id, role),
    ).fetchone()

    values = (
        asset.get("caption_bg"),
        asset.get("source_page"),
        asset.get("source_bbox_json"),
    )
    if existing is None:
        conn.execute(
            """
            INSERT INTO asset_links (
                asset_id, owner_type, owner_id, role, display_order,
                caption_bg, source_page, source_bbox_json
            )
            VALUES (?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (asset_id, owner_type, owner_id, role, *values),
        )
        return "inserted"

    conn.execute(
        """
        UPDATE asset_links
        SET caption_bg = ?,
            source_page = ?,
            source_bbox_json = ?
        WHERE id = ?
        """,
        (*values, existing["id"]),
    )
    return "updated"


def link_assets(
    conn: sqlite3.Connection,
    question_id: int,
    exam_task_id: int,
    task: dict[str, Any],
) -> int:
    assets = task.get("assets") or []
    linked = 0
    for asset in assets:
        asset_id = upsert_asset(conn, asset)
        link_asset(conn, asset_id, "question", question_id, asset)
        link_asset(conn, asset_id, "exam_task", exam_task_id, asset)
        linked += 2
    if assets:
        conn.execute("UPDATE exam_tasks SET has_assets = 1 WHERE id = ?", (exam_task_id,))
    return linked


def upsert_topic_assignment(
    conn: sqlite3.Connection,
    question_id: int,
    topic_id: int | None,
    section_id: int | None,
) -> None:
    if topic_id is None or not table_exists(conn, "question_topic_assignments"):
        return
    existing = conn.execute(
        """
        SELECT id
        FROM question_topic_assignments
        WHERE question_id = ?
          AND assignment_type = 'official_import'
          AND is_active = 1
        ORDER BY id
        LIMIT 1
        """,
        (question_id,),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO question_topic_assignments (
                question_id, topic_id, section_id, assignment_type,
                confidence, method, is_active
            )
            VALUES (?, ?, ?, 'official_import', 1.0, 'json_import', 1)
            """,
            (question_id, topic_id, section_id),
        )
        return

    conn.execute(
        """
        UPDATE question_topic_assignments
        SET topic_id = ?,
            section_id = ?,
            confidence = 1.0,
            method = 'json_import',
            is_active = 1
        WHERE id = ?
        """,
        (topic_id, section_id, existing["id"]),
    )


def validate_payload(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    allow_missing_assets: bool,
    allow_unknown_topic: bool,
    allow_unknown_section: bool,
) -> tuple[sqlite3.Row, list[tuple[dict[str, Any], sqlite3.Row, int | None, int | None]], Summary]:
    source_slug = payload.get("source_slug")
    if not isinstance(source_slug, str) or not source_slug.strip():
        raise ValueError("source_slug is required")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("tasks must be an array")

    exam = resolve_exam(conn, source_slug)
    summary = Summary(source_slug=source_slug, exam_id=int(exam["id"]), tasks_read=len(tasks))
    validated = []
    for index, task in enumerate(tasks, start=1):
        try:
            exam_task, topic_id, section_id = validate_task(
                conn,
                source_slug,
                int(exam["id"]),
                task,
                allow_missing_assets,
                allow_unknown_topic,
                allow_unknown_section,
                summary,
            )
            validated.append((task, exam_task, topic_id, section_id))
        except ValueError as exc:
            summary.validation_errors.append(f"task #{index}: {exc}")

    if summary.validation_errors:
        raise ValueError("\n".join(summary.validation_errors))
    return exam, validated, summary


def validate_sample_payload(payload: dict[str, Any], allow_missing_assets: bool) -> Summary:
    source_slug = payload.get("source_slug")
    if not isinstance(source_slug, str) or not source_slug.strip():
        raise ValueError("source_slug is required")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("tasks must be an array")

    summary = Summary(source_slug=source_slug, exam_id=None, tasks_read=len(tasks), sample_only_dry_run=True)
    for index, task in enumerate(tasks, start=1):
        try:
            if not isinstance(task, dict):
                raise ValueError("each task must be an object")
            task_number = task.get("task_number")
            if not isinstance(task_number, int) or not 1 <= task_number <= 25:
                raise ValueError("task_number must be an integer in 1..25")
            task_kind = task.get("task_kind")
            if task_kind not in QUESTION_TYPE_BY_TASK_KIND:
                raise ValueError("task_kind must be multiple_choice or short_answer")
            points = task.get("points")
            if not isinstance(points, int):
                raise ValueError("points must be an integer")
            prompt = task.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("prompt must be a non-empty string")
            validate_grade_for_section(None, None, task.get("grade"))
            if task_kind == "multiple_choice":
                validate_options(task)
                summary.options_inserted += len(task["options"])
            else:
                validate_short_answer(task)
                summary.fill_in_subquestions_inserted += len(task.get("subquestions") or [task])
            validate_assets(task, allow_missing_assets)
        except ValueError as exc:
            summary.validation_errors.append(f"task #{index}: {exc}")

    if summary.validation_errors:
        raise ValueError("\n".join(summary.validation_errors))
    print(f"sample-only dry-run: {summary.tasks_read} task(s) structurally valid")
    print("sample-only dry-run: no DB writes planned")
    return summary


def planned_question_action(
    conn: sqlite3.Connection,
    source_slug: str,
    task_number: int,
) -> str:
    return "updated" if find_question(conn, source_slug, task_number) is not None else "inserted"


def run_import(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    dry_run: bool,
    allow_missing_assets: bool,
    allow_unknown_topic: bool,
    allow_unknown_section: bool,
) -> Summary:
    if payload.get("_sample_only") is True and not dry_run:
        raise ValueError("sample-only JSON files may only be used with --dry-run")

    if payload.get("_sample_only") is True and dry_run:
        print("sample-only dry-run: skipping exam resolution")
        return validate_sample_payload(payload, allow_missing_assets)

    exam, validated_tasks, summary = validate_payload(
        conn,
        payload,
        allow_missing_assets,
        allow_unknown_topic,
        allow_unknown_section,
    )

    if dry_run:
        for task, exam_task, _topic_id, _section_id in validated_tasks:
            question_action = planned_question_action(conn, summary.source_slug, task["task_number"])
            print(
                f"plan: task_number={task['task_number']} exam_task_id={exam_task['id']} "
                f"question={question_action} task_kind={task['task_kind']}"
            )
            if question_action == "inserted":
                summary.questions_inserted += 1
            else:
                summary.questions_updated += 1
            if task["task_kind"] == "multiple_choice":
                summary.options_inserted += len(task["options"])
            else:
                summary.fill_in_subquestions_inserted += len(task.get("subquestions") or [task])
            summary.exam_task_links_inserted += 1
            summary.assets_linked += len(task.get("assets") or []) * 2
        return summary

    for task, exam_task, topic_id, section_id in validated_tasks:
        question_id, question_action = upsert_question(conn, summary.source_slug, exam, task, topic_id)
        if question_action == "inserted":
            summary.questions_inserted += 1
        else:
            summary.questions_updated += 1

        if task["task_kind"] == "multiple_choice":
            summary.options_inserted += replace_multiple_choice_options(conn, question_id, task)
            conn.execute("DELETE FROM fill_in_subquestions WHERE question_id = ?", (question_id,))
        else:
            conn.execute("DELETE FROM multiple_choice_options WHERE question_id = ?", (question_id,))
            summary.fill_in_subquestions_inserted += replace_fill_in_subquestions(conn, question_id, task)

        link_action = link_exam_task(conn, int(exam_task["id"]), question_id)
        if link_action == "inserted":
            summary.exam_task_links_inserted += 1
        summary.assets_linked += link_assets(conn, question_id, int(exam_task["id"]), task)
        upsert_topic_assignment(conn, question_id, topic_id, section_id)

    return summary


def print_summary(summary: Summary) -> None:
    print(f"source_slug: {summary.source_slug}")
    print(f"exam_id: {summary.exam_id}")
    print(f"tasks read: {summary.tasks_read}")
    print(f"questions inserted: {summary.questions_inserted}")
    print(f"questions updated: {summary.questions_updated}")
    print(f"options inserted: {summary.options_inserted}")
    print(f"fill_in_subquestions inserted: {summary.fill_in_subquestions_inserted}")
    print(f"exam_task links inserted: {summary.exam_task_links_inserted}")
    print(f"assets linked: {summary.assets_linked}")
    print(f"unknown topics: {summary.unknown_topics}")
    print(f"unknown sections: {summary.unknown_sections}")
    print(f"skipped/validation errors: {len(summary.validation_errors)}")


def main() -> int:
    args = parse_args()
    try:
        payload = load_json(Path(args.json))
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            require_tables(conn)
            if args.dry_run:
                summary = run_import(
                    conn,
                    payload,
                    dry_run=True,
                    allow_missing_assets=args.allow_missing_assets,
                    allow_unknown_topic=args.allow_unknown_topic,
                    allow_unknown_section=args.allow_unknown_section,
                )
                if summary.sample_only_dry_run:
                    return 0
            else:
                if payload.get("_sample_only") is True:
                    raise ValueError("sample-only JSON files may only be used with --dry-run")
                try:
                    with conn:
                        summary = run_import(
                            conn,
                            payload,
                            dry_run=False,
                            allow_missing_assets=args.allow_missing_assets,
                            allow_unknown_topic=args.allow_unknown_topic,
                            allow_unknown_section=args.allow_unknown_section,
                        )
                except Exception:
                    print("ROLLED BACK — no DB changes committed", file=sys.stderr)
                    raise
            print_summary(summary)
        finally:
            conn.close()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Dry-run validator for prepared DZI practical-task batch JSON files.

This validator covers official DZI Part 2 tasks 26, 27, 28 only. It is
intentionally dry-run and read-only: it never writes to data/questions.db and
never extracts ZIPs. It checks structural validity of a future practical-task
batch JSON, that referenced resource files exist on disk inside allowed repo
directories, and that the source exam + task skeleton resolve.

The practical-task import format itself is still being designed (see
docs/reviews/dzi_practical_tasks_plan.md). This validator codifies the
invariants that any future format must respect.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote


DEFAULT_DB_PATH = Path("data/questions.db")
ALLOWED_RESOURCE_ROOTS = (Path("data/reference"), Path("data/assets"))
ALLOWED_TASK_NUMBERS = (26, 27, 28)
ALLOWED_PRACTICAL_TASK_KINDS = (
    "practical_spreadsheet",
    "practical_graphics",
    "practical_web",
)
REJECTED_TASK_KINDS = ("multiple_choice", "short_answer")
SUBJECT = "informatika_it"
LEVEL = "DZI"
FORMAT_VERSION = "dzi_it_pp_2025_format"
SESSION_BY_PREFIX = {"may": "may", "aug": "august"}


@dataclass
class PracticalSummary:
    source_slug: str = ""
    exam_id: int | None = None
    tasks_read: int = 0
    resource_files_checked: int = 0
    validation_errors: list[str] = field(default_factory=list)


def readonly_uri(db_path: Path) -> str:
    return f"file:{quote(str(db_path), safe='/')}?mode=ro"


def load_json(path: Path) -> dict:
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
        raise ValueError(
            f"Invalid source_slug '{source_slug}'; expected <may|aug>_<year>_v<variant>"
        )
    session = SESSION_BY_PREFIX.get(parts[0])
    if session is None:
        raise ValueError(f"Invalid source_slug session prefix '{parts[0]}'")
    try:
        year = int(parts[1])
        if not parts[2].startswith("v"):
            raise ValueError
        variant = int(parts[2][1:])
    except ValueError as exc:
        raise ValueError(
            f"Invalid source_slug year/variant in '{source_slug}'"
        ) from exc
    return year, session, variant


def resolve_exam(conn: sqlite3.Connection, source_slug: str) -> sqlite3.Row:
    year, session, variant = parse_source_slug(source_slug)
    params = (SUBJECT, LEVEL, year, session, variant)
    row = conn.execute(
        """
        SELECT *
        FROM exams
        WHERE subject = ? AND level = ? AND year = ?
          AND session = ? AND variant = ? AND format_version = ?
        ORDER BY id LIMIT 1
        """,
        (*params, FORMAT_VERSION),
    ).fetchone()
    if row is not None:
        return row
    row = conn.execute(
        """
        SELECT *
        FROM exams
        WHERE subject = ? AND level = ? AND year = ?
          AND session = ? AND variant = ?
        ORDER BY id LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        raise ValueError(
            f"No matching exam row found for source_slug '{source_slug}'"
        )
    return row


def resolve_exam_task(
    conn: sqlite3.Connection, exam_id: int, task_number: int
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, task_number, task_kind, points
        FROM exam_tasks
        WHERE exam_id = ? AND task_number = ?
        """,
        (exam_id, task_number),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"task_number {task_number} does not exist in exam_tasks for exam_id={exam_id}"
        )
    return row


def is_under_allowed_root(resolved: Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved_real = resolved.resolve(strict=False)
    except OSError:
        return False
    for root in roots:
        try:
            root_real = root.resolve(strict=False)
        except OSError:
            continue
        try:
            resolved_real.relative_to(root_real)
            return True
        except ValueError:
            continue
    return False


def validate_resource_path(
    raw_path: str,
    allowed_roots: tuple[Path, ...],
) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("resource_files entries must be non-empty strings")
    path = Path(raw_path)
    if path.is_absolute():
        raise ValueError(f"resource path must be relative, got absolute: {raw_path}")
    normalised = os.path.normpath(raw_path)
    if normalised.startswith("..") or os.sep + ".." + os.sep in os.sep + normalised + os.sep:
        raise ValueError(f"resource path must not traverse upward: {raw_path}")
    if ".." in Path(normalised).parts:
        raise ValueError(f"resource path must not contain '..': {raw_path}")
    if not is_under_allowed_root(path, allowed_roots):
        allowed = ", ".join(str(r) for r in allowed_roots)
        raise ValueError(
            f"resource path must resolve under one of [{allowed}]: {raw_path}"
        )
    if not path.exists():
        raise ValueError(f"resource file does not exist: {raw_path}")
    if not path.is_file():
        raise ValueError(f"resource path is not a regular file: {raw_path}")
    return path


def validate_task(
    conn: sqlite3.Connection,
    source_slug: str,
    exam_id: int,
    task: object,
    summary: PracticalSummary,
    allowed_roots: tuple[Path, ...],
) -> None:
    if not isinstance(task, dict):
        raise ValueError("each task must be an object")

    task_number = task.get("task_number")
    if not isinstance(task_number, int) or task_number not in ALLOWED_TASK_NUMBERS:
        raise ValueError(
            f"task_number must be one of {ALLOWED_TASK_NUMBERS}, got {task_number!r}"
        )

    task_kind = task.get("task_kind")
    if not isinstance(task_kind, str):
        raise ValueError("task_kind must be a string")
    if task_kind in REJECTED_TASK_KINDS:
        raise ValueError(
            f"task_kind '{task_kind}' is not allowed for practical tasks; "
            f"expected one of {ALLOWED_PRACTICAL_TASK_KINDS}"
        )
    if task_kind not in ALLOWED_PRACTICAL_TASK_KINDS:
        raise ValueError(
            f"task_kind '{task_kind}' is not a recognised practical task_kind; "
            f"expected one of {ALLOWED_PRACTICAL_TASK_KINDS}"
        )

    exam_task = resolve_exam_task(conn, exam_id, task_number)
    if exam_task["task_kind"] != task_kind:
        raise ValueError(
            f"task_number {task_number} task_kind '{task_kind}' does not match "
            f"expected task_kind '{exam_task['task_kind']}'"
        )

    points = task.get("points")
    if not isinstance(points, int):
        raise ValueError(f"task_number {task_number}: points must be an integer")
    if points != exam_task["points"]:
        raise ValueError(
            f"task_number {task_number} points {points} do not match expected "
            f"points {exam_task['points']}"
        )

    prompt_bg = task.get("prompt_bg")
    instructions_bg = task.get("instructions_bg")
    has_prompt = isinstance(prompt_bg, str) and prompt_bg.strip()
    has_instructions = isinstance(instructions_bg, str) and instructions_bg.strip()
    if not has_prompt and not has_instructions:
        raise ValueError(
            f"task_number {task_number}: prompt_bg or instructions_bg is required"
        )

    grading_mode = task.get("grading_mode")
    if grading_mode != "manual":
        raise ValueError(
            f"task_number {task_number}: grading_mode must be 'manual', got {grading_mode!r}"
        )

    resource_files = task.get("resource_files")
    if resource_files is not None:
        if not isinstance(resource_files, list):
            raise ValueError(
                f"task_number {task_number}: resource_files must be a list"
            )
        for raw_path in resource_files:
            validate_resource_path(raw_path, allowed_roots)
            summary.resource_files_checked += 1

    expected_outputs = task.get("expected_outputs")
    if expected_outputs is None:
        expected_outputs = task.get("expected_output_files")
    if expected_outputs is not None and not isinstance(expected_outputs, list):
        raise ValueError(
            f"task_number {task_number}: expected_outputs must be a list when present"
        )
    if isinstance(expected_outputs, list):
        for entry in expected_outputs:
            if not isinstance(entry, (str, dict)):
                raise ValueError(
                    f"task_number {task_number}: expected_outputs entries must be "
                    "strings or objects"
                )


def validate_batch(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    allowed_roots: tuple[Path, ...] = ALLOWED_RESOURCE_ROOTS,
) -> PracticalSummary:
    """Validate a practical-task batch without committing DB changes."""

    before_changes = conn.total_changes

    source_slug = payload.get("source_slug")
    if not isinstance(source_slug, str) or not source_slug.strip():
        raise ValueError("source_slug is required")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("tasks must be a non-empty array")

    exam = resolve_exam(conn, source_slug)
    summary = PracticalSummary(
        source_slug=source_slug,
        exam_id=int(exam["id"]),
        tasks_read=len(tasks),
    )

    seen_numbers: set[int] = set()
    for index, task in enumerate(tasks, start=1):
        try:
            validate_task(
                conn,
                source_slug,
                int(exam["id"]),
                task,
                summary,
                allowed_roots,
            )
            number = task.get("task_number") if isinstance(task, dict) else None
            if isinstance(number, int):
                if number in seen_numbers:
                    raise ValueError(
                        f"duplicate task_number {number} in batch"
                    )
                seen_numbers.add(number)
        except ValueError as exc:
            summary.validation_errors.append(f"task #{index}: {exc}")

    if summary.validation_errors:
        raise ValueError("\n".join(summary.validation_errors))

    if conn.total_changes != before_changes:
        raise RuntimeError("dry-run validation attempted to modify the database")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a DZI practical-task batch JSON (tasks 26–28). "
            "Dry-run and read-only. No DB writes, no ZIP extraction."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--json", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = load_json(args.json)
        print(
            "DRY RUN ONLY: validating practical-task batch; no DB writes, no ZIP extraction"
        )
        conn = sqlite3.connect(readonly_uri(args.db), uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            summary = validate_batch(conn, payload)
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("")
    print("practical-task review summary:")
    print(f"source_slug: {summary.source_slug}")
    print(f"exam_id: {summary.exam_id}")
    print(f"tasks read: {summary.tasks_read}")
    print(f"resource files checked: {summary.resource_files_checked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

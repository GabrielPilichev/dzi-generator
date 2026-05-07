#!/usr/bin/env python3
"""Read-only per-task DZI visual asset audit."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path("data/questions.db")
DEFAULT_SOURCE_SLUG = "may_2025_v2"
FORMAT_VERSION = "dzi_it_pp_2025_format"
SESSION_TO_SLUG = {
    "may": "may",
    "august": "aug",
}
SLUG_TO_SESSION = {
    "may": "may",
    "aug": "august",
}

# Keep this conservative and in sync with web.app QUIZ_VISUAL_DEPENDENT_PATTERNS.
# Do not classify every mention of "електронна таблица" as visual-dependent.
VISUAL_DEPENDENT_PATTERNS = (
    "изображението",
    "на изображението",
    "даденото изображение",
    "следното изображение",
    "показаното изображение",
    "диаграмата",
    "диаграма е представена",
    "графиката",
    "разгледайте графиката",
    "таблицата по-долу",
    "фигурата",
    "в диаграмата",
    "показана диаграма",
    "показаната таблица",
    "показаната диаграма",
    "дадената таблица",
    "даден е фрагмент от електронна таблица",
    "следната диаграма",
    "следната фигура",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit linked visual assets for DZI exam tasks.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--source-slug", default=DEFAULT_SOURCE_SLUG)
    return parser.parse_args()


def parse_source_slug(source_slug: str) -> tuple[int, str, int]:
    parts = source_slug.split("_")
    if len(parts) != 3:
        raise ValueError(f"Invalid source slug: {source_slug}")
    session = SLUG_TO_SESSION.get(parts[0])
    if session is None:
        raise ValueError(f"Unsupported source slug session prefix: {parts[0]}")
    try:
        year = int(parts[1])
        if not parts[2].startswith("v"):
            raise ValueError
        variant = int(parts[2][1:])
    except ValueError as exc:
        raise ValueError(f"Invalid source slug year/variant: {source_slug}") from exc
    return year, session, variant


def make_source_slug(year: int, session: str, variant: Any) -> str:
    prefix = SESSION_TO_SLUG.get(session, session)
    return f"{prefix}_{year}_v{variant}"


def prompt_needs_visual(prompt: str | None) -> bool:
    text = (prompt or "").lower()
    return any(pattern in text for pattern in VISUAL_DEPENDENT_PATTERNS)


def path_exists(project_root: Path, value: object) -> bool:
    raw = str(value or "").strip()
    if not raw or raw in {"-", "—"}:
        return False
    path = Path(raw)
    if path.is_absolute():
        return path.exists()
    return (project_root / path).exists()


def asset_rows_for_owner(conn: sqlite3.Connection, owner_types: tuple[str, ...], owner_id: int) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in owner_types)
    return conn.execute(
        f"""
        SELECT a.id, a.local_path, a.asset_type
        FROM asset_links al
        JOIN assets a ON a.id = al.asset_id
        WHERE al.owner_type IN ({placeholders})
          AND al.owner_id = ?
        ORDER BY al.display_order, a.id
        """,
        (*owner_types, owner_id),
    ).fetchall()


def count_missing_files(project_root: Path, asset_rows: list[sqlite3.Row], image_path: object = None) -> int:
    missing = sum(1 for row in asset_rows if not path_exists(project_root, row["local_path"]))
    if str(image_path or "").strip() and not path_exists(project_root, image_path):
        missing += 1
    return missing


def resolve_exam(conn: sqlite3.Connection, source_slug: str) -> sqlite3.Row | None:
    year, session, variant = parse_source_slug(source_slug)
    return conn.execute(
        """
        SELECT *
        FROM exams
        WHERE format_version = ?
          AND year = ?
          AND session = ?
          AND variant = ?
        """,
        (FORMAT_VERSION, year, session, variant),
    ).fetchone()


def fetch_task_rows(conn: sqlite3.Connection, exam_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            et.id AS task_id,
            et.task_number,
            et.prompt AS task_prompt,
            etq.question_id,
            q.question_type,
            q.prompt AS question_prompt,
            q.image_path
        FROM exam_tasks et
        LEFT JOIN exam_task_questions etq
          ON etq.task_id = et.id
         AND etq.role = 'primary'
        LEFT JOIN questions q
          ON q.id = etq.question_id
        WHERE et.exam_id = ?
          AND et.task_number BETWEEN 1 AND 28
        ORDER BY et.task_number
        """,
        (exam_id,),
    ).fetchall()


def classify_asset_status(
    *,
    has_question: bool,
    needs_visual: bool,
    asset_link_count: int,
    has_image_path: bool,
    missing_asset_files: int,
) -> str:
    if not has_question:
        return "no_question"
    has_any_asset_source = asset_link_count > 0 or has_image_path
    if needs_visual:
        if not has_any_asset_source:
            return "link_missing"
        if missing_asset_files:
            return "file_missing"
        return "present"
    if has_any_asset_source:
        return "extra_unused"
    return "not_required"


def audit_tasks(conn: sqlite3.Connection, exam: sqlite3.Row, project_root: Path) -> list[dict[str, Any]]:
    rows = fetch_task_rows(conn, int(exam["id"]))
    result = []
    for row in rows:
        question_id = row["question_id"]
        question_assets = asset_rows_for_owner(conn, ("question", "questions"), int(question_id)) if question_id else []
        task_assets = asset_rows_for_owner(conn, ("exam_task",), int(row["task_id"]))
        all_assets = question_assets + task_assets
        prompt_text = "\n".join(
            part
            for part in (row["question_prompt"], row["task_prompt"])
            if str(part or "").strip()
        )
        needs_visual = prompt_needs_visual(prompt_text)
        has_image_path = bool(str(row["image_path"] or "").strip()) if question_id else False
        missing_files = count_missing_files(project_root, all_assets, row["image_path"] if question_id else None)
        asset_link_count = len(question_assets) + len(task_assets)
        asset_status = classify_asset_status(
            has_question=bool(question_id),
            needs_visual=needs_visual,
            asset_link_count=asset_link_count,
            has_image_path=has_image_path,
            missing_asset_files=missing_files,
        )
        quiz_blocking = (
            1 <= int(row["task_number"]) <= 25
            and row["question_type"] == "multiple_choice"
            and needs_visual
            and asset_status in {"link_missing", "file_missing"}
        )

        result.append(
            {
                "task_number": row["task_number"],
                "question_id": question_id or "",
                "question_type": row["question_type"] or "",
                "prompt_needs_visual": "yes" if needs_visual else "no",
                "question_asset_links": len(question_assets),
                "task_asset_links": len(task_assets),
                "missing_asset_files": missing_files,
                "asset_status": asset_status,
                "quiz_blocking": "yes" if quiz_blocking else "no",
            }
        )
    return result


def print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    widths = {
        column: max(len(column), *(len("" if row[column] is None else str(row[column])) for row in rows))
        for column in columns
    }
    print(" | ".join(column.ljust(widths[column]) for column in columns))
    print("-+-".join("-" * widths[column] for column in columns))
    for row in rows:
        print(
            " | ".join(
                ("" if row[column] is None else str(row[column])).ljust(widths[column])
                for column in columns
            )
        )


def print_summary(rows: list[dict[str, Any]]) -> None:
    print()
    print("Summary")
    print("-------")
    print(f"total tasks audited: {len(rows)}")
    print(f"linked questions: {sum(1 for row in rows if row['question_id'])}")
    print(
        "visual-dependent linked questions: "
        f"{sum(1 for row in rows if row['question_id'] and row['prompt_needs_visual'] == 'yes')}"
    )
    print(
        "tasks with asset links: "
        f"{sum(1 for row in rows if int(row['question_asset_links']) + int(row['task_asset_links']) > 0)}"
    )
    print(f"tasks with missing asset files: {sum(1 for row in rows if int(row['missing_asset_files']) > 0)}")
    print(f"quiz-blocking visual gaps: {sum(1 for row in rows if row['quiz_blocking'] == 'yes')}")
    print("asset_audit_exit=0")


def main() -> int:
    args = parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"error: DB not found: {db_path}", file=sys.stderr)
        return 1

    project_root = Path(__file__).resolve().parents[1]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"error: could not open DB read-only: {exc}", file=sys.stderr)
        return 1

    conn.row_factory = sqlite3.Row
    try:
        exam = resolve_exam(conn, args.source_slug)
        if exam is None:
            print(f"error: unknown DZI source slug: {args.source_slug}", file=sys.stderr)
            return 1

        source_slug = make_source_slug(exam["year"], exam["session"], exam["variant"])
        print(f"DZI asset audit: {source_slug} (exam_id={exam['id']})")
        print()

        rows = audit_tasks(conn, exam, project_root)
        print_table(
            rows,
            [
                "task_number",
                "question_id",
                "question_type",
                "prompt_needs_visual",
                "question_asset_links",
                "task_asset_links",
                "missing_asset_files",
                "asset_status",
                "quiz_blocking",
            ],
        )
        print_summary(rows)
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"error: SQLite audit failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

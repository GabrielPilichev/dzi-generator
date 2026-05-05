#!/usr/bin/env python3
"""Import an official DZI exam skeleton from an existing DZI blueprint."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path("data/questions.db")
DEFAULT_FORMAT_VERSION = "dzi_it_pp_2025_format"
DEFAULT_BLUEPRINT = "dzi_it_pp_2025_format"
SUBJECT = "informatika_it"
LEVEL = "DZI"
PARSER_VERSION = "manual_skeleton_v1"
PRACTICAL_ENVIRONMENTS = {
    26: "spreadsheet",
    27: "graphics",
    28: "web",
}
ASSET_SUBDIRS = (
    "images",
    "spreadsheets",
    "graphics",
    "web",
    "pdf_crops",
    "resources",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create one DZI exam row plus exam task skeleton rows from a blueprint."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--source-slug", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--session", required=True)
    parser.add_argument("--variant", type=int)
    parser.add_argument("--format-version", default=DEFAULT_FORMAT_VERSION)
    parser.add_argument("--source-url")
    parser.add_argument("--local-pdf-path")
    parser.add_argument("--answer-key-path")
    parser.add_argument("--blueprint", default=DEFAULT_BLUEPRINT)
    return parser.parse_args()


def get_columns(conn: sqlite3.Connection, table: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {
        row["name"]: {
            "notnull": bool(row["notnull"]),
            "default": row["dflt_value"],
            "pk": bool(row["pk"]),
        }
        for row in rows
    }


def require_table(conn: sqlite3.Connection, table: str) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if not exists:
        raise RuntimeError(f"Required table does not exist: {table}")


def null_safe_clause(column: str, value: Any) -> tuple[str, list[Any]]:
    if value is None:
        return f"{column} IS NULL", []
    return f"{column} = ?", [value]


def validate_source_slug(source_slug: str) -> None:
    source_path = Path(source_slug)
    if source_path.is_absolute() or ".." in source_path.parts or len(source_path.parts) != 1:
        raise ValueError("--source-slug must be a single safe path segment")


def create_asset_dirs(source_slug: str) -> Path:
    asset_dir = Path("data/assets/exams") / source_slug
    for subdir in ASSET_SUBDIRS:
        (asset_dir / subdir).mkdir(parents=True, exist_ok=True)
    return asset_dir


def load_blueprint_slots(
    conn: sqlite3.Connection,
    blueprint_slug: str,
) -> list[sqlite3.Row]:
    blueprint = conn.execute(
        """
        SELECT id, total_points
        FROM dzi_blueprints
        WHERE blueprint_slug = ?
        """,
        (blueprint_slug,),
    ).fetchone()
    if blueprint is None:
        raise RuntimeError(f"Blueprint not found: {blueprint_slug}")

    slots = conn.execute(
        """
        SELECT slot_number, exam_part, task_kind, points, topic_id, section_id
        FROM dzi_blueprint_slots
        WHERE blueprint_id = ?
        ORDER BY slot_number
        """,
        (blueprint["id"],),
    ).fetchall()
    if len(slots) != 28:
        raise RuntimeError(f"Expected 28 blueprint slots, found {len(slots)}")

    total_points = sum(row["points"] for row in slots)
    if total_points != blueprint["total_points"]:
        raise RuntimeError(
            f"Blueprint slot points sum to {total_points}, expected {blueprint['total_points']}"
        )

    return slots


def select_existing_exam(
    conn: sqlite3.Connection,
    year: int,
    session: str,
    variant: int | None,
) -> sqlite3.Row | None:
    if variant is None:
        variant_clause = "variant IS NULL"
        params: list[Any] = [SUBJECT, LEVEL, year, session]
    else:
        variant_clause = "variant = ?"
        params = [SUBJECT, LEVEL, year, session, variant]

    row = conn.execute(
        f"""
        SELECT *
        FROM exams
        WHERE subject = ?
          AND level = ?
          AND year = ?
          AND session = ?
          AND {variant_clause}
        ORDER BY id
        LIMIT 1
        """,
        params,
    ).fetchone()
    return row


def upsert_exam(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    columns = get_columns(conn, "exams")
    values: dict[str, Any] = {
        "subject": SUBJECT,
        "level": LEVEL,
        "year": args.year,
        "session": args.session,
        "variant": args.variant,
        "format_version": args.format_version,
        "source_url": args.source_url,
    }

    if "title" in columns:
        values["title"] = args.title
    if "source_slug" in columns:
        values["source_slug"] = args.source_slug
    if "source_file" in columns:
        values["source_file"] = args.local_pdf_path or ""
    if "parser_version" in columns:
        values["parser_version"] = PARSER_VERSION

    existing_exam = select_existing_exam(conn, args.year, args.session, args.variant)
    available_values = {key: value for key, value in values.items() if key in columns}

    if existing_exam is None:
        insert_columns = list(available_values)
        placeholders = ", ".join("?" for _ in insert_columns)
        conn.execute(
            f"""
            INSERT INTO exams ({", ".join(insert_columns)})
            VALUES ({placeholders})
            """,
            [available_values[column] for column in insert_columns],
        )
        exam_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        return exam_id

    exam_id = int(existing_exam["id"])
    update_values: dict[str, Any] = {}
    if "format_version" in columns:
        update_values["format_version"] = args.format_version
    if "title" in columns:
        update_values["title"] = args.title
    if "source_slug" in columns:
        update_values["source_slug"] = args.source_slug
    if "source_url" in columns and args.source_url:
        update_values["source_url"] = args.source_url
    if "source_file" in columns and args.local_pdf_path:
        update_values["source_file"] = args.local_pdf_path
    if "parser_version" in columns and not (existing_exam["parser_version"] or "").strip():
        # Preserve parser provenance from previously parsed exams; skeleton import only fills blanks.
        update_values["parser_version"] = PARSER_VERSION

    if update_values:
        assignments = ", ".join(f"{column} = ?" for column in update_values)
        conn.execute(
            f"UPDATE exams SET {assignments} WHERE id = ?",
            [*update_values.values(), exam_id],
        )
    return exam_id


def find_official_source_id(
    conn: sqlite3.Connection,
    exam_id: int,
    source_kind: str,
    source_url: str | None,
    local_path: str | None,
) -> int | None:
    source_url_clause, source_url_params = null_safe_clause("source_url", source_url)
    local_path_clause, local_path_params = null_safe_clause("local_path", local_path)
    row = conn.execute(
        f"""
        SELECT id
        FROM official_exam_sources
        WHERE exam_id = ?
          AND source_kind = ?
          AND {source_url_clause}
          AND {local_path_clause}
        ORDER BY id
        LIMIT 1
        """,
        [exam_id, source_kind, *source_url_params, *local_path_params],
    ).fetchone()
    return int(row["id"]) if row else None


def upsert_official_source(
    conn: sqlite3.Connection,
    exam_id: int,
    source_kind: str,
    source_url: str | None,
    local_path: str | None,
) -> None:
    source_id = find_official_source_id(conn, exam_id, source_kind, source_url, local_path)
    if source_id is None:
        conn.execute(
            """
            INSERT INTO official_exam_sources (
                exam_id, authority, source_kind, source_url, local_path
            )
            VALUES (?, 'MON', ?, ?, ?)
            """,
            (exam_id, source_kind, source_url, local_path),
        )
    else:
        conn.execute(
            """
            UPDATE official_exam_sources
            SET authority = 'MON',
                source_url = ?,
                local_path = ?
            WHERE id = ?
            """,
            (source_url, local_path, source_id),
        )


def upsert_exam_tasks(
    conn: sqlite3.Connection,
    exam_id: int,
    slots: list[sqlite3.Row],
) -> tuple[int, int]:
    columns = get_columns(conn, "exam_tasks")
    inserted = 0
    updated = 0

    for slot in slots:
        task_number = slot["slot_number"]
        values: dict[str, Any] = {
            "exam_id": exam_id,
            "task_number": task_number,
            "exam_part": slot["exam_part"],
            "task_kind": slot["task_kind"],
            "points": slot["points"],
            "title_bg": f"Задача {task_number}",
            "prompt": None,
            "rubric": None,
            "topic_id": slot["topic_id"],
            "section_id": slot["section_id"],
            "has_assets": 0,
        }
        available_values = {key: value for key, value in values.items() if key in columns}

        existing = conn.execute(
            """
            SELECT id
            FROM exam_tasks
            WHERE exam_id = ? AND task_number = ?
            """,
            (exam_id, task_number),
        ).fetchone()
        if existing is None:
            insert_columns = list(available_values)
            placeholders = ", ".join("?" for _ in insert_columns)
            conn.execute(
                f"""
                INSERT INTO exam_tasks ({", ".join(insert_columns)})
                VALUES ({placeholders})
                """,
                [available_values[column] for column in insert_columns],
            )
            inserted += 1
        else:
            update_values = {
                key: value
                for key, value in available_values.items()
                if key not in {"exam_id", "task_number"}
            }
            assignments = ", ".join(f"{column} = ?" for column in update_values)
            conn.execute(
                f"UPDATE exam_tasks SET {assignments} WHERE id = ?",
                [*update_values.values(), existing["id"]],
            )
            updated += 1

    return inserted, updated


def upsert_practical_tasks(conn: sqlite3.Connection, exam_id: int) -> int:
    upserted = 0
    for task_number, work_environment in PRACTICAL_ENVIRONMENTS.items():
        task = conn.execute(
            """
            SELECT id
            FROM exam_tasks
            WHERE exam_id = ? AND task_number = ?
            """,
            (exam_id, task_number),
        ).fetchone()
        if task is None:
            raise RuntimeError(f"Cannot create practical task row; task {task_number} is missing")

        conn.execute(
            """
            INSERT INTO practical_tasks (task_id, work_environment)
            VALUES (?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                work_environment = excluded.work_environment
            """,
            (task["id"], work_environment),
        )
        upserted += 1

    return upserted


def print_summary(
    conn: sqlite3.Connection,
    exam_id: int,
    source_slug: str,
    tasks_inserted: int,
    tasks_updated: int,
    practical_upserted: int,
    asset_dir: Path,
) -> None:
    totals = conn.execute(
        """
        SELECT COUNT(*) AS total_tasks, COALESCE(SUM(points), 0) AS total_points
        FROM exam_tasks
        WHERE exam_id = ?
        """,
        (exam_id,),
    ).fetchone()

    print(f"exam_id: {exam_id}")
    print(f"source_slug: {source_slug}")
    print(f"tasks inserted: {tasks_inserted}")
    print(f"tasks updated: {tasks_updated}")
    print(f"practical rows upserted: {practical_upserted}")
    print(f"asset directory path: {asset_dir}")
    print(f"total tasks: {totals['total_tasks']}")
    print(f"total points: {totals['total_points']}")


def main() -> None:
    args = parse_args()
    validate_source_slug(args.source_slug)
    asset_dir = create_asset_dirs(args.source_slug)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        with conn:
            for table in (
                "exams",
                "exam_tasks",
                "official_exam_sources",
                "practical_tasks",
                "dzi_blueprints",
                "dzi_blueprint_slots",
            ):
                require_table(conn, table)

            slots = load_blueprint_slots(conn, args.blueprint)
            exam_id = upsert_exam(conn, args)

            if args.answer_key_path:
                upsert_official_source(
                    conn,
                    exam_id,
                    "answer_key_pdf",
                    None,
                    args.answer_key_path,
                )
            if args.local_pdf_path or args.source_url:
                upsert_official_source(
                    conn,
                    exam_id,
                    "exam_pdf",
                    args.source_url,
                    args.local_pdf_path,
                )

            tasks_inserted, tasks_updated = upsert_exam_tasks(conn, exam_id, slots)
            practical_upserted = upsert_practical_tasks(conn, exam_id)

        print_summary(
            conn,
            exam_id,
            args.source_slug,
            tasks_inserted,
            tasks_updated,
            practical_upserted,
            asset_dir,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()

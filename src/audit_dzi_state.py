#!/usr/bin/env python3
"""Read-only readiness audit for official DZI exam sources."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path("data/questions.db")
FORMAT_VERSION = "dzi_it_pp_2025_format"
SESSION_TO_SLUG = {
    "may": "may",
    "august": "aug",
}
SLUG_TO_SESSION = {
    "may": "may",
    "aug": "august",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit DZI source readiness for Part 1 import.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--source-slug")
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


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    value = conn.execute(sql, params).fetchone()[0]
    return int(value or 0)


def linked_asset_paths(conn: sqlite3.Connection, exam_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT a.local_path
        FROM assets a
        JOIN asset_links al ON al.asset_id = a.id
        WHERE (
            al.owner_type = 'exam' AND al.owner_id = ?
        ) OR (
            al.owner_type = 'exam_task'
            AND al.owner_id IN (SELECT id FROM exam_tasks WHERE exam_id = ?)
        ) OR (
            al.owner_type = 'question'
            AND al.owner_id IN (
                SELECT etq.question_id
                FROM exam_task_questions etq
                JOIN exam_tasks et ON et.id = etq.task_id
                WHERE et.exam_id = ?
            )
        )
        ORDER BY a.local_path
        """,
        (exam_id, exam_id, exam_id),
    ).fetchall()
    return [row["local_path"] for row in rows]


def missing_linked_asset_count(conn: sqlite3.Connection, exam_id: int) -> int:
    return sum(1 for local_path in linked_asset_paths(conn, exam_id) if not Path(local_path).exists())


def missing_question_links(conn: sqlite3.Connection, exam_id: int) -> str:
    rows = conn.execute(
        """
        SELECT et.task_number
        FROM exam_tasks et
        LEFT JOIN exam_task_questions etq
          ON etq.task_id = et.id AND etq.role = 'primary'
        WHERE et.exam_id = ?
          AND et.task_number BETWEEN 1 AND 25
        GROUP BY et.id
        HAVING COUNT(etq.question_id) = 0
        ORDER BY et.task_number
        """,
        (exam_id,),
    ).fetchall()
    return ",".join(str(row["task_number"]) for row in rows)


def audit_exam(conn: sqlite3.Connection, exam: sqlite3.Row) -> dict[str, Any]:
    exam_id = int(exam["id"])
    exam_tasks = scalar(conn, "SELECT COUNT(*) FROM exam_tasks WHERE exam_id = ?", (exam_id,))
    points = scalar(conn, "SELECT COALESCE(SUM(points), 0) FROM exam_tasks WHERE exam_id = ?", (exam_id,))
    practical_tasks = scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM practical_tasks pt
        JOIN exam_tasks et ON et.id = pt.task_id
        WHERE et.exam_id = ?
        """,
        (exam_id,),
    )
    official_exam_pdf_sources = scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM official_exam_sources
        WHERE exam_id = ? AND source_kind = 'exam_pdf'
        """,
        (exam_id,),
    )
    source_pdf_assets = scalar(
        conn,
        """
        SELECT COUNT(DISTINCT al.asset_id)
        FROM asset_links al
        WHERE al.owner_type = 'exam'
          AND al.owner_id = ?
          AND al.role = 'source_pdf'
        """,
        (exam_id,),
    )
    missing_source_file = "yes" if not (exam["source_file"] or "").strip() else "no"
    missing_assets = missing_linked_asset_count(conn, exam_id)

    q_links_1_25 = scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM exam_tasks et
        WHERE et.exam_id = ?
          AND et.task_number BETWEEN 1 AND 25
          AND EXISTS (
              SELECT 1
              FROM exam_task_questions etq
              JOIN questions q ON q.id = etq.question_id
              WHERE etq.task_id = et.id
                AND (q.is_ai_generated = 0 OR q.quality_score >= 1.0)
          )
        """,
        (exam_id,),
    )

    row = {
        "exam_id": exam_id,
        "source_slug": make_source_slug(exam["year"], exam["session"], exam["variant"]),
        "year": exam["year"],
        "session": exam["session"],
        "variant": exam["variant"],
        "exam_tasks": exam_tasks,
        "points": points,
        "tasks_1_15": scalar(
            conn,
            "SELECT COUNT(*) FROM exam_tasks WHERE exam_id = ? AND task_number BETWEEN 1 AND 15",
            (exam_id,),
        ),
        "tasks_16_25": scalar(
            conn,
            "SELECT COUNT(*) FROM exam_tasks WHERE exam_id = ? AND task_number BETWEEN 16 AND 25",
            (exam_id,),
        ),
        "tasks_26_28": scalar(
            conn,
            "SELECT COUNT(*) FROM exam_tasks WHERE exam_id = ? AND task_number BETWEEN 26 AND 28",
            (exam_id,),
        ),
        "practical_tasks": practical_tasks,
        "exam_pdf_sources": official_exam_pdf_sources,
        "source_pdf_assets": source_pdf_assets,
        "missing_source_file": missing_source_file,
        "q_links_1_25": q_links_1_25,
        "q_links_26_28": scalar(
            conn,
            """
            SELECT COUNT(DISTINCT etq.question_id)
            FROM exam_task_questions etq
            JOIN exam_tasks et ON et.id = etq.task_id
            WHERE et.exam_id = ? AND et.task_number BETWEEN 26 AND 28
            """,
            (exam_id,),
        ),
        "missing_q_links_1_25": missing_question_links(conn, exam_id),
        "tasks_with_assets": scalar(
            conn,
            "SELECT COUNT(*) FROM exam_tasks WHERE exam_id = ? AND has_assets = 1",
            (exam_id,),
        ),
        "missing_asset_files": missing_assets,
    }
    ready = (
        exam_tasks == 28
        and points == 100
        and practical_tasks == 3
        and official_exam_pdf_sources >= 1
        and source_pdf_assets >= 1
        and missing_source_file == "no"
    )
    if ready and q_links_1_25 == 25:
        row["status"] = "PART1_IMPORTED"
    elif ready:
        row["status"] = "READY_FOR_PART1_IMPORT"
    else:
        row["status"] = "NEEDS_ATTENTION"
    return row


def print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        print("No matching DZI exams found.")
        return
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


def main() -> int:
    args = parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        params: list[Any] = [FORMAT_VERSION]
        where = "WHERE format_version = ?"
        if args.source_slug:
            year, session, variant = parse_source_slug(args.source_slug)
            where += " AND year = ? AND session = ? AND variant = ?"
            params.extend([year, session, variant])

        exams = conn.execute(
            f"""
            SELECT *
            FROM exams
            {where}
            ORDER BY year, session, variant, id
            """,
            params,
        ).fetchall()
        rows = [audit_exam(conn, exam) for exam in exams]
        columns = [
            "exam_id",
            "source_slug",
            "year",
            "session",
            "variant",
            "exam_tasks",
            "points",
            "tasks_1_15",
            "tasks_16_25",
            "tasks_26_28",
            "practical_tasks",
            "exam_pdf_sources",
            "source_pdf_assets",
            "missing_source_file",
            "q_links_1_25",
            "q_links_26_28",
            "missing_q_links_1_25",
            "tasks_with_assets",
            "missing_asset_files",
            "status",
        ]
        print_table(rows, columns)

        imported_count = sum(1 for row in rows if row["status"] == "PART1_IMPORTED")
        ready_count = sum(1 for row in rows if row["status"] == "READY_FOR_PART1_IMPORT")
        needs_attention_count = sum(1 for row in rows if row["status"] == "NEEDS_ATTENTION")
        total_missing_assets = sum(row["missing_asset_files"] for row in rows)
        total_official_sources = scalar(conn, "SELECT COUNT(*) FROM official_exam_sources")
        total_assets = scalar(conn, "SELECT COUNT(*) FROM assets")
        fk_count = scalar(conn, "SELECT COUNT(*) FROM pragma_foreign_key_check")

        print("\nOverall totals")
        print("--------------")
        print(f"DZI exams: {len(rows)}")
        print(f"PART1_IMPORTED: {imported_count}")
        print(f"READY_FOR_PART1_IMPORT: {ready_count}")
        print(f"NEEDS_ATTENTION: {needs_attention_count}")
        print(f"total official sources: {total_official_sources}")
        print(f"total assets: {total_assets}")
        print(f"total missing asset files: {total_missing_assets}")
        print(f"foreign key check rows: {fk_count}")

        return 0 if rows and needs_attention_count == 0 and total_missing_assets == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

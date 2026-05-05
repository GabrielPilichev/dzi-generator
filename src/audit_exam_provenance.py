#!/usr/bin/env python3
"""Read-only audit for ambiguous or stale DZI exam provenance."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Iterable


DEFAULT_DB_PATH = Path("data/questions.db")
DZI_FORMAT_VERSION = "dzi_it_pp_2025_format"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit DZI exam provenance without writing.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    return parser.parse_args()


def print_rows(title: str, columns: list[str], rows: Iterable[sqlite3.Row]) -> int:
    rows = list(rows)
    print(f"\n{title}")
    print("-" * len(title))
    if not rows:
        print("OK: none")
        return 0

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
    return len(rows)


def main() -> int:
    args = parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        warning_count = 0

        duplicate_exam_identities = conn.execute(
            """
            SELECT subject, level, year, session, variant, COUNT(*) AS duplicate_count
            FROM exams
            GROUP BY subject, level, year, session, variant
            HAVING COUNT(*) > 1
            ORDER BY duplicate_count DESC, subject, level, year, session, variant
            """
        ).fetchall()
        warning_count += print_rows(
            "A. Duplicate Exam Identities",
            ["subject", "level", "year", "session", "variant", "duplicate_count"],
            duplicate_exam_identities,
        )

        dzi_zero_tasks = conn.execute(
            """
            SELECT e.id, e.subject, e.level, e.year, e.session, e.variant, e.format_version
            FROM exams e
            LEFT JOIN exam_tasks et ON et.exam_id = e.id
            WHERE e.level = 'DZI'
            GROUP BY e.id
            HAVING COUNT(et.id) = 0
            ORDER BY e.year, e.session, e.variant, e.id
            """
        ).fetchall()
        warning_count += print_rows(
            "B. DZI Exams With Zero Exam Tasks",
            ["id", "subject", "level", "year", "session", "variant", "format_version"],
            dzi_zero_tasks,
        )

        dzi_questions_zero_tasks = conn.execute(
            """
            SELECT e.id, e.subject, e.level, e.year, e.session, e.variant,
                   COUNT(DISTINCT q.id) AS questions
            FROM exams e
            JOIN questions q ON q.exam_id = e.id
            LEFT JOIN exam_tasks et ON et.exam_id = e.id
            WHERE e.level = 'DZI'
            GROUP BY e.id
            HAVING COUNT(et.id) = 0
            ORDER BY e.year, e.session, e.variant, e.id
            """
        ).fetchall()
        warning_count += print_rows(
            "C. DZI Exams With Questions But Zero Exam Tasks",
            ["id", "subject", "level", "year", "session", "variant", "questions"],
            dzi_questions_zero_tasks,
        )

        missing_source_file = conn.execute(
            """
            SELECT id, subject, level, year, session, variant, format_version, source_file
            FROM exams
            WHERE level = 'DZI'
              AND (source_file IS NULL OR TRIM(source_file) = '')
            ORDER BY year, session, variant, id
            """
        ).fetchall()
        warning_count += print_rows(
            "D. DZI Exams With Missing source_file",
            ["id", "subject", "level", "year", "session", "variant", "format_version", "source_file"],
            missing_source_file,
        )

        stale_format = conn.execute(
            """
            SELECT id, subject, level, year, session, variant, format_version, source_file
            FROM exams
            WHERE level = 'DZI'
              AND (format_version IS NULL OR format_version != ?)
            ORDER BY year, session, variant, id
            """,
            (DZI_FORMAT_VERSION,),
        ).fetchall()
        warning_count += print_rows(
            "E. DZI Exams With Non-Current format_version",
            ["id", "subject", "level", "year", "session", "variant", "format_version", "source_file"],
            stale_format,
        )

        print(f"\nWarnings found: {warning_count}")
        return 1 if warning_count else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

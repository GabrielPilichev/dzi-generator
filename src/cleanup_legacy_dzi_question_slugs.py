#!/usr/bin/env python3
"""Rename known legacy DZI question source slugs to canonical variant slugs."""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DB_PATH = Path("data/questions.db")


@dataclass(frozen=True)
class Mapping:
    old: str
    target: str
    year: int
    session: str
    variant: int


MAPPINGS = (
    Mapping("may_2022", "may_2022_v1", 2022, "may", 1),
    Mapping("aug_2024", "aug_2024_v2", 2024, "august", 2),
    Mapping("may_2024", "may_2024_v1", 2024, "may", 1),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean up known legacy DZI source_exam slugs.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def count_questions(conn: sqlite3.Connection, source_exam: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM questions WHERE source_exam = ?",
            (source_exam,),
        ).fetchone()[0]
    )


def canonical_exam_exists(conn: sqlite3.Connection, mapping: Mapping) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
            FROM exams
            WHERE format_version = 'dzi_it_pp_2025_format'
              AND year = ?
              AND session = ?
              AND variant = ?
            LIMIT 1
            """,
            (mapping.year, mapping.session, mapping.variant),
        ).fetchone()
        is not None
    )


def main() -> int:
    args = parse_args()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    refused = 0
    updated_total = 0

    try:
        if args.dry_run:
            print("mode: dry-run")
        else:
            print("mode: real")

        with conn if not args.dry_run else _null_context():
            for mapping in MAPPINGS:
                old_count = count_questions(conn, mapping.old)
                target_count = count_questions(conn, mapping.target)
                exam_exists = canonical_exam_exists(conn, mapping)

                print(f"\n{mapping.old} -> {mapping.target}")
                print(f"old slug count: {old_count}")
                print(f"target slug count: {target_count}")
                print(f"canonical exam exists: {'yes' if exam_exists else 'no'}")

                if old_count == 0:
                    print("action: skip, no old rows")
                    continue
                if target_count > 0:
                    refused += 1
                    print("action: refused, target already exists")
                    continue
                if not exam_exists:
                    refused += 1
                    print("action: refused, canonical exam row missing")
                    continue

                if args.dry_run:
                    print(f"action: would update {old_count} row(s)")
                else:
                    cur = conn.execute(
                        "UPDATE questions SET source_exam = ? WHERE source_exam = ?",
                        (mapping.target, mapping.old),
                    )
                    updated = int(cur.rowcount or 0)
                    updated_total += updated
                    print(f"action: updated {updated} row(s)")

        print("\nSummary")
        print("-------")
        for mapping in MAPPINGS:
            print(f"{mapping.old}: {count_questions(conn, mapping.old)}")
            print(f"{mapping.target}: {count_questions(conn, mapping.target)}")
        print(f"updated rows: {updated_total}")
        print(f"refused mappings: {refused}")
        return 1 if refused else 0
    finally:
        conn.close()


class _null_context:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False


if __name__ == "__main__":
    raise SystemExit(main())

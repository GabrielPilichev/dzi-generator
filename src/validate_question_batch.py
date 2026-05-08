#!/usr/bin/env python3
"""Dry-run review for prepared question batch JSON files.

This command intentionally has no write/import mode. It reuses the official
DZI Part 1 importer validation path, but opens the target database read-only
and always runs with dry-run planning enabled.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.import_dzi_questions_json import (
    DEFAULT_DB_PATH,
    Summary,
    load_json,
    require_tables,
    run_import,
)


def readonly_uri(db_path: Path) -> str:
    return f"file:{quote(str(db_path), safe='/')}?mode=ro"


def validate_batch(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    allow_missing_assets: bool = False,
    allow_unknown_topic: bool = False,
    allow_unknown_section: bool = False,
) -> Summary:
    """Validate and dry-run a question batch without committing DB changes."""

    require_tables(conn)
    before_changes = conn.total_changes
    summary = run_import(
        conn,
        payload,
        dry_run=True,
        allow_missing_assets=allow_missing_assets,
        allow_unknown_topic=allow_unknown_topic,
        allow_unknown_section=allow_unknown_section,
    )
    if conn.total_changes != before_changes:
        raise RuntimeError("dry-run validation attempted to modify the database")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a question batch and print the dry-run import plan."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--allow-missing-assets", action="store_true")
    parser.add_argument("--allow-unknown-topic", action="store_true")
    parser.add_argument("--allow-unknown-section", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = load_json(args.json)
        print("DRY RUN ONLY: validating question batch; no DB writes are allowed")
        conn = sqlite3.connect(readonly_uri(args.db), uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            summary = validate_batch(
                conn,
                payload,
                allow_missing_assets=args.allow_missing_assets,
                allow_unknown_topic=args.allow_unknown_topic,
                allow_unknown_section=args.allow_unknown_section,
            )
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("")
    print("review summary:")
    print(f"source_slug: {summary.source_slug}")
    print(f"exam_id: {summary.exam_id}")
    print(f"tasks read: {summary.tasks_read}")
    print(f"would insert questions: {summary.questions_inserted}")
    print(f"would update questions: {summary.questions_updated}")
    print(f"would replace MC options: {summary.options_inserted}")
    print(f"would replace fill-in subquestions: {summary.fill_in_subquestions_inserted}")
    print(f"would ensure exam_task links: {summary.exam_task_links_inserted}")
    print(f"would link assets: {summary.assets_linked}")
    print(f"unknown topics: {summary.unknown_topics}")
    print(f"unknown sections: {summary.unknown_sections}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

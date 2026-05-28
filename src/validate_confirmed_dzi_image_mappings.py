#!/usr/bin/env python3
"""Validate confirmed DZI image-question mappings before asset import."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from urllib.parse import quote


DEFAULT_DB_PATH = Path("data/questions.db")
DEFAULT_MAPPING_PATH = Path("docs/reviews/confirmed_dzi_image_question_mappings.json")
VALID_CONFIDENCE = {"exact", "likely", "ambiguous", "missing"}
READY_CONFIDENCE = {"exact", "likely"}
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.png$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate confirmed DZI image-question mappings without DB writes."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING_PATH)
    return parser.parse_args()


def readonly_uri(db_path: Path) -> str:
    return f"file:{quote(str(db_path), safe='/')}?mode=ro"


def load_mapping(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"mapping file not found: {path}")
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("mapping JSON root must be an object")
    return payload


def all_entries(payload: dict[str, object]) -> list[tuple[str, int, dict[str, object]]]:
    entries: list[tuple[str, int, dict[str, object]]] = []
    for section_name in ("mappings", "explicit_no_image_notes"):
        section = payload.get(section_name, [])
        if not isinstance(section, list):
            entries.append((section_name, 0, {"__section_error__": f"{section_name} must be a list"}))
            continue
        for index, entry in enumerate(section, start=1):
            if isinstance(entry, dict):
                entries.append((section_name, index, entry))
            else:
                entries.append((section_name, index, {"__entry_error__": "entry must be an object"}))
    return entries


def is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def entry_label(section_name: str, index: int, entry: dict[str, object]) -> str:
    source_slug = entry.get("source_slug")
    official_number = entry.get("official_question_number")
    if isinstance(source_slug, str) and is_plain_int(official_number):
        return f"{section_name}[{index}] {source_slug} Q{official_number}"
    return f"{section_name}[{index}]"


def validate_safe_filename(filename: str) -> list[str]:
    errors = []
    if "\\" in filename:
        errors.append("uses backslashes")
    if "/" in filename:
        errors.append("must be a filename, not a path")
    if "://" in filename:
        errors.append("looks like a URL")
    if filename.startswith(("~", "/")):
        errors.append("is not a relative filename")
    if "vault/Generated/Quizzes" in filename:
        errors.append("points into vault/Generated/Quizzes")

    path = PurePosixPath(filename)
    if path.is_absolute():
        errors.append("is absolute")
    if any(part in {"", ".", ".."} for part in path.parts):
        errors.append("contains an empty, current-directory, or parent-directory path segment")
    if not SAFE_FILENAME_RE.fullmatch(filename):
        errors.append("must be a safe .png filename using letters, digits, '.', '_', or '-'")
    return errors


def fetch_question_rows(conn: sqlite3.Connection, question_ids: set[int]) -> dict[int, sqlite3.Row]:
    if not question_ids:
        return {}
    placeholders = ",".join("?" for _ in sorted(question_ids))
    rows = conn.execute(
        f"""
        SELECT id, source_exam, source_number, prompt
        FROM questions
        WHERE id IN ({placeholders})
        """,
        tuple(sorted(question_ids)),
    ).fetchall()
    return {int(row["id"]): row for row in rows}


def validate_entries(
    payload: dict[str, object],
    question_rows: dict[int, sqlite3.Row],
) -> tuple[list[str], dict[str, int]]:
    errors: list[str] = []
    entries = all_entries(payload)
    confidence_counts = Counter()
    source_pairs: dict[tuple[str, int], list[str]] = defaultdict(list)
    db_ids: dict[int, list[tuple[str, str, int]]] = defaultdict(list)
    ready_mapping_count = 0

    for section_name, index, entry in entries:
        label = entry_label(section_name, index, entry)
        if "__section_error__" in entry:
            errors.append(f"{section_name}: {entry['__section_error__']}")
            continue
        if "__entry_error__" in entry:
            errors.append(f"{label}: {entry['__entry_error__']}")
            continue

        source_slug = entry.get("source_slug")
        official_number = entry.get("official_question_number")
        needs_image = entry.get("needs_image")
        confidence = entry.get("confidence")
        db_question_id = entry.get("db_question_id")
        expected_asset_filename = entry.get("expected_asset_filename")

        if not isinstance(source_slug, str) or not source_slug.strip():
            errors.append(f"{label}: source_slug is required")
        if not is_plain_int(official_number):
            errors.append(f"{label}: official_question_number is required and must be an integer")
        elif official_number <= 0:
            errors.append(f"{label}: official_question_number must be positive")
        if not isinstance(needs_image, bool):
            errors.append(f"{label}: needs_image must be true or false")
        if confidence not in VALID_CONFIDENCE:
            errors.append(f"{label}: confidence must be one of {', '.join(sorted(VALID_CONFIDENCE))}")
        else:
            confidence_counts[str(confidence)] += 1
            if confidence in {"missing", "ambiguous"}:
                errors.append(f"{label}: confidence is {confidence}; resolve before asset import")

        if isinstance(source_slug, str) and is_plain_int(official_number):
            source_pairs[(source_slug, official_number)].append(label)

        if needs_image is True:
            if not isinstance(expected_asset_filename, str) or not expected_asset_filename:
                errors.append(f"{label}: expected_asset_filename is required when needs_image is true")
            elif isinstance(source_slug, str) and is_plain_int(official_number):
                expected = f"{source_slug}_q{official_number}.png"
                if expected_asset_filename != expected:
                    errors.append(
                        f"{label}: expected_asset_filename must be {expected}, got {expected_asset_filename}"
                    )
                for filename_error in validate_safe_filename(expected_asset_filename):
                    errors.append(f"{label}: expected_asset_filename {filename_error}")
        elif expected_asset_filename not in (None, ""):
            errors.append(f"{label}: expected_asset_filename must be null/blank when needs_image is false")
            if isinstance(expected_asset_filename, str):
                for filename_error in validate_safe_filename(expected_asset_filename):
                    errors.append(f"{label}: expected_asset_filename {filename_error}")

        if confidence in READY_CONFIDENCE:
            if not is_plain_int(db_question_id):
                errors.append(f"{label}: db_question_id is required when confidence is {confidence}")
            elif db_question_id <= 0:
                errors.append(f"{label}: db_question_id must be positive")
            elif db_question_id not in question_rows:
                errors.append(f"{label}: db_question_id {db_question_id} does not exist in data/questions.db")
            else:
                db_official_number = official_number if is_plain_int(official_number) else -1
                db_ids[db_question_id].append((label, str(source_slug), db_official_number))
                row = question_rows[db_question_id]
                if confidence == "exact":
                    row_source_number = row["source_number"]
                    source_number_matches = (
                        is_plain_int(official_number)
                        and row_source_number is not None
                        and int(row_source_number) == official_number
                    )
                    if row["source_exam"] != source_slug or not source_number_matches:
                        errors.append(
                            f"{label}: exact db_question_id points to "
                            f"{row['source_exam']} Q{row['source_number']}"
                        )
                if needs_image is True:
                    ready_mapping_count += 1
        elif db_question_id is not None and not is_plain_int(db_question_id):
            errors.append(f"{label}: db_question_id must be an integer or null")

    for (source_slug, official_number), labels in sorted(source_pairs.items()):
        if len(labels) > 1:
            errors.append(
                f"{source_slug} Q{official_number}: duplicate source_slug + official_question_number "
                f"entries at {', '.join(labels)}"
            )

    for db_question_id, labels in sorted(db_ids.items()):
        pairs = {(source_slug, official_number) for _, source_slug, official_number in labels}
        if len(pairs) > 1:
            label_text = ", ".join(label for label, _, _ in labels)
            errors.append(f"DB question {db_question_id}: duplicate db_question_id conflict at {label_text}")

    summary = {
        "total_entries": len(entries),
        "exact": confidence_counts["exact"],
        "likely": confidence_counts["likely"],
        "missing": confidence_counts["missing"],
        "ambiguous": confidence_counts["ambiguous"],
        "ready_mapping_count": ready_mapping_count,
    }
    return errors, summary


def collect_ready_question_ids(payload: dict[str, object]) -> set[int]:
    question_ids = set()
    for _, _, entry in all_entries(payload):
        confidence = entry.get("confidence")
        db_question_id = entry.get("db_question_id")
        if confidence in READY_CONFIDENCE and is_plain_int(db_question_id):
            question_ids.add(db_question_id)
    return question_ids


def print_summary(mapping_path: Path, db_path: Path, summary: dict[str, int], errors: list[str]) -> None:
    print("Confirmed DZI image mapping validation")
    print("======================================")
    print(f"mapping: {mapping_path}")
    print(f"DB: {db_path}")
    print(f"total entries: {summary['total_entries']}")
    print(f"exact entries: {summary['exact']}")
    print(f"likely entries: {summary['likely']}")
    print(f"missing entries: {summary['missing']}")
    print(f"ambiguous entries: {summary['ambiguous']}")
    print(f"ready mapping count: {summary['ready_mapping_count']}")
    print(f"validation errors: {len(errors)}")
    print(f"validation_exit={0 if not errors else 1}")
    if errors:
        print()
        print("Validation errors")
        print("-----------------")
        for error in errors:
            print(f"- {error}")


def main() -> int:
    args = parse_args()
    try:
        if not args.db.exists():
            raise FileNotFoundError(f"DB not found: {args.db}")
        payload = load_mapping(args.mapping)
        question_ids = collect_ready_question_ids(payload)

        conn = sqlite3.connect(readonly_uri(args.db), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            question_rows = fetch_question_rows(conn, question_ids)
        finally:
            conn.close()

        errors, summary = validate_entries(payload, question_rows)
    except (OSError, json.JSONDecodeError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print_summary(args.mapping, args.db, summary, errors)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

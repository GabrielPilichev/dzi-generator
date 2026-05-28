#!/usr/bin/env python3
"""Validate a manual question-image review Markdown file read-only."""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote


DEFAULT_DB_PATH = Path("data/questions.db")
DEFAULT_REVIEW_PATH = Path("docs/reviews/question_image_dependency_review.md")
REQUIRED_MANUAL_FIELDS = ("needs_image", "asset_source", "expected_asset_filename", "notes")
VALID_NEEDS_IMAGE = {"yes", "no", "unclear"}
VALID_SUGGESTED_BY_TOOL = {"yes", "no"}
UNSET_VALUES = {"", "-", "\u2014"}
SAFE_ASSET_PREFIX = "data/assets/exams/"
SAFE_BARE_ASSET_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.[A-Za-z0-9]+$")
SECTION_RE = re.compile(r"^###\s+\d+\.\s+Q(?P<question_id>\d+)\b")


@dataclass(frozen=True)
class ReviewEntry:
    question_id: int
    line_number: int
    fields: dict[str, str]
    field_lines: dict[str, int]

    def value(self, field: str) -> str:
        return self.fields.get(field, "")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a manual image-dependency review file without writing to the DB."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW_PATH)
    parser.add_argument(
        "--require-reviewed",
        action="store_true",
        help="Treat blank needs_image fields as validation errors.",
    )
    return parser.parse_args()


def readonly_uri(db_path: Path) -> str:
    return f"file:{quote(str(db_path), safe='/')}?mode=ro"


def clean_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == "`" and value[-1] == "`":
        return value[1:-1].strip()
    return value


def is_unset(value: str) -> bool:
    return clean_value(value) in UNSET_VALUES


def parse_bullet(line: str) -> tuple[str, str] | None:
    if not line.startswith("- ") or ":" not in line:
        return None
    key, value = line[2:].split(":", 1)
    return key.strip(), clean_value(value)


def parse_review(path: Path) -> list[ReviewEntry]:
    entries: list[ReviewEntry] = []
    current_question_id: int | None = None
    current_start_line = 0
    current_lines: list[tuple[int, str]] = []

    def flush() -> None:
        nonlocal current_question_id, current_start_line, current_lines
        if current_question_id is None:
            return

        fields: dict[str, str] = {}
        field_lines: dict[str, int] = {}
        for line_number, line in current_lines:
            parsed = parse_bullet(line.strip())
            if parsed is None:
                continue
            key, value = parsed
            fields[key] = value
            field_lines[key] = line_number

        entries.append(
            ReviewEntry(
                question_id=current_question_id,
                line_number=current_start_line,
                fields=fields,
                field_lines=field_lines,
            )
        )
        current_question_id = None
        current_start_line = 0
        current_lines = []

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = SECTION_RE.match(line)
        if match:
            flush()
            current_question_id = int(match.group("question_id"))
            current_start_line = line_number
            current_lines = []
            continue
        if current_question_id is not None:
            current_lines.append((line_number, line))

    flush()
    return entries


def fetch_question_ids(db_path: Path) -> set[int]:
    conn = sqlite3.connect(readonly_uri(db_path), uri=True)
    try:
        rows = conn.execute("SELECT id FROM questions").fetchall()
    finally:
        conn.close()
    return {int(row[0]) for row in rows}


def validate_asset_path(value: str) -> list[str]:
    errors = []
    if "\\" in value:
        errors.append("uses backslashes")
    if "://" in value:
        errors.append("looks like a URL")
    if value.startswith(("~", "/")):
        errors.append("is not a relative path")
    if "vault/Generated/Quizzes" in value:
        errors.append("points into vault/Generated/Quizzes")
    if not value.startswith(SAFE_ASSET_PREFIX):
        errors.append(f"must be under {SAFE_ASSET_PREFIX}")

    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        errors.append("contains an empty, current-directory, or parent-directory path segment")

    path = PurePosixPath(value)
    if path.is_absolute():
        errors.append("is absolute")
    if ".." in path.parts:
        errors.append("contains path traversal")
    if not path.name or not path.suffix:
        errors.append("must include a filename with an extension")
    return errors


def validate_bare_asset_filename(value: str) -> list[str]:
    errors = []
    if "\\" in value:
        errors.append("uses backslashes")
    if "/" in value:
        errors.append("must be a filename, not a path")
    if "://" in value:
        errors.append("looks like a URL")
    if value.startswith(("~", "/")):
        errors.append("is not a relative filename")
    if value in {".", ".."} or ".." in PurePosixPath(value).parts:
        errors.append("contains path traversal")
    if not PurePosixPath(value).suffix:
        errors.append("must include a filename extension")
    if not SAFE_BARE_ASSET_FILENAME_RE.fullmatch(value):
        errors.append("must contain only letters, digits, '.', '_', or '-' and start with a letter or digit")
    return errors


def validate_asset_filename(value: str) -> list[str]:
    if value.startswith(SAFE_ASSET_PREFIX):
        return validate_asset_path(value)
    return validate_bare_asset_filename(value)


def duplicate_errors(entries: list[ReviewEntry]) -> list[str]:
    errors = []
    by_question_id: dict[int, list[ReviewEntry]] = defaultdict(list)
    for entry in entries:
        by_question_id[entry.question_id].append(entry)

    for question_id, question_entries in sorted(by_question_id.items()):
        if len(question_entries) < 2:
            continue
        signatures = {
            (
                entry.value("needs_image").lower(),
                entry.value("asset_source"),
                entry.value("expected_asset_filename"),
                entry.value("notes"),
            )
            for entry in question_entries
        }
        lines = ", ".join(str(entry.line_number) for entry in question_entries)
        if len(signatures) == 1:
            errors.append(f"Q{question_id}: duplicate review entries at lines {lines}")
        else:
            errors.append(f"Q{question_id}: duplicate conflicting review entries at lines {lines}")
    return errors


def validate_entries(
    entries: list[ReviewEntry],
    question_ids: set[int],
    *,
    require_reviewed: bool,
) -> tuple[list[str], dict[str, int]]:
    errors: list[str] = []
    summary = {
        "entries": len(entries),
        "reviewed": 0,
        "ready_mappings": 0,
        "unclear": 0,
        "no_image": 0,
        "unreviewed": 0,
        "tool_suggested": 0,
    }

    if not entries:
        errors.append("review file has no candidate sections")

    errors.extend(duplicate_errors(entries))

    for entry in entries:
        label = f"Q{entry.question_id} line {entry.line_number}"
        if entry.question_id not in question_ids:
            errors.append(f"{label}: question_id does not exist in the DB")

        question_id_field = entry.value("question_id")
        if is_unset(question_id_field):
            errors.append(f"{label}: missing question_id field")
        else:
            try:
                field_question_id = int(question_id_field)
            except ValueError:
                errors.append(f"{label}: question_id field is not an integer")
            else:
                if field_question_id != entry.question_id:
                    errors.append(
                        f"{label}: heading question id and question_id field disagree ({field_question_id})"
                    )

        for field in REQUIRED_MANUAL_FIELDS:
            if field not in entry.fields:
                errors.append(f"{label}: missing manual field {field}")

        needs_image = entry.value("needs_image").lower()
        suggested_by_tool = entry.value("suggested_by_tool").lower()
        asset_filename = entry.value("expected_asset_filename")
        asset_errors = [] if is_unset(asset_filename) else validate_asset_filename(asset_filename)

        if not is_unset(suggested_by_tool):
            if suggested_by_tool not in VALID_SUGGESTED_BY_TOOL:
                errors.append(f"{label}: suggested_by_tool must be yes or no when present")
            elif suggested_by_tool == "yes":
                summary["tool_suggested"] += 1
        is_tool_suggested = suggested_by_tool == "yes"

        if is_unset(needs_image):
            summary["unreviewed"] += 1
            if require_reviewed:
                errors.append(f"{label}: needs_image must be yes, no, or unclear")
        elif needs_image not in VALID_NEEDS_IMAGE:
            errors.append(f"{label}: needs_image must be yes, no, or unclear")
        elif is_tool_suggested:
            if require_reviewed:
                errors.append(f"{label}: tool suggestion still needs human approval")
            if needs_image == "yes":
                if is_unset(asset_filename):
                    errors.append(f"{label}: expected_asset_filename is required when needs_image is yes")
                elif asset_errors:
                    details = "; ".join(asset_errors)
                    errors.append(f"{label}: expected_asset_filename {details}")
        else:
            summary["reviewed"] += 1
            if needs_image == "yes":
                if is_unset(asset_filename):
                    errors.append(f"{label}: expected_asset_filename is required when needs_image is yes")
                else:
                    if asset_errors:
                        details = "; ".join(asset_errors)
                        errors.append(f"{label}: expected_asset_filename {details}")
                    else:
                        summary["ready_mappings"] += 1
            elif needs_image == "unclear":
                summary["unclear"] += 1
            elif needs_image == "no":
                summary["no_image"] += 1

        if needs_image != "yes" and asset_errors:
            for asset_error in asset_errors:
                errors.append(f"{label}: expected_asset_filename {asset_error}")

    return errors, summary


def print_summary(review_path: Path, db_path: Path, summary: dict[str, int], errors: list[str]) -> None:
    print("Question image review validation")
    print("================================")
    print(f"review: {review_path}")
    print(f"DB: {db_path}")
    print(f"entries in file: {summary['entries']}")
    print(f"human-reviewed entries: {summary['reviewed']}")
    print(f"ready mappings: {summary['ready_mappings']}")
    print(f"unclear entries: {summary['unclear']}")
    print(f"no-image entries: {summary['no_image']}")
    print(f"unreviewed entries: {summary['unreviewed']}")
    print(f"tool-suggested entries: {summary['tool_suggested']}")
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
        if not args.review.exists():
            raise FileNotFoundError(f"review file not found: {args.review}")
        if not args.db.exists():
            raise FileNotFoundError(f"DB not found: {args.db}")

        entries = parse_review(args.review)
        question_ids = fetch_question_ids(args.db)
        errors, summary = validate_entries(
            entries,
            question_ids,
            require_reviewed=args.require_reviewed,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print_summary(args.review, args.db, summary, errors)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

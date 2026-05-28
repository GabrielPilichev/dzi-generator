#!/usr/bin/env python3
"""Map confirmed official DZI image-dependent task numbers to DB question IDs."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote


DEFAULT_DB_PATH = Path("data/questions.db")
DEFAULT_OUTPUT_PATH = Path("docs/reviews/confirmed_dzi_image_question_mappings.json")
SUBJECT = "informatika_it"
LEVEL = "DZI"
SOURCE_SLUG_RE = re.compile(r"^(?P<session>may|aug|jun)_(?P<year>\d{4})(?:_v(?P<variant>\d+))?$")

CONFIRMED_IMAGE_QUESTIONS = {
    "aug_2022_v2": (2, 12, 13, 23, 24),
    "aug_2023_v2": (20, 21, 22, 23, 24),
    "aug_2024_v2": (3, 9, 17, 18, 19, 21),
    "may_2022_v1": (1, 11, 12, 19),
    "may_2023_v2": (11, 12, 13, 15, 19, 20),
    "may_2024_v1": (1, 16, 17, 18, 20, 21),
    "may_2025_v2": (1, 16, 18, 21, 22),
}

EXPLICIT_NO_IMAGE_QUESTIONS = {
    "aug_2022_v2": (11,),
    "may_2022_v1": (13,),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Map confirmed official DZI image question numbers to DB question IDs."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    return parser.parse_args()


def readonly_uri(db_path: Path) -> str:
    return f"file:{quote(str(db_path), safe='/')}?mode=ro"


def clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def text_snippet(value: object, limit: int = 180) -> str:
    text = clean_text(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def parse_source_slug(source_slug: str) -> tuple[int, str, int] | None:
    match = SOURCE_SLUG_RE.fullmatch(source_slug)
    if not match:
        return None
    session = match.group("session")
    year = int(match.group("year"))
    variant = int(match.group("variant") or "1")
    if session == "aug":
        session = "august"
    elif session == "jun":
        session = "june"
    return year, session, variant


def fetch_direct_matches(
    conn: sqlite3.Connection,
    source_slug: str,
    official_question_number: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, source_exam, source_number, question_type, prompt
        FROM questions
        WHERE source_exam = ?
          AND source_number = ?
        ORDER BY id
        """,
        (source_slug, official_question_number),
    ).fetchall()


def fetch_task_link_matches(
    conn: sqlite3.Connection,
    source_slug: str,
    official_question_number: int,
) -> list[sqlite3.Row]:
    parsed = parse_source_slug(source_slug)
    if parsed is None:
        return []

    year, session, variant = parsed
    return conn.execute(
        """
        SELECT q.id, q.source_exam, q.source_number, q.question_type, q.prompt
        FROM exams e
        JOIN exam_tasks et ON et.exam_id = e.id
        JOIN exam_task_questions etq ON etq.task_id = et.id
        JOIN questions q ON q.id = etq.question_id
        WHERE e.subject = ?
          AND e.level = ?
          AND e.year = ?
          AND e.session = ?
          AND e.variant = ?
          AND et.task_number = ?
          AND etq.role = 'primary'
        ORDER BY q.id
        """,
        (SUBJECT, LEVEL, year, session, variant, official_question_number),
    ).fetchall()


def candidate_summary(row: sqlite3.Row) -> dict[str, object]:
    return {
        "db_question_id": int(row["id"]),
        "source_slug": str(row["source_exam"] or ""),
        "source_number": None if row["source_number"] is None else int(row["source_number"]),
        "question_text_snippet": text_snippet(row["prompt"]),
    }


def mapping_entry(
    *,
    source_slug: str,
    official_question_number: int,
    needs_image: bool,
    confidence: str,
    notes: str,
    row: sqlite3.Row | None = None,
    candidate_rows: list[sqlite3.Row] | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "source_slug": source_slug,
        "official_question_number": official_question_number,
        "db_question_id": int(row["id"]) if row is not None else None,
        "question_text_snippet": text_snippet(row["prompt"]) if row is not None else "",
        "needs_image": needs_image,
        "expected_asset_filename": (
            f"{source_slug}_q{official_question_number}.png" if needs_image else None
        ),
        "confidence": confidence,
        "notes": notes,
    }
    if candidate_rows:
        entry["candidate_matches"] = [candidate_summary(candidate) for candidate in candidate_rows]
    return entry


def map_one_question(
    conn: sqlite3.Connection,
    source_slug: str,
    official_question_number: int,
    *,
    needs_image: bool,
) -> dict[str, object]:
    direct_rows = fetch_direct_matches(conn, source_slug, official_question_number)
    if len(direct_rows) == 1:
        note = "matched questions.source_exam and questions.source_number exactly"
        if not needs_image:
            note = "explicit no-image note; " + note
        return mapping_entry(
            source_slug=source_slug,
            official_question_number=official_question_number,
            needs_image=needs_image,
            confidence="exact",
            notes=note,
            row=direct_rows[0],
        )
    if len(direct_rows) > 1:
        return mapping_entry(
            source_slug=source_slug,
            official_question_number=official_question_number,
            needs_image=needs_image,
            confidence="ambiguous",
            notes="multiple DB questions match the same source_slug/source_number",
            candidate_rows=direct_rows,
        )

    task_link_rows = fetch_task_link_matches(conn, source_slug, official_question_number)
    if len(task_link_rows) == 1:
        return mapping_entry(
            source_slug=source_slug,
            official_question_number=official_question_number,
            needs_image=needs_image,
            confidence="likely",
            notes="no direct source_number match; matched via exam_tasks/exam_task_questions",
            row=task_link_rows[0],
        )
    if len(task_link_rows) > 1:
        return mapping_entry(
            source_slug=source_slug,
            official_question_number=official_question_number,
            needs_image=needs_image,
            confidence="ambiguous",
            notes="no direct source_number match; multiple task-link matches found",
            candidate_rows=task_link_rows,
        )

    return mapping_entry(
        source_slug=source_slug,
        official_question_number=official_question_number,
        needs_image=needs_image,
        confidence="missing",
        notes="no DB question matched by source_slug/source_number or task-link fallback",
    )


def build_mapping_payload(
    conn: sqlite3.Connection,
    *,
    image_questions: dict[str, tuple[int, ...]] = CONFIRMED_IMAGE_QUESTIONS,
    no_image_questions: dict[str, tuple[int, ...]] = EXPLICIT_NO_IMAGE_QUESTIONS,
) -> dict[str, object]:
    mappings = [
        map_one_question(conn, source_slug, number, needs_image=True)
        for source_slug in sorted(image_questions)
        for number in image_questions[source_slug]
    ]
    no_image_notes = [
        map_one_question(conn, source_slug, number, needs_image=False)
        for source_slug in sorted(no_image_questions)
        for number in no_image_questions[source_slug]
    ]

    all_entries = [*mappings, *no_image_notes]
    by_confidence = {
        confidence: sum(1 for entry in all_entries if entry["confidence"] == confidence)
        for confidence in ("exact", "likely", "ambiguous", "missing")
    }
    summary = {
        "confirmed_image_questions": len(mappings),
        "explicit_no_image_notes": len(no_image_notes),
        "exact": by_confidence["exact"],
        "likely": by_confidence["likely"],
        "ambiguous": by_confidence["ambiguous"],
        "missing": by_confidence["missing"],
    }
    return {
        "generated_by": "src/map_confirmed_dzi_image_questions.py",
        "review_status": "review mapping only; no DB writes and no image files added",
        "summary": summary,
        "mappings": mappings,
        "explicit_no_image_notes": no_image_notes,
    }


def write_mapping(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def print_summary(output_path: Path, payload: dict[str, object]) -> None:
    summary = payload["summary"]
    print("Confirmed DZI image question mapping")
    print("====================================")
    print(f"output: {output_path}")
    print(f"confirmed image mappings: {summary['confirmed_image_questions']}")
    print(f"explicit no-image notes: {summary['explicit_no_image_notes']}")
    print(f"exact matches: {summary['exact']}")
    print(f"likely matches: {summary['likely']}")
    print(f"ambiguous matches: {summary['ambiguous']}")
    print(f"missing matches: {summary['missing']}")


def main() -> int:
    args = parse_args()
    if not args.db.exists():
        print(f"error: DB not found: {args.db}", file=sys.stderr)
        return 1

    try:
        conn = sqlite3.connect(readonly_uri(args.db), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            payload = build_mapping_payload(conn)
        finally:
            conn.close()
        write_mapping(args.output, payload)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print_summary(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read-only audit for future auto-gradable open/fill-in quiz readiness."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path("data/questions.db")

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
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit open/fill-in question readiness without writing.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    return parser.parse_args()


def open_read_only_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def prompt_needs_visual(prompt: str | None) -> bool:
    text = (prompt or "").lower()
    return any(pattern in text for pattern in VISUAL_DEPENDENT_PATTERNS)


def answer_values(value: Any) -> list[str]:
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return [text] if text not in {"-", "—", "[]"} else []
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item is not None and str(item).strip()]
    if parsed is None:
        return []
    parsed_text = str(parsed).strip()
    return [parsed_text] if parsed_text and parsed_text not in {"-", "—", "[]"} else []


def subquestion_has_accepted_answer(row: sqlite3.Row) -> bool:
    accepted = []
    accepted.extend(answer_values(row["correct_answer"]))
    accepted.extend(answer_values(row["answer_alternatives"]))
    return bool(accepted)


def audit_open_question_readiness(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("""
        SELECT id, source_exam, source_number, question_type, prompt
        FROM questions
        WHERE question_type IN ('fill_in', 'short_answer')
        ORDER BY source_exam, source_number, id
    """).fetchall()

    grouped_candidates = Counter()
    summary = {
        "total_inspected": len(rows),
        "auto_gradable_count": 0,
        "excluded_practical_count": 0,
        "excluded_visual_dependent_count": 0,
        "excluded_missing_accepted_answers_count": 0,
        "candidates_by_source_slug": grouped_candidates,
    }

    for question in rows:
        task_number = question["source_number"]
        if task_number in {26, 27, 28}:
            summary["excluded_practical_count"] += 1
            continue

        if prompt_needs_visual(question["prompt"]):
            summary["excluded_visual_dependent_count"] += 1
            continue

        subquestions = conn.execute("""
            SELECT correct_answer, answer_alternatives
            FROM fill_in_subquestions
            WHERE question_id = ?
            ORDER BY subquestion_number
        """, (question["id"],)).fetchall()
        if not subquestions or any(not subquestion_has_accepted_answer(row) for row in subquestions):
            summary["excluded_missing_accepted_answers_count"] += 1
            continue

        summary["auto_gradable_count"] += 1
        grouped_candidates[question["source_exam"]] += 1

    summary["candidates_by_source_slug"] = dict(sorted(grouped_candidates.items()))
    return summary


def print_report(summary: dict) -> None:
    print("Open question readiness audit")
    print("-----------------------------")
    print(f"total fill-in/open questions inspected: {summary['total_inspected']}")
    print(f"auto-gradable open candidates: {summary['auto_gradable_count']}")
    print(f"excluded practical tasks: {summary['excluded_practical_count']}")
    print(f"excluded visual-dependent: {summary['excluded_visual_dependent_count']}")
    print(f"excluded missing accepted answers: {summary['excluded_missing_accepted_answers_count']}")
    print("candidates by source_slug:")
    if summary["candidates_by_source_slug"]:
        for source_slug, count in summary["candidates_by_source_slug"].items():
            print(f"  - {source_slug}: {count}")
    else:
        print("  - none")


def main() -> int:
    args = parse_args()
    conn = open_read_only_db(Path(args.db))
    try:
        summary = audit_open_question_readiness(conn)
    finally:
        conn.close()
    print_report(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

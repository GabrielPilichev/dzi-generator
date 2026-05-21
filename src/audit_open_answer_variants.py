#!/usr/bin/env python3
"""Read-only audit for open/fill-in accepted-answer variant coverage."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path("data/questions.db")
OPEN_QUESTION_TYPES = ("fill_in", "short_answer", "free_response")
EMPTY_MARKERS = {"", "-", "—", "[]", "null", "none"}
SLOT_MARKER_RE = re.compile(r"(?:^|\s|\()([1-9][0-9]*)\s*[\).:-]")


@dataclass(frozen=True)
class AcceptedAnswer:
    subquestion_number: int | None
    field: str
    value: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit open/fill-in accepted answers without writing to the database."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    return parser.parse_args()


def open_read_only_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def answer_values(value: Any) -> list[str]:
    if value is None:
        return []
    text = str(value).strip()
    if text.casefold() in EMPTY_MARKERS:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return [text]
    if isinstance(parsed, list):
        values = []
        for item in parsed:
            item_text = "" if item is None else str(item).strip()
            if item_text.casefold() not in EMPTY_MARKERS:
                values.append(item_text)
        return values
    parsed_text = "" if parsed is None else str(parsed).strip()
    return [parsed_text] if parsed_text.casefold() not in EMPTY_MARKERS else []


def has_latin(text: str) -> bool:
    return any("a" <= ch.lower() <= "z" for ch in text)


def has_cyrillic(text: str) -> bool:
    return any("\u0400" <= ch <= "\u04ff" for ch in text)


def separator_flags(value: str) -> list[str]:
    flags = []
    if "/" in value:
        flags.append("slash")
    if ";" in value:
        flags.append("semicolon")
    if "," in value:
        flags.append("comma")
    if re.search(r"\bили\b", value, flags=re.IGNORECASE):
        flags.append("ili")
    if has_latin(value) and has_cyrillic(value):
        flags.append("mixed_latin_cyrillic")
    return flags


def collect_accepted_answers(subquestions: list[sqlite3.Row]) -> list[AcceptedAnswer]:
    answers = []
    for row in subquestions:
        subquestion_number = row["subquestion_number"]
        for field in ("correct_answer", "answer_alternatives"):
            for value in answer_values(row[field]):
                answers.append(AcceptedAnswer(subquestion_number, field, value))
    return answers


def subquestion_answer_counts(subquestions: list[sqlite3.Row]) -> dict[int, int]:
    counts = {}
    for row in subquestions:
        counts[int(row["subquestion_number"])] = (
            len(answer_values(row["correct_answer"]))
            + len(answer_values(row["answer_alternatives"]))
        )
    return counts


def alternatives_values(row: sqlite3.Row) -> list[str]:
    return answer_values(row["answer_alternatives"])


def is_likely_global_alternative_set(subquestions: list[sqlite3.Row]) -> bool:
    if len(subquestions) <= 1:
        return False

    alternative_sets = [
        tuple(sorted(value.casefold() for value in alternatives_values(row)))
        for row in subquestions
    ]
    non_empty_sets = [values for values in alternative_sets if values]
    if (
        len(non_empty_sets) == len(subquestions)
        and len(set(non_empty_sets)) == 1
        and len(non_empty_sets[0]) >= len(subquestions)
    ):
        return True

    known_slots = {int(row["subquestion_number"]) for row in subquestions}
    for answer in collect_accepted_answers(subquestions):
        marker_slots = {
            int(match.group(1))
            for match in SLOT_MARKER_RE.finditer(answer.value)
            if int(match.group(1)) in known_slots
        }
        if len(marker_slots) >= 2:
            return True
    return False


def question_identity(question: sqlite3.Row, subquestion_number: int | None = None) -> dict[str, Any]:
    return {
        "source_slug": question["source_exam"],
        "task_number": question["source_number"],
        "question_id": question["id"],
        "subquestion_number": subquestion_number,
    }


def issue(identity: dict[str, Any], category: str, detail: str) -> dict[str, Any]:
    return {**identity, "category": category, "detail": detail}


def audit_open_answer_variants(conn: sqlite3.Connection) -> dict[str, Any]:
    questions = conn.execute("""
        SELECT id, source_exam, source_number, question_type, prompt
        FROM questions
        WHERE question_type IN ('fill_in', 'short_answer', 'free_response')
        ORDER BY source_exam, source_number, id
    """).fetchall()

    issues: list[dict[str, Any]] = []
    questions_with_issues: set[int] = set()
    category_counts = Counter()
    by_source = Counter()

    for question in questions:
        subquestions = conn.execute("""
            SELECT subquestion_number, subquestion_text, correct_answer, answer_alternatives
            FROM fill_in_subquestions
            WHERE question_id = ?
            ORDER BY subquestion_number
        """, (question["id"],)).fetchall()
        accepted = collect_accepted_answers(subquestions)
        source_slug = question["source_exam"]

        def add(category: str, detail: str, subquestion_number: int | None = None) -> None:
            issues.append(issue(question_identity(question, subquestion_number), category, detail))
            category_counts[category] += 1
            by_source[source_slug] += 1
            questions_with_issues.add(int(question["id"]))

        if not accepted:
            add("missing_accepted_answers", "question has no accepted answers in fill_in_subquestions")
            continue

        counts_by_subquestion = subquestion_answer_counts(subquestions)
        for subquestion_number, count in counts_by_subquestion.items():
            if count == 0:
                add("missing_accepted_answers", "subquestion has no accepted answers", subquestion_number)

        if len(subquestions) > 1 and len(accepted) == 1:
            add(
                "weak_variant_coverage",
                f"{len(subquestions)} subquestions but only one accepted answer across the question",
            )

        if is_likely_global_alternative_set(subquestions):
            add(
                "likely_global_alternatives",
                "alternatives look shared/global rather than tied to individual subquestion slots",
            )

        for row in subquestions:
            correct_values = answer_values(row["correct_answer"])
            alternative_values = answer_values(row["answer_alternatives"])
            if correct_values and not alternative_values:
                add(
                    "accepted_without_alternatives",
                    "subquestion has accepted answer but no alternatives",
                    int(row["subquestion_number"]),
                )

        for answer in accepted:
            flags = separator_flags(answer.value)
            if flags:
                add(
                    "separator_patterns",
                    f"{answer.field} contains {', '.join(flags)}: {answer.value}",
                    answer.subquestion_number,
                )

    return {
        "total_inspected": len(questions),
        "issues": issues,
        "category_counts": dict(sorted(category_counts.items())),
        "issues_by_source_slug": dict(sorted(by_source.items())),
        "missing_accepted_answers": category_counts.get("missing_accepted_answers", 0),
        "weak_variant_coverage": category_counts.get("weak_variant_coverage", 0),
        "likely_needs_manual_review": len(questions_with_issues),
    }


def format_location(item: dict[str, Any]) -> str:
    subq = item.get("subquestion_number")
    suffix = f" subq={subq}" if subq is not None else ""
    return (
        f"{item['source_slug']} task={item['task_number']} "
        f"question_id={item['question_id']}{suffix}"
    )


def print_issue_section(title: str, issues: list[dict[str, Any]]) -> None:
    print(title)
    print("-" * len(title))
    if not issues:
        print("  none")
        return
    for item in issues:
        print(f"  - {format_location(item)}: {item['detail']}")


def print_report(summary: dict[str, Any]) -> None:
    print("Open answer accepted variants audit")
    print("-----------------------------------")
    print(f"total inspected: {summary['total_inspected']}")
    print(f"missing accepted answers: {summary['missing_accepted_answers']}")
    print(f"weak variant coverage: {summary['weak_variant_coverage']}")
    print(f"likely needs manual review: {summary['likely_needs_manual_review']}")
    print()

    print("Issue counts by category")
    print("------------------------")
    if summary["category_counts"]:
        for category, count in summary["category_counts"].items():
            print(f"  - {category}: {count}")
    else:
        print("  - none")
    print()

    print("Issue counts by source_slug")
    print("---------------------------")
    if summary["issues_by_source_slug"]:
        for source_slug, count in summary["issues_by_source_slug"].items():
            print(f"  - {source_slug}: {count}")
    else:
        print("  - none")
    print()

    issues = summary["issues"]
    for category, title in (
        ("missing_accepted_answers", "Missing Accepted Answers"),
        ("weak_variant_coverage", "Weak Variant Coverage"),
        ("likely_global_alternatives", "Likely Global Alternatives"),
        ("accepted_without_alternatives", "Accepted Answers Without Alternatives"),
        ("separator_patterns", "Common Separator Or Mixed-Language Patterns"),
    ):
        print_issue_section(title, [item for item in issues if item["category"] == category])
        print()


def main() -> int:
    args = parse_args()
    conn = open_read_only_db(Path(args.db))
    try:
        summary = audit_open_answer_variants(conn)
    finally:
        conn.close()
    print_report(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read-only audit for questions that likely need visual assets."""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


DEFAULT_DB_PATH = Path("data/questions.db")
DEFAULT_MARKDOWN_REVIEW_PATH = Path("docs/reviews/question_image_dependency_review.md")
VISUAL_ASSET_TYPES = {"image", "pdf_crop"}
CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}
CATEGORY_ORDER = {
    "screenshot/UI": 0,
    "code image": 1,
    "chart/table": 2,
    "diagram": 3,
    "unknown visual dependency": 4,
}


@dataclass(frozen=True)
class VisualPattern:
    category: str
    confidence: str
    phrase: str
    reason: str


@dataclass(frozen=True)
class PatternMatch:
    category: str
    confidence: str
    phrase: str
    reason: str


@dataclass(frozen=True)
class AssetCoverage:
    has_image_flag: bool
    image_path: str
    image_path_exists: bool
    visual_asset_count: int
    existing_visual_asset_count: int
    missing_visual_asset_count: int
    visual_asset_paths: tuple[str, ...]

    @property
    def has_any_visual_reference(self) -> bool:
        return bool(self.has_image_flag or self.image_path or self.visual_asset_count)

    @property
    def has_usable_visual(self) -> bool:
        return self.image_path_exists or self.existing_visual_asset_count > 0

    @property
    def status(self) -> str:
        if self.has_usable_visual:
            return "covered"
        if self.missing_visual_asset_count or self.image_path:
            return "file_missing"
        if self.has_image_flag or self.visual_asset_count:
            return "metadata_only"
        return "needs_mapping"


@dataclass(frozen=True)
class Candidate:
    question_id: int
    source_exam: str
    source_number: str
    question_type: str
    topic_slug: str
    topic_title: str
    section_refs: str
    category: str
    confidence: str
    asset_status: str
    visual_asset_count: int
    missing_visual_asset_count: int
    reason_text: str
    prompt_preview: str


PATTERNS = (
    # Screenshot / UI references.
    VisualPattern("screenshot/UI", "high", "екранна снимка", "explicit screen capture reference"),
    VisualPattern("screenshot/UI", "high", "снимка на екрана", "explicit screen capture reference"),
    VisualPattern("screenshot/UI", "high", "показания прозорец", "shown application window"),
    VisualPattern("screenshot/UI", "high", "показаният прозорец", "shown application window"),
    VisualPattern("screenshot/UI", "high", "показан прозорец", "shown application window"),
    VisualPattern("screenshot/UI", "high", "екрана по-долу", "shown screen reference"),
    VisualPattern("screenshot/UI", "high", "показания екран", "shown screen reference"),
    VisualPattern("screenshot/UI", "high", "показаният екран", "shown screen reference"),
    VisualPattern("screenshot/UI", "medium", "показаното меню", "shown menu reference"),
    VisualPattern("screenshot/UI", "medium", "показаната лента", "shown toolbar/ribbon reference"),

    # Code that is likely supplied visually rather than as selectable text.
    VisualPattern("code image", "high", "показаният фрагмент от код", "shown code fragment"),
    VisualPattern("code image", "high", "показания фрагмент от код", "shown code fragment"),
    VisualPattern("code image", "high", "следния фрагмент от код", "shown code fragment"),
    VisualPattern("code image", "high", "дадения фрагмент от код", "shown code fragment"),
    VisualPattern("code image", "high", "показания програмен код", "shown program code"),
    VisualPattern("code image", "medium", "фрагмент от програма", "program fragment reference"),

    # Tables, charts, and spreadsheet fragments.
    VisualPattern("chart/table", "high", "таблицата по-долу", "table shown outside text flow"),
    VisualPattern("chart/table", "high", "показаната таблица", "shown table"),
    VisualPattern("chart/table", "high", "дадената таблица", "given table"),
    VisualPattern("chart/table", "high", "следната таблица", "shown table"),
    VisualPattern("chart/table", "high", "даден е фрагмент от електронна таблица", "spreadsheet fragment"),
    VisualPattern("chart/table", "high", "показания фрагмент от електронна таблица", "spreadsheet fragment"),
    VisualPattern("chart/table", "high", "кръгова диаграма", "chart reference"),
    VisualPattern("chart/table", "high", "стълбовидна диаграма", "chart reference"),
    VisualPattern("chart/table", "high", "линейна диаграма", "chart reference"),
    VisualPattern("chart/table", "medium", "графиката", "chart/graph reference"),
    VisualPattern("chart/table", "medium", "разгледайте графиката", "chart/graph reference"),

    # Diagrams and figures.
    VisualPattern("diagram", "high", "коя от фигурите", "visual answer choices"),
    VisualPattern("diagram", "high", "показаната схема", "shown scheme/diagram"),
    VisualPattern("diagram", "high", "показана схема", "shown scheme/diagram"),
    VisualPattern("diagram", "high", "на схемата", "scheme/diagram reference"),
    VisualPattern("diagram", "high", "следната схема", "scheme/diagram reference"),
    VisualPattern("diagram", "high", "мрежова схема", "network diagram reference"),
    VisualPattern("diagram", "high", "фигурата", "figure reference"),
    VisualPattern("diagram", "high", "на фигурата", "figure reference"),
    VisualPattern("diagram", "medium", "диаграмата", "diagram reference"),
    VisualPattern("diagram", "medium", "показана диаграма", "shown diagram"),
    VisualPattern("diagram", "medium", "показаната диаграма", "shown diagram"),

    # Generic visual wording where the exact visual type is unclear.
    VisualPattern("unknown visual dependency", "high", "изображението", "image reference"),
    VisualPattern("unknown visual dependency", "high", "на изображението", "image reference"),
    VisualPattern("unknown visual dependency", "high", "даденото изображение", "given image"),
    VisualPattern("unknown visual dependency", "high", "следното изображение", "shown image"),
    VisualPattern("unknown visual dependency", "high", "показаното изображение", "shown image"),
    VisualPattern("unknown visual dependency", "medium", "картинката", "picture reference"),
    VisualPattern("unknown visual dependency", "medium", "снимката", "photo/image reference"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit likely image-dependent questions without writing to the DB."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--source", help="Exact source_exam filter.")
    parser.add_argument("--source-like", help="SQLite LIKE filter for source_exam.")
    parser.add_argument("--missing-only", action="store_true", help="Show only candidates without usable visuals.")
    parser.add_argument("--limit", type=int, help="Limit printed candidate rows.")
    parser.add_argument("--format", choices=("text", "csv"), default="text")
    parser.add_argument(
        "--export-markdown",
        nargs="?",
        const=DEFAULT_MARKDOWN_REVIEW_PATH,
        type=Path,
        help=(
            "Write a manual review Markdown file. Defaults to "
            f"{DEFAULT_MARKDOWN_REVIEW_PATH} when no path is provided."
        ),
    )
    return parser.parse_args()


def readonly_uri(db_path: Path) -> str:
    return f"file:{quote(str(db_path), safe='/')}?mode=ro"


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def path_exists(project_root: Path, value: object) -> bool:
    raw = str(value or "").strip()
    if not raw or raw in {"-", "—"}:
        return False
    path = Path(raw)
    if path.is_absolute():
        return path.exists()
    return (project_root / path).exists()


def clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def preview_text(value: object, limit: int = 120) -> str:
    text = clean_text(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def fetch_questions(conn: sqlite3.Connection, args: argparse.Namespace) -> list[sqlite3.Row]:
    where = []
    params: list[object] = []
    if args.source:
        where.append("q.source_exam = ?")
        params.append(args.source)
    if args.source_like:
        where.append("q.source_exam LIKE ?")
        params.append(args.source_like)

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    return conn.execute(
        f"""
        SELECT
            q.id,
            q.source_exam,
            q.source_number,
            q.question_type,
            q.prompt,
            q.has_image,
            q.image_path,
            ct.topic_slug,
            ct.title_bg AS topic_title,
            (
                SELECT GROUP_CONCAT(
                    cs.class || '. клас: ' || cs.title_bg || ' (' || cs.section_slug || ')',
                    '; '
                )
                FROM topic_section_assignments tsa
                JOIN curriculum_sections cs ON cs.id = tsa.section_id
                WHERE tsa.topic_id = q.topic_id
            ) AS section_refs
        FROM questions q
        LEFT JOIN curriculum_topics ct ON ct.id = q.topic_id
        {where_sql}
        ORDER BY q.source_exam, COALESCE(q.source_number, 999999), q.id
        """,
        params,
    ).fetchall()


def visual_asset_rows(conn: sqlite3.Connection, question_id: int) -> list[sqlite3.Row]:
    if not table_exists(conn, "assets") or not table_exists(conn, "asset_links"):
        return []

    clauses = ["(al.owner_type IN ('question', 'questions') AND al.owner_id = ?)"]
    params: list[object] = [question_id]
    if table_exists(conn, "exam_task_questions"):
        clauses.append(
            """
            (
                al.owner_type = 'exam_task'
                AND al.owner_id IN (
                    SELECT task_id
                    FROM exam_task_questions
                    WHERE question_id = ?
                )
            )
            """
        )
        params.append(question_id)

    owner_filter = " OR ".join(clauses)
    asset_types = sorted(VISUAL_ASSET_TYPES)
    asset_type_placeholders = ",".join("?" for _ in asset_types)
    return conn.execute(
        f"""
        SELECT DISTINCT a.id, a.asset_type, a.local_path
        FROM asset_links al
        JOIN assets a ON a.id = al.asset_id
        WHERE ({owner_filter})
          AND a.asset_type IN ({asset_type_placeholders})
        ORDER BY a.id
        """,
        (*params, *asset_types),
    ).fetchall()


def inspect_asset_coverage(
    conn: sqlite3.Connection,
    project_root: Path,
    question: sqlite3.Row,
) -> AssetCoverage:
    image_path = str(question["image_path"] or "").strip()
    asset_rows = visual_asset_rows(conn, int(question["id"]))
    existing_assets = [row for row in asset_rows if path_exists(project_root, row["local_path"])]
    return AssetCoverage(
        has_image_flag=bool(int(question["has_image"] or 0)),
        image_path=image_path,
        image_path_exists=path_exists(project_root, image_path),
        visual_asset_count=len(asset_rows),
        existing_visual_asset_count=len(existing_assets),
        missing_visual_asset_count=len(asset_rows) - len(existing_assets),
        visual_asset_paths=tuple(str(row["local_path"] or "") for row in asset_rows),
    )


def find_prompt_matches(prompt: object) -> list[PatternMatch]:
    text = clean_text(prompt).lower()
    matches = []
    for pattern in PATTERNS:
        if pattern.phrase in text:
            matches.append(
                PatternMatch(
                    category=pattern.category,
                    confidence=pattern.confidence,
                    phrase=pattern.phrase,
                    reason=pattern.reason,
                )
            )
    return matches


def best_confidence(matches: list[PatternMatch], coverage: AssetCoverage) -> str:
    if matches:
        return max(matches, key=lambda match: CONFIDENCE_RANK[match.confidence]).confidence
    if coverage.visual_asset_count or coverage.image_path:
        return "medium"
    return "low"


def best_category(matches: list[PatternMatch]) -> str:
    if not matches:
        return "unknown visual dependency"
    return min(
        matches,
        key=lambda match: (
            -CONFIDENCE_RANK[match.confidence],
            CATEGORY_ORDER[match.category],
        ),
    ).category


def reason_text(matches: list[PatternMatch], coverage: AssetCoverage) -> str:
    reasons = []
    seen = set()
    for match in sorted(
        matches,
        key=lambda item: (-CONFIDENCE_RANK[item.confidence], CATEGORY_ORDER[item.category], item.phrase),
    ):
        key = (match.category, match.phrase)
        if key in seen:
            continue
        seen.add(key)
        reasons.append(f"{match.category}: '{match.phrase}' ({match.reason}, {match.confidence})")
        if len(reasons) == 4:
            break

    if coverage.has_any_visual_reference and not matches:
        reasons.append("existing image metadata or visual asset link")
    elif coverage.has_any_visual_reference:
        reasons.append("existing image metadata or visual asset link also present")
    return "; ".join(reasons)


def build_candidates(
    conn: sqlite3.Connection,
    project_root: Path,
    questions: list[sqlite3.Row],
) -> list[Candidate]:
    candidates = []
    for question in questions:
        matches = find_prompt_matches(question["prompt"])
        coverage = inspect_asset_coverage(conn, project_root, question)
        if not matches and not coverage.has_any_visual_reference:
            continue

        candidates.append(
            Candidate(
                question_id=int(question["id"]),
                source_exam=str(question["source_exam"] or ""),
                source_number="" if question["source_number"] is None else str(question["source_number"]),
                question_type=str(question["question_type"] or ""),
                topic_slug=str(question["topic_slug"] or ""),
                topic_title=str(question["topic_title"] or ""),
                section_refs=str(question["section_refs"] or ""),
                category=best_category(matches),
                confidence=best_confidence(matches, coverage),
                asset_status=coverage.status,
                visual_asset_count=coverage.visual_asset_count,
                missing_visual_asset_count=coverage.missing_visual_asset_count,
                reason_text=reason_text(matches, coverage),
                prompt_preview=preview_text(question["prompt"]),
            )
        )
    return candidates


def apply_output_filters(candidates: list[Candidate], args: argparse.Namespace) -> list[Candidate]:
    rows = candidates
    if args.missing_only:
        rows = [row for row in rows if row.asset_status != "covered"]
    if args.limit is not None:
        rows = rows[: max(args.limit, 0)]
    return rows


def print_table(rows: list[dict[str, object]], columns: list[str]) -> None:
    if not rows:
        print("(none)")
        return
    widths = {
        column: max(len(column), *(len(str(row.get(column, ""))) for row in rows))
        for column in columns
    }
    print(" | ".join(column.ljust(widths[column]) for column in columns))
    print("-+-".join("-" * widths[column] for column in columns))
    for row in rows:
        print(" | ".join(str(row.get(column, "")).ljust(widths[column]) for column in columns))


def source_breakdown(
    questions: list[sqlite3.Row],
    candidates: list[Candidate],
) -> list[dict[str, object]]:
    inspected = Counter(str(row["source_exam"] or "") for row in questions)
    by_source: dict[str, Counter] = defaultdict(Counter)
    for candidate in candidates:
        source = candidate.source_exam
        by_source[source]["candidates"] += 1
        by_source[source][candidate.asset_status] += 1

    rows = []
    for source in sorted(inspected):
        counts = by_source[source]
        rows.append(
            {
                "source": source or "(no source)",
                "inspected": inspected[source],
                "candidates": counts["candidates"],
                "covered": counts["covered"],
                "needs_mapping": counts["needs_mapping"],
                "file_missing": counts["file_missing"],
                "metadata_only": counts["metadata_only"],
            }
        )
    return rows


def print_text_report(
    *,
    db_path: Path,
    questions: list[sqlite3.Row],
    candidates: list[Candidate],
    visible_candidates: list[Candidate],
) -> None:
    category_counts = Counter(row.category for row in candidates)
    status_counts = Counter(row.asset_status for row in candidates)
    confidence_counts = Counter(row.confidence for row in candidates)

    print("Question image dependency audit")
    print("===============================")
    print(f"DB: {db_path}")
    print(f"questions inspected: {len(questions)}")
    print(f"likely image-dependent candidates: {len(candidates)}")
    print(f"covered by existing usable visual: {status_counts['covered']}")
    print(
        "needs mapping or file fix: "
        f"{status_counts['needs_mapping'] + status_counts['file_missing'] + status_counts['metadata_only']}"
    )
    print("audit_exit=0")
    print()

    print("By category")
    print("-----------")
    print_table(
        [{"category": category, "count": count} for category, count in sorted(category_counts.items())],
        ["category", "count"],
    )
    print()

    print("By confidence")
    print("-------------")
    print_table(
        [{"confidence": confidence, "count": confidence_counts[confidence]} for confidence in ("high", "medium", "low")],
        ["confidence", "count"],
    )
    print()

    print("Per-source breakdown")
    print("--------------------")
    print_table(
        source_breakdown(questions, candidates),
        ["source", "inspected", "candidates", "covered", "needs_mapping", "file_missing", "metadata_only"],
    )
    print()

    print("Candidate question ids by source")
    print("--------------------------------")
    ids_by_source: dict[str, list[str]] = defaultdict(list)
    for candidate in candidates:
        label = candidate.source_exam or "(no source)"
        suffix = f"#{candidate.source_number}" if candidate.source_number else f"id:{candidate.question_id}"
        ids_by_source[label].append(suffix)
    if not ids_by_source:
        print("(none)")
    else:
        for source in sorted(ids_by_source):
            print(f"{source}: {', '.join(ids_by_source[source])}")
    print()

    print("Candidates")
    print("----------")
    print_table(
        [
            {
                "id": row.question_id,
                "source": row.source_exam,
                "no": row.source_number,
                "type": row.question_type,
                "topic": row.topic_slug,
                "confidence": row.confidence,
                "category": row.category,
                "asset_status": row.asset_status,
                "reason": row.reason_text,
                "prompt": row.prompt_preview,
            }
            for row in visible_candidates
        ],
        ["id", "source", "no", "type", "confidence", "category", "asset_status", "reason", "prompt"],
    )
    print()
    print("Manual review notes")
    print("-------------------")
    print("- Treat this as a review queue, not an import plan.")
    print("- Confirm each candidate against the official/source material before adding assets.")
    print("- Add files and DB mappings only in later explicit asset/data PRs.")


def print_csv_report(candidates: list[Candidate]) -> None:
    fieldnames = [
        "question_id",
        "source_exam",
        "source_number",
        "question_type",
        "topic_slug",
        "topic_title",
        "section_refs",
        "confidence",
        "category",
        "asset_status",
        "visual_asset_count",
        "missing_visual_asset_count",
        "reason_text",
        "prompt_preview",
    ]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    for row in candidates:
        writer.writerow({field: getattr(row, field) for field in fieldnames})


def markdown_value(value: str) -> str:
    text = clean_text(value)
    return text if text else "—"


def markdown_prompt(value: str) -> str:
    text = clean_text(value)
    return text.replace("\n", " ")


def validate_markdown_review_path(path: Path) -> None:
    normalised = path.as_posix()
    if "vault/Generated/Quizzes" in normalised:
        raise ValueError("refusing to write image-dependency review into vault/Generated/Quizzes")
    if path.suffix.lower() != ".md":
        raise ValueError("markdown review path must end with .md")


def markdown_review_content(
    *,
    db_path: Path,
    questions: list[sqlite3.Row],
    candidates: list[Candidate],
    visible_candidates: list[Candidate],
) -> str:
    status_counts = Counter(row.asset_status for row in candidates)
    category_counts = Counter(row.category for row in candidates)
    lines = [
        "# Question Image Dependency Review",
        "",
        "Manual review worksheet generated from `src/audit_question_image_dependencies.py`.",
        "This file is for review only. Do not treat it as an import manifest.",
        "",
        "## Summary",
        "",
        f"- DB: `{db_path}`",
        f"- Questions inspected: {len(questions)}",
        f"- Likely image-dependent candidates: {len(candidates)}",
        f"- Covered by existing usable visual: {status_counts['covered']}",
        "- Needs mapping or file fix: "
        f"{status_counts['needs_mapping'] + status_counts['file_missing'] + status_counts['metadata_only']}",
        "",
        "## Category Counts",
        "",
        "| Category | Count |",
        "|---|---:|",
    ]
    for category, count in sorted(category_counts.items()):
        lines.append(f"| {category} | {count} |")

    lines.extend(
        [
            "",
            "## Instructions",
            "",
            "For each candidate, fill the manual fields before any asset import or DB update:",
            "",
            "- `needs_image`: `yes`, `no`, or `unclear`.",
            "- `asset_source`: official PDF page/crop, existing resource ZIP path, or other reviewed source.",
            "- `expected_asset_filename`: proposed repo-relative file under `data/assets/exams/<source_slug>/`.",
            "- `notes`: reviewer context, uncertainty, or reason for rejection.",
            "",
            "## Candidates",
            "",
        ]
    )

    for index, row in enumerate(visible_candidates, start=1):
        source_label = row.source_exam or "no-source"
        number_label = f"#{row.source_number}" if row.source_number else f"id:{row.question_id}"
        lines.extend(
            [
                f"### {index}. Q{row.question_id} — {source_label} {number_label}",
                "",
                f"- question_id: `{row.question_id}`",
                f"- source/exam: `{markdown_value(row.source_exam)}`",
                f"- source_number: `{markdown_value(row.source_number)}`",
                f"- question_type: `{markdown_value(row.question_type)}`",
                f"- topic: `{markdown_value(row.topic_slug)}` — {markdown_value(row.topic_title)}",
                f"- section(s): {markdown_value(row.section_refs)}",
                f"- detected_category: `{row.category}`",
                f"- confidence: `{row.confidence}`",
                f"- asset_status: `{row.asset_status}`",
                f"- visual_asset_count: {row.visual_asset_count}",
                f"- missing_visual_asset_count: {row.missing_visual_asset_count}",
                f"- reason: {markdown_value(row.reason_text)}",
                "",
                "Question text snippet:",
                "",
                f"> {markdown_prompt(row.prompt_preview)}",
                "",
                "Manual fields:",
                "",
                "- needs_image: ",
                "- asset_source: ",
                "- expected_asset_filename: ",
                "- notes: ",
                "",
                "---",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_markdown_review(
    path: Path,
    *,
    db_path: Path,
    questions: list[sqlite3.Row],
    candidates: list[Candidate],
    visible_candidates: list[Candidate],
) -> None:
    validate_markdown_review_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        markdown_review_content(
            db_path=db_path,
            questions=questions,
            candidates=candidates,
            visible_candidates=visible_candidates,
        ),
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    db_path = args.db
    if not db_path.exists():
        print(f"error: DB not found: {db_path}", file=sys.stderr)
        return 1

    project_root = Path(__file__).resolve().parents[1]
    try:
        conn = sqlite3.connect(readonly_uri(db_path), uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        print(f"error: could not open DB read-only: {exc}", file=sys.stderr)
        return 1

    try:
        questions = fetch_questions(conn, args)
        candidates = build_candidates(conn, project_root, questions)
        visible_candidates = apply_output_filters(candidates, args)
    except (sqlite3.Error, ValueError) as exc:
        print(f"error: audit query failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if args.export_markdown is not None:
        try:
            write_markdown_review(
                args.export_markdown,
                db_path=db_path,
                questions=questions,
                candidates=candidates,
                visible_candidates=visible_candidates,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"wrote markdown review: {args.export_markdown}", file=sys.stderr)

    if args.format == "csv":
        print_csv_report(visible_candidates)
    else:
        print_text_report(
            db_path=db_path,
            questions=questions,
            candidates=candidates,
            visible_candidates=visible_candidates,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

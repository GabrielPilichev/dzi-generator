"""
Audit topics that have zero approved questions.

A topic is "approved-empty" when no questions tagged to it pass the production
filter (is_ai_generated = 0 OR quality_score >= 1.0). These are real curriculum
slots that show up in the UI but contribute nothing to test pools or topic
browsing pages — easy to lose track of.

Output: a markdown report grouped by area, with each topic's primary section,
class, note path, and counts of any-quality and AI-pending questions.

This script is read-only. It does not modify the DB or the vault.

Usage:
    python3 src/audit_stub_topics.py              # print to stdout
    python3 src/audit_stub_topics.py --md PATH    # also write markdown to PATH
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path

DEFAULT_DB = Path("data/questions.db")
DEFAULT_REPORT = Path("vault/Generated/Audits/stub-topics.md")


def collect_stub_topics(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT
            ct.id,
            ct.topic_slug,
            ct.title_bg,
            ct.created_at,
            ct.note_path,
            ca.title_bg AS area_title,
            cs.class AS primary_class,
            cs.section_slug AS primary_section,
            COUNT(q_any.id) AS n_any,
            SUM(CASE
                WHEN q_any.is_ai_generated = 1
                 AND (q_any.quality_score IS NULL OR q_any.quality_score < 1.0)
                THEN 1 ELSE 0 END) AS n_ai_pending
        FROM curriculum_topics ct
        LEFT JOIN curriculum_areas ca ON ca.id = ct.area_id
        LEFT JOIN curriculum_sections cs ON cs.id = ct.section_id
        LEFT JOIN questions q_any ON q_any.topic_id = ct.id
        LEFT JOIN questions q_ok ON q_ok.topic_id = ct.id
            AND (q_ok.is_ai_generated = 0 OR q_ok.quality_score >= 1.0)
        GROUP BY ct.id
        HAVING SUM(CASE WHEN q_ok.id IS NOT NULL THEN 1 ELSE 0 END) = 0
        ORDER BY ca.title_bg, cs.class, ct.topic_slug
        """
    ).fetchall()

    result = []
    for r in rows:
        sections = conn.execute(
            """
            SELECT cs.class, cs.section_slug
            FROM topic_section_assignments tsa
            JOIN curriculum_sections cs ON cs.id = tsa.section_id
            WHERE tsa.topic_id = ?
            ORDER BY cs.class, cs.section_slug
            """,
            (r["id"],),
        ).fetchall()
        result.append({
            "id": r["id"],
            "slug": r["topic_slug"],
            "title": r["title_bg"],
            "area": r["area_title"] or "(no area)",
            "primary_class": r["primary_class"],
            "primary_section": r["primary_section"],
            "note_path": r["note_path"],
            "created_at": r["created_at"],
            "n_any": r["n_any"] or 0,
            "n_ai_pending": r["n_ai_pending"] or 0,
            "sections": [(s["class"], s["section_slug"]) for s in sections],
        })

    conn.close()
    return result


def render_markdown(topics: list[dict]) -> str:
    if not topics:
        return "# Stub topics audit\n\nNo topics with zero approved questions. ✅\n"

    by_area: dict[str, list[dict]] = defaultdict(list)
    for t in topics:
        by_area[t["area"]].append(t)

    lines = [
        "---",
        "title: Stub topics audit",
        "type: audit",
        "tags: [audit, stub-topics]",
        "---",
        "",
        "# Stub topics audit",
        "",
        f"Topics with zero approved questions: **{len(topics)}**.",
        "",
        "An approved question satisfies `is_ai_generated = 0 OR quality_score >= 1.0`.",
        "Topics here render in the UI but contribute nothing to quiz pools.",
        "",
    ]

    for area in sorted(by_area):
        lines.append(f"## {area}")
        lines.append("")
        lines.append("| Class | Slug | Title | AI-pending | Sections used |")
        lines.append("|-------|------|-------|-----------:|---------------|")
        for t in sorted(by_area[area], key=lambda x: (x["primary_class"] or 0, x["slug"])):
            secs = ", ".join(f"{c}кл/{s}" for c, s in t["sections"]) or "—"
            lines.append(
                f"| {t['primary_class']} | `{t['slug']}` | {t['title']} | "
                f"{t['n_ai_pending']} | {secs} |"
            )
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- A non-zero `AI-pending` count means there are AI-generated questions "
        "waiting for review (`is_ai_generated = 1` and `quality_score < 1.0`). "
        "Run `src/review_export.py` to surface them, then `src/review_import.py` "
        "after manual approval."
    )
    lines.append(
        "- Topics with `AI-pending = 0` need either (a) human-authored questions, "
        "(b) an AI generation pass via `src/generate_questions.py`, or (c) explicit "
        "documentation that the topic is intentionally questionless."
    )
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument(
        "--md",
        type=Path,
        nargs="?",
        const=DEFAULT_REPORT,
        help=f"Also write markdown report (default path: {DEFAULT_REPORT})",
    )
    args = p.parse_args()

    if not args.db.exists():
        raise SystemExit(f"DB not found: {args.db}")

    topics = collect_stub_topics(args.db)

    print(f"Found {len(topics)} topic(s) with zero approved questions.\n")
    for t in topics:
        sections = ", ".join(f"{c}кл/{s}" for c, s in t["sections"]) or "—"
        ai_note = f" [AI-pending: {t['n_ai_pending']}]" if t["n_ai_pending"] else ""
        print(f"  {t['primary_class']}кл  {t['slug']:32s}  {t['title']}{ai_note}")
        print(f"             area: {t['area']}")
        print(f"             sections: {sections}")
        print()

    if args.md:
        report = render_markdown(topics)
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(report, encoding="utf-8")
        print(f"Markdown report written to {args.md}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def safe_list_from_group_concat(value: str | None) -> list[int]:
    if not value:
        return []
    out = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            pass
    return sorted(set(out))


def frontmatter(data: dict) -> str:
    lines = ["---"]
    for key, value in data.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                if isinstance(item, str):
                    lines.append(f"  - {json.dumps(item, ensure_ascii=False)}")
                else:
                    lines.append(f"  - {item}")
        elif value is None:
            lines.append(f"{key}:")
        elif isinstance(value, str):
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Generate Obsidian stub notes for curriculum topics missing notes.")
    p.add_argument("--db", type=Path, default=Path("data/questions.db"))
    p.add_argument("--vault", type=Path, default=Path("vault"))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
            ct.id,
            ct.topic_slug,
            ct.title_bg,
            ct.description,
            ct.note_path,
            ct.difficulty,
            ct.bloom_level,
            ca.area_id AS area_slug,
            ca.title_bg AS area_title,
            cs.section_slug,
            cs.title_bg AS section_title,
            GROUP_CONCAT(tc.class) AS classes
        FROM curriculum_topics ct
        LEFT JOIN curriculum_areas ca ON ca.id = ct.area_id
        LEFT JOIN curriculum_sections cs ON cs.id = ct.section_id
        LEFT JOIN topic_classes tc ON tc.topic_id = ct.id
        GROUP BY ct.id
        ORDER BY ct.topic_slug
    """).fetchall()

    created = 0
    existing = 0
    skipped_no_path = 0

    for row in rows:
        note_path = row["note_path"] or f"Topics/{row['topic_slug']}.md"
        if not note_path:
            skipped_no_path += 1
            continue

        full_path = args.vault / note_path

        if full_path.exists():
            existing += 1
            continue

        classes = safe_list_from_group_concat(row["classes"])

        fm = {
            "title": row["title_bg"],
            "type": "topic",
            "topic_slug": row["topic_slug"],
            "classes": classes,
            "area": row["area_slug"],
            "section": row["section_slug"],
            "difficulty": row["difficulty"],
            "bloom_level": row["bloom_level"],
            "status": "stub",
            "source": "db-generated",
            "tags": ["topic", "stub"],
        }

        body = f"""{frontmatter(fm)}

# {row["title_bg"]}

> Stub note generated from `curriculum_topics`.

## Описание

{row["description"] or "TODO: Добави описание."}

## Учебна секция

- Област: {row["area_title"] or row["area_slug"] or "—"}
- Секция: {row["section_title"] or row["section_slug"] or "—"}
- Класове: {", ".join(map(str, classes)) if classes else "—"}

## Ключови понятия

TODO

## Примери

TODO

## Типични грешки

TODO

## Свързани теми

TODO
"""

        print(f"{'WOULD CREATE' if args.dry_run else 'CREATE'} {full_path}")

        if not args.dry_run:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(body, encoding="utf-8")
            created += 1

    conn.close()

    print()
    print(f"Existing notes: {existing}")
    print(f"Created notes: {created}")
    print(f"Skipped no path: {skipped_no_path}")
    if args.dry_run:
        print("(dry-run: no files written)")


if __name__ == "__main__":
    main()

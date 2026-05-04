import sqlite3
from datetime import date
from pathlib import Path

DB_PATH = Path("data/questions.db")
OUT_MD = Path("vault/Generated/Audits/empty-sections-fill-plan.md")
OUT_TXT = Path("data/audits/empty_sections_fill_plan.txt")

APPROVED = "(q.is_ai_generated = 0 OR q.quality_score >= 1.0)"

QUERY = f"""
WITH section_counts AS (
  SELECT
    cs.id AS section_id,
    cs.class,
    cs.section_slug,
    cs.title_bg,
    cs.section_type,
    cs.display_order,
    COUNT(DISTINCT tsa.topic_id) AS assigned_topics,
    COUNT(DISTINCT q.id) AS approved_questions
  FROM curriculum_sections cs
  LEFT JOIN topic_section_assignments tsa
    ON tsa.section_id = cs.id
  LEFT JOIN questions q
    ON q.topic_id = tsa.topic_id
   AND {APPROVED}
  WHERE cs.class BETWEEN 8 AND 12
  GROUP BY cs.id
)
SELECT *
FROM section_counts
WHERE approved_questions = 0
ORDER BY class, display_order, section_slug;
"""

def recommendation(row):
    stype = row["section_type"]
    slug = row["section_slug"]

    if stype in {"review", "summary"}:
        return "структурен раздел — може да се попълни по-късно с обобщаващи/преговорни въпроси"
    if stype == "project":
        return "структурен проектен раздел — попълва се с практически задачи, не е спешно за тестови въпроси"
    if stype == "unclassified":
        return "служебен раздел — не е задължително да има въпроси"
    if slug == "grade9-web-publishing":
        return "реален content gap — провери дали има съществуващи теми за HTML/CSS/web, които трябва да се свържат, или добави нови въпроси"
    if slug == "grade11-m2-multimedia-product":
        return "реален content gap — добави/свържи теми за мултимедиен продукт, интеграция, сценарий, ресурси, авторски права"
    return "провери ръчно — вероятно липсва тема или въпроси"

def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(QUERY).fetchall()
    con.close()

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)

    md = []
    txt = []

    md.append("---")
    md.append('title: "План за попълване на празни раздели"')
    md.append("type: empty_sections_fill_plan")
    md.append(f"date: {date.today().isoformat()}")
    md.append("tags: [audit, curriculum, mapping]")
    md.append("---")
    md.append("")
    md.append("# План за попълване на празни раздели")
    md.append("")
    md.append("Тази справка показва раздели без одобрени въпроси и предлага какво да се направи.")
    md.append("")
    md.append("| Клас | Раздел | Тип | Теми | Одобрени въпроси | Препоръка |")
    md.append("|---:|---|---|---:|---:|---|")

    txt.append("Empty sections fill plan")
    txt.append("")

    for row in rows:
        rec = recommendation(row)
        md.append(
            f"| {row['class']} | `{row['section_slug']}` — {row['title_bg']} | "
            f"{row['section_type']} | {row['assigned_topics']} | {row['approved_questions']} | {rec} |"
        )
        txt.append(
            f"class={row['class']} slug={row['section_slug']} type={row['section_type']} "
            f"topics={row['assigned_topics']} questions={row['approved_questions']} recommendation={rec}"
        )

    OUT_MD.write_text("\n".join(md) + "\n", encoding="utf-8")
    OUT_TXT.write_text("\n".join(txt) + "\n", encoding="utf-8")

    print(f"Wrote {OUT_MD}")
    print(f"Wrote {OUT_TXT}")
    print(f"Empty sections: {len(rows)}")

if __name__ == "__main__":
    main()

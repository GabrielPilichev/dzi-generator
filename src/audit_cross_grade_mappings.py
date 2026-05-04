import csv
import sqlite3
from datetime import date
from pathlib import Path

DB_PATH = Path("data/questions.db")
OUT_CSV = Path("data/audits/cross_grade_mapping_violations.csv")
OUT_MD = Path("vault/Generated/Audits/cross-grade-mapping-violations.md")

QUERY = """
SELECT
  target_cs.class AS target_class,
  target_cs.section_slug AS target_section,
  target_cs.title_bg AS target_title,
  ct.id AS topic_id,
  ct.topic_slug,
  ct.title_bg AS topic_title,
  source_cs.class AS topic_primary_class,
  source_cs.section_slug AS topic_primary_section,
  source_cs.title_bg AS topic_primary_section_title,
  COUNT(DISTINCT q.id) AS approved_questions
FROM topic_section_assignments tsa
JOIN curriculum_sections target_cs
  ON target_cs.id = tsa.section_id
JOIN curriculum_topics ct
  ON ct.id = tsa.topic_id
JOIN curriculum_sections source_cs
  ON source_cs.id = ct.section_id
LEFT JOIN questions q
  ON q.topic_id = ct.id
 AND (q.is_ai_generated = 0 OR q.quality_score >= 1.0)
WHERE source_cs.class > target_cs.class
GROUP BY tsa.topic_id, tsa.section_id
ORDER BY
  target_cs.class,
  target_cs.section_slug,
  source_cs.class,
  ct.topic_slug;
"""

FIELDS = [
    "target_class",
    "target_section",
    "target_title",
    "topic_id",
    "topic_slug",
    "topic_title",
    "topic_primary_class",
    "topic_primary_section",
    "topic_primary_section_title",
    "approved_questions",
]

def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(QUERY).fetchall()
    con.close()

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)

    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in FIELDS})

    md = []
    md.append("---")
    md.append('title: "Cross-grade mapping violations"')
    md.append("type: cross_grade_mapping_audit")
    md.append(f"date: {date.today().isoformat()}")
    md.append("tags: [audit, curriculum, mapping]")
    md.append("---")
    md.append("")
    md.append("# Cross-grade mapping violations")
    md.append("")
    md.append("Правило:")
    md.append("")
    md.append("> Може да се използва материал от по-нисък клас към по-висок, но не и от по-висок клас към по-нисък.")
    md.append("")
    md.append(f"- Нарушения: **{len(rows)}**")
    md.append(f"- CSV: `{OUT_CSV}`")
    md.append("")

    if rows:
        md.append("| Target class | Target section | Topic | Topic primary class | Primary section | Questions |")
        md.append("|---:|---|---|---:|---|---:|")
        for row in rows:
            md.append(
                f"| {row['target_class']} | `{row['target_section']}` — {row['target_title']} "
                f"| `{row['topic_slug']}` — {row['topic_title']} "
                f"| {row['topic_primary_class']} "
                f"| `{row['topic_primary_section']}` — {row['topic_primary_section_title']} "
                f"| {row['approved_questions']} |"
            )
    else:
        md.append("Няма нарушения.")

    OUT_MD.write_text("\\n".join(md) + "\\n", encoding="utf-8")

    print(f"Wrote {OUT_CSV}")
    print(f"Wrote {OUT_MD}")
    print(f"Violations: {len(rows)}")
    for row in rows:
        print(
            f"{row['target_class']} {row['target_section']} <- "
            f"{row['topic_primary_class']} {row['topic_slug']} "
            f"({row['approved_questions']} questions)"
        )

if __name__ == "__main__":
    main()

import sqlite3
from pathlib import Path
from datetime import date

DB_PATH = Path("data/questions.db")
OUT_MD = Path("vault/Generated/DZI-Training/audits/training-pool-by-class.md")
OUT_TXT = Path("data/dzi_training/training_pool_by_class.txt")

APPROVED_FILTER = "(q.is_ai_generated = 0 OR q.quality_score >= 1.0)"

QUERY = f"""
WITH question_classes AS (
    SELECT DISTINCT
        q.id AS question_id,
        q.question_type,
        cs.class AS class
    FROM questions q
    JOIN question_topic_assignments qta
      ON qta.question_id = q.id
     AND qta.is_active = 1
    JOIN topic_section_assignments tsa
      ON tsa.topic_id = qta.topic_id
    JOIN curriculum_sections cs
      ON cs.id = tsa.section_id
    WHERE {APPROVED_FILTER}
      AND cs.class IN (8, 9, 10, 11, 12)
)
SELECT
    class,
    COUNT(DISTINCT question_id) AS total_questions,
    COUNT(DISTINCT CASE WHEN question_type = 'multiple_choice' THEN question_id END) AS multiple_choice,
    COUNT(DISTINCT CASE WHEN question_type <> 'multiple_choice' OR question_type IS NULL THEN question_id END) AS other_types
FROM question_classes
GROUP BY class
ORDER BY class;
"""

DETAIL_QUERY = f"""
WITH question_classes AS (
    SELECT DISTINCT
        q.id AS question_id,
        q.question_type,
        cs.class AS class,
        cs.section_slug,
        cs.title_bg AS section_title
    FROM questions q
    JOIN question_topic_assignments qta
      ON qta.question_id = q.id
     AND qta.is_active = 1
    JOIN topic_section_assignments tsa
      ON tsa.topic_id = qta.topic_id
    JOIN curriculum_sections cs
      ON cs.id = tsa.section_id
    WHERE {APPROVED_FILTER}
      AND cs.class IN (8, 9, 10, 11, 12)
)
SELECT
    class,
    section_slug,
    section_title,
    COUNT(DISTINCT question_id) AS question_count,
    COUNT(DISTINCT CASE WHEN question_type = 'multiple_choice' THEN question_id END) AS multiple_choice
FROM question_classes
GROUP BY class, section_slug, section_title
ORDER BY class, section_slug;
"""

def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    rows = con.execute(QUERY).fetchall()
    details = con.execute(DETAIL_QUERY).fetchall()
    con.close()

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    lines.append("---")
    lines.append('title: "ДЗИ тренировъчен пул по класове"')
    lines.append("type: dzi_training_pool_audit")
    lines.append(f"date: {date.today().isoformat()}")
    lines.append("tags: [dzi, training, audit]")
    lines.append("---")
    lines.append("")
    lines.append("# ДЗИ тренировъчен пул по класове")
    lines.append("")
    lines.append("> Това е справка за наличните въпроси, които могат да се използват за тренировъчни комплекти. Това не означава официална ДЗИ релевантност.")
    lines.append("")
    lines.append("## Обобщение")
    lines.append("")
    lines.append("| Клас | Всички въпроси | Избираем отговор | Други типове |")
    lines.append("|---:|---:|---:|---:|")

    txt = []
    txt.append("DZI training pool by class")
    txt.append("")

    for row in rows:
        lines.append(
            f"| {row['class']} | {row['total_questions']} | {row['multiple_choice']} | {row['other_types']} |"
        )
        txt.append(
            f"class={row['class']} total={row['total_questions']} multiple_choice={row['multiple_choice']} other={row['other_types']}"
        )

    lines.append("")
    lines.append("## По раздели")
    lines.append("")

    current_class = None
    for row in details:
        if row["class"] != current_class:
            current_class = row["class"]
            lines.append(f"## {current_class}. клас")
            lines.append("")
            lines.append("| Раздел | Slug | Всички | Избираем отговор |")
            lines.append("|---|---|---:|---:|")

        lines.append(
            f"| {row['section_title']} | `{row['section_slug']}` | {row['question_count']} | {row['multiple_choice']} |"
        )

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    OUT_TXT.write_text("\n".join(txt) + "\n", encoding="utf-8")

    print(f"Wrote {OUT_MD}")
    print(f"Wrote {OUT_TXT}")
    print()
    for row in rows:
        print(
            f"class={row['class']} total={row['total_questions']} "
            f"multiple_choice={row['multiple_choice']} other={row['other_types']}"
        )

if __name__ == "__main__":
    main()

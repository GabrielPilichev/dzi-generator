import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path("data/questions.db")

APPROVED = "(q.is_ai_generated = 0 OR q.quality_score >= 1.0)"

EMPTY_SECTIONS_QUERY = f"""
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

def candidate_topics_for_section(con, section):
    cls = int(section["class"])
    stype = section["section_type"]

    if stype == "unclassified":
        return []

    if stype == "review":
        # 8th grade has no previous high-school grade in this DB, so use its own content.
        source_classes = [8] if cls == 8 else [cls - 1]
    elif stype in {"summary", "project"}:
        source_classes = [cls]
    else:
        return []

    placeholders = ",".join("?" for _ in source_classes)

    query = f"""
    SELECT DISTINCT
      ct.id AS topic_id,
      ct.topic_slug,
      ct.title_bg AS topic_title,
      src.class AS topic_class,
      src.section_slug AS topic_section,
      src.title_bg AS topic_section_title,
      COUNT(DISTINCT q.id) AS approved_questions
    FROM curriculum_topics ct
    JOIN curriculum_sections src
      ON src.id = ct.section_id
    JOIN questions q
      ON q.topic_id = ct.id
     AND {APPROVED}
    WHERE src.class IN ({placeholders})
      AND src.section_type = 'content'
      AND src.class <= ?
    GROUP BY ct.id
    HAVING approved_questions > 0
    ORDER BY src.class, src.display_order, ct.topic_slug;
    """

    return con.execute(query, (*source_classes, cls)).fetchall()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually insert mappings.")
    args = parser.parse_args()

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    empty_sections = con.execute(EMPTY_SECTIONS_QUERY).fetchall()

    planned = []
    skipped = []

    for section in empty_sections:
        candidates = candidate_topics_for_section(con, section)

        if not candidates:
            skipped.append(section)
            continue

        for topic in candidates:
            planned.append((section, topic))

    print(f"Empty sections found: {len(empty_sections)}")
    print(f"Planned mappings: {len(planned)}")
    print(f"Skipped sections: {len(skipped)}")
    print()

    print("=== Planned mappings ===")
    for section, topic in planned:
        print(
            f"{section['class']} | {section['section_slug']} ({section['section_type']}) "
            f"<- class {topic['topic_class']} | {topic['topic_slug']} "
            f"({topic['approved_questions']} questions)"
        )

    print()
    print("=== Skipped sections ===")
    for section in skipped:
        print(
            f"{section['class']} | {section['section_slug']} "
            f"({section['section_type']}) | {section['title_bg']}"
        )

    if args.apply:
        cur = con.cursor()
        cur.execute("BEGIN")
        for section, topic in planned:
            cur.execute(
                """
                INSERT OR IGNORE INTO topic_section_assignments (topic_id, section_id)
                VALUES (?, ?)
                """,
                (topic["topic_id"], section["section_id"]),
            )
        con.commit()
        print()
        print("Applied mappings.")
    else:
        print()
        print("Dry run only. Re-run with --apply to insert mappings.")

    con.close()

if __name__ == "__main__":
    main()

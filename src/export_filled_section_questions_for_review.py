import csv
import sqlite3
from datetime import date
from pathlib import Path

DB_PATH = Path("data/questions.db")
OUT_CSV = Path("data/audits/filled_section_questions_for_review.csv")
OUT_MD = Path("vault/Generated/Audits/filled-section-questions-for-review.md")

SECTIONS = [
    "grade9-web-publishing",
    "grade11-m2-multimedia-product",
]

APPROVED = "(q.is_ai_generated = 0 OR q.quality_score >= 1.0)"

def table_columns(con, table):
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}

def col_expr(cols, col, default="NULL"):
    if col in cols:
        return f"q.{col} AS {col}"
    return f"{default} AS {col}"

def clean(text):
    return " ".join((text or "").split())

def short(text, n=260):
    text = clean(text)
    return text if len(text) <= n else text[:n].rstrip() + "…"

def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    qcols = table_columns(con, "questions")
    source_expr = col_expr(qcols, "source", "''")
    difficulty_expr = col_expr(qcols, "difficulty", "''")
    quality_expr = col_expr(qcols, "quality_score", "NULL")
    ai_expr = col_expr(qcols, "is_ai_generated", "0")

    prompt_col = "prompt" if "prompt" in qcols else None
    if not prompt_col:
        raise SystemExit(f"No prompt column found in questions table. Columns: {sorted(qcols)}")

    query = f"""
    SELECT
      cs.class,
      cs.section_slug,
      cs.title_bg AS section_title,
      ct.topic_slug,
      ct.title_bg AS topic_title,
      q.id AS question_id,
      q.question_type,
      q.{prompt_col} AS prompt,
      {difficulty_expr},
      {source_expr},
      {quality_expr},
      {ai_expr}
    FROM curriculum_sections cs
    JOIN topic_section_assignments tsa
      ON tsa.section_id = cs.id
    JOIN curriculum_topics ct
      ON ct.id = tsa.topic_id
    JOIN questions q
      ON q.topic_id = ct.id
     AND {APPROVED}
    WHERE cs.section_slug IN ({",".join("?" for _ in SECTIONS)})
    ORDER BY cs.class, cs.section_slug, ct.topic_slug, q.id;
    """

    rows = con.execute(query, SECTIONS).fetchall()

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "class",
        "section_slug",
        "section_title",
        "topic_slug",
        "topic_title",
        "question_id",
        "question_type",
        "difficulty",
        "source",
        "quality_score",
        "is_ai_generated",
        "prompt",
        "correct_answer",
    ]

    options_query = """
    SELECT option_letter, option_text, is_correct
    FROM multiple_choice_options
    WHERE question_id = ?
    ORDER BY option_letter;
    """

    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            opts = con.execute(options_query, (row["question_id"],)).fetchall()
            correct = "; ".join(
                f"{o['option_letter']}. {clean(o['option_text'])}"
                for o in opts
                if o["is_correct"]
            )

            writer.writerow({
                "class": row["class"],
                "section_slug": row["section_slug"],
                "section_title": row["section_title"],
                "topic_slug": row["topic_slug"],
                "topic_title": row["topic_title"],
                "question_id": row["question_id"],
                "question_type": row["question_type"],
                "difficulty": row["difficulty"],
                "source": row["source"],
                "quality_score": row["quality_score"],
                "is_ai_generated": row["is_ai_generated"],
                "prompt": clean(row["prompt"]),
                "correct_answer": correct,
            })

    by_section_topic = {}
    for row in rows:
        key = (
            row["class"],
            row["section_slug"],
            row["section_title"],
            row["topic_slug"],
            row["topic_title"],
        )
        by_section_topic.setdefault(key, []).append(row)

    md = []
    md.append("---")
    md.append('title: "Въпроси за преглед след попълване на раздели"')
    md.append("type: filled_section_questions_review")
    md.append(f"date: {date.today().isoformat()}")
    md.append("tags: [audit, review, curriculum, mapping]")
    md.append("---")
    md.append("")
    md.append("# Въпроси за преглед след попълване на раздели")
    md.append("")
    md.append("Това са въпросите, които влизат в новопопълнените раздели след добавяне на topic-section mappings.")
    md.append("")
    md.append(f"- CSV: `{OUT_CSV}`")
    md.append(f"- Общо въпроси: **{len(rows)}**")
    md.append("")

    for key, qs in by_section_topic.items():
        cls, section_slug, section_title, topic_slug, topic_title = key
        md.append(f"## {cls}. клас — {section_title}")
        md.append("")
        md.append(f"- Section: `{section_slug}`")
        md.append(f"- Topic: `{topic_slug}` — {topic_title}")
        md.append(f"- Questions: **{len(qs)}**")
        md.append("")
        md.append("| ID | Тип | Въпрос | Верен отговор |")
        md.append("|---:|---|---|---|")

        for q in qs:
            opts = con.execute(options_query, (q["question_id"],)).fetchall()
            correct = "; ".join(
                f"{o['option_letter']}. {short(o['option_text'], 120)}"
                for o in opts
                if o["is_correct"]
            )
            md.append(
                f"| {q['question_id']} | {q['question_type']} | "
                f"{short(q['prompt'], 220).replace('|', '/')} | "
                f"{(correct or '—').replace('|', '/')} |"
            )

        md.append("")

    con.close()

    OUT_MD.write_text("\n".join(md) + "\n", encoding="utf-8")

    print(f"Wrote {OUT_CSV}")
    print(f"Wrote {OUT_MD}")
    print(f"Total questions: {len(rows)}")
    print()
    for key, qs in by_section_topic.items():
        cls, section_slug, section_title, topic_slug, topic_title = key
        print(f"{cls} | {section_slug} | {topic_slug} | {len(qs)}")

if __name__ == "__main__":
    main()

"""
Експортва AI-генерирани въпроси за ревю в Obsidian.

Workflow:
  1. Намира всички AI въпроси (is_ai_generated=1) с quality_score IS NULL
  2. Групира ги по topic_slug
  3. Записва един markdown файл per topic в vault/Generated/Review/{topic_slug}.md
  4. Във файла: за всеки въпрос — checkbox approve/reject + полета за корекция

Format на markdown файла позволява ръчно редактиране:
  - [ ] approve   ← маркирай с x ако одобряваш
  - [ ] reject    ← маркирай с x ако отхвърляш
  - correct: А    ← ако правилният отговор е друг, попиши буквата

review_import.py чете тези файлове и ъпдейтва DB-то.

Употреба:
    python3 review_export.py
    python3 review_export.py --topic sql-join   (само за един topic)
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


HEADER = """---
title: Review — {topic_title}
type: review
topic_slug: {topic_slug}
generated_at: {generated_at}
question_ids: [{ids}]
status: pending
tags: [review, ai-generated]
---

# Review: {topic_title}

> **Инструкции:**  
> За всеки въпрос отбележи **САМО едно**:  
> - `[x] approve` — въпросът е добър, запиши в production pool  
> - `[x] reject` — въпросът е грешен/безполезен  
>
> **Корекция на правилен отговор** (ако само is_correct е грешен):  
> На реда `correct:` напиши буквата на правилния отговор (А/Б/В/Г).  
> Ако оставиш празно, се счита че is_correct маркерите по-долу са правилни.
>
> Когато си готов: `python3 src/review_import.py`

Topic: [[Topics/{topic_slug}|{topic_title}]]  
Area: {area}  
Класове: {classes}

---

"""


QUESTION_TEMPLATE = """## Q#{qid} [{difficulty}]

{prompt}

{options_block}

- [ ] approve
- [ ] reject
- correct: 

> 💡 {explanation}

---

"""


def fetch_review_questions(conn: sqlite3.Connection,
                           topic_slug: str | None = None) -> dict:
    """
    Връща dict: topic_slug → list of (qid, question_dict).
    """
    where_extra = ""
    params: list = []
    if topic_slug:
        where_extra = " AND t.topic_slug = ?"
        params = [topic_slug]
    
    rows = conn.execute(f"""
        SELECT q.id, q.prompt, q.difficulty, q.source_exam, q.source_number,
               t.topic_slug, t.title_bg, a.title_bg, t.id
        FROM questions q
        LEFT JOIN curriculum_topics t ON t.id = q.topic_id
        LEFT JOIN curriculum_areas a ON a.id = t.area_id
        WHERE q.is_ai_generated = 1
          AND q.quality_score IS NULL
          {where_extra}
        ORDER BY t.topic_slug, q.id
    """, params).fetchall()
    
    by_topic: dict = {}
    
    for r in rows:
        qid, prompt, difficulty, src_exam, src_num, slug, title, area, topic_id = r
        slug = slug or "untagged"
        title = title or "Untagged"
        
        # Fetch options
        opts = conn.execute("""
            SELECT option_letter, option_text, is_correct
            FROM multiple_choice_options
            WHERE question_id = ?
            ORDER BY option_letter
        """, (qid,)).fetchall()
        
        # Get classes
        classes_list = []
        if topic_id:
            classes_list = [
                r[0] for r in conn.execute(
                    "SELECT class FROM topic_classes WHERE topic_id=? ORDER BY class",
                    (topic_id,)
                )
            ]
        
        q_data = {
            "qid": qid,
            "prompt": prompt,
            "difficulty": difficulty or "medium",
            "src_exam": src_exam,
            "src_num": src_num,
            "options": [(o[0], o[1], bool(o[2])) for o in opts],
            "topic_title": title,
            "area": area or "—",
            "classes": classes_list,
        }
        
        by_topic.setdefault(slug, []).append(q_data)
    
    return by_topic


def render_question(q: dict) -> str:
    options_lines = []
    for letter, text, is_correct in q["options"]:
        marker = "**[✓]**" if is_correct else "[ ]"
        options_lines.append(f"- {marker} **{letter})** {text}")
    
    return QUESTION_TEMPLATE.format(
        qid=q["qid"],
        difficulty=q["difficulty"],
        prompt=q["prompt"],
        options_block="\n".join(options_lines),
        explanation=f"Currently marked correct: {next((o[0] for o in q['options'] if o[2]), '?')}",
    )


def render_topic_file(topic_slug: str, questions: list) -> str:
    if not questions:
        return ""
    
    sample = questions[0]
    classes_str = ", ".join(str(c) for c in sample["classes"]) if sample["classes"] else "—"
    
    header = HEADER.format(
        topic_title=sample["topic_title"],
        topic_slug=topic_slug,
        generated_at=datetime.now().isoformat(timespec="seconds"),
        ids=",".join(str(q["qid"]) for q in questions),
        area=sample["area"],
        classes=classes_str,
    )
    
    body = "\n".join(render_question(q) for q in questions)
    
    return header + body


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("data/questions.db"))
    p.add_argument("--vault", type=Path, default=Path("vault"))
    p.add_argument("--topic", default=None,
                   help="Само за един topic_slug (default: всички pending)")
    args = p.parse_args()
    
    if not args.db.exists():
        print(f"❌ DB не съществува: {args.db}")
        sys.exit(1)
    if not args.vault.exists():
        print(f"❌ Vault не съществува: {args.vault}")
        sys.exit(1)
    
    conn = sqlite3.connect(str(args.db))
    by_topic = fetch_review_questions(conn, args.topic)
    
    if not by_topic:
        print(f"✅ Няма AI въпроси за ревю.")
        if args.topic:
            print(f"   (filtered by topic={args.topic})")
        return
    
    review_dir = args.vault / "Generated" / "Review"
    review_dir.mkdir(parents=True, exist_ok=True)
    
    total_q = sum(len(qs) for qs in by_topic.values())
    print(f"📋 Pending review: {total_q} въпроса в {len(by_topic)} topic(s)")
    print()
    
    for slug, questions in by_topic.items():
        out_path = review_dir / f"{slug}.md"
        content = render_topic_file(slug, questions)
        
        if out_path.exists():
            print(f"   ⚠️  Презаписвам {out_path.relative_to(args.vault)} ({len(questions)} q)")
        else:
            print(f"   ✓ {out_path.relative_to(args.vault)} ({len(questions)} q)")
        
        out_path.write_text(content, encoding="utf-8")
    
    conn.close()
    
    print(f"\n✅ Готово.")
    print(f"\nВ Obsidian:")
    print(f"   1. Cmd+R refresh")
    print(f"   2. Отвори vault/Generated/Review/")
    print(f"   3. Маркирай [x] approve или [x] reject за всеки въпрос")
    print(f"   4. (Optional) попълни 'correct: X' ако правилният отговор е друг")
    print(f"   5. python3 src/review_import.py")


if __name__ == "__main__":
    main()

"""
Export на генерирани материали от DB → vault (reverse sync).

Когато build_exam.py генерира изпит, едновременно с .docx файла можем
да създаваме и markdown bookmark в vault-а с frontmatter, който:
  - линква към генерираните questions (по id)
  - указва subject, level, target_points
  - линква към тематичните MOCs

Това позволява генерираните материали да се появят в graph view и
да можеш да ги отвориш директно от Obsidian.

Режими на работа:
  1. От generated_exams таблица: експортирай all/by-id
  2. От ad-hoc списък question_ids: експортирай нов изпит

Употреба:
    # Export един конкретен изпит от generated_exams
    python3 export_to_vault.py --exam-id 5

    # Export по име
    python3 export_to_vault.py --exam-name "Пробен ДЗИ май 2026"

    # Export всички, които още нямат markdown файл
    python3 export_to_vault.py --all-pending

    # Generic: експортирай custom набор от questions
    python3 export_to_vault.py --question-ids 1,2,3,4,5 \
        --title "Бърз тест 9 клас" --output-folder Worksheets
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# ============================================================
# Folder mapping
# ============================================================

FOLDER_MAP = {
    "exam": "Generated/Exams",
    "worksheet": "Generated/Worksheets",
    "homework": "Generated/Homework",
}


# ============================================================
# Markdown generation
# ============================================================

def slugify(text: str) -> str:
    """Прост cyrillic → latin slug. ASCII filename за Obsidian compat."""
    # Транслитерация — minimal, само най-важните букви
    cyr_to_lat = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l",
        "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s",
        "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch",
        "ш": "sh", "щ": "sht", "ъ": "a", "ь": "y", "ю": "yu", "я": "ya",
    }
    
    text = text.lower()
    out = []
    for ch in text:
        if ch in cyr_to_lat:
            out.append(cyr_to_lat[ch])
        elif ch.isascii() and (ch.isalnum() or ch == "-"):
            out.append(ch)
        elif ch in (" ", "_"):
            out.append("-")
        # else: drop
    
    slug = "".join(out)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "untitled"


def fetch_questions(conn: sqlite3.Connection, question_ids: list) -> list:
    """Връща детайлите на въпросите за export."""
    if not question_ids:
        return []
    placeholders = ",".join("?" * len(question_ids))
    rows = conn.execute(f"""
        SELECT id, source_exam, source_number, question_type,
               topic_id, legacy_topic, subject, level, year,
               points, prompt
        FROM questions
        WHERE id IN ({placeholders})
        ORDER BY id
    """, question_ids).fetchall()
    return rows


def fetch_topic_titles(conn: sqlite3.Connection, topic_ids: list) -> dict:
    """topic_id → (title_bg, slug)"""
    if not topic_ids:
        return {}
    placeholders = ",".join("?" * len(topic_ids))
    out = {}
    for row in conn.execute(f"""
        SELECT id, title_bg, topic_slug
        FROM curriculum_topics
        WHERE id IN ({placeholders})
    """, topic_ids):
        out[row[0]] = (row[1], row[2])
    return out


def build_markdown(
    title: str,
    note_type: str,
    question_ids: list,
    questions: list,
    topic_titles: dict,
    extra_metadata: dict,
    output_path: Optional[str] = None,
) -> str:
    """Build the markdown content with YAML frontmatter."""
    
    # Calculate aggregates
    total_points = sum(q[9] for q in questions if q[9])  # points col idx
    by_subject: dict = {}
    by_level: dict = {}
    topics_used: set = set()
    
    for q in questions:
        subj = q[6] or "unknown"
        lvl = q[7] or "unknown"
        by_subject[subj] = by_subject.get(subj, 0) + 1
        by_level[lvl] = by_level.get(lvl, 0) + 1
        if q[4] is not None and q[4] in topic_titles:
            topics_used.add(topic_titles[q[4]][1])  # slug
    
    # Frontmatter
    fm_lines = [
        "---",
        f"title: {title}",
        f"type: generated",
        f"generated_type: {note_type}",
        f"generated_at: {datetime.now().isoformat(timespec='seconds')}",
        f"question_count: {len(question_ids)}",
        f"total_points: {total_points}",
        f"question_ids: [{','.join(str(qid) for qid in question_ids)}]",
    ]
    
    if topics_used:
        fm_lines.append(f"topics: [{', '.join(sorted(topics_used))}]")
    
    if by_subject:
        # Just list dominant subject
        dominant = max(by_subject.items(), key=lambda x: x[1])[0]
        fm_lines.append(f"subject: {dominant}")
    if by_level:
        dominant_lvl = max(by_level.items(), key=lambda x: x[1])[0]
        fm_lines.append(f"level: {dominant_lvl}")
    
    if extra_metadata:
        for k, v in extra_metadata.items():
            fm_lines.append(f"{k}: {v}")
    
    fm_lines.append("tags: [generated, " + note_type + "]")
    fm_lines.append("---")
    
    # Body
    body_lines = [
        "",
        f"# {title}",
        "",
    ]
    
    if output_path:
        body_lines.append(f"**Файл:** [[{output_path}]]")
        body_lines.append("")
    
    body_lines.extend([
        "## Метаданни",
        "",
        f"- **Тип:** {note_type}",
        f"- **Въпроси:** {len(question_ids)}",
        f"- **Точки общо:** {total_points}",
        f"- **Генериран:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ])
    
    if topics_used:
        body_lines.append("## Покрити теми")
        body_lines.append("")
        for slug in sorted(topics_used):
            title_bg = next(
                (t[0] for tid, t in topic_titles.items() if t[1] == slug),
                slug
            )
            body_lines.append(f"- [[Topics/{slug}|{title_bg}]]")
        body_lines.append("")
    
    body_lines.extend([
        "## Въпроси",
        "",
        "| № | Тип | Точки | Source | Topic |",
        "|---|---|---|---|---|",
    ])
    
    for i, q in enumerate(questions, 1):
        qid, source, src_num, qtype, topic_id, legacy, _subj, _lvl, _yr, points, prompt = q
        topic_label = "—"
        if topic_id is not None and topic_id in topic_titles:
            slug = topic_titles[topic_id][1]
            topic_label = f"[[Topics/{slug}\\|{topic_titles[topic_id][0]}]]"
        elif legacy:
            topic_label = f"_{legacy}_"
        
        type_short = {
            "multiple_choice": "MC",
            "fill_in": "FI",
        }.get(qtype, qtype)
        
        prompt_preview = (prompt or "").strip().replace("\n", " ")[:80]
        body_lines.append(
            f"| {i} | {type_short} | {points} | {source} #{src_num} | {topic_label} |"
        )
    
    body_lines.append("")
    body_lines.append("## Бележки")
    body_lines.append("")
    body_lines.append("> Място за пост-фактум наблюдения — как се справиха учениците, какво беше трудно, какво да променя.")
    body_lines.append("")
    
    return "\n".join(fm_lines) + "\n" + "\n".join(body_lines)


# ============================================================
# Modes
# ============================================================

def export_from_generated_exams(conn: sqlite3.Connection, vault: Path,
                                 exam_id: Optional[int] = None,
                                 exam_name: Optional[str] = None,
                                 only_pending: bool = False) -> int:
    """Export от generated_exams таблица. Връща броя exported."""
    
    where = []
    params: list = []
    if exam_id is not None:
        where.append("id = ?")
        params.append(exam_id)
    elif exam_name is not None:
        where.append("exam_name = ?")
        params.append(exam_name)
    
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    rows = conn.execute(
        f"SELECT id, exam_name, generated_at, question_ids, output_file_path,"
        f" subject, level, target_points "
        f"FROM generated_exams{where_sql} ORDER BY generated_at DESC",
        params
    ).fetchall()
    
    if not rows:
        print("⚠️  Не намирам матчващи генерирани изпити в generated_exams")
        return 0
    
    folder = vault / FOLDER_MAP["exam"]
    folder.mkdir(parents=True, exist_ok=True)
    exported = 0
    
    for row in rows:
        gen_id, name, gen_at, qids_json, out_path, subj, lvl, target_pts = row
        
        # Filename
        date_part = (gen_at or datetime.now().isoformat())[:10]
        slug = slugify(name or f"exam-{gen_id}")
        md_filename = f"{date_part}-{slug}.md"
        md_path = folder / md_filename
        
        if md_path.exists() and only_pending:
            print(f"   ⏭️  Skip (вече има): {md_filename}")
            continue
        
        # Parse question_ids
        try:
            question_ids = json.loads(qids_json) if qids_json else []
        except json.JSONDecodeError:
            print(f"   ❌ Invalid question_ids JSON за exam {gen_id}: {qids_json}")
            continue
        
        questions = fetch_questions(conn, question_ids)
        topic_ids = [q[4] for q in questions if q[4] is not None]
        topic_titles = fetch_topic_titles(conn, topic_ids)
        
        extra_meta: dict = {}
        if target_pts:
            extra_meta["target_points"] = target_pts
        if subj:
            extra_meta["target_subject"] = subj
        if lvl:
            extra_meta["target_level"] = lvl
        extra_meta["generated_exam_id"] = gen_id
        
        md = build_markdown(
            title=name or f"Изпит #{gen_id}",
            note_type="exam",
            question_ids=question_ids,
            questions=questions,
            topic_titles=topic_titles,
            extra_metadata=extra_meta,
            output_path=out_path,
        )
        md_path.write_text(md, encoding="utf-8")
        print(f"   ✓ {md_filename}")
        exported += 1
    
    return exported


def export_custom(conn: sqlite3.Connection, vault: Path,
                  question_ids: list, title: str,
                  output_folder: str, note_type: str) -> bool:
    """Ad-hoc export — за worksheet/homework без запис в generated_exams."""
    questions = fetch_questions(conn, question_ids)
    if not questions:
        print(f"❌ Никой от {len(question_ids)} въпроса не е намерен в DB")
        return False
    
    topic_ids = [q[4] for q in questions if q[4] is not None]
    topic_titles = fetch_topic_titles(conn, topic_ids)
    
    folder_key = note_type if note_type in FOLDER_MAP else "exam"
    if output_folder:
        folder = vault / output_folder
    else:
        folder = vault / FOLDER_MAP[folder_key]
    folder.mkdir(parents=True, exist_ok=True)
    
    date_part = datetime.now().strftime("%Y-%m-%d")
    slug = slugify(title)
    md_filename = f"{date_part}-{slug}.md"
    md_path = folder / md_filename
    
    md = build_markdown(
        title=title,
        note_type=note_type,
        question_ids=question_ids,
        questions=questions,
        topic_titles=topic_titles,
        extra_metadata={},
    )
    md_path.write_text(md, encoding="utf-8")
    print(f"✓ {md_path.relative_to(vault)}")
    return True


# ============================================================
# Main
# ============================================================

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--vault", type=Path,
                   default=Path.home() / "dzi-generator" / "vault")
    p.add_argument("--db", type=Path,
                   default=Path("data/questions.db"))
    
    # Mode 1: from generated_exams
    p.add_argument("--exam-id", type=int)
    p.add_argument("--exam-name")
    p.add_argument("--all-pending", action="store_true",
                   help="Export всички в generated_exams които още нямат markdown")
    
    # Mode 2: custom set
    p.add_argument("--question-ids",
                   help="Comma-separated списък question id-та")
    p.add_argument("--title", default="Без заглавие")
    p.add_argument("--type", choices=["exam", "worksheet", "homework"],
                   default="exam")
    p.add_argument("--output-folder", default=None)
    
    args = p.parse_args()
    
    if not args.vault.exists():
        print(f"❌ Vault не съществува: {args.vault}")
        sys.exit(1)
    if not args.db.exists():
        print(f"❌ DB не съществува: {args.db}")
        sys.exit(1)
    
    conn = sqlite3.connect(str(args.db))
    
    if args.question_ids:
        ids = [int(x.strip()) for x in args.question_ids.split(",") if x.strip()]
        ok = export_custom(conn, args.vault, ids, args.title,
                           args.output_folder, args.type)
        if not ok:
            sys.exit(1)
    elif args.exam_id or args.exam_name or args.all_pending:
        n = export_from_generated_exams(
            conn, args.vault,
            exam_id=args.exam_id,
            exam_name=args.exam_name,
            only_pending=args.all_pending,
        )
        print(f"\n✅ Exported {n} изпит(а)")
    else:
        print("❌ Трябва да укажеш --exam-id, --exam-name, --all-pending или --question-ids")
        sys.exit(1)
    
    conn.close()


if __name__ == "__main__":
    main()

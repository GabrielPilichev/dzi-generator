"""
Migration v1 -> v2 за questions.db.

Какво прави:
  1. Прави backup на оригиналното DB (questions.db.v1.bak)
  2. Създава нова празна DB по schema_v2.sql
  3. Копира всички редове от старата DB в новата
  4. Парсва source_exam ('may_2025_v2' -> subject/level/year/session/variant)
  5. Създава записи в exams таблицата за всеки уникален source_exam
  6. Записва нова DB на същото място, премества стара като .v1.bak

Употреба:
    python3 migrate_to_v2.py --db data/questions.db --schema schema_v2.sql
"""

import argparse
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from datetime import datetime


# Известни патерни на source_exam от стария parse_v2.py
# Формат: '<session>_<year>[_v<variant>]'
SOURCE_PATTERN = re.compile(
    r"^(?P<session>may|aug|jun|january|february|march|april|may|june|july|august|september|october|november|december)"
    r"_(?P<year>\d{4})"
    r"(?:_v(?P<variant>\d+))?$",
    re.IGNORECASE,
)

SESSION_NORMALIZE = {
    "may": "may", "aug": "august", "jun": "june",
    "january": "january", "february": "february", "march": "march",
    "april": "april", "june": "june", "july": "july",
    "august": "august", "september": "september",
    "october": "october", "november": "november", "december": "december",
}


def parse_source_exam(source_exam: str) -> dict:
    """
    Парсва 'may_2025_v2' -> {session: 'may', year: 2025, variant: 2}.
    Връща dict с None полета ако не може да парсне.
    """
    m = SOURCE_PATTERN.match(source_exam)
    if not m:
        return {"session": None, "year": None, "variant": 1}
    
    session = SESSION_NORMALIZE.get(m.group("session").lower(), m.group("session").lower())
    year = int(m.group("year"))
    variant = int(m.group("variant")) if m.group("variant") else 1
    
    return {"session": session, "year": year, "variant": variant}


def detect_subject_and_level(source_exam: str) -> tuple:
    """
    Опит за detection на subject/level от source_exam.
    За съществуващите данни всичко е ДЗИ информатика, но оставяме hooks за бъдещето.
    """
    s = source_exam.lower()
    
    # Level
    if "nvo" in s or "нво" in s:
        if "7" in s:
            level = "NVO_7"
        elif "10" in s:
            level = "NVO_10"
        else:
            level = "NVO"
    else:
        level = "DZI"
    
    # Subject
    if "matem" in s or "математ" in s:
        subject = "matematika"
    elif "bel" in s or "бел" in s:
        subject = "bel"
    elif "angl" in s or "англ" in s:
        subject = "angliyski"
    elif "istor" in s or "истор" in s:
        subject = "istoriya"
    elif "informat" in s or "информат" in s:
        subject = "informatika_it"
    else:
        # Default за съществуващите данни
        subject = "informatika_it"
    
    return subject, level


def migrate(db_path: Path, schema_path: Path) -> None:
    if not db_path.exists():
        print(f"❌ DB не намерен: {db_path}")
        sys.exit(1)
    if not schema_path.exists():
        print(f"❌ Schema не намерен: {schema_path}")
        sys.exit(1)
    
    # 1. Backup
    backup_path = db_path.with_suffix(db_path.suffix + ".v1.bak")
    shutil.copy2(db_path, backup_path)
    print(f"📦 Backup на старата DB: {backup_path}")
    
    # 2. Чета старите данни
    old_conn = sqlite3.connect(str(db_path))
    old_conn.row_factory = sqlite3.Row
    
    old_questions = old_conn.execute("SELECT * FROM questions").fetchall()
    old_options = old_conn.execute("SELECT * FROM multiple_choice_options").fetchall()
    old_subq = old_conn.execute("SELECT * FROM fill_in_subquestions").fetchall()
    old_gen = old_conn.execute("SELECT * FROM generated_exams").fetchall()
    
    print(f"📊 Стари данни: {len(old_questions)} въпроса, {len(old_options)} опции, "
          f"{len(old_subq)} подзадачи, {len(old_gen)} генерации")
    
    old_conn.close()
    
    # 3. Създавам нова DB временно като .new
    new_db_path = db_path.with_suffix(db_path.suffix + ".new")
    if new_db_path.exists():
        new_db_path.unlink()
    
    new_conn = sqlite3.connect(str(new_db_path))
    new_conn.executescript(schema_path.read_text(encoding="utf-8"))
    cur = new_conn.cursor()
    
    # 4. Създавам exams записи (един на уникален source_exam)
    unique_sources = sorted({q["source_exam"] for q in old_questions})
    source_to_exam_id = {}
    
    for src in unique_sources:
        meta = parse_source_exam(src)
        subject, level = detect_subject_and_level(src)
        
        cur.execute("""
            INSERT INTO exams (
                subject, level, year, session, variant, format_version,
                source_file, parser_version, parsed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            subject, level,
            meta["year"] or 0,           # 0 за непознати години
            meta["session"],
            meta["variant"] or 1,
            "modern_2023" if (meta["year"] or 0) >= 2023 else "legacy_pre2023",
            f"reference/{src}/",         # няма точен path, използваме prefix
            "v1_migrated",
            datetime.now().isoformat(timespec="seconds"),
        ))
        source_to_exam_id[src] = cur.lastrowid
    
    print(f"✓ Създадени {len(source_to_exam_id)} exam записи")
    
    # 5. Копирам questions с новите полета
    old_to_new_qid = {}
    for q in old_questions:
        src = q["source_exam"]
        meta = parse_source_exam(src)
        subject, level = detect_subject_and_level(src)
        exam_id = source_to_exam_id[src]
        
        cur.execute("""
            INSERT INTO questions (
                exam_id, source_exam, source_number,
                subject, level, year,
                question_type, topic, difficulty, points,
                prompt, has_image, image_path, created_at, is_ai_generated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            exam_id, q["source_exam"], q["source_number"],
            subject, level, meta["year"],
            q["question_type"], q["topic"], q["difficulty"], q["points"],
            q["prompt"], q["has_image"], q["image_path"],
            q["created_at"], q["is_ai_generated"],
        ))
        old_to_new_qid[q["id"]] = cur.lastrowid
    
    print(f"✓ Копирани {len(old_to_new_qid)} въпроса")
    
    # 6. Копирам options
    for o in old_options:
        new_qid = old_to_new_qid.get(o["question_id"])
        if new_qid is None:
            continue
        cur.execute("""
            INSERT INTO multiple_choice_options (question_id, option_letter, option_text, is_correct)
            VALUES (?, ?, ?, ?)
        """, (new_qid, o["option_letter"], o["option_text"], o["is_correct"]))
    
    print(f"✓ Копирани {len(old_options)} опции")
    
    # 7. Копирам subquestions
    for s in old_subq:
        new_qid = old_to_new_qid.get(s["question_id"])
        if new_qid is None:
            continue
        cur.execute("""
            INSERT INTO fill_in_subquestions
                (question_id, subquestion_number, subquestion_text, correct_answer, answer_alternatives, points)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (new_qid, s["subquestion_number"], s["subquestion_text"],
              s["correct_answer"], s["answer_alternatives"], s["points"]))
    
    print(f"✓ Копирани {len(old_subq)} подзадачи")
    
    # 8. Копирам generated_exams
    for g in old_gen:
        cur.execute("""
            INSERT INTO generated_exams (exam_name, generated_at, question_ids, output_file_path)
            VALUES (?, ?, ?, ?)
        """, (g["exam_name"], g["generated_at"], g["question_ids"], g["output_file_path"]))
    
    print(f"✓ Копирани {len(old_gen)} генерации")
    
    new_conn.commit()
    
    # 9. Sanity check
    new_q_count = new_conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    if new_q_count != len(old_questions):
        print(f"❌ Мисматч в брой въпроси: {new_q_count} != {len(old_questions)}. ABORT.")
        new_conn.close()
        new_db_path.unlink()
        sys.exit(1)
    
    new_conn.close()
    
    # 10. Replace оригиналното DB
    db_path.unlink()
    shutil.move(str(new_db_path), str(db_path))
    print(f"\n✅ Миграция готова. {db_path}")
    print(f"   Старата DB е запазена като: {backup_path}")
    print(f"   За да се върнеш назад: cp {backup_path} {db_path}")


def main() -> None:
    p = argparse.ArgumentParser(description="Migrate questions.db v1 -> v2")
    p.add_argument("--db", default="data/questions.db", type=Path)
    p.add_argument("--schema", default="src/schema_v2.sql", type=Path)
    args = p.parse_args()
    migrate(args.db, args.schema)


if __name__ == "__main__":
    main()

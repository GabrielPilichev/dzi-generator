"""
AI генератор на multiple choice въпроси.

Дава topic_slug → BgGPT генерира N нови въпроса с 4 опции и правилен отговор.

Workflow:
  1. Прочита topic-а от DB (slug, title, description, area, classes)
  2. Прочита 1-3 примерни въпроса от ВЕЧЕ съществуващи MC въпроси (като style примери)
  3. Изпраща на BgGPT structured prompt с:
     - Topic info
     - Few-shot examples (от existing ДЗИ въпроси)
     - Изисквания (формат, точки, стил)
  4. BgGPT връща JSON: array от въпроси
  5. Validation: всеки въпрос трябва да има prompt, 4 опции, един правилен
  6. UPSERT в DB с is_ai_generated=1, source_exam='ai_generated_YYYYMMDD'
  7. Audit log в data/qgen_log.jsonl

Употреба:
    # Тест: 1 въпрос за SQL JOIN, dry-run (само print)
    python3 generate_questions.py --topic sql-join --count 1 --dry-run

    # Реално генерира 3 въпроса и записва
    python3 generate_questions.py --topic sql-join --count 3

    # За топик без съществуващи въпроси (без few-shot examples)
    python3 generate_questions.py --topic foreign-key --count 5

    # Batch: генерирай по 3 въпроса за всеки empty topic в зададена area
    python3 generate_questions.py --area databases --count 3 --empty-only
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# Path hack
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.ollama_client import (
    OllamaClient,
    OllamaError,
    DEFAULT_CHAT_MODEL,
    DEFAULT_HOST,
)


LOG_PATH = Path("data/qgen_log.jsonl")


# ============================================================
# System prompt
# ============================================================

SYSTEM_PROMPT = """Ти си експерт по съставяне на изпитни въпроси за български гимназиален курс по Информационни технологии.

Твоята задача: генерирай multiple choice въпроси по зададена тема.

ИЗИСКВАНИЯ:
- Език: български
- Формат: multiple choice с точно 4 опции
- Опциите се означават с А, Б, В, Г (НЕ с латински букви)
- Един правилен отговор
- Останалите 3 опции (distractors) трябва да са правдоподобни — типични грешки или объркващи концепти
- Стил: кратки, ясни въпроси, без излишно усложнение
- Ниво на трудност: подходящо за зададения клас

ОТГОВАРЯЙ САМО С JSON масив, без markdown, без коментари. Формат:
[
  {
    "prompt": "текст на въпроса",
    "options": [
      {"letter": "А", "text": "опция 1", "is_correct": false},
      {"letter": "Б", "text": "опция 2", "is_correct": true},
      {"letter": "В", "text": "опция 3", "is_correct": false},
      {"letter": "Г", "text": "опция 4", "is_correct": false}
    ],
    "difficulty": "easy|medium|hard",
    "explanation": "защо този отговор е верен"
  }
]
"""


# ============================================================
# Build user prompt
# ============================================================

def build_user_prompt(topic_info: dict, count: int, examples: list) -> str:
    parts = []
    
    parts.append(f"Тема: {topic_info['title']}")
    if topic_info.get("area"):
        parts.append(f"Област: {topic_info['area']}")
    if topic_info.get("classes"):
        parts.append(f"Клас(ове): {', '.join(str(c) for c in topic_info['classes'])}")
    if topic_info.get("description"):
        parts.append(f"Описание: {topic_info['description']}")
    
    parts.append("")
    
    if examples:
        parts.append(f"Примерни въпроси по подобни теми (за стил):")
        for i, ex in enumerate(examples, 1):
            parts.append(f"\nПример {i}:")
            parts.append(f"Въпрос: {ex['prompt']}")
            for opt in ex.get("options", []):
                marker = " ✓" if opt["is_correct"] else ""
                parts.append(f"  {opt['letter']}) {opt['text']}{marker}")
        parts.append("")
    
    parts.append(f"Генерирай {count} нов(и) multiple choice въпрос(а) по темата.")
    parts.append("Различни сложности и подвъпроси, без повтаряне на existing examples.")
    parts.append("")
    parts.append("Отговор (само JSON масив):")
    
    return "\n".join(parts)


# ============================================================
# Topic info fetching
# ============================================================

def fetch_topic_info(conn: sqlite3.Connection, slug: str) -> dict | None:
    row = conn.execute("""
        SELECT t.id, t.topic_slug, t.title_bg, t.description,
               a.area_id, a.title_bg as area_title
        FROM curriculum_topics t
        LEFT JOIN curriculum_areas a ON a.id = t.area_id
        WHERE t.topic_slug = ?
    """, (slug,)).fetchone()
    
    if not row:
        return None
    
    classes = [r[0] for r in conn.execute(
        "SELECT class FROM topic_classes WHERE topic_id=? ORDER BY class",
        (row[0],)
    )]
    
    return {
        "id": row[0],
        "slug": row[1],
        "title": row[2],
        "description": row[3],
        "area_slug": row[4],
        "area": row[5],
        "classes": classes,
    }


def fetch_few_shot_examples(conn: sqlite3.Connection,
                            area_slug: str | None,
                            limit: int = 3) -> list:
    """Взима MC въпроси от същата area като few-shot examples."""
    if not area_slug:
        return []
    
    rows = conn.execute("""
        SELECT q.id, q.prompt
        FROM questions q
        JOIN curriculum_topics t ON t.id = q.topic_id
        JOIN curriculum_areas a ON a.id = t.area_id
        WHERE a.area_id = ? AND q.question_type = 'multiple_choice'
        ORDER BY RANDOM()
        LIMIT ?
    """, (area_slug, limit)).fetchall()
    
    examples = []
    for q_id, prompt in rows:
        opts = conn.execute("""
            SELECT option_letter, option_text, is_correct
            FROM multiple_choice_options
            WHERE question_id = ?
            ORDER BY option_letter
        """, (q_id,)).fetchall()
        
        if len(opts) != 4:
            continue
        
        examples.append({
            "prompt": prompt,
            "options": [
                {"letter": o[0], "text": o[1], "is_correct": bool(o[2])}
                for o in opts
            ],
        })
    
    return examples


# ============================================================
# Response parsing & validation
# ============================================================

def parse_questions_array(raw: str) -> list:
    """Извлича JSON array от raw text. Връща list of dicts или []."""
    if not raw:
        return []
    
    # Find first [
    start = raw.find("[")
    if start == -1:
        return []
    
    # Try the standard approach first: from [ to last ]
    end = raw.rfind("]")
    if end != -1 and end > start:
        json_str = raw[start:end + 1]
        try:
            parsed = json.loads(json_str)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
    
    # Fallback: output is truncated. Try to extract complete objects from
    # the array by finding balanced { ... } pairs.
    text = raw[start + 1:]  # skip the leading [
    parsed = []
    i = 0
    n = len(text)
    
    while i < n:
        # Find next {
        while i < n and text[i] != "{":
            i += 1
        if i >= n:
            break
        
        # Track brace depth, respecting strings
        depth = 0
        obj_start = i
        in_string = False
        escape = False
        
        while i < n:
            ch = text[i]
            if escape:
                escape = False
            elif ch == "\\" and in_string:
                escape = True
            elif ch == '"':
                in_string = not in_string
            elif not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        # Complete object
                        try:
                            obj = json.loads(text[obj_start:i + 1])
                            if isinstance(obj, dict):
                                parsed.append(obj)
                        except json.JSONDecodeError:
                            pass
                        i += 1
                        break
            i += 1
        else:
            # Reached end without closing — incomplete object, skip
            break
    
    return parsed


def validate_question(q: dict) -> tuple:
    """Връща (is_valid, error_msg)."""
    if not isinstance(q, dict):
        return False, "not a dict"
    if "prompt" not in q or not q["prompt"]:
        return False, "missing prompt"
    if "options" not in q or not isinstance(q["options"], list):
        return False, "missing options array"
    if len(q["options"]) != 4:
        return False, f"expected 4 options, got {len(q['options'])}"
    
    letters_seen = set()
    correct_count = 0
    for opt in q["options"]:
        if not isinstance(opt, dict):
            return False, "option is not dict"
        if "letter" not in opt or "text" not in opt or "is_correct" not in opt:
            return False, "option missing fields"
        if opt["letter"] not in ("А", "Б", "В", "Г"):
            return False, f"invalid letter: {opt['letter']!r} (must be А/Б/В/Г)"
        if opt["letter"] in letters_seen:
            return False, f"duplicate letter: {opt['letter']}"
        letters_seen.add(opt["letter"])
        if opt["is_correct"]:
            correct_count += 1
        if not opt["text"]:
            return False, f"empty text for {opt['letter']}"
    
    if correct_count != 1:
        return False, f"expected 1 correct, got {correct_count}"
    
    if letters_seen != {"А", "Б", "В", "Г"}:
        return False, "missing one of А/Б/В/Г"
    
    return True, ""


# ============================================================
# DB persistence
# ============================================================

def save_question(conn: sqlite3.Connection,
                  topic_info: dict,
                  q: dict,
                  source_exam: str,
                  difficulty: str,
                  explanation: str) -> int:
    """Записва въпрос в DB. Връща question_id."""
    cur = conn.cursor()
    
    # Get next source_number for this AI batch (auto-increment per source_exam)
    last_num = cur.execute(
        "SELECT MAX(source_number) FROM questions WHERE source_exam = ?",
        (source_exam,)
    ).fetchone()[0] or 0
    next_num = last_num + 1
    
    # Insert question
    cur.execute("""
        INSERT INTO questions (
            source_exam, source_number, question_type,
            topic_id, legacy_topic, subject, level,
            difficulty, points, prompt,
            is_ai_generated, quality_score
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        source_exam, next_num, "multiple_choice",
        topic_info["id"], topic_info["area_slug"], "informatika_it", "DZI",
        difficulty, 1, q["prompt"],
        1, None,  # is_ai_generated=1, quality_score=null (нерезюирано)
    ))
    qid = cur.lastrowid
    
    # Insert options
    for opt in q["options"]:
        cur.execute("""
            INSERT INTO multiple_choice_options
                (question_id, option_letter, option_text, is_correct)
            VALUES (?, ?, ?, ?)
        """, (qid, opt["letter"], opt["text"], int(opt["is_correct"])))
    
    return qid


# ============================================================
# Logging
# ============================================================

def write_log(log_path: Path, entry: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ============================================================
# Display
# ============================================================

def print_question(q: dict, num: int) -> None:
    print(f"\n{'─' * 60}")
    print(f"#{num} [{q.get('difficulty', '?')}]")
    print(f"\n{q['prompt']}\n")
    for opt in q["options"]:
        marker = "✓" if opt["is_correct"] else " "
        print(f"  {marker} {opt['letter']}) {opt['text']}")
    if q.get("explanation"):
        print(f"\n💡 {q['explanation']}")


# ============================================================
# Main
# ============================================================

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("data/questions.db"))
    p.add_argument("--model", default=DEFAULT_CHAT_MODEL)
    p.add_argument("--host", default=DEFAULT_HOST)
    
    # Mode
    p.add_argument("--topic", help="Topic slug (генерирай за един topic)")
    p.add_argument("--area", help="Area slug (batch за всички empty topics в area)")
    p.add_argument("--empty-only", action="store_true",
                   help="(--area mode) Само topics без въпроси")
    
    p.add_argument("--count", type=int, default=3,
                   help="Колко въпроса да генерира за топик")
    p.add_argument("--dry-run", action="store_true",
                   help="Покажи генерираните въпроси, не записвай в DB")
    p.add_argument("--no-examples", action="store_true",
                   help="Не давай few-shot examples (полезно ако няма съществуващи)")
    
    args = p.parse_args()
    
    if not args.db.exists():
        print(f"❌ DB не съществува: {args.db}")
        sys.exit(1)
    
    if not args.topic and not args.area:
        print(f"❌ Трябва --topic или --area")
        sys.exit(1)
    
    print(f"🤖 AI Question Generator")
    print(f"   Model:      {args.model}")
    print(f"   Count/topic: {args.count}")
    if args.dry_run:
        print(f"   ⚠️  DRY RUN")
    
    client = OllamaClient(host=args.host)
    if not client.is_alive():
        print(f"\n❌ Ollama не работи. Стартирай: ollama serve")
        sys.exit(1)
    
    conn = sqlite3.connect(str(args.db))
    
    # Determine which topics to process
    topics_to_process = []
    
    if args.topic:
        info = fetch_topic_info(conn, args.topic)
        if not info:
            print(f"❌ Topic '{args.topic}' не съществува в DB.")
            sys.exit(1)
        topics_to_process.append(info)
    elif args.area:
        sql = """
            SELECT t.topic_slug FROM curriculum_topics t
            JOIN curriculum_areas a ON a.id = t.area_id
            WHERE a.area_id = ?
        """
        if args.empty_only:
            sql += """
                AND NOT EXISTS (
                    SELECT 1 FROM questions q WHERE q.topic_id = t.id
                )
            """
        slugs = [r[0] for r in conn.execute(sql, (args.area,))]
        for s in slugs:
            info = fetch_topic_info(conn, s)
            if info:
                topics_to_process.append(info)
    
    print(f"\n📋 Topics to process: {len(topics_to_process)}")
    if not topics_to_process:
        print(f"❌ Няма нищо за обработка.")
        return
    
    source_exam = f"ai_generated_{datetime.now().strftime('%Y%m%d')}"
    
    total_generated = 0
    total_saved = 0
    total_invalid = 0
    
    for topic in topics_to_process:
        print(f"\n{'=' * 60}")
        print(f"📌 {topic['title']} ({topic['slug']})")
        print(f"   Area: {topic['area']}, Classes: {topic['classes']}")
        print(f"{'=' * 60}")
        
        # Few-shot examples
        examples = []
        if not args.no_examples:
            examples = fetch_few_shot_examples(conn, topic["area_slug"], limit=2)
            if examples:
                print(f"   📚 Using {len(examples)} few-shot examples от area")
            else:
                print(f"   ⏭️  Няма available examples")
        
        # Build prompt
        user_prompt = build_user_prompt(topic, args.count, examples)
        
        # Call BgGPT
        t0 = time.monotonic()
        try:
            result = client.chat(
                messages=[{"role": "user", "content": user_prompt}],
                model=args.model,
                system=SYSTEM_PROMPT,
                options={
                    "temperature": 0.4,        # повече креативност отколкото за classification
                    "num_predict": 4096,       # български е verbose
                },
            )
        except OllamaError as e:
            print(f"   ❌ Ollama error: {e}")
            continue
        elapsed = time.monotonic() - t0
        
        print(f"\n   ⏱  {elapsed:.1f}s | output={result['eval_count']}t")
        
        # Parse
        questions = parse_questions_array(result["content"])
        if not questions:
            print(f"   ❌ Не можах да парсна JSON. Raw:\n{result['content'][:500]}")
            write_log(LOG_PATH, {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "topic": topic["slug"],
                "error": "parse_failed",
                "raw_response": result["content"][:2000],
            })
            continue
        
        print(f"   ✓ Generated {len(questions)} въпрос(а)")
        total_generated += len(questions)
        
        # Validate and save
        for i, q in enumerate(questions, 1):
            valid, err = validate_question(q)
            if not valid:
                print(f"   ⚠️  #{i} INVALID: {err}")
                total_invalid += 1
                write_log(LOG_PATH, {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "topic": topic["slug"],
                    "validation_error": err,
                    "raw_question": q,
                })
                continue
            
            print_question(q, i)
            
            if not args.dry_run:
                difficulty = q.get("difficulty", "medium")
                if difficulty not in ("easy", "medium", "hard"):
                    difficulty = "medium"
                
                qid = save_question(
                    conn, topic, q, source_exam, difficulty,
                    q.get("explanation", "")
                )
                total_saved += 1
                print(f"   💾 Saved as question #{qid}")
                
                write_log(LOG_PATH, {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "topic": topic["slug"],
                    "question_id": qid,
                    "saved": True,
                })
        
        if not args.dry_run:
            conn.commit()
    
    conn.close()
    
    # Summary
    print(f"\n{'=' * 60}")
    print(f"📊 SUMMARY")
    print(f"{'=' * 60}")
    print(f"   Topics processed: {len(topics_to_process)}")
    print(f"   Generated:        {total_generated}")
    print(f"   Saved:            {total_saved}")
    print(f"   Invalid:          {total_invalid}")
    print(f"\n📋 Лог: {LOG_PATH}")
    
    if not args.dry_run and total_saved > 0:
        print(f"\n💡 Source exam за тези въпроси: '{source_exam}'")
        print(f"   За преглед: SELECT * FROM questions WHERE source_exam='{source_exam}'")
        print(f"   За изтриване: DELETE FROM questions WHERE source_exam='{source_exam}'")


if __name__ == "__main__":
    main()

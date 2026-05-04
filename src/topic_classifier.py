"""
Topic classifier с BgGPT.

Класифицира въпроси без topic_id към един от валидните topic_slugs от DB.

Workflow:
  1. Чете list of valid topic_slugs от curriculum_topics (whitelist)
  2. За всеки orphan въпрос — извиква BgGPT с prompt:
     "Дадени са следните топици: [...]. В кой попада този въпрос?"
  3. BgGPT отговаря с JSON: {"slug": "...", "confidence": 0.0-1.0}
  4. Whitelist валидация — slug-ът трябва да е в списъка
  5. Threshold проверка — confidence >= prag (default 0.6)
  6. UPDATE questions SET topic_id = ... ако пасва

Защити:
  - Whitelist (защита от халюцинации на slug-ове)
  - Confidence threshold (default 0.6)
  - Audit log: data/classifier_log.jsonl (за всяка decision — въпрос, slug, confidence, time)
  - --dry-run опция за безопасен тест

Употреба:
    # Test на 10 въпроса (без запис)
    python3 topic_classifier.py --limit 10 --dry-run

    # Test на 10 (с запис)
    python3 topic_classifier.py --limit 10

    # Batch на всички orphans
    python3 topic_classifier.py

    # Custom threshold
    python3 topic_classifier.py --threshold 0.7
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


# ============================================================
# Config
# ============================================================

DEFAULT_THRESHOLD = 0.6
LOG_PATH = Path("data/classifier_log.jsonl")


# ============================================================
# System prompt
# ============================================================

SYSTEM_PROMPT = """Ти си експерт по класификация на учебни въпроси по Информационни технологии за български гимназиален курс.

Твоята задача: за даден ИТ въпрос, избери НАЙ-ПОДХОДЯЩИЯТ topic_slug от предоставения списък.

ПРАВИЛА:
- Отговаряй САМО с JSON обект, без markdown, без коментари, без обяснения.
- Формат: {"slug": "<topic_slug>", "confidence": <число 0.0-1.0>}
- slug ТРЯБВА да е точно копие от предоставения списък (case-sensitive, без шпации)
- confidence:
  * 0.9-1.0 — въпросът е директно за тази тема (споменава ключови термини)
  * 0.7-0.9 — въпросът е свързан с темата, но широко
  * 0.4-0.7 — въпросът само частично се отнася към темата
  * 0.0-0.4 — въпросът не е свързан със зададения списък
- Ако НИКОЙ от slug-овете не е добър match, върни slug "none" с confidence 0.0
"""


# ============================================================
# Build classification prompt
# ============================================================

def build_user_prompt(question_text: str, valid_topics: list) -> str:
    """
    valid_topics: list of (slug, title_bg, area_title)
    """
    topics_section = "\n".join(
        f"  - {slug}: {title} (област: {area})"
        for slug, title, area in valid_topics
    )
    
    return f"""Налични topic_slugs:
{topics_section}

Въпрос за класификация:
\"\"\"
{question_text}
\"\"\"

Отговор (само JSON):"""


# ============================================================
# Parse model response
# ============================================================

def parse_json_response(raw: str) -> dict | None:
    """
    Извлича JSON обект от raw отговор. Връща None ако не може.
    """
    if not raw:
        return None
    
    # Find first { and last }
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    
    json_str = raw[start:end + 1]
    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        return None
    
    if not isinstance(parsed, dict):
        return None
    if "slug" not in parsed:
        return None
    
    return parsed


# ============================================================
# Classifier core
# ============================================================

def fetch_orphan_questions(conn: sqlite3.Connection, limit: int | None = None) -> list:
    sql = """
        SELECT id, prompt, legacy_topic, source_exam, source_number
        FROM questions
        WHERE topic_id IS NULL
        ORDER BY id
    """
    if limit:
        sql += f" LIMIT {limit}"
    return conn.execute(sql).fetchall()


def fetch_valid_topics(conn: sqlite3.Connection) -> list:
    """Връща list of (slug, title, area_title)."""
    return [
        (row[0], row[1], row[2] or "—")
        for row in conn.execute("""
            SELECT t.topic_slug, t.title_bg, a.title_bg
            FROM curriculum_topics t
            LEFT JOIN curriculum_areas a ON a.id = t.area_id
            ORDER BY a.area_id, t.topic_slug
        """)
    ]


def classify_one(client: OllamaClient,
                 question_text: str,
                 valid_topics: list,
                 model: str,
                 valid_slugs: set) -> dict:
    """
    Класифицира един въпрос. Връща dict с:
      - slug (str | None)
      - confidence (float)
      - elapsed (float)
      - error (str | None)
      - raw_response (str)
    """
    user_prompt = build_user_prompt(question_text, valid_topics)
    
    try:
        result = client.chat(
            messages=[{"role": "user", "content": user_prompt}],
            model=model,
            system=SYSTEM_PROMPT,
            options={"temperature": 0.0},  # за detеrminистичност
        )
    except OllamaError as e:
        return {
            "slug": None,
            "confidence": 0.0,
            "elapsed": 0.0,
            "error": str(e),
            "raw_response": "",
        }
    
    raw = result["content"]
    parsed = parse_json_response(raw)
    
    if parsed is None:
        return {
            "slug": None,
            "confidence": 0.0,
            "elapsed": result["elapsed_seconds"],
            "error": "JSON parse failed",
            "raw_response": raw,
        }
    
    slug = parsed.get("slug")
    confidence = float(parsed.get("confidence", 0.0))
    
    # Whitelist валидация
    if slug == "none":
        return {
            "slug": None,
            "confidence": 0.0,
            "elapsed": result["elapsed_seconds"],
            "error": None,
            "raw_response": raw,
        }
    
    if slug not in valid_slugs:
        return {
            "slug": None,
            "confidence": 0.0,
            "elapsed": result["elapsed_seconds"],
            "error": f"Invalid slug returned: {slug!r}",
            "raw_response": raw,
        }
    
    return {
        "slug": slug,
        "confidence": confidence,
        "elapsed": result["elapsed_seconds"],
        "error": None,
        "raw_response": raw,
    }


def write_log_entry(log_path: Path, entry: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ============================================================
# Main
# ============================================================

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("data/questions.db"))
    p.add_argument("--model", default=DEFAULT_CHAT_MODEL)
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p.add_argument("--limit", type=int, default=None,
                   help="Класифицирай макс N въпроса (за тест)")
    p.add_argument("--dry-run", action="store_true",
                   help="Не записвай в DB, само логирай решенията")
    p.add_argument("--log-path", type=Path, default=LOG_PATH)
    args = p.parse_args()
    
    if not args.db.exists():
        print(f"❌ DB не съществува: {args.db}")
        sys.exit(1)
    
    print(f"🤖 Topic Classifier")
    print(f"   Model:     {args.model}")
    print(f"   Threshold: {args.threshold}")
    print(f"   DB:        {args.db}")
    print(f"   Log:       {args.log_path}")
    if args.dry_run:
        print(f"   ⚠️  DRY RUN — нищо няма да се запише в DB")
    
    client = OllamaClient(host=args.host)
    if not client.is_alive():
        print(f"\n❌ Ollama не работи. Стартирай: ollama serve")
        sys.exit(1)
    
    conn = sqlite3.connect(str(args.db))
    cur = conn.cursor()
    
    # Build slug → topic_id lookup
    slug_to_id = {
        row[0]: row[1]
        for row in cur.execute("SELECT topic_slug, id FROM curriculum_topics")
    }
    valid_topics = fetch_valid_topics(conn)
    valid_slugs = set(slug_to_id.keys())
    
    print(f"\n📋 Валидни topics: {len(valid_topics)}")
    
    orphans = fetch_orphan_questions(conn, limit=args.limit)
    print(f"🔍 Orphan въпроси за класификация: {len(orphans)}")
    
    if not orphans:
        print(f"✅ Няма orphan въпроси.")
        return
    
    print()
    
    stats = {
        "total": len(orphans),
        "classified_above_threshold": 0,
        "classified_below_threshold": 0,
        "no_match": 0,
        "errors": 0,
        "elapsed_total": 0.0,
        "by_slug": {},
    }
    
    t_start = time.monotonic()
    
    for i, (q_id, prompt, legacy, source, src_num) in enumerate(orphans, 1):
        prompt_short = (prompt or "").strip()[:200]
        
        result = classify_one(
            client, prompt, valid_topics, args.model, valid_slugs
        )
        
        log_entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "question_id": q_id,
            "source": f"{source}#{src_num}",
            "legacy_topic": legacy,
            "prompt_preview": prompt_short,
            "slug": result["slug"],
            "confidence": result["confidence"],
            "elapsed": result["elapsed"],
            "error": result["error"],
        }
        write_log_entry(args.log_path, log_entry)
        
        stats["elapsed_total"] += result["elapsed"]
        
        # Determine action
        if result["error"]:
            stats["errors"] += 1
            status = f"❌ ERROR: {result['error']}"
        elif result["slug"] is None:
            stats["no_match"] += 1
            status = f"⏭️  no match"
        elif result["confidence"] < args.threshold:
            stats["classified_below_threshold"] += 1
            status = (
                f"⚠️  LOW conf ({result['confidence']:.2f}): "
                f"{result['slug']}"
            )
        else:
            # Above threshold — record
            slug = result["slug"]
            stats["classified_above_threshold"] += 1
            stats["by_slug"].setdefault(slug, 0)
            stats["by_slug"][slug] += 1
            status = (
                f"✓ {slug} (conf {result['confidence']:.2f})"
            )
            
            if not args.dry_run:
                topic_id = slug_to_id[slug]
                cur.execute(
                    "UPDATE questions SET topic_id=? WHERE id=?",
                    (topic_id, q_id),
                )
        
        # Progress display
        progress = f"[{i:3}/{len(orphans)}]"
        print(f"{progress} Q#{q_id} ({legacy or '—'}): {status}")
        
        # Commit every 20 to avoid losing work
        if not args.dry_run and i % 20 == 0:
            conn.commit()
    
    if not args.dry_run:
        conn.commit()
    conn.close()
    
    # Summary
    total_elapsed = time.monotonic() - t_start
    print(f"\n{'=' * 60}")
    print(f"📊 SUMMARY")
    print(f"{'=' * 60}")
    print(f"   Общо: {stats['total']}")
    print(f"   ✓ Classified (≥{args.threshold}): {stats['classified_above_threshold']}")
    print(f"   ⚠️  Below threshold: {stats['classified_below_threshold']}")
    print(f"   ⏭️  No match (none): {stats['no_match']}")
    print(f"   ❌ Errors: {stats['errors']}")
    print()
    print(f"   ⏱  Total time: {total_elapsed:.1f}s")
    print(f"   ⏱  Avg per Q: {stats['elapsed_total'] / max(1, stats['total']):.1f}s")
    
    if stats["by_slug"]:
        print(f"\n📈 Distribution (above threshold):")
        for slug, count in sorted(stats["by_slug"].items(), key=lambda x: -x[1]):
            print(f"   {slug}: {count}")
    
    print(f"\n📋 Подробен лог: {args.log_path}")
    
    if args.dry_run:
        print(f"\n(dry-run: нищо не е записано в DB)")


if __name__ == "__main__":
    main()

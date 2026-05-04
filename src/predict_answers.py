"""
Predict-answers агент с BgGPT.

Попълва is_correct за multiple_choice въпроси, които нямат маркиран отговор.

Целеви въпроси:
  - имат ≥2 опции в multiple_choice_options
  - НИТО ЕДНА опция няма is_correct=1
  - по default филтрира само класнирани батчове (source_exam LIKE 'classroom_tests%')

Workflow:
  1. Чете въпрос + опциите
  2. Извиква BgGPT със system prompt: "Ти си учител по ИТ. Избери правилния отговор."
  3. BgGPT отговаря с JSON: {"answer": "А|Б|В|Г", "confidence": 0.0-1.0}
  4. Whitelist валидация: answer трябва да е едно от наличните option_letter за въпроса
  5. Threshold (default 0.6) — confidence < threshold → пропусни
  6. UPDATE multiple_choice_options SET is_correct=1 за избраното писмо
  7. quality_score остава NULL → въпросът все още изисква човешки преглед

Защити:
  - Whitelist на буквата (А/Б/В/Г само ако наистина съществуват за въпроса)
  - Confidence threshold
  - Idempotent: пропуска въпроси, които вече имат is_correct=1
  - Audit log: data/predict_answers_log.jsonl
  - --dry-run опция

Употреба:
    # Test на 10 въпроса (без запис)
    python3 predict_answers.py --limit 10 --dry-run

    # Реален run (всички pending)
    python3 predict_answers.py

    # Само определен source_exam
    python3 predict_answers.py --source-tag classroom_tests_alt_2026

    # Custom threshold (по-консервативно)
    python3 predict_answers.py --threshold 0.8
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# Path hack — same pattern as topic_classifier.py
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
DEFAULT_SOURCE_PATTERN = "classroom_tests%"
LOG_PATH = Path("data/predict_answers_log.jsonl")
VALID_LETTERS = {"А", "Б", "В", "Г", "Д"}  # Cyrillic А-Д


# ============================================================
# System prompt
# ============================================================

SYSTEM_PROMPT = """Ти си учител по Информационни технологии за български гимназиален курс. Имаш дълбоки познания по: хардуер, операционни системи, мрежи, бази данни, електронни таблици, MS Office, уеб технологии, алгоритми, лицензи, киберсигурност и облачни услуги.

Твоята задача: за даден тестов въпрос с няколко опции, избери правилния отговор.

ПРАВИЛА:
- Отговаряй САМО с JSON обект, без markdown, без коментари, без обяснения преди или след JSON-а.
- Формат: {"answer": "<буква>", "confidence": <число 0.0-1.0>}
- answer ТРЯБВА да е една от буквите, които са посочени във въпроса (А, Б, В, Г).
- confidence:
  * 0.9-1.0 — сигурен си в отговора, фактологично проверим
  * 0.7-0.9 — вероятно вярно, но има малка несигурност
  * 0.5-0.7 — възможно вярно, но има две правдоподобни опции
  * 0.0-0.5 — не си сигурен, въпросът е неясен или извън знанията ти
- Внимавай за отрицания ("НЕ се отнася", "не е характерна") — те обръщат логиката.
"""


# ============================================================
# Build prompt
# ============================================================

def build_user_prompt(question_prompt: str, options: list[tuple]) -> str:
    """
    options: list of (option_letter, option_text)
    """
    options_section = "\n".join(
        f"{letter}) {text}" for letter, text in options
    )
    return f"""Въпрос:
{question_prompt}

Опции:
{options_section}

Отговор (само JSON):"""


# ============================================================
# Parse model response
# ============================================================

def parse_json_response(raw: str) -> dict | None:
    if not raw:
        return None
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
    if "answer" not in parsed:
        return None
    return parsed


def normalize_letter(s: str) -> str:
    """Trim, take first character, upper. Convert Latin A→Cyrillic А."""
    s = (s or "").strip()
    if not s:
        return ""
    c = s[0].upper()
    if c == "A":
        c = "А"
    if c == "B":
        c = "Б"
    if c == "C":
        c = "В"
    if c == "D":
        c = "Г"
    return c


# ============================================================
# Question fetcher
# ============================================================

def fetch_pending_questions(conn: sqlite3.Connection,
                            source_pattern: str,
                            limit: int | None = None) -> list[tuple]:
    """
    Връща MC въпроси, които:
      - имат ≥2 опции
      - нямат маркиран is_correct=1 при никоя опция
      - source_exam отговаря на pattern (LIKE)
    """
    sql = """
        SELECT q.id, q.prompt, q.source_exam
        FROM questions q
        WHERE q.question_type = 'multiple_choice'
          AND q.source_exam LIKE ?
          AND EXISTS (
              SELECT 1 FROM multiple_choice_options o
              WHERE o.question_id = q.id
              GROUP BY o.question_id
              HAVING COUNT(*) >= 2
          )
          AND NOT EXISTS (
              SELECT 1 FROM multiple_choice_options o2
              WHERE o2.question_id = q.id AND o2.is_correct = 1
          )
        ORDER BY q.id
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, (source_pattern,)).fetchall()


def fetch_options(conn: sqlite3.Connection, question_id: int) -> list[tuple]:
    return conn.execute(
        "SELECT option_letter, option_text FROM multiple_choice_options "
        "WHERE question_id = ? ORDER BY option_letter",
        (question_id,)
    ).fetchall()


# ============================================================
# Logging
# ============================================================

def log_decision(entry: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ============================================================
# Core
# ============================================================

def predict_one(client: OllamaClient,
                qid: int,
                prompt: str,
                source_exam: str,
                options: list[tuple],
                threshold: float,
                model: str) -> dict:
    """
    Връща dict:
      {status: 'ok'|'low_conf'|'invalid'|'error',
       letter: str|None,
       confidence: float,
       elapsed: float,
       error: str|None}
    """
    valid_letters = {letter for letter, _ in options}
    user_prompt = build_user_prompt(prompt, options)

    t0 = time.monotonic()
    try:
        resp = client.chat(
            messages=[{"role": "user", "content": user_prompt}],
            model=model,
            system=SYSTEM_PROMPT,
        )
    except OllamaError as e:
        return {"status": "error", "letter": None, "confidence": 0.0,
                "elapsed": time.monotonic() - t0, "error": str(e)}

    elapsed = round(time.monotonic() - t0, 2)
    parsed = parse_json_response(resp["content"])

    if parsed is None:
        return {"status": "error", "letter": None, "confidence": 0.0,
                "elapsed": elapsed,
                "error": f"Невалиден JSON: {resp['content'][:120]!r}"}

    raw_answer = str(parsed.get("answer", ""))
    letter = normalize_letter(raw_answer)
    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0

    if letter not in valid_letters:
        return {"status": "invalid", "letter": letter, "confidence": conf,
                "elapsed": elapsed,
                "error": f"Letter {letter!r} not in options {sorted(valid_letters)}"}

    if conf < threshold:
        return {"status": "low_conf", "letter": letter, "confidence": conf,
                "elapsed": elapsed, "error": None}

    return {"status": "ok", "letter": letter, "confidence": conf,
            "elapsed": elapsed, "error": None}


def apply_answer(conn: sqlite3.Connection, qid: int, letter: str) -> None:
    """Set is_correct=1 on the chosen option (and 0 on all others, defensive)."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE multiple_choice_options SET is_correct = 0 WHERE question_id = ?",
        (qid,)
    )
    cur.execute(
        "UPDATE multiple_choice_options SET is_correct = 1 "
        "WHERE question_id = ? AND option_letter = ?",
        (qid, letter)
    )


# ============================================================
# Main
# ============================================================

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=Path("data/questions.db"))
    p.add_argument("--model", default=DEFAULT_CHAT_MODEL)
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                   help=f"Минимален confidence (default: {DEFAULT_THRESHOLD})")
    p.add_argument("--source-tag", default=DEFAULT_SOURCE_PATTERN,
                   help=f"source_exam pattern (LIKE; default: {DEFAULT_SOURCE_PATTERN})")
    p.add_argument("--limit", type=int, default=None,
                   help="Predict само първите N въпроса")
    p.add_argument("--dry-run", action="store_true",
                   help="Не записвай в DB, само логирай")
    p.add_argument("--log-path", type=Path, default=LOG_PATH)
    args = p.parse_args()

    print(f"🤖 Predict-Answers Agent")
    print(f"   Model:        {args.model}")
    print(f"   Threshold:    {args.threshold}")
    print(f"   DB:           {args.db}")
    print(f"   Source filter: source_exam LIKE '{args.source_tag}'")
    print(f"   Log:          {args.log_path}")
    if args.dry_run:
        print(f"   ⚠️  DRY RUN — нищо няма да се запише в DB")

    if not args.db.exists():
        print(f"❌ DB не намерен: {args.db}")
        sys.exit(1)

    client = OllamaClient(host=args.host)
    if not client.is_alive():
        print(f"❌ Ollama не е достъпен на {args.host}. Стартирай: ollama serve")
        sys.exit(1)

    conn = sqlite3.connect(str(args.db))
    pending = fetch_pending_questions(conn, args.source_tag, args.limit)

    print(f"\n🔍 Pending questions for prediction: {len(pending)}")
    if not pending:
        print("   (нищо за вършене)")
        conn.close()
        return

    counts = {"ok": 0, "low_conf": 0, "invalid": 0, "error": 0}
    t_total = time.monotonic()

    for i, (qid, prompt, src) in enumerate(pending, start=1):
        options = fetch_options(conn, qid)
        if len(options) < 2:
            print(f"[{i:>3}/{len(pending)}] Q#{qid}: ⏭️  <2 options, skip")
            continue

        result = predict_one(
            client, qid, prompt, src, options,
            args.threshold, args.model,
        )

        # Log
        log_decision({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "question_id": qid,
            "source_exam": src,
            "prompt_preview": prompt[:120],
            "letter": result["letter"],
            "confidence": result["confidence"],
            "status": result["status"],
            "elapsed": result["elapsed"],
            "error": result["error"],
        })

        # Console output
        status = result["status"]
        letter = result["letter"]
        conf = result["confidence"]

        if status == "ok":
            counts["ok"] += 1
            applied = " (dry-run)" if args.dry_run else ""
            print(f"[{i:>3}/{len(pending)}] Q#{qid}: ✓ {letter} (conf {conf:.2f}){applied}")
            if not args.dry_run:
                apply_answer(conn, qid, letter)
                if i % 25 == 0:
                    conn.commit()
        elif status == "low_conf":
            counts["low_conf"] += 1
            print(f"[{i:>3}/{len(pending)}] Q#{qid}: ⚠️  LOW conf ({conf:.2f}): {letter}")
        elif status == "invalid":
            counts["invalid"] += 1
            print(f"[{i:>3}/{len(pending)}] Q#{qid}: ❌ INVALID: {result['error']}")
        else:
            counts["error"] += 1
            print(f"[{i:>3}/{len(pending)}] Q#{qid}: ❌ ERROR: {result['error']}")

    if not args.dry_run:
        conn.commit()
    conn.close()

    total_elapsed = time.monotonic() - t_total

    print(f"\n{'=' * 60}")
    print(f"📊 SUMMARY")
    print(f"{'=' * 60}")
    print(f"   Общо: {len(pending)}")
    print(f"   ✓ Predicted (≥{args.threshold}): {counts['ok']}")
    print(f"   ⚠️  Below threshold:         {counts['low_conf']}")
    print(f"   ❌ Invalid letter:           {counts['invalid']}")
    print(f"   ❌ Errors:                   {counts['error']}")
    print(f"   ⏱  Total time: {total_elapsed:.1f}s")
    if pending:
        print(f"   ⏱  Avg per Q:  {total_elapsed / len(pending):.2f}s")
    print(f"\n📋 Подробен лог: {args.log_path}")
    if args.dry_run:
        print(f"\n(dry-run: нищо не е записано в DB)")
    else:
        print(f"\n💡 quality_score остава NULL — въпросите все още изискват преглед")
        print(f"   Push to vault for review:  python3 src/review_export.py")


if __name__ == "__main__":
    main()

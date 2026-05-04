"""
Импортва тестовите въпроси от обединения PDF (8.-12. клас, без отговори).

Тези въпроси отиват в DB с:
    is_ai_generated = 1      → невидими за worksheet generator-а
    quality_score   = NULL   → pending review
    legacy_topic    = 'classroom_test_unanswered'
    source_exam     = 'classroom_tests_8_12_2026'

Всички MC опции се записват с is_correct = 0 — няма отговори.
След ръчен преглед (set is_correct=1 на правилния и quality_score=1.0),
въпросите стават достъпни за worksheet-и.

Употреба:
    python3 src/import_classroom_tests.py --pdf path/to/merged.pdf
    python3 src/import_classroom_tests.py --pdf path/to/merged.pdf --dry-run
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pdfplumber


SOURCE_EXAM_DEFAULT = "classroom_tests_8_12_2026"
LEGACY_MARKER = "classroom_test_unanswered"

# Question header: starts with N. (digit dot space)
QUESTION_HEAD = re.compile(r"^\s*(\d{1,3})\.\s+(.+)$")

# Option header: "• Х) ..." or "Х) ..." where Х is А/Б/В/Г (Cyrillic) or A/Б/В/Г (mixed)
# Also handles the rare Latin "A" that appears on one page.
OPTION_HEAD = re.compile(
    r"^\s*[•\-]?\s*([АБВГAБВГ])\s*\)\s*(.+)$"
)

# Section/image markers — skip these lines
SKIP_LINE = re.compile(
    r"^\s*(Страница\s+\d+|Раздел\s+\d+|image_[a-z0-9_]+\.(jpg|png|jpeg)|"
    r"Първо\s+изображение|Второ\s+изображение|Формат:\s*към)",
    re.IGNORECASE,
)

# True/False detection: question with only А/Б options that are ДА/НЕ
TF_VALUES = {"да", "не", "вярно", "невярно"}


def normalize_letter(c: str) -> str:
    """Convert Latin A to Cyrillic А for consistency."""
    return "А" if c == "A" else c


def parse_pdf(pdf_path: Path) -> list[dict]:
    """Extract questions from the merged PDF.

    Returns a list of dicts:
        {prompt: str, type: 'multiple_choice'|'true_false',
         options: [(letter, text), ...]}
    """
    with pdfplumber.open(str(pdf_path)) as pdf:
        full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)

    questions: list[dict] = []
    current: dict | None = None
    last_option_letter: str | None = None

    def commit():
        if current is None:
            return
        opts = current["options"]
        if len(opts) < 2:
            return  # not enough options, skip
        # Detect true/false
        opt_text_lower = {t.strip().lower() for _, t in opts}
        if len(opts) == 2 and opt_text_lower.issubset(TF_VALUES):
            current["type"] = "true_false"
        questions.append(current)

    for raw_line in full_text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if SKIP_LINE.match(line):
            commit()
            current = None
            last_option_letter = None
            continue

        # Option line?
        m = OPTION_HEAD.match(line)
        if m and current is not None:
            letter = normalize_letter(m.group(1))
            text = m.group(2).strip()
            current["options"].append([letter, text])
            last_option_letter = letter
            continue

        # Question header?
        m = QUESTION_HEAD.match(line)
        if m:
            commit()
            current = {
                "prompt": m.group(2).strip(),
                "type": "multiple_choice",
                "options": [],
            }
            last_option_letter = None
            continue

        # Continuation line — append to last option (if we're in options)
        # or to the question prompt (if we're not)
        if current is not None:
            stripped = line.strip().lstrip("•").strip()
            if last_option_letter and current["options"]:
                current["options"][-1][1] += " " + stripped
            else:
                current["prompt"] += " " + stripped

    commit()
    return questions


def insert_into_db(db_path: Path, questions: list[dict], dry_run: bool,
                   source_exam: str):
    if dry_run:
        print(f"📂 (dry-run) Would insert {len(questions)} questions")
        for i, q in enumerate(questions[:5], 1):
            print(f"   {i}. [{q['type']}] {q['prompt'][:70]}")
            for letter, text in q["options"][:2]:
                print(f"      {letter}) {text[:60]}")
        if len(questions) > 5:
            print(f"   ... and {len(questions) - 5} more")
        return

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    inserted_q = 0
    inserted_opts = 0
    skipped = 0

    for q in questions:
        prompt = q["prompt"]
        # Skip if exact same prompt+source already exists (idempotency)
        existing = cur.execute(
            "SELECT id FROM questions WHERE source_exam = ? AND prompt = ?",
            (source_exam, prompt),
        ).fetchone()
        if existing:
            skipped += 1
            continue

        cur.execute(
            """
            INSERT INTO questions (
                exam_id, source_exam, source_number, subject, level, year,
                question_type, topic, topic_id, legacy_topic,
                difficulty, points, prompt,
                has_image, image_path, created_at,
                is_ai_generated, quality_score
            )
            VALUES (NULL, ?, NULL, 'IT', NULL, 2026, ?, NULL, NULL, ?,
                    NULL, 1, ?, 0, NULL, ?, 1, NULL)
            """,
            (source_exam, q["type"], LEGACY_MARKER, prompt,
             datetime.now().isoformat(timespec="seconds")),
        )
        qid = cur.lastrowid
        inserted_q += 1

        for letter, text in q["options"]:
            cur.execute(
                """
                INSERT INTO multiple_choice_options
                    (question_id, option_letter, option_text, is_correct)
                VALUES (?, ?, ?, 0)
                """,
                (qid, letter, text),
            )
            inserted_opts += 1

        if inserted_q % 50 == 0:
            conn.commit()

    conn.commit()
    conn.close()

    print(f"✅ Inserted: {inserted_q} questions, {inserted_opts} options")
    if skipped:
        print(f"⏭️  Skipped (already in DB): {skipped}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--pdf", type=Path, required=True)
    p.add_argument("--db", type=Path, default=Path("data/questions.db"))
    p.add_argument("--source-tag", default=SOURCE_EXAM_DEFAULT,
                   help=f"source_exam value (default: {SOURCE_EXAM_DEFAULT})")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.pdf.exists():
        print(f"❌ PDF not found: {args.pdf}")
        sys.exit(1)
    if not args.dry_run and not args.db.exists():
        print(f"❌ DB not found: {args.db}")
        sys.exit(1)

    print(f"📄 Парсване: {args.pdf.name}")
    questions = parse_pdf(args.pdf)
    print(f"📊 Извлечени {len(questions)} въпроса")

    types = {}
    for q in questions:
        types[q["type"]] = types.get(q["type"], 0) + 1
    print(f"   По тип: {types}")

    insert_into_db(args.db, questions, args.dry_run, args.source_tag)

    print(f"\n✅ Готово.")
    if not args.dry_run:
        print(f"   Източник: source_exam='{args.source_tag}'")
        print(f"   Statе: is_ai_generated=1, quality_score=NULL  "
              f"(невидими за worksheet-и до review)")
        print(f"   Преглед: SELECT * FROM questions "
              f"WHERE source_exam='{args.source_tag}'")
        print(f"   Изтриване: DELETE FROM questions "
              f"WHERE source_exam='{args.source_tag}'")


if __name__ == "__main__":
    main()

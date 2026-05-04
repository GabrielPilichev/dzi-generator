"""
Новият PDF parser CLI — заменя parse_v2.py.

Разлики:
  * Използва parsers registry за auto-detection на формата
  * Записва в новата schema_v2 (с exam_id и нови мета колони)
  * По-добри warnings & quality scores

Употреба:
    python3 -m parsers.parse_pdf data/reference/may_2025/exam.pdf
    python3 -m parsers.parse_pdf data/reference/may_2025/exam.pdf --db data/questions.db
    python3 -m parsers.parse_pdf data/reference/may_2025/exam.pdf --force-parser dzi_it_modern
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pdfplumber

from .registry import detect_format, ALL_PARSERS, get_parser_for
from .base import ParsedExam


def extract_text_from_pdf(pdf_path: Path) -> str:
    """Извлича целия текст от PDF (стр. по стр. с newline между тях)."""
    with pdfplumber.open(str(pdf_path)) as pdf:
        return "\n".join(
            (page.extract_text() or "") for page in pdf.pages
        )


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def write_to_db(parsed: ParsedExam, pdf_path: Path, db_path: Path,
                source_url: str = "") -> dict:
    """Записва ParsedExam в DB. Връща статистика."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    
    sha = file_sha256(pdf_path)
    
    # Проверка за дубликат: същ sha256?
    existing = cur.execute("SELECT id FROM exams WHERE sha256=?", (sha,)).fetchone()
    if existing:
        print(f"⚠️  Тази PDF вече е парсвана (exam_id={existing[0]}). Пропускам.")
        conn.close()
        return {"skipped": True, "exam_id": existing[0]}
    
    # Insert exam record
    cur.execute("""
        INSERT INTO exams (
            subject, level, year, session, variant, format_version,
            source_url, source_file, sha256, parser_version, parsed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        parsed.subject, parsed.level, parsed.year, parsed.session,
        parsed.variant, parsed.format_version,
        source_url, str(pdf_path), sha,
        parsed.parser_used,
        datetime.now().isoformat(timespec="seconds"),
    ))
    exam_id = cur.lastrowid
    
    # Build legacy source_exam string (за обратна съвместимост)
    legacy_source = pdf_path.stem  # fallback
    if parsed.year and parsed.session:
        legacy_source = f"{parsed.session[:3]}_{parsed.year}"
        if parsed.variant > 1:
            legacy_source += f"_v{parsed.variant}"
    
    inserted = {"mc": 0, "fi": 0, "skipped": 0, "warnings": 0}
    
    for q in parsed.questions:
        try:
            cur.execute("""
                INSERT INTO questions (
                    exam_id, source_exam, source_number,
                    subject, level, year,
                    question_type, topic, points, prompt,
                    has_image, image_path, quality_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                exam_id, legacy_source, q.number,
                parsed.subject, parsed.level, parsed.year,
                q.question_type, q.topic, q.points, q.prompt,
                int(q.has_image), q.image_path, q.quality_score,
            ))
            qid = cur.lastrowid
            
            for opt in q.options:
                cur.execute("""
                    INSERT INTO multiple_choice_options
                        (question_id, option_letter, option_text, is_correct)
                    VALUES (?, ?, ?, ?)
                """, (qid, opt.letter, opt.text, int(opt.is_correct)))
            
            for sub in q.subquestions:
                cur.execute("""
                    INSERT INTO fill_in_subquestions
                        (question_id, subquestion_number, subquestion_text,
                         correct_answer, points)
                    VALUES (?, ?, ?, ?, ?)
                """, (qid, sub.number, sub.text, sub.correct_answer, 1))
            
            if q.question_type == "multiple_choice":
                inserted["mc"] += 1
            elif q.question_type == "fill_in":
                inserted["fi"] += 1
            
            if q.warnings:
                inserted["warnings"] += 1
        except sqlite3.IntegrityError as e:
            print(f"   ⚠️  Skip Q#{q.number}: {e}")
            inserted["skipped"] += 1
            continue
    
    conn.commit()
    conn.close()
    
    return {"skipped": False, "exam_id": exam_id, **inserted}


def main() -> None:
    parser = argparse.ArgumentParser(description="Парсва ДЗИ PDF в DB (v2)")
    parser.add_argument("pdf", type=Path, nargs="?", default=None, help="Път до PDF")
    parser.add_argument("--db", default=Path("data/questions.db"), type=Path)
    parser.add_argument("--force-parser", default=None,
                        help="Директно използвай parser по име (напр. 'DziItModernParser')")
    parser.add_argument("--source-url", default="",
                        help="Откъде е свален PDF-ът")
    parser.add_argument("--list-parsers", action="store_true",
                        help="Показва всички регистрирани parsers и излиза")
    args = parser.parse_args()
    
    if args.list_parsers:
        print("Регистрирани parsers:")
        for cls in ALL_PARSERS:
            print(f"  • {cls.__name__}: {cls.SUBJECT}/{cls.LEVEL}/{cls.FORMAT_VERSION}")
            print(f"    Range: {cls.SUPPORTED_RANGE}")
        return
    
    if args.pdf is None:
        parser.error('PDF е задължителен (или използвай --list-parsers)')
    if not args.pdf.exists():
        print(f"❌ PDF не намерен: {args.pdf}")
        sys.exit(1)
    if not args.db.exists():
        print(f"❌ DB не намерен: {args.db}. Пусни първо migrate_to_v2.py.")
        sys.exit(1)
    
    print(f"📄 Чета PDF: {args.pdf}")
    text = extract_text_from_pdf(args.pdf)
    print(f"   Извлечени {len(text):,} chars")
    
    # Choose parser
    if args.force_parser:
        chosen = None
        for cls in ALL_PARSERS:
            if cls.__name__ == args.force_parser:
                chosen = cls()
                break
        if chosen is None:
            print(f"❌ Parser '{args.force_parser}' не съществува")
            sys.exit(1)
        print(f"🔧 Forced parser: {chosen.__class__.__name__}")
    else:
        print(f"🔍 Auto-detect формат...")
        # Show all scores for debug
        for cls in ALL_PARSERS:
            instance = cls()
            score = instance.detect(text)
            print(f"   {cls.__name__}: {score:.2f}")
        chosen = detect_format(text)
        if chosen is None:
            print(f"❌ Никой parser не разпозна формата с достатъчна увереност (>=0.30)")
            sys.exit(1)
        print(f"✓ Избран: {chosen.__class__.__name__}")
    
    print(f"📋 Парсвам...")
    result = chosen.parse(text)
    
    print(f"\n📊 Резултат:")
    print(f"   Subject: {result.subject}")
    print(f"   Level: {result.level}")
    print(f"   Year/Session/Variant: {result.year}/{result.session}/{result.variant}")
    print(f"   Format: {result.format_version}")
    print(f"   Confidence: {result.confidence:.2f}")
    print(f"   Brой въпроси: {len(result.questions)}")
    
    mc = [q for q in result.questions if q.question_type == "multiple_choice"]
    fi = [q for q in result.questions if q.question_type == "fill_in"]
    
    mc_with_answer = sum(1 for q in mc if any(o.is_correct for o in q.options))
    fi_with_subs = sum(1 for q in fi if q.subquestions)
    
    print(f"   Multiple choice: {len(mc)} (с отговор: {mc_with_answer})")
    print(f"   Fill-in: {len(fi)} (с подотговори: {fi_with_subs})")
    
    warned = [q for q in result.questions if q.warnings]
    if warned:
        print(f"   ⚠️  {len(warned)} въпроса с warnings")
        for q in warned[:5]:
            print(f"      Q{q.number}: {q.warnings}")
    
    print(f"\n💾 Записвам в {args.db}...")
    stats = write_to_db(result, args.pdf, args.db, args.source_url)
    
    if stats.get("skipped"):
        print(f"⚠️  Skipped (вече парсвана; exam_id={stats['exam_id']})")
    else:
        print(f"✅ Готово. exam_id={stats['exam_id']}, "
              f"MC={stats['mc']}, FI={stats['fi']}, skipped={stats['skipped']}")


if __name__ == "__main__":
    main()

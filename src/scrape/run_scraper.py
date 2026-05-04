"""
CLI оркестратор на скрейпърите.

Употреба:
    # Zamatura, само listing (dry-run):
    python3 -m scrape.run_scraper --source zamatura --subject informatika_it --dry-run

    # МОН, действително сваляне + parse:
    python3 -m scrape.run_scraper --source mon --subject informatika_it \\
        --db ../data/questions.db --parse-after-download

    # Multiple subjects:
    python3 -m scrape.run_scraper --source mon \\
        --subject informatika_it --subject matematika

Структура на изхода:
    data/reference/{subject}/{YYYY-MM-DD}/<filename>.pdf
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .http import PoliteFetcher
from .zamatura import ZamaturaScraper, SUBJECT_SLUGS as ZAMATURA_SLUGS
from . import mon


def main() -> None:
    p = argparse.ArgumentParser(description="ДЗИ scraper")
    p.add_argument("--source", choices=["zamatura", "mon"], default="zamatura")
    p.add_argument("--subject", action="append", default=[],
                   help="Предмет (повторим). Известни subjects зависят от --source.")
    p.add_argument("--reference-dir", default=Path("data/reference"), type=Path)
    p.add_argument("--db", default=None, type=Path,
                   help="Path до questions.db (за scrape_log)")
    p.add_argument("--dry-run", action="store_true",
                   help="Само изброи, не сваляй")
    p.add_argument("--force", action="store_true",
                   help="Сваляй пак, дори ако вече е сваляно")
    p.add_argument("--parse-after-download", action="store_true",
                   help="Авто-парсвай новите PDFs след сваляне (изисква --db)")
    p.add_argument("--min-interval", type=float, default=1.0,
                   help="Минимум секунди между заявките (default: 1.0)")
    args = p.parse_args()

    # Pick scraper class + valid subject set
    if args.source == "zamatura":
        valid_slugs = list(ZAMATURA_SLUGS)
        ScraperCls = ZamaturaScraper
        source_label = "zamatura.eu"
    elif args.source == "mon":
        valid_slugs = list(mon.SUBJECT_SLUGS)
        ScraperCls = mon.MonScraper
        source_label = "mon.bg"
    else:
        print(f"❌ Непознат source: {args.source}")
        sys.exit(1)

    if not args.subject:
        print(f"❌ Поне един --subject е задължителен.")
        print(f"   Известни за {args.source}: {', '.join(valid_slugs)}")
        sys.exit(1)

    fetcher = PoliteFetcher(min_interval=args.min_interval)
    scraper = ScraperCls(
        fetcher=fetcher,
        reference_dir=args.reference_dir,
        db_path=args.db,
    )

    all_downloaded: list = []

    for subject in args.subject:
        print(f"\n{'=' * 60}")
        print(f"📚 Предмет: {subject}  (source: {source_label})")
        print(f"{'=' * 60}")

        exams = scraper.list_subject_pdfs(subject)
        print(f"\n📋 Намерени общо {len(exams)} PDF-а:")
        for e in exams:
            print(f"   {e.date_iso} — {e.filename}")

        if args.dry_run:
            continue

        print(f"\n⬇️  Сваляне...")
        for e in exams:
            path = scraper.download_exam(e, force=args.force)
            if path is not None:
                all_downloaded.append((e, path))

    if args.dry_run:
        print(f"\n(dry-run: нищо не е свалено)")
        return

    print(f"\n✅ Готово. Свалени {len(all_downloaded)} нови файла.")

    if args.parse_after_download:
        if args.db is None or not args.db.exists():
            print("⚠️  --parse-after-download изисква --db. Пропускам парсването.")
            return

        print(f"\n📋 Auto-парсване...")
        try:
            from parsers.parse_pdf import extract_text_from_pdf, write_to_db
            from parsers.registry import detect_format
        except ImportError as e:
            print(f"❌ Не мога да импортна parsers: {e}")
            print("   (трябва скриптът да се пуска от src/, не от scrape/)")
            return

        for exam, pdf_path in all_downloaded:
            # Skip answer keys — те не са въпросници
            if "otgovori" in pdf_path.name.lower():
                print(f"\n📄 {pdf_path.name}  ⏭️  answer key, skip parse")
                continue

            print(f"\n📄 {pdf_path.name}")
            try:
                text = extract_text_from_pdf(pdf_path)
                parser = detect_format(text)
                if parser is None:
                    print(f"   ⚠️  Няма подходящ parser. Skip.")
                    continue
                print(f"   Parser: {parser.__class__.__name__}")
                parsed = parser.parse(text)
                stats = write_to_db(parsed, pdf_path, args.db, exam.pdf_url)
                if stats.get("skipped"):
                    print(f"   ⏭️  Вече парсван (exam_id={stats['exam_id']})")
                else:
                    print(f"   ✓ MC={stats['mc']}, FI={stats['fi']}")
            except Exception as e:
                print(f"   ❌ Parse error: {e}")


if __name__ == "__main__":
    main()

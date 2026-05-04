"""
Scraper за МОН (mon.bg).

МОН вече не блокира realistic UA-та (2026 update). Изпитни материали по
предмет са на:
    https://www.mon.bg/obshto-obrazovanie/darzhavni-zrelostni-izpiti-dzi/
        izpitni-materiali-za-dzi-po-predmeti/{subject-slug}/

PDF линковете са:
    /nfs/YYYY/MM/filename.pdf            (relative)
    https://www.mon.bg/nfs/YYYY/MM/file  (absolute)

Използваме същия ScrapedExam dataclass като ZamaturaScraper, така че
останалата pipeline (download, log, parse-after-download) е идентична.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .http import PoliteFetcher
from .zamatura import ScrapedExam


BASE = "https://www.mon.bg"

# Mapping: вътрешно име → URL slug на mon.bg
# (някои slug-ове са непотвърдени — добавяй според нуждата след проверка)
SUBJECT_SLUGS = {
    "informatika_it": "informatsionni-tehnologii",
    # TODO: confirm and add more (informatika, bel, matematika, ...)
}


# Pattern за PDF линкове — хваща и relative, и absolute
PDF_HREF_PATTERN = re.compile(
    r'href="((?:https?://www\.mon\.bg)?/nfs/(\d{4})/(\d{2})/[^"]+\.pdf)"',
    re.IGNORECASE,
)

# Дата от filename: _DD.MM.YYYY или _DDMMYYYY
FILENAME_DATE_DOTTED = re.compile(r"_(\d{2})\.(\d{2})\.(\d{4})")
FILENAME_DATE_PACKED = re.compile(r"_(\d{2})(\d{2})(\d{4})(?:[-_.]|$)")


def _parse_day_from_filename(filename: str) -> Optional[int]:
    """Опитва да извлече DD от filename. None ако не може."""
    m = FILENAME_DATE_DOTTED.search(filename)
    if m:
        return int(m.group(1))
    m = FILENAME_DATE_PACKED.search(filename)
    if m:
        return int(m.group(1))
    return None


def _is_answer_key(url: str) -> bool:
    return "otgovori" in url.lower()


class MonScraper:
    """Открива и сваля изпити от mon.bg."""

    def __init__(self, fetcher: Optional[PoliteFetcher] = None,
                 reference_dir: Path = Path("data/reference"),
                 db_path: Optional[Path] = None):
        self.fetcher = fetcher or PoliteFetcher()
        self.reference_dir = reference_dir
        self.db_path = db_path

    # ------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------

    def list_subject_pdfs(self, subject: str) -> list:
        slug = SUBJECT_SLUGS.get(subject)
        if not slug:
            print(f"❌ Непознат subject за МОН: {subject}. "
                  f"Известни: {list(SUBJECT_SLUGS)}")
            return []

        listing_url = (
            f"{BASE}/obshto-obrazovanie/darzhavni-zrelostni-izpiti-dzi/"
            f"izpitni-materiali-za-dzi-po-predmeti/{slug}/"
        )
        print(f"🔍 Чета listing: {listing_url}")

        try:
            html = self.fetcher.get_text(listing_url)
        except Exception as e:
            print(f"   ❌ Не мога да заредя listing-а: {e}")
            return []

        seen: set = set()
        results: list = []

        for m in PDF_HREF_PATTERN.finditer(html):
            href = m.group(1)
            year = int(m.group(2))
            month = int(m.group(3))

            pdf_url = (BASE + href) if href.startswith("/") else href
            if pdf_url in seen:
                continue
            seen.add(pdf_url)

            filename = Path(pdf_url).name
            day = _parse_day_from_filename(filename) or 1

            results.append(ScrapedExam(
                url=listing_url,
                pdf_url=pdf_url,
                subject=subject,
                year=year,
                month=month,
                day=day,
            ))

        results.sort(key=lambda e: (e.year, e.month, e.day))
        print(f"   Намерени {len(results)} PDF-а")
        return results

    # ------------------------------------------------------------
    # DB log helpers
    # ------------------------------------------------------------

    def is_already_scraped(self, url: str) -> bool:
        if self.db_path is None or not self.db_path.exists():
            return False
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute(
                "SELECT status FROM scrape_log WHERE url=? AND status='success'",
                (url,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def log_scrape(self, source: str, url: str, status: str,
                   file_path: str = "", notes: str = "") -> None:
        if self.db_path is None or not self.db_path.exists():
            return
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("""
                INSERT OR REPLACE INTO scrape_log
                    (source, url, fetched_at, status, file_path, notes)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (source, url, datetime.now().isoformat(timespec="seconds"),
                  status, file_path, notes))
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------
    # Download
    # ------------------------------------------------------------

    def download_exam(self, exam: ScrapedExam, force: bool = False) -> Optional[Path]:
        if not force and self.is_already_scraped(exam.pdf_url):
            print(f"   ⏭️  Вече сваляно: {exam.filename}")
            return None

        target_dir = self.reference_dir / exam.subject / exam.date_iso
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / exam.filename

        is_key = _is_answer_key(exam.pdf_url)
        notes = "answer_key" if is_key else ""

        if target_path.exists() and not force:
            print(f"   ⏭️  Файл съществува: {target_path}")
            self.log_scrape("mon.bg", exam.pdf_url, "skipped_dup",
                            str(target_path), notes or "file already on disk")
            return target_path

        print(f"   ⬇️  {exam.pdf_url}")
        try:
            size = self.fetcher.download_to(exam.pdf_url, target_path)
            tag = "  [ANSWER KEY]" if is_key else ""
            print(f"      → {target_path} ({size:,} bytes){tag}")
            self.log_scrape("mon.bg", exam.pdf_url, "success",
                            str(target_path), notes)
            return target_path
        except Exception as e:
            print(f"      ❌ {e}")
            self.log_scrape("mon.bg", exam.pdf_url, "http_error",
                            "", str(e))
            return None


# Запазено за обратна съвместимост (старият stub извикваше това)
URL_DZI_BY_YEAR = (
    f"{BASE}/obshto-obrazovanie/darzhavni-zrelostni-izpiti-dzi/"
    "izpitni-materiali-za-dzi-po-godini/"
)
URL_DZI_BY_SUBJECT = (
    f"{BASE}/obshto-obrazovanie/darzhavni-zrelostni-izpiti-dzi/"
    "izpitni-materiali-za-dzi-po-predmeti/"
)
URL_DZI_IT = URL_DZI_BY_SUBJECT + "informatsionni-tehnologii/"


def print_manual_instructions() -> None:
    """Стара функция — МОН вече работи автоматично."""
    print("\n" + "=" * 60)
    print("ℹ️  МОН вече позволява автоматизирани заявки.")
    print("=" * 60)
    print()
    print("Автоматично сваляне:")
    print("  python3 -m scrape.run_scraper --source mon "
          "--subject informatika_it \\")
    print("      --db ../data/questions.db --parse-after-download")
    print()

"""
Scraper за zamatura.eu.

Сайтът има стабилна структура:
  * Listing pages: https://zamatura.eu/maturi-12klas/predmeti/{slug}/maturi/
  * Конкретни матури: https://zamatura.eu/maturi-12klas/predmeti/{slug}/maturi/matura-{slug}-{date}/
  * PDF файлове: https://zamatura.eu/wp-content/uploads/{YYYY}/{MM}/matura-{slug}-{date}.pdf

Slug-ове за предмети: informacionni-tehnologii, informatika, bel, matematika,
                     angliyski-ezik, istoriya, biologiya, himiya, fizika, geografiya

Този module открива всички PDF линкове в дадена listing page и ги сваля
в data/reference/{subject}/{filename}.pdf, ако вече не са свалени.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .http import PoliteFetcher


BASE = "https://zamatura.eu"

# Mapping: вътрешно име → URL slug на zamatura
SUBJECT_SLUGS = {
    "informatika_it": "informacionni-tehnologii",
    "informatika": "informatika",
    "bel": "bel",
    "matematika": "matematika",
    "angliyski": "angliyski-ezik",
    "istoriya": "istoriya",
    "biologiya": "biologiya",
    "himiya": "himiya",
    "fizika": "fizika",
    "geografiya": "geografiya",
}

# Pattern за PDF линкове в HTML на zamatura
PDF_HREF_PATTERN = re.compile(
    r'href="(https://zamatura\.eu/wp-content/uploads/[^"]+\.pdf)"',
    re.IGNORECASE,
)

# Pattern за извличане на дата от URL: matura-{slug}-{DD}-{MM}-{YYYY}.pdf
DATE_PATTERN = re.compile(r"matura-[a-z\-]+-(\d{2})-(\d{2})-(\d{4})\.pdf")


@dataclass
class ScrapedExam:
    url: str
    pdf_url: str
    subject: str          # internal name
    year: int
    month: int
    day: int
    
    @property
    def date_iso(self) -> str:
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
    
    @property
    def session(self) -> str:
        # Май = main session, август = re-take, юни = NVO
        if self.month == 5:
            return "may"
        if self.month == 8:
            return "august"
        if self.month == 6:
            return "june"
        if self.month == 9:
            return "september"
        return f"month_{self.month}"
    
    @property
    def filename(self) -> str:
        return Path(self.pdf_url).name


class ZamaturaScraper:
    """Открива и сваля изпити от zamatura.eu."""
    
    def __init__(self, fetcher: Optional[PoliteFetcher] = None,
                 reference_dir: Path = Path("data/reference"),
                 db_path: Optional[Path] = None):
        self.fetcher = fetcher or PoliteFetcher()
        self.reference_dir = reference_dir
        self.db_path = db_path  # за scrape_log
    
    # ------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------
    
    def list_subject_pdfs(self, subject: str) -> list:
        """
        Връща списък с ScrapedExam за всички PDF-и публикувани за този предмет.
        
        zamatura.eu няма официален индекс — обхождаме listing page-а и
        отделните матури за да намерим всички PDF линкове.
        """
        slug = SUBJECT_SLUGS.get(subject)
        if not slug:
            print(f"❌ Непознат subject: {subject}. Известни: {list(SUBJECT_SLUGS)}")
            return []
        
        listing_url = f"{BASE}/maturi-12klas/predmeti/{slug}/"
        print(f"🔍 Чета listing: {listing_url}")
        
        try:
            html = self.fetcher.get_text(listing_url)
        except Exception as e:
            print(f"   ❌ Не мога да заредя listing-а: {e}")
            return []
        
        # Намираме линкове към индивидуални матури
        # Pattern: /maturi-12klas/predmeti/{slug}/maturi/matura-{slug}-{date}/
        sub_pages_pattern = re.compile(
            rf'href="(/maturi-12klas/predmeti/{re.escape(slug)}/maturi/matura-[^"]+/)"'
        )
        sub_paths = sorted(set(sub_pages_pattern.findall(html)))
        print(f"   Намерени {len(sub_paths)} индивидуални матури")
        
        results: list = []
        for path in sub_paths:
            full_url = BASE + path
            try:
                page_html = self.fetcher.get_text(full_url)
            except Exception as e:
                print(f"   ⚠️  Skip {full_url}: {e}")
                continue
            
            pdf_urls = sorted(set(PDF_HREF_PATTERN.findall(page_html)))
            for pdf_url in pdf_urls:
                date_m = DATE_PATTERN.search(pdf_url)
                if not date_m:
                    continue
                day, month, year = (int(x) for x in date_m.groups())
                results.append(ScrapedExam(
                    url=full_url,
                    pdf_url=pdf_url,
                    subject=subject,
                    year=year,
                    month=month,
                    day=day,
                ))
        
        return results
    
    # ------------------------------------------------------------
    # Download
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
    
    def download_exam(self, exam: ScrapedExam, force: bool = False) -> Optional[Path]:
        """Сваля PDF в reference/{subject}/{date}/exam.pdf. Връща path или None."""
        if not force and self.is_already_scraped(exam.pdf_url):
            print(f"   ⏭️  Вече сваляно: {exam.filename}")
            return None
        
        target_dir = self.reference_dir / exam.subject / exam.date_iso
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / exam.filename
        
        if target_path.exists() and not force:
            print(f"   ⏭️  Файл съществува: {target_path}")
            self.log_scrape("zamatura.eu", exam.pdf_url, "skipped_dup",
                            str(target_path), "file already on disk")
            return target_path
        
        print(f"   ⬇️  {exam.pdf_url}")
        try:
            size = self.fetcher.download_to(exam.pdf_url, target_path)
            print(f"      → {target_path} ({size:,} bytes)")
            self.log_scrape("zamatura.eu", exam.pdf_url, "success",
                            str(target_path))
            return target_path
        except Exception as e:
            print(f"      ❌ {e}")
            self.log_scrape("zamatura.eu", exam.pdf_url, "http_error",
                            "", str(e))
            return None

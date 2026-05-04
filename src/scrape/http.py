"""
HTTP utilities за scraping.

Цел: вежлив fetcher с разумни defaults:
  * Rate limiting (1 заявка / секунда по подразбиране)
  * Retry с exponential backoff
  * User-Agent който идентифицира бота
  * Timeout
  * Auto-decode на UTF-8

Зависимости: requests (pip3 install requests)
Ако requests не е инсталирана, скриптът дава ясно съобщение.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("❌ Липсва пакет 'requests'. Инсталирай го с:\n   pip3 install requests")
    raise SystemExit(1)


USER_AGENT = (
    "DziGeneratorBot/0.1 (educational research; "
    "contact: gabriel.pilichev[at]121sou.bg)"
)

DEFAULT_TIMEOUT = 30
DEFAULT_MIN_INTERVAL = 1.0  # секунди между заявките към един и същи host


class PoliteFetcher:
    """
    HTTP fetcher с rate limiting per host. Запазва session между заявки.
    
    Употреба:
        f = PoliteFetcher()
        text = f.get_text("https://zamatura.eu/...")
        f.download_to("https://.../file.pdf", Path("data/reference/.../exam.pdf"))
    """
    
    def __init__(self, min_interval: float = DEFAULT_MIN_INTERVAL,
                 timeout: int = DEFAULT_TIMEOUT,
                 max_retries: int = 3,
                 user_agent: str = USER_AGENT):
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "bg,en;q=0.5",
        })
        self._last_fetch: dict = {}  # host -> timestamp
    
    def _wait_for_host(self, url: str) -> None:
        from urllib.parse import urlparse
        host = urlparse(url).netloc
        last = self._last_fetch.get(host, 0)
        elapsed = time.monotonic() - last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_fetch[host] = time.monotonic()
    
    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        attempt = 0
        last_err: Optional[Exception] = None
        while attempt < self.max_retries:
            attempt += 1
            self._wait_for_host(url)
            try:
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_err = Exception(f"HTTP {resp.status_code}")
                    sleep_s = 2 ** attempt
                    print(f"   ⏳ {resp.status_code} от {url}, изчаквам {sleep_s}s")
                    time.sleep(sleep_s)
                    continue
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                last_err = e
                sleep_s = 2 ** attempt
                print(f"   ⚠️  {e} (опит {attempt}/{self.max_retries}), изчаквам {sleep_s}s")
                time.sleep(sleep_s)
        raise RuntimeError(f"Failed to fetch {url}: {last_err}")
    
    def get_text(self, url: str) -> str:
        resp = self._request("GET", url)
        resp.encoding = resp.encoding or "utf-8"
        return resp.text
    
    def download_to(self, url: str, dest: Path) -> int:
        """Сваля в dest. Връща броя байтове."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = self._request("GET", url, stream=True)
        total = 0
        with dest.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
        return total

import os
import re
import datetime as dt
from typing import List, Dict, Iterable, Optional

import requests
from bs4 import BeautifulSoup
from db import upsert_draw

BASE = "https://australia.national-lottery.com"
PAST_RESULTS = f"{BASE}/powerball/past-results"
ARCHIVE_FMT = f"{BASE}/powerball/results-archive-{{year}}"

UA = {
    "User-Agent": "Mozilla/5.0 (compatible; AU-Powerball-Stats/1.0; +https://github.com/barnard704344/au-powerball-stats)",
    "Accept-Language": "en-AU,en;q=0.9"
}

YEAR_START = int(os.environ.get("YEARS_START", "2018"))

def _try_parse_date(s: str) -> Optional[str]:
    """Try several common date formats seen on results pages.
    Returns ISO date string (YYYY-MM-DD) or None.
    """
    s = s.strip()
    for fmt in (
        "%d %B, %Y",  # 12 October, 2024
        "%d %B %Y",   # 12 October 2024
        "%d %b, %Y",  # 12 Oct, 2024
        "%d %b %Y",   # 12 Oct 2024
        "%A %d %B %Y",  # Thursday 12 October 2024
        "%a %d %b %Y",  # Thu 12 Oct 2024
    ):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_rows(soup: BeautifulSoup, source_url: str) -> List[Dict]:
    results = []
    # Look for anchors whose text mentions a draw number; date may follow in various formats
    for a in soup.find_all("a"):
        t = a.get_text(" ", strip=True)
        m_no = re.search(r"\bDraw\s+(\d+)\b", t, flags=re.IGNORECASE)
        if not m_no:
            continue

        draw_no = int(m_no.group(1))

        # Try to find a date token near the anchor text first
        m_date = re.search(r"(\d{1,2}\s+\w+\s*,?\s*\d{4})", t)
        draw_date: Optional[str] = _try_parse_date(m_date.group(1)) if m_date else None

        # If not found on the anchor, look in the nearest container text
        if not draw_date:
            container = a.find_parent()
            if container:
                text = container.get_text(" ", strip=True)
                m_date2 = re.search(r"(\d{1,2}\s+\w+\s*,?\s*\d{4})", text)
                if m_date2:
                    draw_date = _try_parse_date(m_date2.group(1))

        if not draw_date:
            # If no date could be parsed, skip this entry
            continue

        # Numbers are typically in the next <ul> as 8 <li> items (7 main + 1 PB)
        nums: List[int] = []
        ul = a.find_next("ul")
        if ul:
            # Extract integers from list items; ignore any non-numeric bits
            for li in ul.find_all("li"):
                mnum = re.search(r"\b(\d{1,2})\b", li.get_text(strip=True))
                if mnum:
                    nums.append(int(mnum.group(1)))
        if len(nums) < 8:
            # Fallback: extract plausible numbers from the surrounding text
            block = (a.find_parent() or a).get_text(" ", strip=True)
            cand = [int(s) for s in re.findall(r"\b\d{1,2}\b", block)]
            # Look for the first window of 8 numbers; prefer where last is <= 20
            found = False
            for i in range(0, max(0, len(cand) - 7)):
                window = cand[i:i+8]
                if len(window) == 8:
                    if window[-1] <= 20 and all(1 <= n <= 35 for n in window[:-1]):
                        nums = window
                        found = True
                        break
            if not found and len(cand) >= 8:
                nums = cand[:8]

        if len(nums) != 8:
            continue

        results.append({
            "draw_no": draw_no,
            "draw_date": draw_date,
            "nums": nums[:7],
            "pb": nums[7],
            "source_url": source_url
        })
    return results

def fetch_year(year: int) -> List[Dict]:
    url = ARCHIVE_FMT.format(year=year)
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return _parse_rows(BeautifulSoup(r.text, "lxml"), url)

def fetch_latest_six_months() -> List[Dict]:
    r = requests.get(PAST_RESULTS, headers=UA, timeout=30)
    r.raise_for_status()
    return _parse_rows(BeautifulSoup(r.text, "lxml"), PAST_RESULTS)

def sync_all() -> Dict[str, int]:
    """
    Pull YEAR_START..current and the 'past results' page, upsert into DB.
    Returns {"upserted": N}
    """
    added_or_updated = 0
    seen = set()

    def upsert_many(items: Iterable[Dict]):
        nonlocal added_or_updated
        for d in items:
            if d["draw_no"] in seen:
                continue
            seen.add(d["draw_no"])
            upsert_draw(d)
            added_or_updated += 1

    year_now = dt.date.today().year
    for y in range(YEAR_START, year_now + 1):
        upsert_many(fetch_year(y))
    upsert_many(fetch_latest_six_months())

    return {"upserted": added_or_updated}

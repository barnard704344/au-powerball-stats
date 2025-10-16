import os
import re
import time
import datetime as dt
from typing import List, Dict, Iterable, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from db import upsert_draw

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------
BASE = "https://australia.national-lottery.com"
PAST_RESULTS = f"{BASE}/powerball/past-results"
ARCHIVE_FMT = f"{BASE}/powerball/results-archive-{{year}}"

YEAR_START = int(os.environ.get("YEARS_START", "2018"))

UA = {
    "User-Agent": "Mozilla/5.0 (compatible; AU-Powerball-Stats/1.1; +https://github.com/barnard704344/au-powerball-stats)",
    "Accept-Language": "en-AU,en;q=0.9",
    "Cache-Control": "no-cache",
}

TIMEOUT = 30
RETRIES = 3
RETRY_BACKOFF = 3  # seconds

# ---------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------
def _try_parse_date(s: str) -> Optional[str]:
    s = s.strip()
    for fmt in (
        "%d %B, %Y",   # 12 October, 2024
        "%d %B %Y",    # 12 October 2024
        "%d %b, %Y",   # 12 Oct, 2024
        "%d %b %Y",    # 12 Oct 2024
        "%A %d %B %Y", # Thursday 12 October 2024
        "%a %d %b %Y", # Thu 12 Oct 2024
    ):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None

# ---------------------------------------------------------------------
# HTTP with retries
# ---------------------------------------------------------------------
def _fetch_html(url: str) -> str:
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
            else:
                raise
    raise last_err  # type: ignore

# ---------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------
DRAW_HEAD_RE = re.compile(r"Draw\s+(\d+)\s+(.*)$", re.IGNORECASE)
DATE_RE = re.compile(r"(\d{1,2}\s+\w+\s*,?\s*\d{4})")
INT2_RE = re.compile(r"\b(\d{1,2})\b")

def _safe_ints(xs) -> List[int]:
    out = []
    for x in xs:
        try:
            out.append(int(str(x)))
        except Exception:
            pass
    return out

def _extract_8_numbers_from_ul(ul) -> Optional[List[int]]:
    if not ul:
        return None
    lis = [li.get_text(strip=True) for li in ul.find_all("li")]
    nums = _safe_ints(INT2_RE.findall(" ".join(lis)))
    return nums if len(nums) == 8 else None

def _extract_8_numbers_from_nearby_text(node_text_after_header: str) -> Optional[List[int]]:
    """
    Given a short slice of text AFTER the 'Draw #### date' header, try to
    find an 8-number window that looks like 7 mains (≤35) + PB (≤20).
    """
    cand = _safe_ints(INT2_RE.findall(node_text_after_header))
    if not cand:
        return None
    # prefer a plausible window
    for i in range(0, max(0, len(cand) - 7)):
        window = cand[i:i+8]
        if len(window) == 8 and window[-1] <= 20 and all(1 <= n <= 35 for n in window[:-1]):
            return window
    # fallback: first 8 if nothing matched (still better than nothing)
    return cand[:8] if len(cand) >= 8 else None

def _parse_block(draw_text: str, anchor_or_tag) -> Optional[Dict]:
    """
    Parse one 'Draw #### <date>' block using:
      - date from the same text
      - numbers from the next <ul> or a small slice of nearby text
    """
    m_no = re.search(r"\bDraw\s+(\d+)\b", draw_text, flags=re.IGNORECASE)
    if not m_no:
        return None
    draw_no = int(m_no.group(1))

    m_date = DATE_RE.search(draw_text)
    draw_date = _try_parse_date(m_date.group(1)) if m_date else None
    if not draw_date:
        return None

    # Prefer an immediate next <ul> full of 8 items
    ul = anchor_or_tag.find_next("ul") if hasattr(anchor_or_tag, "find_next") else None
    nums = _extract_8_numbers_from_ul(ul)

    if not nums:
        # Limit the scan to a short slice AFTER the date match to avoid year/prize table noise
        full = anchor_or_tag.get_text(" ", strip=True) if hasattr(anchor_or_tag, "get_text") else str(anchor_or_tag)
        m_head = DATE_RE.search(full)
        after = full[m_head.end():m_head.end()+200] if m_head else full[:200]
        nums = _extract_8_numbers_from_nearby_text(after)

    if not nums or len(nums) != 8:
        return None

    return {
        "draw_no": draw_no,
        "draw_date": draw_date,
        "nums": nums[:7],
        "pb": nums[7],
        "source_url": "",  # filled by caller
    }

def _parse_rows_from_soup(soup: BeautifulSoup, source_url: str) -> List[Dict]:
    out: List[Dict] = []

    # Strategy 1: <a> tags whose text includes "Draw ####"
    for a in soup.find_all("a"):
        t = a.get_text(" ", strip=True)
        if "draw" not in t.lower():
            continue
        item = _parse_block(t, a)
        if item:
            item["source_url"] = source_url
            out.append(item)

    # Strategy 2: headings/paragraphs with "Draw ####" not in <a>
    # (Only if Strategy 1 found nothing on this page; avoids duplicates.)
    if not out:
        for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "div", "span"]):
            txt = tag.get_text(" ", strip=True)
            if "draw" not in txt.lower():
                continue
            if not re.search(r"\bDraw\s+\d+\b", txt, flags=re.IGNORECASE):
                continue
            item = _parse_block(txt, tag)
            if item:
                item["source_url"] = source_url
                out.append(item)

    # De-dup in case both strategies find the same draw
    seen = set()
    unique: List[Dict] = []
    for d in out:
        if d["draw_no"] in seen:
            continue
        seen.add(d["draw_no"])
        unique.append(d)
    return unique

# ---------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------
def fetch_year(year: int) -> List[Dict]:
    url = ARCHIVE_FMT.format(year=year)
    html = _fetch_html(url)
    soup = BeautifulSoup(html, "lxml")
    items = _parse_rows_from_soup(soup, url)
    print(f"[scraper] {url} -> {len(items)} rows")
    return items

def fetch_latest_six_months() -> List[Dict]:
    html = _fetch_html(PAST_RESULTS)
    soup = BeautifulSoup(html, "lxml")
    items = _parse_rows_from_soup(soup, PAST_RESULTS)
    print(f"[scraper] {PAST_RESULTS} -> {len(items)} rows")
    return items

def sync_all() -> Dict[str, int]:
    """
    Pull YEAR_START..current and the 'past results' page, upsert into DB.
    Returns diagnostic counts.
    """
    added_or_updated = 0
    seen = set()
    problems: List[Tuple[str, str]] = []

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
        try:
            upsert_many(fetch_year(y))
        except Exception as e:
            problems.append((ARCHIVE_FMT.format(year=y), str(e)))

    try:
        upsert_many(fetch_latest_six_months())
    except Exception as e:
        problems.append((PAST_RESULTS, str(e)))

    print(f"[scraper] upserted total: {added_or_updated}, problems: {len(problems)}")
    for url, err in problems:
        print(f"[scraper] ERROR {url}: {err}")

    # Return shape is backward-compatible with your caller
    return {"upserted": added_or_updated}

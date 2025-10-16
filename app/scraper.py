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
    # Heavier, desktop UA helps bypass some lightweight filters/CDNs
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "en-AU,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": BASE + "/powerball/"
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
            # Some CDNs gzip without header; requests handles it but we keep text only
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
DRAW_NO_RE = re.compile(r"\bDraw\s+(\d+)\b", re.IGNORECASE)
DATE_RE    = re.compile(r"(\d{1,2}\s+\w+\s*,?\s*\d{4})")
INT2_RE    = re.compile(r"\b(\d{1,2})\b")

def _safe_ints(xs) -> List[int]:
    out = []
    for x in xs:
        try:
            out.append(int(str(x)))
        except Exception:
            pass
    return out

def _extract_8_from_ul(ul) -> Optional[List[int]]:
    if not ul:
        return None
    lis = [li.get_text(strip=True) for li in ul.find_all("li")]
    nums = _safe_ints(INT2_RE.findall(" ".join(lis)))
    return nums if len(nums) == 8 else None

def _extract_8_from_siblings(tag) -> Optional[List[int]]:
    """
    Walk a few next siblings looking for a list of 8 numbers (7 mains ≤35 + PB ≤20).
    Stops quickly to avoid scooping prize tables.
    """
    hops = 0
    cur = tag
    while cur and hops < 6:
        cur = cur.find_next_sibling()
        hops += 1
        if not cur:
            break
        # try list first
        nums = _extract_8_from_ul(cur.find("ul"))
        if nums:
            return nums
        # fallback: scrape numbery text from this sibling
        txt = cur.get_text(" ", strip=True)
        cand = _safe_ints(INT2_RE.findall(txt))
        for i in range(0, max(0, len(cand) - 7)):
            window = cand[i:i+8]
            if len(window) == 8 and window[-1] <= 20 and all(1 <= n <= 35 for n in window[:-1]):
                return window
    return None

def _extract_8_from_near(tag) -> Optional[List[int]]:
    """
    Very local text scan limited to ~200 chars AFTER where the date appears.
    """
    full = tag.get_text(" ", strip=True)
    m = DATE_RE.search(full)
    after = full[m.end():m.end()+220] if m else full[:220]
    cand = _safe_ints(INT2_RE.findall(after))
    for i in range(0, max(0, len(cand) - 7)):
        window = cand[i:i+8]
        if len(window) == 8 and window[-1] <= 20 and all(1 <= n <= 35 for n in window[:-1]):
            return window
    return cand[:8] if len(cand) >= 8 else None

def _parse_block(text: str, origin_tag, source_url: str) -> Optional[Dict]:
    """
    Given a block of text that contains 'Draw ####' and a parseable date,
    extract numbers from the next <ul>, siblings, or a constrained text slice.
    """
    m_no = DRAW_NO_RE.search(text)
    if not m_no:
        return None
    draw_no = int(m_no.group(1))

    m_date = DATE_RE.search(text)
    draw_date = _try_parse_date(m_date.group(1)) if m_date else None
    if not draw_date:
        return None

    # Try: next UL under/after the tag
    ul = origin_tag.find_next("ul") if hasattr(origin_tag, "find_next") else None
    nums = _extract_8_from_ul(ul)

    # Try: a few next siblings
    if not nums:
        nums = _extract_8_from_siblings(origin_tag)

    # Try: constrained nearby text
    if not nums:
        nums = _extract_8_from_near(origin_tag)

    if not nums or len(nums) != 8:
        return None

    return {
        "draw_no": draw_no,
        "draw_date": draw_date,
        "nums": nums[:7],
        "pb": nums[7],
        "source_url": source_url
    }

def _parse_page(html: str, source_url: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    items: List[Dict] = []

    # Strategy 1: <a> elements that mention "Draw ####"
    for a in soup.find_all("a"):
        t = a.get_text(" ", strip=True)
        if "draw" not in t.lower():
            continue
        if not DRAW_NO_RE.search(t):
            continue
        row = _parse_block(t, a, source_url)
        if row:
            items.append(row)

    # Strategy 2: headings/paragraphs/divs with "Draw ####" (not inside <a>)
    if not items:
        for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "div", "span", "li"]):
            txt = tag.get_text(" ", strip=True)
            if "draw" not in txt.lower():
                continue
            if not DRAW_NO_RE.search(txt):
                continue
            row = _parse_block(txt, tag, source_url)
            if row:
                items.append(row)

    # De-dup by draw_no
    seen = set()
    uniq: List[Dict] = []
    for d in items:
        if d["draw_no"] in seen:
            continue
        seen.add(d["draw_no"])
        uniq.append(d)
    return uniq

# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------
def fetch_year(year: int) -> List[Dict]:
    url = ARCHIVE_FMT.format(year=year)
    html = _fetch_html(url)
    items = _parse_page(html, url)
    print(f"[scraper] {url} -> {len(items)} rows")
    return items

def fetch_latest_six_months() -> List[Dict]:
    html = _fetch_html(PAST_RESULTS)
    items = _parse_page(html, PAST_RESULTS)
    print(f"[scraper] {PAST_RESULTS} -> {len(items)} rows")
    return items

def sync_all() -> Dict[str, int]:
    """
    Pull YEAR_START..current plus 'past results' page, upsert into DB.
    Returns a simple counter for the caller.
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

    return {"upserted": added_or_updated}

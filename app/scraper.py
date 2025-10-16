import os
import re
import time
import json
import datetime as dt
from typing import List, Dict, Iterable, Optional, Tuple, Any
import logging
log = logging.getLogger("scraper")


import requests
from bs4 import BeautifulSoup
from db import upsert_draw

# =============================================================================
# Config
# =============================================================================
# Primary (JSON) source — used by thelott.com web.
API_BASE = "https://data.api.thelott.com/sales/vmax/web/data/lotto/"
API_HEADERS = {
    # A common desktop browser UA helps dodge trivial bot screens
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.thelott.com",
    "Referer": "https://www.thelott.com/",
}

# HTML fallback (secondary source)
HTML_BASE = "https://australia.national-lottery.com"
PAST_RESULTS = f"{HTML_BASE}/powerball/past-results"
ARCHIVE_FMT = f"{HTML_BASE}/powerball/results-archive-{{year}}"

# First year to backfill
YEAR_START = int(os.environ.get("YEARS_START", "2018"))

TIMEOUT = 30
RETRIES = 3
RETRY_BACKOFF = 3  # seconds


# =============================================================================
# Utilities
# =============================================================================
def _try_parse_date(s: str) -> Optional[str]:
    s = s.strip()
    for fmt in (
        "%Y-%m-%d",     # API dates often already ISO
        "%d %B, %Y",    # 12 October, 2024
        "%d %B %Y",     # 12 October 2024
        "%d %b, %Y",    # 12 Oct, 2024
        "%d %b %Y",     # 12 Oct 2024
        "%A %d %B %Y",  # Thursday 12 October 2024
        "%a %d %b %Y",  # Thu 12 Oct 2024
    ):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _http_get(url: str, headers: Dict[str, str], timeout: int = TIMEOUT) -> str:
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    if last_err:
        raise last_err
    raise RuntimeError("GET failed")


def _http_post_json(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    POST to The Lott JSON endpoints with retries. Returns parsed JSON.
    """
    url = API_BASE + path.lstrip("/")
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.post(url, headers=API_HEADERS, json=payload, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    if last_err:
        raise last_err
    raise RuntimeError("POST failed")


def _api_fetch_productdraws(max_items: int = 600) -> List[Dict]:
    """
    Zero-dependency GET endpoint that returns a mixed list of recent draws.
    We filter Powerball and normalise to our draw dicts.
    """
    url = "https://data.api.thelott.com/sales/vmax/web/data/lotto/productdraws"
    # Use same headers we use elsewhere (desktop UA helps)
    text = _http_get(url, headers=API_HEADERS)
    try:
        payload = json.loads(text)
    except Exception as e:
        log.warning("[API] productdraws JSON parse error: %s", e)
        return []
    raw = payload.get("Draws") or payload.get("draws") or []
    items: List[Dict] = []
    for obj in raw:
        d = _to_draw_dict_from_api(obj)
        if d and str(obj.get("ProductId", "")).lower() == "powerball":
            items.append(d)
            if len(items) >= max_items:
                break
    # sort ascending by draw number & de-dup
    items.sort(key=lambda x: x["draw_no"])
    dedup = []
    seen = set()
    for d in items:
        if d["draw_no"] in seen:
            continue
        seen.add(d["draw_no"])
        dedup.append(d)
    log.info("[API] productdraws -> powerball rows=%d (pre-filter)", len(dedup))
    return dedup


# =============================================================================
# JSON (The Lott) parsing
# =============================================================================
def _to_draw_dict_from_api(obj: Dict[str, Any]) -> Optional[Dict]:
    """
    Convert a single API result object into our canonical draw dict:
      { draw_no, draw_date, nums[7], pb, source_url }
    The API schema can vary slightly, so we defend against multiple field names.
    """
    # Identify Powerball-only rows
    product = (obj.get("ProductId") or obj.get("ProductType") or obj.get("Product")) or ""
    if str(product).lower() != "powerball":
        return None

    draw_no = obj.get("DrawNumber") or obj.get("DrawNo") or obj.get("DrawId")
    draw_date_raw = obj.get("DrawDate") or obj.get("DrawDateTime") or obj.get("OpenDate")
    date_iso = None
    if isinstance(draw_date_raw, str):
        # Many API returns are already 'YYYY-MM-DDT...' — extract the date
        date_iso = _try_parse_date(draw_date_raw.split("T")[0])
    if not date_iso and isinstance(draw_date_raw, str):
        date_iso = _try_parse_date(draw_date_raw)

    # Numbers (prefer explicit arrays)
    main = (obj.get("PrimaryNumbers") or obj.get("WinningNumbers") or obj.get("Numbers") or [])
    # Normalize if the API gave a "1,2,3,..." string
    if isinstance(main, str):
        main = [int(x) for x in re.findall(r"\d{1,2}", main)]
    # Some responses include all numbers together; PB might be elsewhere
    pb = obj.get("PowerballNumber") or obj.get("Powerball") or obj.get("SupplementaryNumbers")
    if isinstance(pb, list) and pb:
        # Some formats put PB as single-element list
        pb = pb[0]
    if pb is None:
        # Last-resort guess: treat 8th as PB if exactly 8 numbers present
        if isinstance(main, list) and len(main) == 8:
            pb = main[-1]
            main = main[:7]

    # Final shape checks
    if not (isinstance(main, list) and len(main) == 7 and isinstance(pb, (int, str))):
        return None
    try:
        main = [int(x) for x in main]
        pb = int(pb)
    except Exception:
        return None

    if not (1 <= pb <= 20 and all(1 <= n <= 35 for n in main)):
        # Out-of-range; probably mis-parsed
        return None

    if not draw_no or not date_iso:
        return None

    return {
        "draw_no": int(draw_no),
        "draw_date": date_iso,
        "nums": main,
        "pb": pb,
        "source_url": "thelott-api",
    }


def _api_fetch_range_by_date(date_from: str, date_to: str) -> List[Dict]:
    """
    Try the 'historyresults' shape first; fall back to 'drawresults' if needed.
    """
    items: List[Dict] = []

    # Attempt 1: historyresults by date range
    try:
        payload = {
            "CompanyId": "GoldenCasket",  # common public value; NT/NSW variants also work
            "MinDrawDate": date_from,
            "MaxDrawDate": date_to,
            "ProductFilter": ["Powerball"],
        }
        data = _http_post_json("historyresults", payload)
        raw = data.get("DrawResults") or data.get("Results") or data
        if isinstance(raw, list):
            for obj in raw:
                d = _to_draw_dict_from_api(obj)
                if d:
                    items.append(d)
    except Exception as e:
        print(f"[scraper] API historyresults error: {e}")

    # Attempt 2: drawresults by (very wide) draw number range
    if not items:
        try:
            payload = {
                "CompanyId": "GoldenCasket",
                "ProductFilter": ["Powerball"],
                "DrawNumberFrom": 1,
                "DrawNumberTo": 999999,
            }
            data = _http_post_json("drawresults", payload)
            raw = data.get("DrawResults") or data.get("Results") or data
            if isinstance(raw, list):
                for obj in raw:
                    d = _to_draw_dict_from_api(obj)
                    if d:
                        items.append(d)
        except Exception as e:
            print(f"[scraper] API drawresults error: {e}")

    # Attempt 3: latestresults + paginate back by year (as last resort)
    if not items:
        try:
            payload = {
                "CompanyId": "GoldenCasket",
                "MaxDrawCountPerProduct": 120,              # about ~2 years
                "OptionalProductFilter": ["Powerball"],
            }
            data = _http_post_json("latestresults", payload)
            raw = data.get("DrawResults") or data.get("Results") or data
            if isinstance(raw, list):
                for obj in raw:
                    d = _to_draw_dict_from_api(obj)
                    if d:
                        items.append(d)
        except Exception as e:
            print(f"[scraper] API latestresults error: {e}")

    print(f"[scraper] API range {date_from}..{date_to} -> rows={len(items)}")
    # De-dup on draw_no (prefer latest instance)
    seen = set()
    deduped: List[Dict] = []
    for d in sorted(items, key=lambda x: x["draw_no"]):
        if d["draw_no"] in seen:
            continue
        seen.add(d["draw_no"])
        deduped.append(d)
    return deduped


def _api_fetch_year(year: int) -> List[Dict]:
    start = f"{year}-01-01"
    end = f"{year}-12-31"
    return _api_fetch_range_by_date(start, end)


def _api_fetch_latest_6m(today: Optional[dt.date] = None) -> List[Dict]:
    if today is None:
        today = dt.date.today()
    start = (today - dt.timedelta(days=183)).isoformat()
    end = today.isoformat()
    return _api_fetch_range_by_date(start, end)


# =============================================================================
# HTML fallback parsing (national-lottery.com)
# =============================================================================
DRAW_NO_RE = re.compile(r"\bDraw\s+(\d+)\b", re.IGNORECASE)
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


def _extract_8_from_ul(ul) -> Optional[List[int]]:
    if not ul:
        return None
    lis = [li.get_text(strip=True) for li in ul.find_all("li")]
    nums = _safe_ints(INT2_RE.findall(" ".join(lis)))
    return nums if len(nums) == 8 else None


def _extract_8_from_near(tag) -> Optional[List[int]]:
    full = tag.get_text(" ", strip=True)
    m = DATE_RE.search(full)
    after = full[m.end():m.end()+220] if m else full[:220]
    cand = _safe_ints(INT2_RE.findall(after))
    for i in range(0, max(0, len(cand) - 7)):
        window = cand[i:i+8]
        if len(window) == 8 and window[-1] <= 20 and all(1 <= n <= 35 for n in window[:-1]):
            return window
    return cand[:8] if len(cand) >= 8 else None


def _parse_block(text: str, tag, source_url: str) -> Optional[Dict]:
    m_no = DRAW_NO_RE.search(text)
    if not m_no:
        return None
    draw_no = int(m_no.group(1))

    m_date = DATE_RE.search(text)
    draw_date = _try_parse_date(m_date.group(1)) if m_date else None
    if not draw_date:
        return None

    ul = tag.find_next("ul") if hasattr(tag, "find_next") else None
    nums = _extract_8_from_ul(ul) or _extract_8_from_near(tag)
    if not nums or len(nums) != 8:
        return None

    main, pb = nums[:7], nums[7]
    if not (1 <= pb <= 20 and all(1 <= n <= 35 for n in main)):
        return None

    return {
        "draw_no": draw_no,
        "draw_date": draw_date,
        "nums": main,
        "pb": pb,
        "source_url": source_url,
    }


def _parse_html_page(html: str, source_url: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    items: List[Dict] = []

    # Strategy 1: anchors that include "Draw ####"
    for a in soup.find_all("a"):
        t = a.get_text(" ", strip=True)
        if "draw" not in t.lower():
            continue
        if not DRAW_NO_RE.search(t):
            continue
        row = _parse_block(t, a, source_url)
        if row:
            items.append(row)

    # Strategy 2: headers/paragraphs with "Draw ####" if anchors found nothing
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

    # De-dup
    seen = set()
    uniq: List[Dict] = []
    for d in items:
        if d["draw_no"] in seen:
            continue
        seen.add(d["draw_no"])
        uniq.append(d)
    return uniq


def fetch_year(year: int) -> List[Dict]:
    """
    Prefer: GET /productdraws (filter to the given year)
    Fallbacks: POST history/draw results, then HTML archive.
    """
    try:
        rows = _api_fetch_productdraws(max_items=1200)  # plenty to cover a full year
        yrows = [d for d in rows if d["draw_date"].startswith(f"{year}-")]
        if yrows:
            log.info("[API] productdraws year %s -> rows=%d", year, len(yrows))
            return yrows
        log.info("[API] productdraws year %s -> 0; trying POST endpoints", year)
    except Exception as e:
        log.warning("[API] productdraws year %s error: %s; trying POST endpoints", year, e)

    # Old POST path (might be blocked from your box)
    try:
        rows = _api_fetch_year(year)
        if rows:
            log.info("[API] history/drawresults year %s -> rows=%d", year, len(rows))
            return rows
        log.info("[API] POST year %s -> 0; falling back to HTML", year)
    except Exception as e:
        log.warning("[API] POST year %s error: %s; falling back to HTML", year, e)

    # HTML fallback
    try:
        rows = _html_fetch_year(year)
        log.info("[HTML] year %s -> rows=%d", year, len(rows))
        return rows
    except Exception as e:
        log.error("[HTML] year %s error: %s", year, e)
        return []




def _html_fetch_latest_6m() -> List[Dict]:
     """
    Prefer the GET /productdraws feed then trim to last ~6 months.
    Fallbacks: POST latest/history, then HTML 'past results'.
    """
    # 1) GET productdraws (fastest + most reliable)
    try:
        all_rows = _api_fetch_productdraws(max_items=600)
        if all_rows:
            # keep last ~183 days
            today = dt.date.today()
            cutoff = (today - dt.timedelta(days=183)).isoformat()
            rows = [d for d in all_rows if d["draw_date"] >= cutoff]
            log.info("[API] productdraws latest6m -> rows=%d (cutoff %s)", len(rows), cutoff)
            return rows
        log.info("[API] productdraws latest6m -> 0; trying POST endpoints")
    except Exception as e:
        log.warning("[API] productdraws latest6m error: %s; trying POST endpoints", e)

    # 2) POST endpoints
    try:
        rows = _api_fetch_latest_6m()
        if rows:
            log.info("[API] latestresults/history latest6m -> rows=%d", len(rows))
            return rows
        log.info("[API] latestresults/history latest6m -> 0; falling back to HTML")
    except Exception as e:
        log.warning("[API] latestresults/history latest6m error: %s; falling back to HTML", e)

    # 3) HTML fallback
    try:
        rows = _html_fetch_latest_6m()
        log.info("[HTML] latest6m -> rows=%d", len(rows))
        return rows
    except Exception as e:
        log.error("[HTML] latest6m error: %s", e)
        return []


# =============================================================================
# Public API expected by app.py
# =============================================================================
def fetch_year(year: int) -> List[Dict]:
    """
    Prefer JSON API; fall back to HTML.
    """
    rows = _api_fetch_year(year)
    if rows:
        return rows
    print(f"[scraper] API returned 0 for year {year}; falling back to HTML…")
    return _html_fetch_year(year)


def fetch_latest_six_months() -> List[Dict]:
    rows = _api_fetch_latest_6m()
    if rows:
        return rows
    print("[scraper] API returned 0 for latest; falling back to HTML…")
    return _html_fetch_latest_6m()


def sync_all() -> Dict[str, int]:
    """
    Pull YEAR_START..current + latest 6 months and upsert.
    """
    added_or_updated = 0
    seen = set()
    problems: List[Tuple[str, str]] = []

    def upsert_many(items: Iterable[Dict], label: str):
        nonlocal added_or_updated
        log.info("Upserting %d items from %s", len(items), label)
        for d in items:
            if d["draw_no"] in seen:
                continue
            seen.add(d["draw_no"])
            upsert_draw(d)
            added_or_updated += 1

    year_now = dt.date.today().year
    log.info("sync_all: starting YEAR_START=%s..%s", YEAR_START, year_now)

    for y in range(YEAR_START, year_now + 1):
        try:
            rows = fetch_year(y)
            upsert_many(rows, f"year {y}")
        except Exception as e:
            problems.append((f"year:{y}", str(e)))
            log.exception("sync_all year %s failed: %s", y, e)

    try:
        latest_rows = fetch_latest_six_months()
        upsert_many(latest_rows, "latest6m")
    except Exception as e:
        problems.append(("latest6m", str(e)))
        log.exception("sync_all latest6m failed: %s", e)

    log.info("sync_all: upserted=%d, problems=%d", added_or_updated, len(problems))
    for where, err in problems:
        log.error("sync_all ERROR %s: %s", where, err)

    return {"upserted": added_or_updated}

import os
import re
import datetime as dt
from typing import List, Dict, Iterable

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

def _parse_rows(soup: BeautifulSoup, source_url: str) -> List[Dict]:
    results = []
    for a in soup.find_all("a"):
        t = a.text.strip()
        if not t.startswith("Draw "):
            continue
        m = re.match(r"Draw\s+(\d+)\s+(\d{1,2}\s+\w+,\s+\d{4})", t)
        if not m:
            continue
        draw_no = int(m.group(1))
        draw_date = dt.datetime.strptime(m.group(2), "%d %B, %Y").date().isoformat()

        ul = a.find_next("ul")
        if ul:
            nums = [int(li.text.strip()) for li in ul.find_all("li")]
        else:
            block = a.find_parent().get_text(separator=" ").strip()
            nums = list(map(int, re.findall(r"\b\d+\b", block)))[0:8]

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
    r = requests.get(ARCHIVE_FMT.format(year=year), headers=UA, timeout=30)
    r.raise_for_status()
    return _parse_rows(BeautifulSoup(r.text, "lxml"), ARCHIVE_FMT.format(year=year))

def fetch_latest_six_months() -> List[Dict]:
    r = requests.get(PAST_RESULTS, headers=UA, timeout=30)
    r.raise_for_status()
    return _parse_rows(BeautifulSoup(r.text, "lxml"), PAST_RESULTS)

def sync_all() -> Dict[str, int]:
    added_or_updated = 0
    seen = set()

    def upsert_many(items: Iterable[Dict]):
        nonlocal added_or_updated, seen
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

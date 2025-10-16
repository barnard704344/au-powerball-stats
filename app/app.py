import os
import sys
import threading
import logging
import itertools
from collections import Counter

from flask import Flask, jsonify, render_template, request
from apscheduler.schedulers.background import BackgroundScheduler
from pytz import timezone

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(asctime)s %(name)s: %(message)s')
log = logging.getLogger("app")

# Ensure local module imports work under gunicorn
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from db import init_db, get_draws, get_frequencies  # Powerball DB helpers
from scraper import sync_all, fetch_year, fetch_latest_six_months, debug_probe

app = Flask(__name__, template_folder="templates", static_folder="static")

# Initialize DB schema
init_db()

TZ = os.environ.get("TZ", "Australia/Adelaide")
LOCAL_TZ = timezone(TZ)
UPDATE_CRON = os.environ.get("UPDATE_CRON", "*/15 * * * *")

scheduler = BackgroundScheduler(timezone=LOCAL_TZ)

def job_sync():
    try:
        log.info("Scheduled sync: starting…")
        result = sync_all()
        log.info("Scheduled sync complete: %s", result)
    except Exception as e:
        log.exception("Scheduled sync failed: %s", e)

def schedule_job():
    from apscheduler.triggers.cron import CronTrigger
    trigger = CronTrigger.from_crontab(UPDATE_CRON, timezone=LOCAL_TZ)
    scheduler.add_job(job_sync, trigger, id="sync_job", replace_existing=True)
    scheduler.start()
    log.info("Scheduler started: %s", UPDATE_CRON)

def initial_sync_async():
    def _run():
        try:
            log.info("Initial sync: starting…")
            res = sync_all()
            log.info("Initial sync: %s", res)
        except Exception as e:
            log.exception("Initial sync failed: %s", e)
    threading.Thread(target=_run, daemon=True).start()

initial_sync_async()
schedule_job()

# ----------------------- Computation helpers -----------------------
def compute_group_stats(window: int = 100, ks: tuple[int, ...] = (2, 3, 4), limit: int = 20):
    """
    Compute:
      - Most common main-number groups (pairs/triples/quads) over the last `window` draws.
      - Most common Powerball numbers over the last `window` draws.
    Returns dict suitable for JSON or templating.
    """
    draws = get_draws(limit=window)  # newest first
    mains_list = []
    powerballs = []
    for d in draws:
        nums = list(d.get("nums", []))
        if len(nums) >= 7:
            mains_list.append(sorted(nums[:7]))
        pb = d.get("pb")
        if isinstance(pb, int):
            powerballs.append(pb)

    # PB frequencies
    pb_counts = Counter(powerballs)

    # Group counts for each k
    group_counts: dict[int, Counter] = {}
    for k in ks:
        c = Counter()
        for nums in mains_list:
            for combo in itertools.combinations(nums, k):  # combos already sorted/unique
                c[combo] += 1
        group_counts[k] = c

    # Top-N selections
    top_groups = {}
    for k, counter in group_counts.items():
        top_groups[k] = [{"combo": list(combo), "count": cnt} for combo, cnt in counter.most_common(limit)]

    top_pbs = [{"pb": n, "count": cnt} for n, cnt in pb_counts.most_common(limit)]

    return {
        "window": window,
        "ks": list(ks),
        "limit": limit,
        "group_top": top_groups,   # {2: [...], 3: [...], 4: [...]}
        "powerball_top": top_pbs,
        "sample_size": len(mains_list),
    }

# ----------------------- Routes: UI & APIs -----------------------
@app.get("/")
def index():
    window = request.args.get("window", type=int)
    draws = get_draws(limit=200)
    freqs = get_frequencies(window=window)
    return render_template("index.html", draws=draws, freqs=freqs, window=window)

@app.get("/api/draws")
def api_draws():
    limit = request.args.get("limit", type=int)
    return jsonify(get_draws(limit=limit))

@app.get("/api/frequencies")
def api_freqs():
    window = request.args.get("window", type=int)
    return jsonify(get_frequencies(window=window))

@app.get("/api/groups")
def api_groups():
    """
    JSON API: /api/groups?window=100&limit=20&ks=2,3,4
    """
    window = request.args.get("window", default=100, type=int)
    limit = request.args.get("limit", default=20, type=int)
    ks_param = request.args.get("ks", default="2,3,4")
    try:
        ks = tuple(sorted({int(x) for x in ks_param.split(",") if x.strip()}))
    except Exception:
        ks = (2, 3, 4)
    stats = compute_group_stats(window=window, ks=ks, limit=limit)
    return jsonify(stats)

@app.get("/groups")
def groups_page():
    """
    HTML page: /groups?window=100&limit=20&ks=2,3,4
    """
    window = request.args.get("window", default=100, type=int)
    limit = request.args.get("limit", default=20, type=int)
    ks_param = request.args.get("ks", default="2,3,4")
    try:
        ks = tuple(sorted({int(x) for x in ks_param.split(",") if x.strip()}))
    except Exception:
        ks = (2, 3, 4)
    stats = compute_group_stats(window=window, ks=ks, limit=limit)
    return render_template("groups.html", stats=stats)

@app.post("/refresh")
def refresh():
    try:
        log.info("Manual refresh from %s", request.remote_addr)
        result = sync_all()
        log.info("Manual refresh: %s", result)
        return jsonify({"status": "ok", **result})
    except Exception as e:
        log.exception("Manual refresh failed: %s", e)
        return jsonify({"status": "error", "error": str(e)}), 500

@app.get("/debug/scrape")
def debug_scrape():
    try:
        year = request.args.get("year", type=int)
        diag = debug_probe(year=year)
        if year:
            rows = fetch_year(year)
            return jsonify({"ok": True, "mode": "year", "count": len(rows), "sample": rows[:3], **diag})
        rows = fetch_latest_six_months()
        return jsonify({"ok": True, "mode": "latest", "count": len(rows), "sample": rows[:3], **diag})
    except Exception as e:
        log.exception("Debug scrape failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})

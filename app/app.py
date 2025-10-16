import os
import sys
import threading
import logging
from flask import Flask, jsonify, render_template, request
from apscheduler.schedulers.background import BackgroundScheduler
from pytz import timezone

# ---------- Logging config ----------
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(asctime)s %(name)s: %(message)s'
)
log = logging.getLogger("app")

# Ensure local imports under gunicorn
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from db import init_db, get_draws, get_frequencies
from scraper import sync_all, fetch_year, fetch_latest_six_months

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
        log.info("Scheduled sync: %s", result)
    except Exception as e:
        log.exception("Scheduled sync failed: %s", e)

def schedule_job():
    from apscheduler.triggers.cron import CronTrigger
    trigger = CronTrigger.from_crontab(UPDATE_CRON, timezone=LOCAL_TZ)
    scheduler.add_job(job_sync, trigger, id="sync_job", replace_existing=True)
    scheduler.start()
    log.info("Scheduler started with cron: %s", UPDATE_CRON)

def initial_sync_async():
    def _run():
        try:
            log.info("Initial sync: starting…")
            result = sync_all()
            log.info("Initial sync: %s", result)
        except Exception as e:
            log.exception("Initial sync failed: %s", e)
    threading.Thread(target=_run, daemon=True).start()

# Kick off initial sync and schedule recurring sync
initial_sync_async()
schedule_job()

# ----------------------- Routes -----------------------
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

@app.post("/refresh")
def refresh():
    try:
        log.info("Manual refresh: /refresh called from %s", request.remote_addr)
        result = sync_all()
        log.info("Manual refresh: %s", result)
        return jsonify({"status": "ok", **result})
    except Exception as e:
        log.exception("Manual refresh failed: %s", e)
        return jsonify({"status": "error", "error": str(e)}), 500

# Debug endpoint: quick probe of parsers
@app.get("/debug/scrape")
def debug_scrape():
    try:
        year = request.args.get("year", type=int)
        from scraper import debug_probe  # local import to avoid cycles
        diag = debug_probe(year=year)
        # Also include the high-level count via the normal fetch paths
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

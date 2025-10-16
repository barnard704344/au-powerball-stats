import os
import sys
import threading
from flask import Flask, jsonify, render_template, request
from apscheduler.schedulers.background import BackgroundScheduler
from pytz import timezone

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
        result = sync_all()
        app.logger.info(f"Sync complete: {result}")
    except Exception as e:
        app.logger.exception(f"Sync failed: {e}")

def schedule_job():
    from apscheduler.triggers.cron import CronTrigger
    trigger = CronTrigger.from_crontab(UPDATE_CRON, timezone=LOCAL_TZ)
    scheduler.add_job(job_sync, trigger, id="sync_job", replace_existing=True)
    scheduler.start()

def initial_sync_async():
    def _run():
        try:
            app.logger.info("Initial sync started…")
            sync_all()
            app.logger.info("Initial sync finished.")
        except Exception as e:
            app.logger.exception(f"Initial sync failed: {e}")
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
        result = sync_all()
        return jsonify({"status": "ok", **result})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

# ---------- NEW: Debug endpoint to inspect scraper output ----------
@app.get("/debug/scrape")
def debug_scrape():
    """
    /debug/scrape?year=2024  -> returns JSON with count & sample for that year
    /debug/scrape            -> returns JSON with count & sample for 'past results'
    """
    try:
        year = request.args.get("year", type=int)
        if year:
            rows = fetch_year(year)
            return jsonify({"ok": True, "mode": "year", "year": year, "count": len(rows), "sample": rows[:3]})
        rows = fetch_latest_six_months()
        return jsonify({"ok": True, "mode": "latest", "count": len(rows), "sample": rows[:3]})
    except Exception as e:
        # Always return JSON so jq won't choke
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})

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

def compute_prediction(window: int = 100):
    """
    Returns a 'prediction' based on simple frequencies over the last `window` draws:
      - main_top_numbers: list of the most frequent main numbers (could be multiple on tie)
      - powerball_top_numbers: list of the most frequent PB numbers (could be multiple on tie)
      - chosen_main / chosen_powerball: single picks after deterministic tie-breaking
        Tie-break rules:
          1) Highest frequency
          2) If tie: pick the number that appeared most recently (latest draw)
          3) If still tie: lowest numeric value
    Also returns full frequency tables and counts for transparency.
    """
    freqs = get_frequencies(window=window)
    main_freq = {int(k): int(v) for k, v in (freqs.get("main") or {}).items()}
    pb_freq   = {int(k): int(v) for k, v in (freqs.get("powerball") or {}).items()}

    def pick_top(freq_map: dict[int, int], look_at_powerball: bool = False):
        if not freq_map:
            return {"top_list": [], "chosen": None, "top_count": 0}
        items = list(freq_map.items())
        max_count = max(c for _, c in items)
        top_list = sorted([n for n, c in items if c == max_count])

        if len(top_list) == 1:
            chosen = top_list[0]
        else:
            # Recency tiebreak — scan newest→oldest across last `window` draws
            draws = get_draws(limit=window)
            most_recent_rank = {}
            for idx, d in enumerate(draws):
                nums = d.get("nums") or []
                pb = d.get("pb")
                for n in top_list:
                    hit = (n in nums) or (look_at_powerball and n == pb)
                    if hit and n not in most_recent_rank:
                        most_recent_rank[n] = idx
                if len(most_recent_rank) == len(top_list):
                    break
            ranked = [(most_recent_rank.get(n, 10**9), n) for n in top_list]
            ranked.sort(key=lambda t: (t[0], t[1]))  # more recent first, then lowest number
            chosen = ranked[0][1]

        return {"top_list": top_list, "chosen": chosen, "top_count": max_count}

    main_pick = pick_top(main_freq, look_at_powerball=False)
    pb_pick   = pick_top(pb_freq,   look_at_powerball=True)

    return {
        "window": window,
        "main": {
            "top_numbers": main_pick["top_list"],
            "top_count": main_pick["top_count"],
            "chosen_main": main_pick["chosen"],
            "frequency_table": main_freq,
        },
        "powerball": {
            "top_numbers": pb_pick["top_list"],
            "top_count": pb_pick["top_count"],
            "chosen_powerball": pb_pick["chosen"],
            "frequency_table": pb_freq,
        },
        "note": "Descriptive stats over last N draws. Powerball draws are independent; this is not predictive.",
    }


def compute_prediction(window: int = 100):
    """
    Returns a 'prediction' based on simple frequencies over the last `window` draws:
      - main_top_numbers: list of the most frequent main numbers (could be multiple on tie)
      - powerball_top_numbers: list of the most frequent PB numbers (could be multiple on tie)
      - chosen_main / chosen_powerball: single picks after deterministic tie-breaking
        Tie-break rules:
          1) Highest frequency
          2) If tie: pick the number that appeared most recently (latest draw)
          3) If still tie: lowest numeric value
    Also returns full frequency tables and counts for transparency.
    """
    # Frequency tables over the last N
    freqs = get_frequencies(window=window)
    main_freq = freqs.get("main", {}) or {}
    pb_freq = freqs.get("powerball", {}) or {}

    # Normalize keys to ints
    main_freq = {int(k): int(v) for k, v in main_freq.items()}
    pb_freq = {int(k): int(v) for k, v in pb_freq.items()}

    # Helper to compute top list + chosen via tie-breaking
    def pick_top(freq_map: dict[int, int], candidates_pool: set[int] | None = None):
        if not freq_map:
            return {"top_list": [], "chosen": None, "top_count": 0}

        # If a pool is provided, restrict to it (we won’t use pool here, but keep generic)
        items = [(n, c) for n, c in freq_map.items() if (candidates_pool is None or n in candidates_pool)]
        if not items:
            return {"top_list": [], "chosen": None, "top_count": 0}

        max_count = max(c for _, c in items)
        top_list = sorted([n for n, c in items if c == max_count])

        # Tie-breaker: recency, then lowest number
        if len(top_list) == 1:
            chosen = top_list[0]
        else:
            # look through the last `window` draws (newest first) to find the earliest occurrence index
            draws = get_draws(limit=window)  # newest first
            most_recent_rank = {}
            for idx, d in enumerate(draws):
                # smaller idx = more recent
                nums = d.get("nums") or []
                pb = d.get("pb")
                for n in top_list:
                    if n in nums or n == pb:
                        # record first time we see it in the reverse-chronological list
                        most_recent_rank.setdefault(n, idx)
                # Early exit if all have been ranked
                if len(most_recent_rank) == len(top_list):
                    break

            # Build candidates with (recency_idx, numeric) for stable sort
            ranked = []
            for n in top_list:
                # if somehow never seen in the window (shouldn’t happen), give a large idx
                idx = most_recent_rank.get(n, 10**9)
                ranked.append((idx, n))
            ranked.sort(key=lambda t: (t[0], t[1]))  # smaller idx (more recent) first, then smaller number
            chosen = ranked[0][1]

        return {"top_list": top_list, "chosen": chosen, "top_count": max_count}

    main_pick = pick_top(main_freq)
    pb_pick = pick_top(pb_freq)

    return {
        "window": window,
        "main": {
            "top_numbers": main_pick["top_list"],
            "top_count": main_pick["top_count"],
            "chosen_main": main_pick["chosen"],
            "frequency_table": main_freq,
        },
        "powerball": {
            "top_numbers": pb_pick["top_list"],
            "top_count": pb_pick["top_count"],
            "chosen_powerball": pb_pick["chosen"],
            "frequency_table": pb_freq,
        },
        "note": "This is a descriptive stat over the last N draws, not a true predictor. Powerball draws are independent.",
    }


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

@app.get("/api/prediction")
def api_prediction():
    """
    /api/prediction?window=100
    Returns top mains+PB over the last N with tie-break explanation.
    """
    window = request.args.get("window", default=100, type=int)
    data = compute_prediction(window=window)
    return jsonify(data)


@app.get("/api/prediction")
def api_prediction():
    """
    Returns the most frequent main number(s) and powerball over the last N draws,
    with deterministic tie-breaking to pick a single suggested main and PB.
    Query: /api/prediction?window=100
    """
    window = request.args.get("window", default=100, type=int)
    data = compute_prediction(window=window)
    return jsonify(data)

@app.get("/prediction")
def prediction_page():
    """
    HTML page: /prediction?window=100
    Shows the most frequent main numbers & powerball over the last N draws,
    with deterministic tie-breaking to select a single pick for each.
    """
    window = request.args.get("window", default=100, type=int)
    data = compute_prediction(window=window)
    return render_template("prediction.html", data=data)


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

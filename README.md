# AU Powerball Stats

Fetches Australian Powerball draw data, stores it in SQLite, and serves a small web UI with recent draws and frequency charts.

- **Repo:** https://github.com/barnard704344/au-powerball-stats  
- **Stack:** Python 3.12, Flask, Gunicorn, APScheduler, SQLite  
- **Runtime:** Docker + docker compose  
- **Data sources:** The Lott web JSON (zero-auth **GET** feed) with HTML fallback  
- **TZ default:** Australia/Adelaide

---

## Deployment

### Quick Deploy Script

Create a deployment script for easy updates:

```bash
cat > /usr/local/bin/au_pb_deploy <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd /opt/au-powerball-stats
git fetch --all --prune
git reset --hard origin/main
git clean -fd
docker compose up -d
docker exec -it au-powerball-stats /bin/sh -lc 'python - <<PY
from app import app; print(app.url_map)
PY'
SH
chmod +x /usr/local/bin/au_pb_deploy
```

Then deploy/update with:

```bash
au_pb_deploy
```

### Initial Setup

```bash
# 1) ensure docker + compose are installed
docker version
docker compose version

# 2) clone and enter the repo
git clone https://github.com/barnard704344/au-powerball-stats.git /opt/au-powerball-stats
cd /opt/au-powerball-stats

# 3) start (builds image once, code is bind-mounted at /srv/app)
docker compose up -d

# 4) healthcheck
curl -s http://localhost:8080/healthz | jq .

# 5) populate data (incremental sync)
curl -s -X POST http://localhost:8080/refresh | jq .
```

### Manual Update Process

If you prefer manual updates instead of the deploy script:

```bash
cd /opt/au-powerball-stats
git fetch origin
git reset --hard origin/main   # change 'main' if your default branch differs
git clean -fd
docker compose up -d           # restarts the container with the new code
```

Verify the container is serving the new code:

```bash
docker exec -it au-powerball-stats /bin/sh -lc 'python - <<PY
from app import app
print(app.url_map)
PY'
```

Alternative: Immutable Image (copy-on-build)

If you prefer not to bind-mount code, remove/comment the - ./app:/srv/app:ro volume in docker-compose.yml. Then after each git pull:

git fetch origin
git reset --hard origin/main
git clean -fd

docker compose down
docker builder prune -f
docker compose build --no-cache
docker compose up -d


If you see pull access denied, ensure docker-compose.yml contains build: . and optionally image: au-powerball-stats-app:local.

Environment Variables

(Defined in docker-compose.yml)

Var	Default	Description
TZ	Australia/Adelaide	Container timezone
YEARS_START	2018	Backfill start year
DB_PATH	/data/powerball.sqlite	SQLite DB path (inside container)
UPDATE_CRON	*/15 * * * *	APScheduler cron for auto-sync
PYTHONUNBUFFERED	1	Flush Python stdout
GUNICORN_CMD_ARGS	--log-level info --access-logfile - --error-logfile -	Log to STDOUT/ERR

Change values in docker-compose.yml and run docker compose up -d.

Endpoints

GET / — UI (recent draws + frequency charts)

GET /api/draws?limit=N — latest N draws (JSON)

GET /api/frequencies?window=N — frequency tables (JSON)

POST /refresh — incremental sync (JSON)

POST /refresh?full=1 — full reset (clears DB, then backfills)

GET /debug/scrape — diagnostics (counts/first item/HTML token stats)

GET /debug/scrape?year=YYYY — year-scoped diagnostics

GET /healthz — health probe (JSON)

Tip: /debug/scrape is great for local diagnostics. Disable before public exposure if desired.

Common Workflows
Manual Incremental Refresh
curl -s -X POST http://localhost:8080/refresh | jq .

Full Reset + Backfill

Use if the DB only shows old data (e.g., 2018) or you want a clean slate.

curl -s -X POST 'http://localhost:8080/refresh?full=1' | jq .

Quick Checks
curl -s http://localhost:8080/healthz | jq .                 # service OK?
curl -s 'http://localhost:8080/api/draws?limit=5' | jq .     # latest draws
curl -s 'http://localhost:8080/api/frequencies?window=100' | jq .  # freq

Logs

Stream logs:

docker logs -f au-powerball-stats


Typical lines:

[INFO] app: Scheduled sync starting…
[INFO] scraper: [API] productdraws -> rows=xxx
[INFO] scraper: Upserting N items from year 2024
[INFO] scraper: sync_all: upserted=NNN, problems=0


If nothing changes when you call /refresh, confirm the container is reading your updated code (see verification snippet above).

Data Location & Backups

DB lives on the host at ./data/powerball.sqlite (mounted to /data inside the container).

Backup:

cp -a data/powerball.sqlite data/powerball.sqlite.bak.$(date +%F)


Reset:

docker compose down
rm -rf data/
docker compose up -d

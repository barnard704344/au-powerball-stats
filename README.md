# AU Powerball Stats (Dockerized)

Live frequency charts and recent draws for Australian Powerball (7/35 + PB 1–20, since 2018).  
Scrapes a public results archive, stores to SQLite, auto-updates, and serves a web UI + JSON APIs.

## Quick install (one liner)

```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/barnard704344/au-powerball-stats/main/scripts/install.sh)"

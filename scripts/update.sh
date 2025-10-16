#!/usr/bin/env bash
set -euo pipefail
REPO_DIR="/opt/au-powerball-stats"
if [[ "$(id -u)" -ne 0 ]]; then echo "Run as root"; exit 1; fi
cd "$REPO_DIR"
git fetch --all
git reset --hard origin/main
docker compose pull || true
docker compose up -d --build
systemctl restart au-powerball-stats.service
echo "Updated."

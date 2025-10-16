#!/usr/bin/env bash
set -euo pipefail
SERVICE="au-powerball-stats"
DIR="/opt/au-powerball-stats"
if [[ "$(id -u)" -ne 0 ]]; then echo "Run as root"; exit 1; fi
systemctl disable --now "$SERVICE" || true
docker compose -f "$DIR/docker-compose.yml" down || true
rm -f /etc/systemd/system/${SERVICE}.service
systemctl daemon-reload
rm -rf "$DIR"
echo "Uninstalled."

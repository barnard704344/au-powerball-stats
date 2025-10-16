#!/usr/bin/env bash
# Install & launch au-powerball-stats via Docker on Debian/Ubuntu
set -euo pipefail

REPO_OWNER="barnard704344"
REPO_NAME="au-powerball-stats"
INSTALL_DIR="/opt/${REPO_NAME}"

echo "==> Checking root privileges"
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Please run as root: sudo $0"
  exit 1
fi

echo "==> Installing prerequisites"
apt-get update -y
apt-get install -y ca-certificates curl git

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker Engine"
  curl -fsSL https://get.docker.com | sh
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "==> Installing docker compose plugin"
  mkdir -p /usr/local/lib/docker/cli-plugins
  curl -fsSL https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-$(uname -m) \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
  chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi

echo "==> Cloning repository"
rm -rf "${INSTALL_DIR}"
git clone "https://github.com/${REPO_OWNER}/${REPO_NAME}.git" "${INSTALL_DIR}"

cd "${INSTALL_DIR}"

if [[ ! -f ".env" ]]; then
  echo "==> Creating .env from .env.example"
  cp .env.example .env
fi

echo "==> Building and starting containers"
docker compose up -d --build

echo "==> Installing systemd service"
cp scripts/au-powerball-stats.service /etc/systemd/system/au-powerball-stats.service
systemctl daemon-reload
systemctl enable --now au-powerball-stats.service

echo "==> Done!"
echo "Open: http://<this-host>:$(grep -E '^FLASK_PORT=' .env | cut -d= -f2 | tr -d '\r' | sed 's/^$//')"

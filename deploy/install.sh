#!/usr/bin/env bash
# Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Install the shared Footnote platform on Ubuntu (20.04+): application code
# and virtualenv in APP_DIR, and the footnote@ systemd template unit. Run as
# root:
#
#   sudo bash deploy/install.sh
#
# Then create one instance per person — the service runs as that user and
# writes dossiers into that user's own (synced) folder:
#
#   sudo bash deploy/add-instance.sh <user> <port> [output-dir]
#
# Idempotent — safe to re-run after pulling updates; restart instances
# afterwards with:  systemctl restart 'footnote@*'
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/footnote}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo bash deploy/install.sh" >&2
  exit 1
fi

echo "==> Installing system packages"
apt-get update
apt-get install -y python3-venv python3-pip rsync

echo "==> Copying application to $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
  --exclude '.env' --exclude 'data' --exclude 'server.log' \
  "$REPO_DIR/" "$APP_DIR/"
# Shared, instance-independent secrets (Parallel/Firecrawl/Notion/VAPID keys)
# live here; per-person settings (port, output dir, token, DATA_DIR — and any
# per-person key overrides) live in /etc/footnote/<user>.env, which wins:
# systemd exports it into the process environment, and load_dotenv() never
# overrides existing environment variables.
[ -f "$APP_DIR/.env" ] || cp "$APP_DIR/.env.example" "$APP_DIR/.env"

echo "==> Creating virtualenv and installing Python dependencies"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

# Instances run as their own users — they need read access to the shared code
chmod -R a+rX "$APP_DIR"

echo "==> Installing systemd template unit footnote@.service"
mkdir -p /etc/footnote
sed "s|/opt/footnote|$APP_DIR|g" "$REPO_DIR/deploy/footnote@.service" \
  > /etc/systemd/system/footnote@.service
systemctl daemon-reload

echo
echo "Platform installed. Add shared API keys to $APP_DIR/.env, then create"
echo "an instance per person, e.g.:"
echo "  sudo bash $REPO_DIR/deploy/add-instance.sh ${SUDO_USER:-<user>} 8010"
echo "Existing instances keep running; pick up this update with:"
echo "  systemctl restart 'footnote@*'"

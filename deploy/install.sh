#!/usr/bin/env bash
# Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Install the shared Footnote platform on Ubuntu (22.04+, for Python 3.10;
# 20.04's default python3 is 3.8 and the installer refuses it): application code
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
SHARED_GROUP="${SHARED_GROUP:-footnote}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo bash deploy/install.sh" >&2
  exit 1
fi

echo "==> Installing system packages"
apt-get update
apt-get install -y python3-venv python3-pip rsync

# Footnote needs 3.10 (README's stated floor). Ubuntu 22.04 ships 3.10 and
# 24.04 ships 3.12; 20.04's default python3 is 3.8, which starts the service
# and then fails inside jobs — the worst possible way to find out.
PYTHON="${PYTHON:-python3}"
PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Footnote needs Python 3.10 or newer; $PYTHON is $PY_VERSION." >&2
  echo "On Ubuntu 20.04, install a newer Python (for example the deadsnakes" >&2
  echo "PPA) and re-run with PYTHON=/usr/bin/python3.12, or use 22.04+." >&2
  exit 1
fi

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
[ -d "$APP_DIR/.venv" ] || "$PYTHON" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

# Instances run as their own users — they need read access to the shared code
chmod -R a+rX "$APP_DIR"
# …but not to the shared API keys. A dedicated group carries that access;
# add-instance.sh puts each instance's user into it.
groupadd -f "$SHARED_GROUP"
chgrp "$SHARED_GROUP" "$APP_DIR/.env"
chmod 640 "$APP_DIR/.env"
# Instances that already exist were created before the group did, and the
# upgrade path is this script plus a restart — so put their users in it here
# rather than leaving a service that cannot read its own keys.
for env_file in /etc/footnote/*.env; do
  [ -e "$env_file" ] || continue
  instance_user="$(basename "$env_file" .env)"
  if id -u "$instance_user" >/dev/null 2>&1; then
    usermod -a -G "$SHARED_GROUP" "$instance_user"
    echo "    $instance_user can read the shared keys"
  fi
done

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

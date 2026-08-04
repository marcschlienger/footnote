#!/usr/bin/env bash
# Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Create (or repair) a per-person Footnote instance: writes
# /etc/footnote/<user>.env, creates the output and data directories, and
# enables the footnote@<user> service. Run as root:
#
#   sudo bash deploy/add-instance.sh <user> <port> [output-dir]
#
# Defaults: output-dir = /home/<user>/Research/inbox; a FOOTNOTE_TOKEN is
# generated on first run. API keys are read from the shared /opt/footnote/.env
# unless overridden in the per-user env file. Edit the env file to change
# anything, then `systemctl restart footnote@<user>`. Idempotent — an
# existing env file is kept untouched.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/footnote}"
USAGE="usage: sudo bash deploy/add-instance.sh <user> <port> [output-dir]"
USER_NAME="${1:?$USAGE}"
PORT="${2:?$USAGE}"
OUT="${3:-/home/$USER_NAME/Research/inbox}"
DATA="/var/lib/footnote/$USER_NAME"
ENV_FILE="/etc/footnote/$USER_NAME.env"

if [ "$(id -u)" -ne 0 ]; then
  echo "$USAGE  (must run as root)" >&2
  exit 1
fi
id -u "$USER_NAME" >/dev/null   # errors out if the user doesn't exist
GROUP_NAME="$(id -gn "$USER_NAME")"

mkdir -p /etc/footnote
if [ -f "$ENV_FILE" ]; then
  echo "==> $ENV_FILE exists — keeping it (edit it + restart to change settings)"
else
  echo "==> Writing $ENV_FILE"
  TOKEN="$("$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(24))')"
  cat > "$ENV_FILE" <<EOF
HOST=0.0.0.0
PORT=$PORT
OUTPUT_DIR=$OUT
DATA_DIR=$DATA
FOOTNOTE_TOKEN=$TOKEN
# Per-person API key overrides (otherwise the shared $APP_DIR/.env applies):
#PARALLEL_API_KEY=
#FIRECRAWL_API_KEY=
#NOTION_API_KEY=
#NOTION_DATABASE_ID=
EOF
  chmod 600 "$ENV_FILE"
fi

echo "==> Creating output directory $OUT and data directory $DATA"
install -d -o "$USER_NAME" -g "$GROUP_NAME" "$OUT"
install -d -o "$USER_NAME" -g "$GROUP_NAME" -m 700 "$DATA"

echo "==> Enabling footnote@$USER_NAME"
systemctl daemon-reload
systemctl enable --now "footnote@$USER_NAME"
sleep 2
systemctl --no-pager status "footnote@$USER_NAME" || true

echo
echo "Instance ready:"
echo "  App URL:    http://<server>:$PORT/  (open once with ?token=<token> to store the cookie)"
echo "  Output dir: $OUT"
echo "  Config:     $ENV_FILE  (restart footnote@$USER_NAME after edits)"
echo "  Token:      $(grep '^FOOTNOTE_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
echo "  Logs:       journalctl -u footnote@$USER_NAME -f"

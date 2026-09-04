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
#
# Firecrawl's rate limit belongs to the API key, and each instance paces
# itself independently. Instances sharing one key must therefore divide the
# budget between them — e.g. FIRECRAWL_RATE_LIMIT=5 in each of two env files
# on a free key that allows 10/min — or use one key per person.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/footnote}"
SHARED_GROUP="${SHARED_GROUP:-footnote}"
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

# Read access to the shared API keys is carried by a group, not by "everyone".
if getent group "$SHARED_GROUP" >/dev/null; then
  usermod -a -G "$SHARED_GROUP" "$USER_NAME"
fi
# An existing env file is authoritative: a customized instance keeps its own
# directories, and repairing the ones computed from this invocation's
# arguments would tighten permissions on the wrong paths.
# systemd's EnvironmentFile allows quoting, and sed strips only the literal
# prefix — OUTPUT_DIR="/srv/My Notes" would otherwise come back with its
# quotes attached and have install(1) create a relative directory of that
# name. Sourcing the file as root is not an option, so: strip one layer of
# matching quotes and accept the result only if it is an absolute path.
read_env_path() {
  local raw
  raw="$(sed -n "s/^$1=//p" "$ENV_FILE" | tail -1)"
  case "$raw" in
    \"*\") raw="${raw#\"}"; raw="${raw%\"}" ;;
    \'*\') raw="${raw#\'}"; raw="${raw%\'}" ;;
  esac
  case "$raw" in
    /*) printf '%s' "$raw" ;;
    *)  printf '' ;;
  esac
}

if [ -f "$ENV_FILE" ]; then
  EXISTING_OUT="$(read_env_path OUTPUT_DIR)"
  EXISTING_DATA="$(read_env_path DATA_DIR)"
  [ -n "$EXISTING_OUT" ] && OUT="$EXISTING_OUT"
  [ -n "$EXISTING_DATA" ] && DATA="$EXISTING_DATA"
fi

echo "==> Creating output directory $OUT and data directory $DATA"
# 0700: a dossier is research someone paid for, and the host's default umask
# is not a decision this script should inherit.
install -d -o "$USER_NAME" -g "$GROUP_NAME" -m 700 "$OUT"
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

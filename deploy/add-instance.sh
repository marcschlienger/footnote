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

# shellcheck source=deploy/paths.sh
. "$(dirname "${BASH_SOURCE[0]}")/paths.sh"

# Everything is checked here, before a single file is written. A relative
# output directory used to be caught after the env file existed, which left a
# poisoned /etc/footnote/<user>.env behind — and a corrected re-run keeps an
# existing env file untouched, so the bad path survived into the service.
# "Absolute" was the whole test, so "/" passed it, and the script would then
# hand the root of the filesystem to one user at mode 700.
check_target_dir output-dir "$OUT" || exit 1
check_target_dir DATA_DIR "$DATA" || exit 1
case "$PORT" in
  ''|*[!0-9]*) echo "port must be a number, not: $PORT" >&2; exit 1 ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "port must be between 1 and 65535, not: $PORT" >&2
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "$USAGE  (must run as root)" >&2
  exit 1
fi
id -u "$USER_NAME" >/dev/null   # errors out if the user doesn't exist
GROUP_NAME="$(id -gn "$USER_NAME")"

mkdir -p /etc/footnote
if [ -f "$ENV_FILE" ]; then
  ENV_EXISTED=yes
  echo "==> $ENV_FILE exists — keeping it (edit it + restart to change settings)"
else
  ENV_EXISTED=no
  echo "==> Writing $ENV_FILE"
  TOKEN="$("$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(24))')"
  # The token is a credential from the moment the first byte lands. Written
  # first and chmodded afterwards, it was world-readable in between, and an
  # interrupted run left it that way.
  OLD_UMASK="$(umask)"
  umask 077
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
  umask "$OLD_UMASK"
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
read_env_value() {
  local raw
  raw="$(sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | tail -1)"
  # Trim surrounding whitespace, then one layer of matching quotes.
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"
  case "$raw" in
    \"*\") raw="${raw#\"}"; raw="${raw%\"}" ;;
    \'*\') raw="${raw#\'}"; raw="${raw%\'}" ;;
  esac
  case "$raw" in
    # Escapes and variable references are systemd syntax this cannot resolve.
    # Say so rather than acting on a half-read value.
    *\\*|*'$'*) echo "cannot parse $1 in $ENV_FILE; leaving it alone" >&2
                printf '' ;;
    *) printf '%s' "$raw" ;;
  esac
}

read_env_path() {
  local raw
  raw="$(read_env_value "$1")"
  case "$raw" in
    /*) printf '%s' "$raw" ;;
    "") printf '' ;;
    *)  echo "$1 in $ENV_FILE is not an absolute path; leaving it alone" >&2
        printf '' ;;
  esac
}

read_env_port() {
  local raw
  raw="$(read_env_value PORT)"
  case "$raw" in
    ''|*[!0-9]*) printf '' ;;
    *) printf '%s' "$raw" ;;
  esac
}

# The stored file is what systemd reads, so it decides — and where it does
# not say, this script has nothing to fall back on. The arguments are not a
# fallback: systemd never sees them. Falling back to them meant creating
# directories the service would not use and printing a URL on a port nothing
# was listening on, which is a worse outcome than stopping.
check_stored_env() {
  local bad=0
  STORED_OUT="$(read_env_path OUTPUT_DIR)"
  STORED_DATA="$(read_env_path DATA_DIR)"
  STORED_PORT="$(read_env_port)"
  if [ -z "$STORED_OUT" ]; then
    echo "  OUTPUT_DIR: missing, or not an absolute path this can read" >&2
    bad=1
  elif ! check_target_dir OUTPUT_DIR "$STORED_OUT"; then
    bad=1
  fi
  if [ -z "$STORED_DATA" ]; then
    echo "  DATA_DIR: missing, or not an absolute path this can read" >&2
    bad=1
  elif ! check_target_dir DATA_DIR "$STORED_DATA"; then
    bad=1
  fi
  if [ -z "$STORED_PORT" ]; then
    echo "  PORT: missing, or not a number" >&2
    bad=1
  elif [ "$STORED_PORT" -lt 1 ] || [ "$STORED_PORT" -gt 65535 ]; then
    echo "  PORT: $STORED_PORT is not between 1 and 65535" >&2
    bad=1
  fi
  [ "$bad" -eq 0 ] || return 1
  return 0
}

if [ "$ENV_EXISTED" = yes ]; then
  if ! check_stored_env; then
    echo >&2
    echo "$ENV_FILE does not say what this instance runs as, and this script" >&2
    echo "will not invent it: systemd reads that file, not this command line." >&2
    echo "Fix the settings above and re-run, or delete the file to have a" >&2
    echo "fresh one written." >&2
    exit 1
  fi
  if [ "$STORED_PORT" != "$PORT" ]; then
    echo "==> $ENV_FILE already sets PORT=$STORED_PORT — keeping it."
    echo "    Edit that file and restart footnote@$USER_NAME to change it."
  fi
  OUT="$STORED_OUT"
  DATA="$STORED_DATA"
  PORT="$STORED_PORT"
fi

echo "==> Creating output directory $OUT and data directory $DATA"
# 0700: a dossier is research someone paid for, and the host's default umask
# is not a decision this script should inherit.
install -d -o "$USER_NAME" -g "$GROUP_NAME" -m 700 -- "$OUT"
install -d -o "$USER_NAME" -g "$GROUP_NAME" -m 700 -- "$DATA"

echo "==> Enabling footnote@$USER_NAME"
systemctl daemon-reload
systemctl enable --now "footnote@$USER_NAME"
sleep 2
systemctl --no-pager status "footnote@$USER_NAME" || true

# The status above is informational; this decides what the script claims. A
# port collision or a delayed startup failure was being reported as a
# successful install.
if ! systemctl is-active --quiet "footnote@$USER_NAME"; then
  echo
  echo "Instance created, but footnote@$USER_NAME is not running." >&2
  echo "  Config:  $ENV_FILE" >&2
  echo "  Why:     journalctl -u footnote@$USER_NAME -n 30" >&2
  echo "A port already in use is the usual cause." >&2
  exit 1
fi

echo
echo "Instance ready:"
echo "  App URL:    http://<server>:$PORT/  (open once with ?token=<token> to store the cookie)"
echo "  Output dir: $OUT"
echo "  Config:     $ENV_FILE  (restart footnote@$USER_NAME after edits)"
echo "  Token:      $(grep '^FOOTNOTE_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
echo "  Logs:       journalctl -u footnote@$USER_NAME -f"

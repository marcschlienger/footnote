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
# Written after a successful copy, so a later run can tell its own
# installation from a directory that merely happens to be there.
APP_MARKER=".footnote-install"

# shellcheck source=deploy/paths.sh
. "$(dirname "${BASH_SOURCE[0]}")/paths.sh"

# APP_DIR is an override and everything below runs as root: the copy is
# rsync --delete, which empties the destination, and the permissions pass is
# a recursive chmod. Aimed at /opt, /usr, or any populated directory that is
# not a Footnote installation, that is not an install — it is a deletion of
# whatever was there. Refuse unless the destination is empty, carries the
# marker, or is recognisably a Footnote installation from before the marker
# existed.
check_app_dir() {
  local dir="$1" repo="${2:-}" resolved repo_resolved
  check_target_dir APP_DIR "$dir" || return 1
  # Compared after resolving, not as typed: "/opt/footnote/" and a symlink
  # both name the same directory as "/opt/footnote", and both walked past a
  # check that compared the strings.
  resolved="$(canon_path "$dir")"
  if [ -n "$repo" ]; then
    repo_resolved="$(canon_path "$repo")"
    if [ "$resolved" = "$repo_resolved" ]; then
      echo "APP_DIR is this checkout; install to a directory of its own." >&2
      return 1
    fi
    case "$resolved/" in
      "$repo_resolved"/*)
        echo "APP_DIR=$dir is inside this checkout, which rsync --delete" >&2
        echo "would then empty out from under itself." >&2
        return 1 ;;
    esac
    # And the other way round, which the marker check would otherwise wave
    # through: a checkout living inside an installation is deleted by the
    # same rsync that is reading from it, half way through, leaving neither
    # a checkout nor a finished install.
    case "$repo_resolved/" in
      "$resolved"/*)
        echo "This checkout is inside APP_DIR=$dir. The copy below is" >&2
        echo "rsync --delete, which would delete the checkout while it is" >&2
        echo "reading from it. Keep the two apart." >&2
        return 1 ;;
    esac
  fi
  dir="$resolved"
  if [ -e "$dir" ] && [ ! -d "$dir" ]; then
    echo "APP_DIR=$dir exists and is not a directory." >&2
    return 1
  fi
  [ -d "$dir" ] || return 0                       # nothing there yet: fine
  [ -f "$dir/$APP_MARKER" ] && return 0           # ours, from a previous run
  # An installation made before the marker existed: recognise it rather than
  # refusing the upgrade path this script exists to be.
  if [ -f "$dir/app.py" ] && [ -f "$dir/pipeline.py" ] && [ -d "$dir/static" ]
  then
    return 0
  fi
  [ -z "$(ls -A -- "$dir" 2>/dev/null)" ] && return 0
  echo "APP_DIR=$dir is not empty and was not installed by this script." >&2
  echo "Copying into it runs rsync --delete, which would remove what is" >&2
  echo "there. Choose an empty directory, or if this really is a Footnote" >&2
  echo "installation, create $dir/$APP_MARKER and re-run." >&2
  return 1
}

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo bash deploy/install.sh" >&2
  exit 1
fi

check_app_dir "$APP_DIR" "$REPO_DIR" || exit 1

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
  --exclude "$APP_MARKER" \
  "$REPO_DIR/" "$APP_DIR/"
echo "Footnote application directory, managed by deploy/install.sh." \
  > "$APP_DIR/$APP_MARKER"
# Shared, instance-independent secrets (Parallel/Firecrawl/Notion/VAPID keys)
# live here; per-person settings (port, output dir, token, DATA_DIR — and any
# per-person key overrides) live in /etc/footnote/<user>.env, which wins:
# systemd exports it into the process environment, and load_dotenv() never
# overrides existing environment variables.
# Created under a private umask and given its group and mode straight away.
# Written first and secured afterwards, an interrupted run left the shared
# API keys readable by every account on the machine.
groupadd -f "$SHARED_GROUP"
if [ ! -f "$APP_DIR/.env" ]; then
  (umask 077 && cp "$APP_DIR/.env.example" "$APP_DIR/.env")
fi
chgrp "$SHARED_GROUP" "$APP_DIR/.env"
chmod 640 "$APP_DIR/.env"

echo "==> Creating virtualenv and installing Python dependencies"
# An existing venv is reused only if it *is* the interpreter that was asked
# for. "New enough" was the old test, and it kept a 3.10 environment on a run
# that said PYTHON=/usr/bin/python3.12 — so the override silently did nothing
# and pins resolved for 3.12 were then installed into 3.10.
PY_IDENTITY='import sys; print("%d.%d %s" % (sys.version_info[0], sys.version_info[1], getattr(sys, "base_prefix", sys.prefix)))'
WANT_PY="$("$PYTHON" -c "$PY_IDENTITY")"
if [ -d "$APP_DIR/.venv" ]; then
  HAVE_PY="$("$APP_DIR/.venv/bin/python" -c "$PY_IDENTITY" 2>/dev/null || true)"
  if [ "$HAVE_PY" != "$WANT_PY" ]; then
    echo "    virtualenv is ${HAVE_PY:-unreadable}; $PYTHON is $WANT_PY"
    echo "    rebuilding it"
    rm -rf "$APP_DIR/.venv"
  fi
fi
[ -d "$APP_DIR/.venv" ] || "$PYTHON" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
# requirements.txt states lower bounds, so an install resolves them afresh
# and two installs a month apart are not the same software. When a
# constraints file has been generated on this platform (see
# deploy/make-constraints.sh) it decides the versions instead.
if [ -f "$APP_DIR/deploy/constraints.txt" ]; then
  # Wheels and dependency markers differ by interpreter and platform, so a
  # constraints file resolved elsewhere is a set of pins for software this
  # machine is not running. It says where it came from; check before using it.
  RESOLVED_FOR="$("$APP_DIR/.venv/bin/python" -c \
    'import sys; print("Python %d.%d, %s" % (sys.version_info[0], sys.version_info[1], sys.platform))')"
  if ! grep -qF "$RESOLVED_FOR" "$APP_DIR/deploy/constraints.txt"; then
    echo "deploy/constraints.txt was not resolved for $RESOLVED_FOR:" >&2
    grep -m1 '^# Resolved from' "$APP_DIR/deploy/constraints.txt" >&2 || true
    echo "Regenerate it here and run the tests:" >&2
    echo "  bash $REPO_DIR/deploy/make-constraints.sh $PYTHON" >&2
    echo "or delete it to resolve versions afresh." >&2
    exit 1
  fi
  echo "    pinned by deploy/constraints.txt ($RESOLVED_FOR)"
  "$APP_DIR/.venv/bin/pip" install -c "$APP_DIR/deploy/constraints.txt" \
    -r "$APP_DIR/requirements.txt" -q
else
  echo "    no deploy/constraints.txt — resolving versions afresh"
  "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
fi

# Instances run as their own users — they need read access to the shared
# code, but not to the shared API keys, which a dedicated group carries
# instead (add-instance.sh puts each instance's user into it). The recursive
# pass steps over the env file rather than widening it and putting it back:
# between those two commands the keys were world-readable, and an interrupted
# run left them that way.
find "$APP_DIR" -path "$APP_DIR/.env" -prune -o -exec chmod a+rX {} +
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

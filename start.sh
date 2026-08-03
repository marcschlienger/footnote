#!/bin/bash
# Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

cd "$(dirname "$0")"

# Rotate the log if it has grown past ~5 MB — keep only the last 1000 lines.
LOG="$(pwd)/server.log"
if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG" 2>/dev/null || stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 5000000 ]; then
  tail -n 1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# Use the venv's Python directly — sourcing 'activate' triggers a launchd
# sandbox PermissionError on pyvenv.cfg under some macOS configurations.
# app.py honours OUTPUT_DIR / HOST / PORT env vars and --output-dir.
exec .venv/bin/python app.py "$@"

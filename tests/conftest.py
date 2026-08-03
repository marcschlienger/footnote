# Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import sys
import tempfile
from pathlib import Path

# Point the app at throwaway dirs before it is imported.
_tmp = tempfile.mkdtemp(prefix="footnote-test-")
os.environ["DATA_DIR"] = str(Path(_tmp) / "data")
os.environ["OUTPUT_DIR"] = str(Path(_tmp) / "out")
os.environ.setdefault("PARALLEL_API_KEY", "")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

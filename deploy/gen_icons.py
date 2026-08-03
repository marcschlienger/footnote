# Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Re-render the PNG icons from static/icon.svg (run after editing the SVG).

Usage:  .venv/bin/python deploy/gen_icons.py

Renders square, full-bleed PNGs via headless Chromium — platforms mask their
own corners on touch icons, so the SVG's rounded corners are flattened first.
"""
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent

# (size, filename, rounded) — square/full-bleed for touch icons (platforms
# mask their own corners); rounded + transparent corners for tab favicons.
SIZES = [
    (180, "apple-touch-icon.png", False),
    (512, "icon-512.png", False),
    (192, "icon-192.png", False),
    (32, "favicon-32.png", True),
    (16, "favicon-16.png", True),
]

PAPER = "#EDE2C9"  # keep in sync with the <rect> fill in icon.svg

svg = (REPO / "static" / "icon.svg").read_text(encoding="utf-8")
svg_square = re.sub(r'rx="14"', 'rx="0"', svg)

with sync_playwright() as p:
    browser = p.chromium.launch()
    for size, name, rounded in SIZES:
        page = browser.new_page(viewport={"width": size, "height": size})
        page.set_content(
            f"<body style='margin:0;background:transparent'>"
            f"<div style='width:{size}px;height:{size}px'>"
            f"{svg if rounded else svg_square}</div></body>"
        )
        page.locator("svg").evaluate(
            "el => { el.setAttribute('width','100%'); el.setAttribute('height','100%'); }"
        )
        page.screenshot(path=str(REPO / "static" / name),
                        clip={"x": 0, "y": 0, "width": size, "height": size},
                        omit_background=rounded)
        page.close()
        print(f"wrote static/{name} ({size}x{size})")

    # Maskable PWA variant: Android applies a circle/squircle mask, so all
    # critical content must sit in the central 80% — render the (rounded)
    # artwork scaled to 80% on a full-bleed paper background.
    size, inner = 512, 410  # inner = 80% safe zone
    pad = (size - inner) // 2
    page = browser.new_page(viewport={"width": size, "height": size})
    page.set_content(
        f"<body style='margin:0;background:{PAPER}'>"
        f"<div style='padding:{pad}px'>"
        f"<div style='width:{inner}px;height:{inner}px'>{svg}</div></div></body>"
    )
    page.locator("svg").evaluate(
        "el => { el.setAttribute('width','100%'); el.setAttribute('height','100%'); }"
    )
    page.screenshot(path=str(REPO / "static" / "icon-512-maskable.png"),
                    clip={"x": 0, "y": 0, "width": size, "height": size})
    page.close()
    print("wrote static/icon-512-maskable.png (512x512, 80% safe zone)")
    browser.close()

# Bundle a real .ico (16 + 32) — Safari distrusts non-ICO /favicon.ico content.
from PIL import Image  # noqa: E402  (dev dependency, see requirements-dev.txt)

img32 = Image.open(REPO / "static" / "favicon-32.png")
img16 = Image.open(REPO / "static" / "favicon-16.png")
img32.save(REPO / "static" / "favicon.ico", format="ICO",
           append_images=[img16], sizes=[(32, 32), (16, 16)])
print("wrote static/favicon.ico (32+16)")

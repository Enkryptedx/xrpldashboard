"""Generate favicon assets for xrpldashboard.

Produces:
    static/favicon.svg          — vector mark; modern browsers prefer this
    static/favicon-32.png       — 32×32 fallback for older browsers
    static/favicon-16.png       — 16×16 fallback
    static/apple-touch-icon.png — 180×180 for iOS home-screen pins

Design: cyan lowercase "x" on the site's dark background — same palette
as the OG image so the brand reads consistently across share unfurls,
browser tabs, and pinned home-screen icons.

Run:
    ./venv/bin/python scripts/generate_favicon.py

Commit the output PNGs + SVG. Production never regenerates these.
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "static")

BG = (10, 14, 39)          # #0a0e27
CYAN = (34, 211, 238)      # #22d3ee


def _font(size):
    """Prefer a heavy sans-serif so the glyph is legible at 16×16."""
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Black.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _draw_x(size, padding_ratio=0.10):
    """Render a single cyan 'x' centered on the dark background. Returns
    a PIL Image at (size, size). At 16×16 the glyph is almost the full
    canvas so it stays readable."""
    img = Image.new("RGB", (size, size), BG)
    draw = ImageDraw.Draw(img)

    # Pick a font size that fills the canvas minus padding.
    font_size = int(size * (1 - 2 * padding_ratio))
    # Bias up: 'x' has lower visual weight than capital letters.
    font_size = int(font_size * 1.15)
    font = _font(font_size)

    text = "x"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    # Center accounting for font's bbox offset.
    x = (size - tw) // 2 - bbox[0]
    y = (size - th) // 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=CYAN)
    return img


SVG_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="10" fill="#0a0e27"/>
  <text x="50%" y="50%"
        text-anchor="middle"
        dominant-baseline="central"
        font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif"
        font-weight="900"
        font-size="52"
        fill="#22d3ee">x</text>
</svg>
"""


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    # SVG (vector — preferred by modern browsers)
    svg_path = os.path.join(OUT_DIR, "favicon.svg")
    with open(svg_path, "w") as f:
        f.write(SVG_TEMPLATE)

    # PNG fallbacks
    for size, name in [
        (16, "favicon-16.png"),
        (32, "favicon-32.png"),
        (180, "apple-touch-icon.png"),
    ]:
        out = os.path.join(OUT_DIR, name)
        _draw_x(size).save(out, "PNG", optimize=True)

    print(f"wrote favicon.svg + 3 PNG variants to {OUT_DIR}")


if __name__ == "__main__":
    sys.exit(main() or 0)

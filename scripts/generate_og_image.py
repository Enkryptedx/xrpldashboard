"""Generate the 1200×630 OG/Twitter card image for xrpldashboard.

Produces static/og-image.png. Run once on a Mac (uses system fonts for
SF Pro / Arial fallback). The committed PNG is what production serves —
no Pillow runtime dep, no per-request generation.

    ./venv/bin/python scripts/generate_og_image.py

If you tweak the design and re-run, commit the new PNG.
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "static", "og-image.png")

W, H = 1200, 630

# Site palette (mirror of templates/index.html)
BG = (10, 14, 39)            # #0a0e27
CYAN = (34, 211, 238)        # #22d3ee
BLUE = (59, 130, 246)        # #3b82f6
PURPLE = (168, 85, 247)      # #a855f7
TEXT = (230, 233, 242)       # #e6e9f2
MUTED = (128, 144, 176)      # #8090b0
DIM = (90, 102, 128)         # #5a6680


def _font(size, bold=False):
    """Try SF Pro first, then Helvetica, then Arial. PIL's bundled font
    is the last resort — if we hit it, the PNG will look chunky."""
    candidates = [
        ("/System/Library/Fonts/SFNS.ttf", size),
        ("/System/Library/Fonts/HelveticaNeue.ttc", size),
        (f"/System/Library/Fonts/Supplemental/Arial{' Bold' if bold else ''}.ttf", size),
    ]
    for path, sz in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, sz)
            except Exception:
                continue
    return ImageFont.load_default()


def _radial(img, cx, cy, radius, color, max_alpha=70):
    """Soft radial glow centered at (cx, cy). Cheap layer-blend instead of
    a real gaussian — perceptually fine at 1200×630."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    steps = 40
    for i in range(steps, 0, -1):
        r = int(radius * (i / steps))
        a = int(max_alpha * (1 - i / steps))
        draw.ellipse(
            (cx - r, cy - r, cx + r, cy + r),
            fill=(color[0], color[1], color[2], a),
        )
    return Image.alpha_composite(img, overlay)


def _wordmark(draw, x, y):
    """'xrpldashboard' with cyan-accented 'dashboard' suffix. Mirrors the
    in-site brand bar."""
    f = _font(40)
    draw.text((x, y), "xrpl", font=f, fill=TEXT)
    bbox = draw.textbbox((x, y), "xrpl", font=f)
    draw.text((bbox[2], y), "dashboard", font=f, fill=CYAN)


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)

    img = Image.new("RGBA", (W, H), BG + (255,))
    img = _radial(img, 180, 80, 600, CYAN, max_alpha=18)
    img = _radial(img, 1050, 80, 700, BLUE, max_alpha=14)
    img = _radial(img, 1100, 600, 500, PURPLE, max_alpha=10)

    draw = ImageDraw.Draw(img)

    _wordmark(draw, 64, 56)

    # Hero headline — two lines, large.
    f_hero = _font(78)
    draw.text((64, 200), "The XRP Ledger,", font=f_hero, fill=TEXT)
    draw.text((64, 290), "made legible.", font=f_hero, fill=CYAN)

    # Sub-headline
    f_sub = _font(28)
    draw.text(
        (64, 400),
        "Whales · AMM pools · token activity · cold storage.",
        font=f_sub,
        fill=MUTED,
    )
    draw.text(
        (64, 442),
        "Independent on-chain data, no jargon, no upsells.",
        font=f_sub,
        fill=MUTED,
    )

    # Footer URL + accent rule
    draw.line([(64, 540), (1136, 540)], fill=(255, 255, 255, 28), width=1)
    f_url = _font(24)
    draw.text((64, 560), "xrpldashboard.com", font=f_url, fill=DIM)

    img.convert("RGB").save(OUT, "PNG", optimize=True)
    size_kb = os.path.getsize(OUT) // 1024
    print(f"wrote {OUT} ({W}×{H}, {size_kb} KB)")


if __name__ == "__main__":
    sys.exit(main() or 0)

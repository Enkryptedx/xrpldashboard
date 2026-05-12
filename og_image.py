"""Per-page Open Graph image renderer (1200x630 PNG).

Generates the image a social-card preview (Slack, Twitter, LinkedIn, etc.)
shows when someone shares a page from xrpldashboard. Each route can supply
its own (title, subtitle, accent) tuple via the registry in app.py.

Defense in depth: Pillow is imported at the top inside a try/except so a
missing dep won't crash the WSGI app on import. The caller (app.py route)
wraps render() in another try/except and falls back to /static/og-image.png
on any failure. So three things can go wrong and the site still serves the
old shared image:
  1. Pillow missing on the host (e.g. requirements drift)
  2. render() raising mid-draw (font load, gradient math, anything)
  3. Unknown page slug — route returns the static fallback
"""
from __future__ import annotations

import io
from typing import Optional

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except Exception:
    _PIL_OK = False

WIDTH = 1200
HEIGHT = 630

# Backdrop matches the site's dark theme. Vertical gradient from near-black
# at the top to a faint blue at the bottom so the accent stripe pops without
# clashing.
BG_TOP = (10, 14, 26)        # #0a0e1a
BG_BOTTOM = (13, 20, 34)     # #0d1422

WORDMARK_COLOR = (200, 220, 240)
TITLE_COLOR = (255, 255, 255)
SUBTITLE_COLOR = (170, 185, 205)
FOOTER_COLOR = (130, 150, 175)


def _hex_to_rgb(h: str) -> tuple:
    """Convert '#rrggbb' (or 'rrggbb') to an (r, g, b) tuple. Defaults to
    the cyan accent if the input is malformed — never raises, so a bad
    config row can't break the render."""
    try:
        s = (h or "").lstrip("#")
        if len(s) != 6:
            return (62, 200, 224)
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return (62, 200, 224)


def _vgradient(top_rgb, bottom_rgb) -> "Image.Image":
    """Cheap top-to-bottom linear gradient. PIL has no native gradient so we
    write a 1-pixel-wide column and stretch it."""
    col = Image.new("RGB", (1, HEIGHT))
    for y in range(HEIGHT):
        t = y / max(1, HEIGHT - 1)
        r = int(top_rgb[0] + (bottom_rgb[0] - top_rgb[0]) * t)
        g = int(top_rgb[1] + (bottom_rgb[1] - top_rgb[1]) * t)
        b = int(top_rgb[2] + (bottom_rgb[2] - top_rgb[2]) * t)
        col.putpixel((0, y), (r, g, b))
    return col.resize((WIDTH, HEIGHT))


def _wrap(text: str, font, max_width: int) -> list:
    """Greedy word-wrap to fit max_width pixels. Empty list for empty
    input."""
    if not text:
        return []
    words = text.split()
    if not words:
        return []
    lines = []
    current = words[0]
    dummy = Image.new("RGB", (10, 10))
    d = ImageDraw.Draw(dummy)
    for w in words[1:]:
        trial = current + " " + w
        bbox = d.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = w
    lines.append(current)
    return lines


def render(title: str, subtitle: str, accent: str) -> Optional[bytes]:
    """Return PNG bytes for the OG card, or None on any failure.

    Layout (1200x630):
      - 12px accent stripe across the top
      - "xrpldashboard" wordmark, top-left, 36px
      - Title, large bold, ~90px, wrapped to <= 2 lines
      - Subtitle, 38px, wrapped to <= 2 lines, light gray
      - Footer: "live data from the XRP Ledger" + ".com" alignment

    Returns None (caller serves the static fallback) if anything goes
    wrong — including Pillow missing."""
    if not _PIL_OK:
        return None
    try:
        img = _vgradient(BG_TOP, BG_BOTTOM)
        draw = ImageDraw.Draw(img)

        accent_rgb = _hex_to_rgb(accent)
        # Top accent stripe.
        draw.rectangle((0, 0, WIDTH, 12), fill=accent_rgb)

        # Soft accent halo in the lower-right corner — gives the card a
        # bit of depth without dominating the type.
        for radius, alpha in [(420, 24), (300, 40), (180, 60)]:
            overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            cx, cy = WIDTH + 60, HEIGHT + 60
            od.ellipse(
                (cx - radius, cy - radius, cx + radius, cy + radius),
                fill=(accent_rgb[0], accent_rgb[1], accent_rgb[2], alpha),
            )
            img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
            draw = ImageDraw.Draw(img)

        # Fonts (Pillow's bundled DejaVu, scaled).
        f_mark = ImageFont.load_default(size=34)
        f_title = ImageFont.load_default(size=84)
        f_sub = ImageFont.load_default(size=36)
        f_foot = ImageFont.load_default(size=26)

        # Wordmark, top-left. Slight inset.
        draw.text((72, 64), "xrpldashboard", fill=WORDMARK_COLOR, font=f_mark)

        # Title block. Wrap to width = 1056 (72 padding either side).
        max_w = WIDTH - 144
        title_lines = _wrap(title or "", f_title, max_w)[:2]
        y = 200
        for line in title_lines:
            draw.text((72, y), line, fill=TITLE_COLOR, font=f_title)
            y += 102

        # Subtitle block.
        y += 12
        sub_lines = _wrap(subtitle or "", f_sub, max_w)[:2]
        for line in sub_lines:
            draw.text((72, y), line, fill=SUBTITLE_COLOR, font=f_sub)
            y += 48

        # Footer pinned to bottom.
        footer = "live data from the XRP Ledger · xrpldashboard.com"
        draw.text((72, HEIGHT - 64), footer, fill=FOOTER_COLOR, font=f_foot)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return None

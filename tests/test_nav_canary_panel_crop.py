"""The nav canary must compare the dropdown panel, not the page behind it.

2026-11-10: rwa_desktop diverged 1.19% (tolerance 1%) with the dropdown
itself pixel identical -- same 11 items, same order, same colours. The
desktop capture clipped a VIEWPORT rect spanning summary+menu with pad=6,
so the pad ring and the gap beside the narrower summary showed the live
page underneath. 88% of the changed pixels sat in the left/right 25px
strips, where a digit behind the panel had gone 7 -> 8. Nothing in
templates/, CSS or JS had changed; only data had.

Re-baselining would have reset the clock until the next data change. The
fix is to compare the panel only, and these tests pin exactly that: a
digit changing OUTSIDE the panel is not a divergence, a menu item changing
INSIDE it is.
"""
from __future__ import annotations

import io
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from scripts import nav_dropdown_screenshot_canary as nav  # noqa: E402


W, H = 213, 495
# The dropdown surface within the capture. Everything outside is the live
# page showing through -- exactly what must not count.
PANEL = (25, 0, 188, 420)
BG = (10, 14, 39)       # page background
SURFACE = (18, 24, 54)  # dropdown panel
TEXT = (235, 238, 245)


def _capture() -> Image.Image:
    """A synthetic capture: panel surface, with page background around it."""
    img = Image.new("RGB", (W, H), BG)
    for x in range(PANEL[0], PANEL[2]):
        for y in range(PANEL[1], PANEL[3]):
            img.putpixel((x, y), SURFACE)
    # a few "menu item" rows inside the panel
    for row in (40, 80, 120, 160):
        for x in range(PANEL[0] + 8, PANEL[2] - 8):
            for y in range(row, row + 10):
                img.putpixel((x, y), TEXT)
    return img


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _change_digit_outside(img: Image.Image) -> Image.Image:
    """Simulate the 7 -> 8 behind the panel: a blob in the left strip."""
    out = img.copy()
    # Sized to clear DIVERGENCE_MAX_PCT when UNcropped (~1.7% of the frame).
    # A smaller blob measured 0.91% and slipped under the 1% tolerance, which
    # would have made the guard-the-guard test below vacuous: it would pass
    # because the change was too small to register, not because cropping
    # worked. Must stay left of PANEL[0]=25 to count as "outside".
    for x in range(2, 24):
        for y in range(190, 270):
            out.putpixel((x, y), TEXT)
    return out


def _change_menu_item(img: Image.Image) -> Image.Image:
    """Simulate a real regression: one menu row loses its text."""
    out = img.copy()
    for x in range(PANEL[0] + 8, PANEL[2] - 8):
        for y in range(80, 90):
            out.putpixel((x, y), SURFACE)
    return out


def test_digit_outside_the_panel_is_not_a_divergence():
    base = _capture()
    after = _change_digit_outside(base)
    a = nav.crop_to_panel(_png(base), PANEL)
    b = nav.crop_to_panel(_png(after), PANEL)
    pct = nav._diff_pct(a, b)
    assert pct == 0.0, f"live data behind the panel must not count, got {pct}"


def test_without_cropping_that_same_digit_would_have_fired():
    """Guard the guard: prove the crop is what fixes it, not the tolerance.

    Without this, the test above would pass even if the change were too
    small to register, and would prove nothing.
    """
    base = _capture()
    after = _change_digit_outside(base)
    pct = nav._diff_pct(_png(base), _png(after))
    assert pct > nav.DIVERGENCE_MAX_PCT, (
        f"uncropped, the outside digit should exceed the tolerance; got {pct}")


def test_changed_menu_item_is_still_a_divergence():
    base = _capture()
    after = _change_menu_item(base)
    a = nav.crop_to_panel(_png(base), PANEL)
    b = nav.crop_to_panel(_png(after), PANEL)
    pct = nav._diff_pct(a, b)
    assert pct > nav.DIVERGENCE_MAX_PCT, (
        f"a changed menu item must still fire; got {pct}")


def test_identical_captures_are_zero():
    base = _capture()
    a = nav.crop_to_panel(_png(base), PANEL)
    assert nav._diff_pct(a, a) == 0.0


def test_capture_clip_targets_the_menu_rect_only():
    """The capture must clip to .menu, not span summary+menu with padding.

    Pinned as source text because the clip runs inside the browser and
    cannot be exercised headlessly here.
    """
    src = open(os.path.join(HERE, "scripts",
                            "nav_dropdown_screenshot_canary.py")).read()
    assert "const mr = m.getBoundingClientRect();" in src
    assert "Math.min(sr.left, mr.left)" not in src, (
        "clip must not span the summary again -- that reintroduces the "
        "live-page bleed")
    assert "const pad = 6;" not in src, "the pad ring showed the page behind"

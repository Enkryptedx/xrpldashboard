"""nav_dropdown_screenshot_canary — weekly headless-screenshot check.

Charlie ruling 2026-09-22 Tue PM after the /observatory transparent-
dropdown bug: every week, load 3 pages in a headless browser, open
the Live-group nav dropdown, capture a screenshot, and compare the
pixel checksum of the DROPDOWN REGION against the previous week's.

Divergence beyond a small threshold = a CSS regression touched the
shared nav — walker_health finding + BetterStack page. Same shape as
public_route_200_canary except the trip is visual, not HTTP.

Runs weekly (StartInterval=604800). Uses Playwright's chromium since
that's what the venv already carries for the token-toml-fetcher path.

## Output

- Screenshots into `~/xrpl_test/nav_screenshots/<date>/<page>.png`
- Retention: last 8 weeks (matches the wallet retention weekly tier)
- Diff report into walker_health per page
- last_ok stamp `launchd_state/nav_dropdown_screenshot_canary_last_ok`
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db  # noqa: E402


WALKER_NAME = "nav_dropdown_screenshot_canary"
WALKER_CADENCE_SECONDS = 604800  # weekly
LAST_OK_STAMP = os.path.join(HERE, "launchd_state", "nav_dropdown_screenshot_canary_last_ok")
SCREENSHOT_DIR = os.path.join(HERE, "nav_screenshots")
KEEP_WEEKS = 8

BASE_URL = os.environ.get("PROBE_BASE_URL", "https://xrpldashboard.com")

# Pages to check — 5 shapes per Charlie's midday ruling: /, /observatory,
# /tokens, /whales, /rwa. Each entry: (slug, path, dropdown_selector).
# The desktop dropdown selector targets `details.group[data-group='live']`;
# the mobile hamburger is a separate structure (`.mobile-nav`) and uses a
# different selector — captured as a second pass on phone width.
PAGES = [
    # Home uses its own nav-strip (index.html) — the `.lang-switch` details
    # is the only real dropdown on that surface (no Live-group nav-group on /
    # despite _nav_groups.html being included). Regression surface on / is
    # therefore the language menu, not a nav-group menu. Charlie ruling
    # 2026-09-22 Tue 4:54 PM ET.
    ("home",        "/",            "nav.nav-strip .lang-switch"),
    ("observatory", "/observatory", "details.group[data-group='live']"),
    ("tokens",      "/tokens",      "details.group[data-group='live']"),
    ("whales",      "/whales",      "details.group[data-group='live']"),
    ("rwa",         "/rwa",         "details.group[data-group='live']"),
]


def _viewport_variants():
    """Desktop and phone widths. On phone, the desktop dropdown is
    display:none per the 780px breakpoint in _nav_groups.html, so we
    target the mobile hamburger's live section via a different selector
    in `_mobile_selector()`. Both catch CSS regressions in their own lane."""
    return [
        ("desktop", 1280, 800),
        ("phone", 390, 844),
    ]


# Mobile-specific selector: on phone width, click the hamburger button
# to open the full-screen menu, then screenshot the entire open panel.
# The mobile panel uses `<h4>` headers per group instead of separate
# details, so we capture the whole `.mobile-panel` — any CSS regression
# affecting the Live section will show up in that screenshot.
MOBILE_OPEN_SELECTOR = ".mobile-nav > summary"
MOBILE_LIVE_SECTION = ".mobile-nav[open] .mobile-panel"


def _pixel_hash(png_bytes: bytes) -> str:
    """SHA-256 of the PNG bytes — quick equality check. A full pixel
    diff can come later if we ever want a similarity score."""
    return hashlib.sha256(png_bytes).hexdigest()


def _prune_old_screenshots():
    """Keep the last KEEP_WEEKS weekly dirs. Tolerant of failures."""
    try:
        entries = sorted(
            (e for e in os.listdir(SCREENSHOT_DIR)
             if os.path.isdir(os.path.join(SCREENSHOT_DIR, e))),
            reverse=True,
        )
        for stale in entries[KEEP_WEEKS:]:
            stale_dir = os.path.join(SCREENSHOT_DIR, stale)
            for f in os.listdir(stale_dir):
                try:
                    os.remove(os.path.join(stale_dir, f))
                except OSError:
                    pass
            try:
                os.rmdir(stale_dir)
            except OSError:
                pass
    except (OSError, FileNotFoundError):
        pass


def _stamp_last_ok() -> None:
    try:
        os.makedirs(os.path.dirname(LAST_OK_STAMP), exist_ok=True)
        with open(LAST_OK_STAMP, "w") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


# ── Determinism (2026-09-26, Charlie chore "region mask") ──
# Two captures minutes apart differed on 1–8 percent of pixels across the
# WHOLE clip (see DAILY_2026-09-26): the dropdown's padding + box-shadow
# and its 0.5 percent translucency show the live page underneath (ledger
# chip, hero animations, coin runway), so identical CSS still hashed
# differently. Fix = paint an opaque overlay UNDER the nav (same page
# background colour) before capturing, kill animations/transitions, wait
# for web fonts. Belt-and-braces: compare with a small pixel tolerance
# (anti-aliasing) instead of an exact hash, and report the max share.
STABILIZE_CSS = """
  html, body { background: #0a0e27 !important; }
  body::before { content: ''; position: fixed; inset: 0; background: #0a0e27; z-index: 2147483000; pointer-events: none; }
  header, .nav, .nav-groups, .mobile-nav, nav { position: relative; z-index: 2147483100; }
  *, *::before, *::after { animation: none !important; transition: none !important; caret-color: transparent !important; }
  body::before { animation: none !important; }
  /* live XRP price chip sits INSIDE the nav (above the overlay): freeze it */
  .nav .xrp-chip { visibility: hidden !important; }
"""
DIVERGENCE_MAX_PCT = float(os.environ.get("NAV_CANARY_DIVERGENCE_MAX_PCT", "1.0"))


def _stabilize(page) -> None:
    page.add_style_tag(content=STABILIZE_CSS)
    try:
        page.evaluate("() => document.fonts && document.fonts.ready")
    except Exception:
        pass
    page.wait_for_timeout(300)


def _diff_pct(png_a: bytes, png_b: bytes) -> float | None:
    """Share (percent) of pixels whose max channel delta exceeds 24/255.
    None when sizes differ (that IS a divergence) or Pillow is missing."""
    try:
        import io
        from PIL import Image, ImageChops
    except ImportError:
        return None
    a = Image.open(io.BytesIO(png_a)).convert("RGB")
    b = Image.open(io.BytesIO(png_b)).convert("RGB")
    if a.size != b.size:
        return None
    d = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 24 else 0)
    hist = d.histogram()
    changed = hist[255] if len(hist) > 255 else 0
    return 100.0 * changed / float(a.size[0] * a.size[1])


def _capture_one(page, path: str, selector: str, is_mobile: bool = False) -> tuple[bytes, dict]:
    """Navigate, open the Live-group dropdown (or the mobile hamburger's
    Live section), screenshot the region. Returns (png_bytes, meta)."""
    url = BASE_URL.rstrip("/") + path
    page.goto(url, wait_until="networkidle", timeout=15_000)
    _stabilize(page)
    if is_mobile:
        # Phone: open the hamburger, screenshot the Live section within.
        page.wait_for_selector(MOBILE_OPEN_SELECTOR, timeout=5_000)
        page.click(MOBILE_OPEN_SELECTOR)
        try:
            page.wait_for_selector(MOBILE_LIVE_SECTION, state="visible", timeout=2_000)
            handle = page.query_selector(MOBILE_LIVE_SECTION)
        except Exception:
            # If the mobile-nav doesn't expose a .mobile-group[data-group='live']
            # section, fall back to screenshotting the full open panel.
            handle = page.query_selector(".mobile-nav[open]")
        if not handle:
            raise RuntimeError("mobile-nav open panel not found after click")
        png = handle.screenshot()
        return png, {"url": url, "mode": "mobile"}
    # Desktop: click summary, wait for menu, screenshot the summary+menu
    # region. Screenshotting the details element itself is wrong here —
    # `.menu` is `position: absolute`, so it does not contribute to the
    # details bounding box, and `handle.screenshot()` on the details
    # returns a summary-sized ~57x30 placeholder that misses the menu
    # entirely. Instead, take a viewport screenshot clipped to a rect
    # that spans the summary and the open menu together, so any CSS
    # regression to the dropdown surface actually lands in the image.
    page.wait_for_selector(selector, timeout=5_000)
    page.click(selector + " > summary")
    page.wait_for_selector(selector + " .menu", state="visible", timeout=2_000)
    # `state="visible"` returns as soon as .menu.display != "none", which
    # can happen a frame before the native <details> toggle finishes and
    # the menu's getBoundingClientRect() reports non-zero dimensions.
    # Without this wait, the clip below collapses to just the summary
    # rect (~57x30) and the screenshot is a useless placeholder — that
    # was the 2026-09-22 desktop-placeholder incident. Wait for a
    # meaningful menu height before taking the clip rect.
    page.wait_for_function(
        f"() => {{ const m = document.querySelector({selector!r} + ' .menu'); "
        "return m && m.getBoundingClientRect().height > 20; }",
        timeout=2_000,
    )
    clip = page.evaluate(
        """
        (sel) => {
          const d = document.querySelector(sel);
          if (!d) return null;
          const s = d.querySelector('summary');
          const m = d.querySelector('.menu');
          if (!s || !m) return null;
          const sr = s.getBoundingClientRect();
          const mr = m.getBoundingClientRect();
          const pad = 6;
          const x = Math.max(0, Math.floor(Math.min(sr.left, mr.left) - pad));
          const y = Math.max(0, Math.floor(Math.min(sr.top, mr.top) - pad));
          const right = Math.ceil(Math.max(sr.right, mr.right) + pad);
          const bottom = Math.ceil(Math.max(sr.bottom, mr.bottom) + pad);
          return { x, y, width: right - x, height: bottom - y };
        }
        """,
        selector,
    )
    if not clip or clip["width"] <= 0 or clip["height"] <= 0:
        raise RuntimeError(f"clip not computable for {selector}")
    png = page.screenshot(clip=clip)
    return png, {"url": url, "mode": "desktop"}


def main() -> int:
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = "not_yet_stamped"
    try:
        # Import lazily so we can report a clean walker_health message
        # if playwright isn't installed yet.
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            message = "playwright_not_installed (pip install playwright && playwright install chromium)"
            ok = False
            print(f"[{WALKER_NAME}] {message}")
            return 1

        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        run_dir = os.path.join(SCREENSHOT_DIR, time.strftime("%Y-%m-%d"))
        os.makedirs(run_dir, exist_ok=True)

        # Load previous run's hashes for regression detection
        prev_hash = {}
        prev_png = {}
        max_pct = 0.0
        prev_dirs = sorted(
            (e for e in os.listdir(SCREENSHOT_DIR)
             if os.path.isdir(os.path.join(SCREENSHOT_DIR, e))
             and e < time.strftime("%Y-%m-%d")),
            reverse=True,
        )
        if prev_dirs:
            prev = os.path.join(SCREENSHOT_DIR, prev_dirs[0])
            for f in os.listdir(prev):
                if f.endswith(".png"):
                    with open(os.path.join(prev, f), "rb") as fh:
                        prev_png[f] = fh.read()
                        prev_hash[f] = _pixel_hash(prev_png[f])

        results = []
        divergences = []
        capture_errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for slug, path, selector in PAGES:
                    for vname, width, height in _viewport_variants():
                        ctx = browser.new_context(viewport={"width": width, "height": height})
                        page = ctx.new_page()
                        try:
                            png, meta = _capture_one(
                                page, path, selector,
                                is_mobile=(vname == "phone"),
                            )
                            fname = f"{slug}_{vname}.png"
                            out = os.path.join(run_dir, fname)
                            with open(out, "wb") as fh:
                                fh.write(png)
                            h = _pixel_hash(png)
                            results.append((fname, h))
                            if fname in prev_hash and prev_hash[fname] != h:
                                pct = _diff_pct(prev_png[fname], png)
                                if pct is not None:
                                    max_pct = max(max_pct, pct)
                                if pct is None or pct > DIVERGENCE_MAX_PCT:
                                    divergences.append(
                                        f"{fname}: {prev_hash[fname][:12]} → {h[:12]}"
                                        f" ({'size' if pct is None else f'{pct:.2f}%'})")
                        except Exception as e:
                            # Capture errors are NOT divergences on first-run
                            # (no previous baseline). They only page after we
                            # have a baseline that the capture would compare
                            # against.
                            capture_errors.append(
                                f"{slug}_{vname}: {type(e).__name__}: {str(e)[:80]}"
                            )
                        finally:
                            ctx.close()
            finally:
                browser.close()

        # First-run semantics: if we have NO prior baseline, everything
        # captured this run BECOMES the baseline. Capture errors get
        # logged but don't fail the walker (they'd otherwise block first-
        # cycle install).
        is_first_run = not prev_hash

        _prune_old_screenshots()
        _stamp_last_ok()

        run_label = "first-run/baseline" if is_first_run else "regression-check"
        message = (
            f"captured={len(results)} divergences={len(divergences)} "
            f"capture_errors={len(capture_errors)} mode={run_label} "
            f"max_diff_pct={max_pct:.2f} tolerance_pct={DIVERGENCE_MAX_PCT:g}"
        )
        if capture_errors:
            # Print all errors — first-run may hit selectors that don't
            # exist yet (mobile-nav open panel etc.); we log so we can
            # tune. Don't fail the walker on errors alone.
            for e in capture_errors:
                print(f"  capture_error: {e}", file=sys.stderr)
        if divergences and not is_first_run:
            message += " | " + "; ".join(divergences[:3])
            print(f"[{WALKER_NAME}] {message}")
            ok = False
            return 1
        ok = True
        print(f"[{WALKER_NAME}] {message}")
        return 0
    except Exception as e:
        message = f"exception: {type(e).__name__}: {e}"
        raise
    finally:
        if ok is not None:  # write once, either from the early divergence path or here
            try:
                db.write_walker_health_end(
                    WALKER_NAME, ok=ok,
                    message=message or ("clean_no_message" if ok else "unlabeled_failure"),
                )
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())

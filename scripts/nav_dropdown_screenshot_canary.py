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

# Pages to check — one per template shape (extends base, standalone, and
# the specific one Charlie flagged). Each entry:
#   (slug, path, dropdown_selector)
# The dropdown_selector is a Playwright selector for the <summary> that
# opens the Live-group menu. We click it, then screenshot the parent
# details.group with the menu attached.
PAGES = [
    ("home",        "/",           "details.group[data-group='live']"),
    ("observatory", "/observatory", "details.group[data-group='live']"),
    ("tokens",      "/tokens",      "details.group[data-group='live']"),
]


def _viewport_variants():
    """Desktop and phone widths — the 780px breakpoint in _nav_groups.html
    swaps to a hamburger, so capturing both catches the swap boundary too."""
    return [
        ("desktop", 1280, 800),
        ("phone", 390, 844),
    ]


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


def _capture_one(page, path: str, selector: str) -> tuple[bytes, dict]:
    """Navigate, open the Live-group dropdown, screenshot the dropdown
    region. Returns (png_bytes, meta)."""
    url = BASE_URL.rstrip("/") + path
    page.goto(url, wait_until="networkidle", timeout=15_000)
    # Wait for the nav to hydrate
    page.wait_for_selector(selector, timeout=5_000)
    # Click the summary to open the dropdown
    page.click(selector + " > summary")
    # Small wait for the menu to render
    page.wait_for_selector(selector + " .menu", state="visible", timeout=2_000)
    # Screenshot the details.group (includes the summary + open menu)
    handle = page.query_selector(selector)
    if not handle:
        raise RuntimeError(f"selector not found after click: {selector}")
    png = handle.screenshot()
    return png, {"url": url}


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
                        prev_hash[f] = _pixel_hash(fh.read())

        results = []
        divergences = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for slug, path, selector in PAGES:
                    for vname, width, height in _viewport_variants():
                        ctx = browser.new_context(viewport={"width": width, "height": height})
                        page = ctx.new_page()
                        try:
                            png, meta = _capture_one(page, path, selector)
                            fname = f"{slug}_{vname}.png"
                            out = os.path.join(run_dir, fname)
                            with open(out, "wb") as fh:
                                fh.write(png)
                            h = _pixel_hash(png)
                            results.append((fname, h))
                            if fname in prev_hash and prev_hash[fname] != h:
                                divergences.append(f"{fname}: {prev_hash[fname][:12]} → {h[:12]}")
                        except Exception as e:
                            divergences.append(f"{slug}_{vname}: capture_error {type(e).__name__}: {e}")
                        finally:
                            ctx.close()
            finally:
                browser.close()

        _prune_old_screenshots()
        _stamp_last_ok()

        message = f"captured={len(results)} divergences={len(divergences)}"
        if divergences:
            message += " | " + "; ".join(divergences[:3])
            # Report as a walker_health finding — pager L1 reads
            # walker_health.findings_count so a divergence pages.
            db.write_walker_health_end(
                WALKER_NAME, ok=False, message=message,
            )
            print(f"[{WALKER_NAME}] {message}")
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

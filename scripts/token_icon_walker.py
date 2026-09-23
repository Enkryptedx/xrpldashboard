"""token_icon_walker — hourly coin emblem fetcher + rasterizer.

Charlie ruling 2026-09-23 16:29 ET (item 2 coin emblems overnight cycle):
  - Read the icon URL from each verified / self-described issuer's TOML
    `[[TOKENS]]` entry. Today's 14; more as TOMLs add them.
  - Fetch with size cap (≤256 KB) + short timeout.
  - Rasterize SVG → PNG (never serve SVG).
  - Store under a sha256-hashed filename in static/coin_emblems/.
  - Record (currency, issuer, sha256, source_url, fetched_at, ...) in
    token_icon (jj_ro-readable).
  - Flagged tokens (ticker_collision OR non_standard_code) NEVER get an
    emblem — no row inserted, so template fallback (initials + amber
    ring) fires and an impostor can't wear a real logo.
  - Fail-open on any fetch error: record fail_status, continue with the
    next candidate. Never abort the run for a single upstream 404.

Cadence: hourly (StartInterval=3600s). walker_health-tracked.

## Storage
  - PNG only (SVG is rasterized to PNG via cairosvg).
  - Filename = <sha256_of_png_bytes>.png. Content-addressed → same bytes
    across issuers collapse to one file; a change in an issuer's icon
    lands as a new file (old one orphans until the next hourly sweep
    picks it up).
  - Path: `static/coin_emblems/<sha256>.png`. Served directly by Flask
    at `/static/coin_emblems/<sha256>.png`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import os
import ssl
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import certifi

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import db
from two_way_toml_verifier_walker import _fetch_toml  # reuses TOML fetcher

# Match two_way_toml_verifier_walker's SSL context (Charlie ruling
# 2026-09-10 Part C): macOS Python 3.14 doesn't ship a system trust
# store — without certifi, urlopen() SSLCertVerificationError's every
# https icon URL.
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

from PIL import Image
try:
    import cairosvg
    _HAVE_CAIROSVG = True
except Exception:
    _HAVE_CAIROSVG = False


WALKER_NAME = "token_icon_walker"
WALKER_CADENCE_SECONDS = 3600  # hourly

ICON_MAX_BYTES = 2 * 1024 * 1024  # 2 MB — Charlie ruling 2026-09-23 16:48
                                  # ET: 256 KB rejected 29/48 real logos on the
                                  # first run. The served PNG is downscaled to
                                  # 128×128 by the rasterizer below (typically
                                  # <5 KB out), so the source cap is only about
                                  # what we're willing to pull over the wire.
FETCH_TIMEOUT = 15  # seconds — bumped with the cap so a 2 MB fetch on a slow
                    # gateway has room to finish inside one attempt.
RASTER_SIZE = 128  # square PNG output — the served-file dimension. Source is
                   # always downscaled to this, so any fetched size is tiny out.

EMBLEM_DIR = Path(REPO_ROOT) / "static" / "coin_emblems"
EMBLEM_DIR.mkdir(parents=True, exist_ok=True)

_UA = "xrpldashboard-token-icon-walker/1.0 (+https://xrpldashboard.com)"


def _load_workload() -> list[dict]:
    """Return one row per (currency_hex, issuer) that we should attempt
    an icon fetch for: verified OR self-described, non-flagged, with a
    TOML URL to read from.

    Flagged rows (ticker_collision OR non_standard_code) are excluded
    at the SQL layer per Charlie's anti-impostor rule."""
    with db.pg_connect() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT
                   tcc.currency_hex, tcc.issuer, tcc.citation_url
              FROM token_category_current tcc
              LEFT JOIN token_facts tf
                ON tf.currency_hex = tcc.currency_hex
               AND tf.issuer = tcc.issuer
             WHERE tcc.tier IN ('verified', 'self-described')
               AND tcc.citation_url IS NOT NULL
               AND tcc.citation_url LIKE 'https://%%.well-known/xrp-ledger.toml'
               AND COALESCE(tf.ticker_collision, FALSE) = FALSE
               AND COALESCE(tf.non_standard_code, FALSE) = FALSE
            """
        )
        return [
            {"currency_hex": r[0], "issuer": r[1], "toml_url": r[2]}
            for r in cur.fetchall()
        ]


def _find_icon_url(toml: dict, currency_hex: str, issuer: str) -> Optional[str]:
    """Look for an `icon` field in [[TOKENS]] matching (currency, issuer).
    The registry_form_submissions path uses hex currency; TOML files
    can use either the short form or hex. Match either."""
    if not isinstance(toml, dict):
        return None
    tokens = toml.get("TOKENS") or toml.get("tokens") or []
    if isinstance(tokens, dict):
        tokens = [tokens]
    # Derive the short (3-letter) form if the hex is 40 chars ASCII-padded
    short = None
    try:
        if len(currency_hex) == 40:
            decoded = bytes.fromhex(currency_hex).rstrip(b"\0").decode(
                "utf-8", "ignore"
            )
            if decoded and all(0x20 <= ord(c) < 0x7F for c in decoded):
                short = decoded
    except Exception:
        pass

    for t in tokens:
        if not isinstance(t, dict):
            continue
        if (t.get("issuer") or "").strip() != issuer:
            continue
        toml_cx = (t.get("currency") or "").strip()
        # Match either hex form or the short-ticker form
        if toml_cx == currency_hex or (short and toml_cx == short):
            icon = t.get("icon")
            if isinstance(icon, str) and icon.strip().startswith(("http://", "https://")):
                return icon.strip()
    return None


def _fetch_icon(url: str) -> tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Return (raw_bytes, content_type, error_reason). Fails open — a
    failed fetch returns (None, None, reason)."""
    try:
        req = Request(url, headers={"User-Agent": _UA})
        with urlopen(req, timeout=FETCH_TIMEOUT, context=_SSL_CTX) as resp:
            ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            raw = resp.read(ICON_MAX_BYTES + 1)
            if len(raw) > ICON_MAX_BYTES:
                return None, ctype, f"over_size_cap_{ICON_MAX_BYTES}"
            return raw, ctype, None
    except HTTPError as e:
        return None, None, f"http_{e.code}"
    except URLError as e:
        return None, None, f"url_error_{type(e.reason).__name__ if hasattr(e, 'reason') else 'unknown'}"
    except Exception as e:
        return None, None, f"unhandled_{type(e).__name__}"


def _rasterize_to_png(raw: bytes, ctype: str) -> tuple[Optional[bytes], Optional[str], Optional[int], Optional[int]]:
    """Return (png_bytes, error_reason, width, height). Accepts PNG /
    JPG / WEBP / GIF via Pillow, SVG via cairosvg. Normalizes to a
    square PNG at RASTER_SIZE×RASTER_SIZE."""
    # Detect SVG by content-type or by magic markers
    is_svg = (
        "svg" in (ctype or "")
        or raw[:5] == b"<?xml"
        or raw[:4] == b"<svg"
        or b"<svg" in raw[:512]
    )
    try:
        if is_svg:
            if not _HAVE_CAIROSVG:
                return None, "svg_no_rasterizer", None, None
            png_bytes = cairosvg.svg2png(
                bytestring=raw,
                output_width=RASTER_SIZE,
                output_height=RASTER_SIZE,
            )
            # Round-trip through Pillow to normalize colorspace + get dims
            img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        else:
            img = Image.open(io.BytesIO(raw))
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
        # Center-crop to square, then resize
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        img = img.resize((RASTER_SIZE, RASTER_SIZE), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue(), None, RASTER_SIZE, RASTER_SIZE
    except Exception as e:
        return None, f"raster_{type(e).__name__}", None, None


def _store_and_upsert(cur, row: dict, png: bytes, source_url: str,
                     ctype: Optional[str], size_bytes: int,
                     width: int, height: int) -> str:
    sha = hashlib.sha256(png).hexdigest()
    stored_path = f"coin_emblems/{sha}.png"
    fs_path = EMBLEM_DIR / f"{sha}.png"
    if not fs_path.exists():
        fs_path.write_bytes(png)
    cur.execute(
        """
        INSERT INTO token_icon (
            currency_hex, issuer, sha256, source_url, source_mime_type,
            stored_path, width_px, height_px, source_size_bytes,
            fetched_at, fetch_status, fail_reason
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), 'ok', NULL)
        ON CONFLICT (currency_hex, issuer) DO UPDATE
        SET sha256 = EXCLUDED.sha256,
            source_url = EXCLUDED.source_url,
            source_mime_type = EXCLUDED.source_mime_type,
            stored_path = EXCLUDED.stored_path,
            width_px = EXCLUDED.width_px,
            height_px = EXCLUDED.height_px,
            source_size_bytes = EXCLUDED.source_size_bytes,
            fetched_at = EXCLUDED.fetched_at,
            fetch_status = 'ok',
            fail_reason = NULL
        """,
        (row["currency_hex"], row["issuer"], sha, source_url, ctype,
         stored_path, width, height, size_bytes),
    )
    return sha


def _record_failure(cur, row: dict, source_url: str, ctype: Optional[str],
                    fetch_status: str, reason: str) -> None:
    cur.execute(
        """
        INSERT INTO token_icon (
            currency_hex, issuer, sha256, source_url, source_mime_type,
            stored_path, width_px, height_px, source_size_bytes,
            fetched_at, fetch_status, fail_reason
        ) VALUES (%s, %s, '', %s, %s, '', NULL, NULL, NULL,
                  NOW(), %s, %s)
        ON CONFLICT (currency_hex, issuer) DO UPDATE
        SET source_url = EXCLUDED.source_url,
            source_mime_type = EXCLUDED.source_mime_type,
            fetched_at = EXCLUDED.fetched_at,
            fetch_status = EXCLUDED.fetch_status,
            fail_reason = EXCLUDED.fail_reason
        """,
        (row["currency_hex"], row["issuer"], source_url or "", ctype,
         fetch_status, reason[:400] if reason else None),
    )


def run_walker() -> dict:
    if not db.pg_available():
        raise SystemExit("STRICT-REFUSE: PG unavailable")
    workload = _load_workload()
    stats = dict(workload=len(workload), toml_fetched=0, no_icon=0,
                 fetched_ok=0, raster_ok=0, upserted=0, failures=0)
    # Cache TOML per URL so we don't re-fetch for tokens sharing a domain
    toml_cache: dict[str, tuple[Optional[dict], Optional[str]]] = {}

    with db.pg_connect() as conn:
        for row in workload:
            toml_url = row["toml_url"]
            if toml_url in toml_cache:
                toml, toml_err = toml_cache[toml_url]
            else:
                toml, toml_err = _fetch_toml(toml_url)
                toml_cache[toml_url] = (toml, toml_err)
                if toml is not None:
                    stats["toml_fetched"] += 1
            if toml_err:
                with conn.cursor() as cur:
                    _record_failure(cur, row, toml_url, None,
                                    f"toml_{toml_err}", toml_err)
                    conn.commit()
                stats["failures"] += 1
                continue
            icon_url = _find_icon_url(toml, row["currency_hex"], row["issuer"])
            if not icon_url:
                stats["no_icon"] += 1
                with conn.cursor() as cur:
                    _record_failure(cur, row, toml_url, None,
                                    "no_icon_in_toml",
                                    "TOML has no [[TOKENS]] entry with icon "
                                    "field for this (currency, issuer)")
                    conn.commit()
                continue
            raw, ctype, fetch_err = _fetch_icon(icon_url)
            if fetch_err:
                stats["failures"] += 1
                with conn.cursor() as cur:
                    _record_failure(cur, row, icon_url, ctype,
                                    fetch_err, fetch_err)
                    conn.commit()
                continue
            stats["fetched_ok"] += 1
            png, raster_err, w, h = _rasterize_to_png(raw, ctype or "")
            if raster_err or png is None:
                stats["failures"] += 1
                with conn.cursor() as cur:
                    _record_failure(cur, row, icon_url, ctype,
                                    raster_err or "raster_unknown",
                                    raster_err or "raster failed")
                    conn.commit()
                continue
            stats["raster_ok"] += 1
            with conn.cursor() as cur:
                _store_and_upsert(cur, row, png, icon_url, ctype,
                                  len(raw), w or 0, h or 0)
                conn.commit()
            stats["upserted"] += 1
            print(
                f"[token_icon] OK {row['issuer']} {row['currency_hex'][:12]}… "
                f"← {icon_url}  ({len(raw)}B → {len(png)}B PNG)",
                flush=True,
            )
    return stats


def main() -> int:
    db.write_walker_health_start(
        WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS,
    )
    ok = False
    message = "not_yet_stamped"
    t0 = time.time()
    print(
        f"[token_icon] start {dt.datetime.now(dt.timezone.utc).isoformat()}",
        flush=True,
    )
    try:
        stats = run_walker()
        elapsed = time.time() - t0
        ok = True
        message = " ".join(f"{k}={v}" for k, v in stats.items()) + f" elapsed={elapsed:.1f}s"
        print(f"[token_icon] end OK: {message}", flush=True)
    except SystemExit as e:
        message = f"strict_refuse: {e}"
        print(f"[token_icon] {message}", file=sys.stderr, flush=True)
        return 1
    except Exception as e:
        message = f"unhandled_{type(e).__name__}: {str(e)[:120]}"
        print(f"[token_icon] {message}", file=sys.stderr, flush=True)
        return 1
    finally:
        try:
            db.write_walker_health_end(
                WALKER_NAME, ok=ok,
                message=message or ("clean_no_message" if ok else "unlabeled_failure"),
            )
        except Exception as e:
            print(f"[token_icon] walker_health end write failed: {e}",
                  file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

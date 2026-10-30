#!/usr/bin/env python3
"""tokens_summary_walker -- precompute /tokens' heavy inputs into one row.

House pattern from /nfts (nft_activity_walker --mode summary). Every 300 s:

  1. ensure_tokens_summary_table()  (single owner; SET lock_timeout first)
  2. compute, using the SAME app.py functions the route used live:
       aggregates_24h / _7d / _all   ranked lists, 50 rows each
       hero                          _compute_hero_30d over the 30-day list
       bucket_stats                  (min, max, distinct hour buckets)
       warnings_recent               3 h / 15 rows
       tier_lookup, price_map        the two lookup maps
       label_lookup_json             the ~7,300-pair inferred-label JSON,
                                     built ONCE here instead of per request
       latest_bucket                 drives the page's data-age label
  3. write only the columns whose content hash changed (computed_at always
     advances so the route's staleness label stays honest).

WHY. On 2026-10-19 the live Server-Timing showed /tokens cold-worker
requests at 7-12 s TTFB: three Render dynos x three gunicorn workers each
held a private 60 s ttl_cache, so most visitors paid the full compute. A
single precomputed row makes every request the warm case, and the walker
has a walker_health row so the L1 pager notices if it stops -- which a
per-worker cache never could.

Normalisation on write: Decimal -> float, tuple rows -> lists, tuple dict
keys -> 'cur|iss'. The route decodes the pair keys back.

Runs from the Mac via launchd (launchd/run_tokens_summary_walker.sh).
"""
from __future__ import annotations

import argparse
import datetime
import decimal
import json
import logging
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [tokens_summary_walker] %(levelname)s %(message)s")
log = logging.getLogger("tokens_summary_walker")

WALKER_NAME = "tokens_summary_walker"
CADENCE_SECONDS = 300
RANGES = {"aggregates_24h": 24, "aggregates_7d": 24 * 7, "aggregates_all": None}
LIST_LIMIT = 50


def _jsonable(o):
    """Deep-convert to what JSONB can hold losslessly for the route."""
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, (datetime.datetime, datetime.date)):
        return o.isoformat()
    if isinstance(o, dict):
        return {(k if isinstance(k, str) else "|".join(map(str, k)) if isinstance(k, tuple) else str(k)):
                _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    return o


def compute_summary(app_mod, db_mod):
    """Return the dict of column -> value for write_tokens_summary.
    Uses the route's own loaders so the row cannot drift from live."""
    t0 = time.perf_counter()
    stages = {}

    def mark(name):
        stages[name] = round((time.perf_counter() - t0) * 1000)

    values = {}
    for col, hours in RANGES.items():
        values[col] = _jsonable(app_mod._cached_token_volume_aggregates(hours_back=hours, limit=LIST_LIMIT))
    mark("aggregates")

    stats = app_mod._cached_token_volume_bucket_stats()
    values["bucket_stats"] = _jsonable(list(stats)) if stats else None
    values["latest_bucket"] = int(stats[1]) if stats and stats[1] is not None else None
    mark("stats")

    tokens_meta = app_mod._load_token_names_dict()
    hero_snapshot = app_mod._load_d1_hero_snapshot()
    hero_enriched = app_mod._build_hero_enriched_30d(tokens_meta)
    values["hero"] = _jsonable(app_mod._compute_hero_30d(hero_enriched, hero_snapshot))
    mark("hero")

    tier_lookup, _src = app_mod._cached_tier_lookup_map()
    values["tier_lookup"] = _jsonable(tier_lookup)
    values["price_map"] = _jsonable(app_mod._cached_token_prices_map())
    mark("maps")

    inferred = app_mod._cached_token_category_inferred_map()
    values["label_lookup_json"] = json.dumps(
        app_mod._build_inferred_label_lookup(inferred, tier_lookup), separators=(",", ":"))
    mark("labels")

    values["warnings_recent"] = _jsonable(app_mod._cached_token_warnings_recent(hours_back=3, limit=15))
    mark("warnings")
    return values, stages


def _record_xrp_usd_sample(db_mod, dry_run: bool) -> str:
    """Append this tick's XRP/USD observation to xrp_usd_history.

    WHY HERE. The anchor is otherwise never persisted -- it lives in
    price_oracle's ~60s in-memory cache and nowhere else -- so there is no
    way to answer "what was XRP worth when this payment closed?". /wallet
    can therefore only label its dollar figures as render-time
    conversions: the same 185,017,485.5 XRP transfer read $276.9M on one
    page and $232.1M on another (2026-10-30). This walker is the write
    point because it already ticks every 300s, which bounds a future
    tx-time lookup to +/-2.5 min of a payment's close.

    THIS IS A PASSENGER. The walker's job is the /tokens summary row; a
    price-history failure must not mark the tick failed, because that
    pages L1 for an unrelated concern and would let one bad oracle sample
    suppress the summary that the route actually serves. But a silent
    failure is just as bad, so the status string is returned and folded
    into the walker_health message -- visible in the row, without
    flipping ok.

    A missing table is a normal pre-DDL state, not an error: this code
    ships before the owner pastes the migration, and until then every
    tick reports skipped_no_table and writes nothing.
    """
    try:
        from price_oracle import xrp_usd_sample
        sample = xrp_usd_sample()
    except Exception as e:  # noqa: BLE001 -- reported, never fails the tick
        log.warning("xrp_usd sample failed: %s", e)
        return f"xrp_usd=oracle_error:{type(e).__name__}"

    rate = sample.get("chosen_rate")
    shape = "rate=" + (f"{rate:.4f}" if rate is not None else "none")
    if sample.get("check_failed"):
        shape += ",check_failed"

    if dry_run:
        return f"xrp_usd=dry-run({shape})"
    try:
        status = db_mod.write_xrp_usd_history_sample(sample)
    except Exception as e:  # noqa: BLE001 -- reported, never fails the tick
        log.warning("xrp_usd_history write failed: %s", e)
        return f"xrp_usd=error:{type(e).__name__}"
    return f"xrp_usd={status}({shape})"


def run(dry_run: bool = False) -> tuple[bool, str]:
    import db
    os.environ.setdefault("TOKENS_SUMMARY_WALKER", "1")
    import app as app_mod  # the route's loaders; heavy import, done once per tick

    if not dry_run:
        ensured = db.ensure_tokens_summary_table()
        log.info("ensure_tokens_summary_table -> %s", ensured)
        if not ensured:
            return False, "ensure_tokens_summary_table failed (see db error log)"

    values, stages = compute_summary(app_mod, db)
    sizes = {c: len(json.dumps(v, default=str)) if v is not None else 0 for c, v in values.items()}
    xrp_usd_status = _record_xrp_usd_sample(db, dry_run)
    if dry_run:
        return True, f"dry-run stages_ms={stages} bytes={sizes} {xrp_usd_status}"

    prior = db.read_tokens_summary_hashes()
    changed = db.write_tokens_summary(values, prior_hashes=prior)
    msg = (f"wrote={len(changed)}/{len(db.TOKENS_SUMMARY_COLUMNS)} changed={changed} "
           f"label_bytes={sizes.get('label_lookup_json', 0)} stages_ms={stages} "
           f"{xrp_usd_status}")
    return True, msg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="compute, report sizes, write nothing")
    args = ap.parse_args()
    import db

    if not args.dry_run:
        db.write_walker_health_start(WALKER_NAME, cadence_seconds=CADENCE_SECONDS)
    ok, message = False, None
    t = time.time()
    try:
        ok, message = run(dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001 -- recorded, never swallowed
        ok, message = False, f"{type(e).__name__}: {str(e)[:200]}"
        log.exception("tick failed")
    finally:
        message = f"{message} elapsed={time.time() - t:.1f}s"
        if not args.dry_run:
            db.write_walker_health_end(WALKER_NAME, ok=ok, message=message)
    log.info("end ok=%s %s", ok, message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

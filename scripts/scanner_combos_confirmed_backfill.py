#!/usr/bin/env python3
"""Retroactive backfill for page_view_scanner_combos_confirmed.

Walks page_views history one day at a time. For each day D, computes the
scanner-detection rule (≥30 hits AND hits ≤ 1.10 × distinct visitor_hashes)
over the trailing 7-day window [D-7d, D]. Any (path, user_agent) combo that
meets the rule in ANY historical window is a candidate for confirmed status.

Emits a review file (JSON) — does NOT insert into
page_view_scanner_combos_confirmed. The insert step is a separate
human-reviewed operation (see docs/IS_BOT_SCANNER_MEMORY_FIX_2026-07-26.md
§2, deploy-order theorem).

Governance fields captured per candidate:
  - first_seen_window_start/end: earliest 7d window in which the combo qualified
  - first_seen_ratio: hits / distinct_visitors at first confirmation
  - first_seen_row_count: hits at first confirmation
  - total_windows_qualified: how many daily-step windows this combo has qualified in
                             (a proxy for how persistent the burst was)
  - most_recent_window_end: last day's window in which the combo qualified

Rider from Charlie 2026-07-26: 'a full-history walk may surface combos we don't
want ratified (a real user's weird UA that once tripped a ratio on low volume);
the governance fields make that review a ten-minute read, and anything ambiguous
gets left unconfirmed rather than ratcheted.' — hence review-file, not direct-insert.

Usage:
    python3 scripts/scanner_combos_confirmed_backfill.py [--step-days N] [--out PATH]

Default step: 1 day. Default output: scratch/scanner_combos_confirmed_review_YYYY-MM-DD.json
"""
import argparse
import datetime
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

logging.basicConfig(
    format="%(asctime)s [scanner_backfill] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

WINDOW_SECS = 7 * 86400
MIN_HITS = 30
RATIO_CEILING = 1.10  # hits / distinct_visitors — matches existing scanner detection


def _detect_window(cur, ts_end):
    """Run the scanner-detection rule over [ts_end - 7d, ts_end].
    Returns list of (path, user_agent, hits, distinct_visitors, ratio)."""
    ts_start = ts_end - WINDOW_SECS
    cur.execute(
        "SELECT path, user_agent, COUNT(*)::int AS hits, "
        "       COUNT(DISTINCT visitor_hash)::int AS distinct_visitors "
        "  FROM page_views "
        " WHERE user_agent IS NOT NULL "
        "   AND ts > %s AND ts <= %s "
        " GROUP BY path, user_agent "
        "HAVING COUNT(*) >= %s "
        "   AND COUNT(*) <= COUNT(DISTINCT visitor_hash) * %s",
        [ts_start, ts_end, MIN_HITS, RATIO_CEILING],
    )
    rows = []
    for path, ua, hits, dv in cur.fetchall():
        ratio = float(hits) / max(dv, 1)
        rows.append((path, ua, hits, dv, ratio))
    return ts_start, rows


def run(step_days=1, out_path=None):
    if not db.pg_available():
        log.error("DATABASE_URL not configured — exiting")
        sys.exit(1)

    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM page_views")
            ts_min, ts_max, total_rows = cur.fetchone()
            if ts_min is None:
                log.error("page_views is empty — nothing to backfill")
                sys.exit(1)

            log.info(
                "history: %s → %s (%d rows, %.1f days)",
                datetime.datetime.fromtimestamp(ts_min, datetime.timezone.utc).date(),
                datetime.datetime.fromtimestamp(ts_max, datetime.timezone.utc).date(),
                total_rows,
                (ts_max - ts_min) / 86400,
            )

            # Step daily from (ts_min + 7d) to ts_max. Each iteration computes
            # detection over the trailing 7-day window ending at that day.
            # First eligible window ends at ts_min + 7d (so it has a full 7d
            # of history to look at).
            step = int(step_days * 86400)
            first_end = ts_min + WINDOW_SECS
            windows = list(range(first_end, ts_max + step, step))
            log.info(
                "will scan %d daily windows (step=%d days, window=7 days)",
                len(windows),
                step_days,
            )

            # Accumulator: (path, ua) → {first_seen_...., total_windows, most_recent_end}
            candidates = {}
            for i, ts_end in enumerate(windows, start=1):
                ts_end = min(ts_end, ts_max)
                ts_start, detected = _detect_window(cur, ts_end)
                if i % 10 == 0 or i == len(windows):
                    log.info(
                        "window %d/%d ending %s: %d combos qualified",
                        i,
                        len(windows),
                        datetime.datetime.fromtimestamp(ts_end, datetime.timezone.utc).date(),
                        len(detected),
                    )
                for path, ua, hits, dv, ratio in detected:
                    key = (path, ua)
                    if key not in candidates:
                        candidates[key] = {
                            "path": path,
                            "user_agent": ua,
                            "first_seen_window_start": ts_start,
                            "first_seen_window_end": ts_end,
                            "first_seen_hits": hits,
                            "first_seen_distinct_visitors": dv,
                            "first_seen_ratio": round(ratio, 4),
                            "total_windows_qualified": 1,
                            "max_hits_in_any_window": hits,
                            "max_ratio_in_any_window": round(ratio, 4),
                            "most_recent_window_end": ts_end,
                        }
                    else:
                        c = candidates[key]
                        c["total_windows_qualified"] += 1
                        c["most_recent_window_end"] = ts_end
                        if hits > c["max_hits_in_any_window"]:
                            c["max_hits_in_any_window"] = hits
                        if ratio > c["max_ratio_in_any_window"]:
                            c["max_ratio_in_any_window"] = round(ratio, 4)

    # Sort output: by total_windows_qualified DESC (most persistent bursts first),
    # then by first_seen_window_end ASC (chronological within same persistence)
    sorted_candidates = sorted(
        candidates.values(),
        key=lambda c: (-c["total_windows_qualified"], c["first_seen_window_end"]),
    )

    # Add human-readable timestamps for the reviewer
    for c in sorted_candidates:
        c["first_seen_window_start_iso"] = (
            datetime.datetime.fromtimestamp(
                c["first_seen_window_start"], datetime.timezone.utc
            ).isoformat()
        )
        c["first_seen_window_end_iso"] = (
            datetime.datetime.fromtimestamp(
                c["first_seen_window_end"], datetime.timezone.utc
            ).isoformat()
        )
        c["most_recent_window_end_iso"] = (
            datetime.datetime.fromtimestamp(
                c["most_recent_window_end"], datetime.timezone.utc
            ).isoformat()
        )

    if out_path is None:
        out_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scratch",
            f"scanner_combos_confirmed_review_{datetime.date.today().isoformat()}.json",
        )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    output = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "scanned_windows": len(windows),
        "step_days": step_days,
        "window_days": 7,
        "detection_rule": {
            "min_hits": MIN_HITS,
            "ratio_ceiling": RATIO_CEILING,
        },
        "history_range": {
            "ts_min": ts_min,
            "ts_max": ts_max,
            "days": round((ts_max - ts_min) / 86400, 1),
            "total_page_views_rows": total_rows,
        },
        "candidate_count": len(sorted_candidates),
        "candidates": sorted_candidates,
    }

    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, sort_keys=False)

    log.info("wrote %d candidates to %s", len(sorted_candidates), out_path)
    log.info(
        "review this file, then run scanner_combos_confirmed_populate.py "
        "with the approved subset"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--step-days", type=int, default=1)
    ap.add_argument("--out", type=str, default=None)
    args = ap.parse_args()
    run(step_days=args.step_days, out_path=args.out)

"""AI Web Observatory — daily roll-up walker (Charlie ruling
2026-09-21 Mon PM, item 3a).

Aggregates `ai_crawler_hits` by (ua_class, page_family, day),
excluding self-probes, into `ai_crawler_daily_rollup`. Powers the
public /observatory page which serves ONE-WEEK-DELAYED weekly views.

Page family classifier — a small closed vocabulary so the public
page can group without leaking exact endpoints:
- amendments, tokens, check, changes, registry, snapshots, whales,
  pools, lending, rlusd, methodology, home, thisweek, other.

`token_pages_top5` (top 5 token pages fetched by AI crawlers, names
only) is computed separately by the /observatory route from
page_views + token_names; this walker only handles the family
roll-up.

Cadence: hourly (so partial days show up during the day; the final
row settles at day boundary).
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db  # noqa: E402


WALKER_NAME = "ai_crawler_daily_rollup"
WALKER_CADENCE_SECONDS = 3600


def _classify_path(path: str) -> str:
    """Map a URL path to a page family. Closed vocabulary; anything
    unrecognized lands in 'other'. Case-insensitive."""
    if not path:
        return "other"
    p = path.lower()
    if p == "/" or p == "":
        return "home"
    if p.startswith("/amendments"):
        return "amendments"
    if p.startswith("/token") or p.startswith("/tokens"):
        return "tokens"
    if p.startswith("/check"):
        return "check"
    if p.startswith("/changes"):
        return "changes"
    if p.startswith("/registry"):
        return "registry"
    if p.startswith("/.well-known/snapshots") or p.startswith("/snapshots"):
        return "snapshots"
    if p.startswith("/.well-known/registry"):
        return "registry"
    if p.startswith("/whales"):
        return "whales"
    if p.startswith("/pools"):
        return "pools"
    if p.startswith("/lending"):
        return "lending"
    if p.startswith("/rlusd"):
        return "rlusd"
    if p.startswith("/nfts") or p.startswith("/nft"):
        return "nfts"
    if p.startswith("/mpts") or p.startswith("/mpt"):
        return "mpts"
    if p.startswith("/methodology"):
        return "methodology"
    if p.startswith("/thisweek"):
        return "thisweek"
    return "other"


def rollup_day(day_iso: str) -> int:
    """Read ai_crawler_hits for UTC day `day_iso` and upsert one row per
    (ua_class, page_family). Returns number of rows written."""
    if not db.pg_available():
        return 0
    day = dt.date.fromisoformat(day_iso)
    start_ts = int(dt.datetime.combine(day, dt.time.min).replace(
        tzinfo=dt.timezone.utc).timestamp())
    end_ts = int(dt.datetime.combine(
        day + dt.timedelta(days=1), dt.time.min
    ).replace(tzinfo=dt.timezone.utc).timestamp())

    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ua_class, path, COUNT(*) "
                "FROM ai_crawler_hits "
                "WHERE ts >= %s AND ts < %s "
                "  AND ua_class IS NOT NULL AND ua_class <> '' "
                "GROUP BY ua_class, path",
                (start_ts, end_ts),
            )
            rows = cur.fetchall()

    # Reduce (ua_class, path) → (ua_class, family)
    per_family: dict[tuple[str, str], int] = {}
    for ua_class, path, cnt in rows:
        family = _classify_path(path or "")
        key = (ua_class, family)
        per_family[key] = per_family.get(key, 0) + int(cnt)

    def _do(conn):
        with conn.cursor() as cur:
            for (ua_class, family), hits in per_family.items():
                cur.execute(
                    "INSERT INTO ai_crawler_daily_rollup "
                    "  (as_of_date, ua_class, page_family, hits, computed_at) "
                    "VALUES (%s::date, %s, %s, %s, now()) "
                    "ON CONFLICT (as_of_date, ua_class, page_family) "
                    "DO UPDATE SET hits = EXCLUDED.hits, computed_at = now()",
                    (day_iso, ua_class, family, hits),
                )
    db._writer_execute_with_retry(f"ai_crawler_rollup[{day_iso}]", _do)
    return len(per_family)


def main() -> int:
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = "not_yet_stamped"
    try:
        today = dt.date.today().isoformat()
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        # Roll up yesterday (final) + today (partial). Yesterday is a
        # settled day; today keeps updating throughout the day and
        # settles at day boundary.
        n_today = rollup_day(today)
        n_yesterday = rollup_day(yesterday)
        message = (
            f"today={today}:{n_today} yesterday={yesterday}:{n_yesterday}"
        )
        print(f"[ai_crawler_daily_rollup] {message}")
        ok = True
        return 0
    except Exception as e:
        message = f"exception: {type(e).__name__}: {e}"
        raise
    finally:
        db.write_walker_health_end(
            WALKER_NAME, ok=ok,
            message=message or ("clean_no_message" if ok else "unlabeled_failure"),
        )


if __name__ == "__main__":
    sys.exit(main())

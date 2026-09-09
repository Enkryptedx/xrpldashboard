"""Route 5xx-rate walker — rolling 1h 5xx% per route from page_views.

Charlie ruling 2026-09-09 morning (post /registry/taxonomy incident):
"a walker that queries this shape every 15 min and pages when any
route's rolling pct_500 exceeds 5% over the last hour. Would give us
a proactive 5xx-rate signal on any route, not just what the
public_route_200_canary happens to probe."

Behavior:
  - StartInterval 900s (15 min). Reads page_views for the last 3600s
    with status IS NOT NULL, groups by path, computes n_total / n_5xx /
    pct_5xx per path.
  - Whitelist filter: only paths present in the public_route_200_canary
    ROUTES list are considered. Vanity-path noise (scanner probes,
    404 farms, /wp-admin fishing) is ignored regardless of 5xx rate —
    those aren't our routes.
  - Threshold: findings emitted when a whitelisted route has
    n_total >= 20 AND pct_5xx > 5%. The 20-request floor stops a
    single bad hit on a quiet route from paging.
  - Raw numbers per whitelisted route are ALWAYS included in the
    walker_health message, whether paged or not — helps triage
    when the pager fires and gives a running signal-of-signals to
    read the message even on OK runs.
  - Backfill note: page_views rows written before 2026-09-09 00:53 UTC
    (commit 226df44) have status IS NULL and are silently dropped by
    the "status IS NOT NULL" filter. So the walker's first day of
    signal is rolling forward, not retroactive.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import traceback

WALKER_NAME = "route_5xx_rate_walker"
CADENCE_SECONDS = 900  # matches plist StartInterval
WINDOW_SECONDS = 3600  # rolling 1h
MIN_REQUESTS = 20      # floor to filter out low-volume noise
PCT_THRESHOLD = 5.0    # pct_5xx above this = finding
DAILY_SUMMARY_PCT = 1.0  # in the daily report, "no route above X%" line


def _monitored_paths() -> set[str]:
    """Whitelist derived from public_route_200_canary's ROUTES list.
    Query strings stripped so /check.json matches /check.json?q=..."""
    import public_route_200_canary as pc
    out = set()
    for entry in pc.ROUTES:
        # ROUTES is a mix of 3-tuples and 4-tuples per per-route override
        path = entry[0]
        if "?" in path:
            path = path.split("?", 1)[0]
        out.add(path)
    return out


def _query_window(cur, start_ts: int, end_ts: int, only_paths: set[str]):
    """Return list of dicts {path, n_total, n_5xx, pct_5xx, n_2xx, n_4xx}
    for whitelisted paths active in [start_ts, end_ts) with any status
    recorded. Sorted by n_5xx desc so paged routes read first."""
    if not only_paths:
        return []
    cur.execute(
        """
        SELECT path,
               COUNT(*) FILTER (WHERE status IS NOT NULL)    AS n_total,
               COUNT(*) FILTER (WHERE status >= 500)         AS n_5xx,
               COUNT(*) FILTER (WHERE status >= 200 AND status < 300) AS n_2xx,
               COUNT(*) FILTER (WHERE status >= 400 AND status < 500) AS n_4xx
        FROM page_views
        WHERE ts >= %s AND ts < %s
          AND status IS NOT NULL
          AND path = ANY(%s)
        GROUP BY path
        HAVING COUNT(*) FILTER (WHERE status IS NOT NULL) > 0
        """,
        (start_ts, end_ts, sorted(only_paths)),
    )
    rows = []
    for r in cur.fetchall():
        n_total = r[1]
        n_5xx = r[2]
        pct_5xx = (100.0 * n_5xx / n_total) if n_total else 0.0
        rows.append({
            "path": r[0],
            "n_total": n_total,
            "n_5xx": n_5xx,
            "n_2xx": r[3],
            "n_4xx": r[4],
            "pct_5xx": round(pct_5xx, 2),
        })
    rows.sort(key=lambda x: (-x["n_5xx"], -x["n_total"]))
    return rows


def _findings(rows) -> list[dict]:
    return [r for r in rows
            if r["n_total"] >= MIN_REQUESTS and r["pct_5xx"] > PCT_THRESHOLD]


def _format_message(rows, findings) -> str:
    """Compact walker_health message. Findings first (with counts),
    then raw per-route line for every route in the window."""
    parts = []
    if findings:
        finding_summaries = ", ".join(
            f"{f['path']}={f['pct_5xx']}%({f['n_5xx']}/{f['n_total']})"
            for f in findings
        )
        parts.append(f"BREACH: {finding_summaries}")
    else:
        parts.append("ok")
    if rows:
        raw = " | ".join(
            f"{r['path']}={r['pct_5xx']}%({r['n_5xx']}/{r['n_total']})"
            for r in rows
        )
        parts.append(raw)
    else:
        parts.append("no status-tagged rows in window")
    return " · ".join(parts)


def daily_summary_line(cur, date: dt.date) -> str:
    """One-line summary for the morning report — worst route by 5xx%
    yesterday (full UTC day), floored at DAILY_SUMMARY_PCT."""
    start = int(dt.datetime(date.year, date.month, date.day, 0, 0, 0,
                            tzinfo=dt.timezone.utc).timestamp())
    end = start + 86400
    rows = _query_window(cur, start, end, _monitored_paths())
    if not rows:
        return "5xx: no status-tagged pageviews yesterday."
    above = [r for r in rows if r["pct_5xx"] >= DAILY_SUMMARY_PCT]
    if not above:
        return f"5xx: no route above {DAILY_SUMMARY_PCT:g}% yesterday."
    worst = above[0]  # already sorted by n_5xx desc
    return (f"5xx worst yesterday: {worst['path']}="
            f"{worst['pct_5xx']:g}% ({worst['n_5xx']}/{worst['n_total']}).")


def main(argv: list[str]) -> int:
    import db
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=CADENCE_SECONDS)
    try:
        end_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        start_ts = end_ts - WINDOW_SECONDS
        monitored = _monitored_paths()
        with db.pg_connect() as conn:
            with conn.cursor() as cur:
                rows = _query_window(cur, start_ts, end_ts, monitored)
        findings = _findings(rows)
        msg = _format_message(rows, findings)
        # Truncate walker_health message to keep it readable in
        # /walker_health rendering; the full row list can be re-queried.
        msg = msg[:800]
        # Print full detail to stdout for the launchd log — no truncation.
        print(f"[{WALKER_NAME}] window=1h monitored_paths={len(monitored)} "
              f"rows_with_data={len(rows)} findings={len(findings)}")
        for r in rows:
            marker = "!" if r in findings else " "
            print(f"  {marker} {r['path']:<40} "
                  f"n={r['n_total']:>4} 5xx={r['n_5xx']:>3} "
                  f"({r['pct_5xx']:.2f}%)")
        db.write_walker_health_end(
            WALKER_NAME, ok=True, message=msg,
            findings_count=len(findings),
        )
        return 0
    except Exception:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        try:
            import db
            db.write_walker_health_end(
                WALKER_NAME, ok=False,
                message=f"exc={tb.strip().splitlines()[-1][:180]}",
                findings_count=1,
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))

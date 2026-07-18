#!/usr/bin/env python3
"""Re-stamp rlusd_supply_history.eth_mints_24h/eth_burns_24h to calendar-day UTC.

Two eras of the ETH column co-existed until 2026-07-18:
  * 2026-05-25 → 2026-06-19 (25 rows): non-null but *trailing-24h ending at
    snapshot time*. Snapshots ran near midnight UTC, so numbers approximated
    calendar-day but were structurally off by hours — occasionally a full day
    (the 2026-06-19 row was actually the 2026-06-18 $20.5M burn).
  * 2026-06-20 → 2026-07-18 (27 rows): null'd by
    migrations/2026_07_17_rlusd_false_flat_null_history.sql after the eth_getLogs
    silent-fabricate bug shipped 28 days of $0. Live walker went through
    a8ad475 and 1566b07 partial fixes but publicnode rate-limits meant the 24h
    walk never completed after 2026-06-19.

This script re-derives all existing rows in [2026-05-25, 2026-07-18] as UTC
calendar-day totals from Etherscan V2 `tokentx` (see rlusd_etherscan.py). One
column, one semantic — no June-20 seam where a chart quietly splices two
definitions.

Behavior:
  * Default: DRY-RUN. Prints per-row before/after diff, no writes.
  * --apply: performs UPDATEs. Only touches eth_mints_24h + eth_burns_24h;
    all other columns (supply, holders, xrpl_*) left untouched.
  * --date YYYY-MM-DD: re-derive only that single date (spot-check).
  * Does NOT insert missing dates (2026-05-26 and 2026-07-15 gaps). Those
    are pre-existing walker outages; filling them would require historical
    supply values (a separate concern).

Usage:
  cd ~/xrpl_test && source ~/.config/xrpldashboard/env
  ./venv/bin/python ops/backfill_rlusd_eth_calendar_day.py            # dry-run all
  ./venv/bin/python ops/backfill_rlusd_eth_calendar_day.py --apply    # commit all
  ./venv/bin/python ops/backfill_rlusd_eth_calendar_day.py --date 2026-06-19
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

# Allow importing rlusd_etherscan / db from the repo root when invoked from ops/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
import rlusd_etherscan as rle  # noqa: E402


def _connect():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL not set — source ~/.config/xrpldashboard/env first")
    return psycopg.connect(dsn)


def _existing_dates(conn, only: date | None) -> list[date]:
    with conn.cursor() as cur:
        if only:
            cur.execute(
                "SELECT snapshot_date FROM rlusd_supply_history "
                "WHERE snapshot_date = %s",
                (only,),
            )
        else:
            cur.execute(
                "SELECT snapshot_date FROM rlusd_supply_history "
                "ORDER BY snapshot_date"
            )
        return [r[0] for r in cur.fetchall()]


def _current_row(conn, d: date) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT eth_mints_24h, eth_burns_24h FROM rlusd_supply_history "
            "WHERE snapshot_date = %s",
            (d,),
        )
        return cur.fetchone()


def _fmt(v) -> str:
    if v is None:
        return "         —      "
    return f"${float(v):>14,.2f}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="commit UPDATEs (default: dry-run print only)")
    parser.add_argument("--date", type=str, default=None,
                        help="single UTC date YYYY-MM-DD (default: all existing rows)")
    args = parser.parse_args()

    only = None
    if args.date:
        only = datetime.strptime(args.date, "%Y-%m-%d").date()

    conn = _connect()
    dates = _existing_dates(conn, only)
    if not dates:
        print(f"No rows found for {'date ' + str(only) if only else 'range'}.")
        return
    today = datetime.utcnow().date()

    print(f"Re-deriving {len(dates)} row(s) from Etherscan V2 tokentx (calendar-day UTC)")
    print(f"Mode: {'APPLY (writes to Neon)' if args.apply else 'DRY-RUN (no writes)'}")
    print()
    print(f"{'date':<12} {'old mints':>16} {'old burns':>16}   {'new mints':>16} {'new burns':>16}   change")
    print("-" * 108)

    updates: list[tuple[date, float, float]] = []
    total_delta_mints = 0.0
    total_delta_burns = 0.0

    for d in dates:
        old = _current_row(conn, d)
        old_m, old_b = old if old else (None, None)
        try:
            new_m, new_b = rle.aggregate_calendar_day(d)
        except Exception as e:
            print(f"{d}  ERROR: {type(e).__name__}: {e}")
            continue

        # Flag today: partial calendar day (not yet finalized)
        today_marker = "  ← partial (today so far)" if d == today else ""

        def _diff(old_val, new_val):
            if old_val is None:
                return "SET" if new_val > 0.01 else "SET·0"
            oldf = float(old_val)
            if abs(oldf - new_val) < 0.005:
                return "="
            return f"{'+' if new_val > oldf else '−'}${abs(new_val - oldf):,.2f}"

        change = f"m:{_diff(old_m, new_m)}  b:{_diff(old_b, new_b)}"
        print(f"{d}  {_fmt(old_m)} {_fmt(old_b)}   {_fmt(new_m)} {_fmt(new_b)}   {change}{today_marker}")

        if old_m is not None:
            total_delta_mints += new_m - float(old_m)
        if old_b is not None:
            total_delta_burns += new_b - float(old_b)
        updates.append((d, new_m, new_b))

    print("-" * 108)
    print(f"Aggregate delta vs prior non-null: mints {total_delta_mints:+,.2f}   burns {total_delta_burns:+,.2f}")
    print()

    if not args.apply:
        print("Dry-run complete. Re-run with --apply to write.")
        return

    print(f"Writing {len(updates)} UPDATEs...")
    with conn.cursor() as cur:
        for d, m, b in updates:
            cur.execute(
                "UPDATE rlusd_supply_history SET "
                "  eth_mints_24h = %s, "
                "  eth_burns_24h = %s "
                "WHERE snapshot_date = %s",
                (Decimal(str(m)), Decimal(str(b)), d),
            )
    conn.commit()
    print(f"Applied. {len(updates)} rows updated.")


if __name__ == "__main__":
    main()

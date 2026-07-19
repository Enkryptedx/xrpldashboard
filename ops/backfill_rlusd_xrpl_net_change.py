#!/usr/bin/env python3
"""Backfill rlusd_supply_history.xrpl_net_change_24h from gateway_balances.

Option A backfill. Peer to ops/backfill_rlusd_eth_calendar_day.py
(shipped 2026-07-18 for the ETH side re-derivation).

Context:
  * migrations/2026_07_17_rlusd_false_flat_null_history.sql NULL'd
    xrpl_mints_24h / xrpl_burns_24h across the entire history window
    (2026-05-25 → 2026-07-17, 52 rows) because a Payment-only sweep
    produced 53 days of silent-fabricate $0.
  * migrations/2026_07_19_xrpl_net_change_24h.sql adds a new
    xrpl_net_change_24h column, with different semantic (signed net
    supply change) and different derivation (gateway_balances snapshot-
    diff at UTC-day-boundary ledgers).
  * The old columns stay NULL — this backfill only writes the new column.

Backfill IS derivable: fixture pull 2026-07-18 confirmed
gateway_balances works cleanly on s2.ripple.com (full-history rippled)
for arbitrary historical ledgers back through 2026-05-25 at least.
Cost: 4 RPC calls per row (find_boundary × 2 + gateway_balances × 2),
~2 seconds per row with rate-limit sleep, so ~2-3 minutes for the
full 52-row backfill.

Behavior:
  * Default: DRY-RUN. Prints per-row proposed value + before (NULL
    everywhere), no writes.
  * --apply: performs UPDATEs. Only touches xrpl_net_change_24h;
    all other columns left untouched.
  * --date YYYY-MM-DD: re-derive only that single date (spot-check).
  * Skips today's row (partial day) — the live walker owns it.
  * Does NOT insert missing dates (a couple of walker-outage gaps
    exist per the ETH backfill notes). Filling gaps would require
    inserting new rows with supply values from live gateway_balances
    at those historical ledgers — a related but separate task, gated
    on Charlie's review of the ETH backfill's same-scoped choice.

Usage:
  cd ~/xrpl_test && source ~/.config/xrpldashboard/env
  ./venv/bin/python ops/backfill_rlusd_xrpl_net_change.py           # dry-run all
  ./venv/bin/python ops/backfill_rlusd_xrpl_net_change.py --apply   # commit all
  ./venv/bin/python ops/backfill_rlusd_xrpl_net_change.py --date 2026-07-02
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

# Allow importing rlusd_xrpl_option_a / db from the repo root when
# invoked from ops/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
import rlusd_xrpl_option_a as rxo  # noqa: E402


def _connect():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit(
            "DATABASE_URL not set — source ~/.config/xrpldashboard/env first"
        )
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


def _current_row(conn, d: date):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT xrpl_net_change_24h FROM rlusd_supply_history "
            "WHERE snapshot_date = %s",
            (d,),
        )
        r = cur.fetchone()
        return r[0] if r else None


def _fmt(v) -> str:
    if v is None:
        return "         —      "
    return f"{'+' if float(v) >= 0 else '−'}${abs(float(v)):>13,.2f}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true",
        help="commit UPDATEs (default: dry-run print only)",
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="single UTC date YYYY-MM-DD (default: all existing rows)",
    )
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

    print(f"Deriving xrpl_net_change_24h for {len(dates)} row(s) "
          f"from gateway_balances snapshot-diff (Option A, s2.ripple.com)")
    print(f"Mode: {'APPLY (writes to Neon)' if args.apply else 'DRY-RUN (no writes)'}")
    print()
    print(f"{'date':<12} {'old':>18}   {'new':>18}   note")
    print("-" * 78)

    updates: list[tuple[date, float]] = []

    for d in dates:
        if d == today:
            print(f"{d}  {_fmt(_current_row(conn, d))}   {'(skipped)':>18}   today (partial) — owned by live walker")
            continue
        old = _current_row(conn, d)
        try:
            net = rxo.aggregate_calendar_day(d)
        except Exception as e:
            print(f"{d}  ERROR: {type(e).__name__}: {e}")
            continue

        if old is None:
            change = "SET"
        elif abs(float(old) - net) < 0.005:
            change = "="
        else:
            change = f"Δ ${abs(net - float(old)):,.2f}"
        print(f"{d}  {_fmt(old)}   {_fmt(net)}   {change}")
        updates.append((d, net))

        # Be nice to s2.ripple.com — 4 RPC calls per row already, add a
        # small pause between rows so a 52-row backfill doesn't hammer.
        time.sleep(0.5)

    print("-" * 78)
    print(f"{len(updates)} row(s) ready to write.")
    print()

    if not args.apply:
        print("Dry-run complete. Re-run with --apply to write.")
        return

    print(f"Writing {len(updates)} UPDATEs...")
    with conn.cursor() as cur:
        for d, net in updates:
            cur.execute(
                "UPDATE rlusd_supply_history SET "
                "  xrpl_net_change_24h = %s "
                "WHERE snapshot_date = %s",
                (Decimal(str(net)), d),
            )
    conn.commit()
    print(f"Applied. {len(updates)} rows updated.")


if __name__ == "__main__":
    main()

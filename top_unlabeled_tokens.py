#!/usr/bin/env python3
"""Print the top unlabeled tokens by recent trade count.

Use this to decide which tokens to add to TOKEN_NAMES.md so they migrate
out of the "unlabeled" lane on /tokens into their proper category lane.

Reads from the same source the /tokens route does — Postgres if available,
local volumes.db otherwise. Prints rank, trade count, hours active, decoded
symbol attempt, issuer, and an XRPSCAN account link to identify each one.

Usage:
    python top_unlabeled_tokens.py            # default: 7d, top 25
    python top_unlabeled_tokens.py --hours 24 --top 50
"""
import argparse
import os
import sys
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import app
import db


def fetch_rows(hours_back, limit):
    """Pull (currency, issuer, trades, hours_active) — PG first, SQLite fallback."""
    if db.pg_available():
        try:
            return db.read_token_volume_aggregates(hours_back=hours_back, limit=limit)
        except Exception as e:
            print(f"# pg read failed ({e}); falling back to local volumes.db", file=sys.stderr)
    if not os.path.exists(app.VOLUMES_DB_PATH):
        print(f"no volumes.db at {app.VOLUMES_DB_PATH}", file=sys.stderr)
        sys.exit(1)
    where, params = "", []
    if hours_back is not None:
        cutoff = int(time.time() // 3600) - hours_back
        where = "WHERE hour_bucket >= ?"
        params.append(cutoff)
    params.append(limit)
    conn = sqlite3.connect(f"file:{app.VOLUMES_DB_PATH}?mode=ro", uri=True)
    try:
        return conn.execute(
            f"SELECT currency, issuer, SUM(trade_count) AS trades, "
            f"COUNT(*) AS hours_active FROM token_volume {where} "
            f"GROUP BY currency, issuer ORDER BY trades DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=24 * 7, help="lookback window in hours (default 168 = 7d)")
    ap.add_argument("--top", type=int, default=25, help="how many unlabeled to print (default 25)")
    ap.add_argument("--scan", type=int, default=300, help="how deep to scan for candidates (default 300)")
    args = ap.parse_args()

    rows = fetch_rows(args.hours, args.scan)
    labels = app._load_token_names_dict()
    unlabeled = [r for r in rows if (r[0], r[1]) not in labels]

    src = "postgres" if db.pg_available() else f"sqlite ({app.VOLUMES_DB_PATH})"
    print(f"# source: {src}")
    print(f"# window: last {args.hours}h  ·  scanned: {len(rows)} tokens  ·  unlabeled: {len(unlabeled)}")
    print()
    print(f"{'#':>3}  {'trades':>8}  {'hrs':>4}  {'symbol':<14}  issuer")
    print("-" * 100)
    for i, (cur, iss, trades, hours) in enumerate(unlabeled[: args.top], 1):
        decoded = app._decode_currency_hex(cur) or ""
        if decoded:
            symbol = decoded
        elif cur and len(cur) == 40:
            symbol = cur[:6] + "…"
        else:
            symbol = cur or "?"
        print(f"{i:>3}  {int(trades or 0):>8,}  {int(hours or 0):>4}  {symbol:<14}  {iss}")
        if not decoded and cur and len(cur) == 40:
            print(f"      hex:    {cur}")
        print(f"      issuer: https://xrpscan.com/account/{iss}")


if __name__ == "__main__":
    main()

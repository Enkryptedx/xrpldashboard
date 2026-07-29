#!/usr/bin/env python3
"""D1 pull 1: top-100 unlabeled (currency, issuer) pairs by 30d trade count.

Unlabeled = key `{currency_hex}:{issuer}` is NOT in token_names.json.
NOTE: token_volume.volume_xrp is not populated by the current walker
(all zeros as of 2026-07-03). The /tokens page is ranked by trade_count
per the brief; we use the same measure here. All "share" columns
below are share-of-trades, not share-of-XRP-value.

Writes JSON to /Users/charliebruce/xrpl_test/scratch/d1_unlabeled_top100.json
and prints a preview.
"""
import json
import os
import sys

import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TOKEN_NAMES = json.load(open(os.path.join(ROOT, "token_names.json")))
LABELED_KEYS = set(TOKEN_NAMES.keys())

WINDOW_HOURS = 24 * 30

def main():
    import time
    now_bucket = int(time.time()) // 3600
    since = now_bucket - WINDOW_HOURS

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT currency, issuer,
                       SUM(trade_count) AS trades_30d,
                       SUM(volume_xrp)  AS volume_xrp_30d
                FROM token_volume
                WHERE hour_bucket >= %s
                GROUP BY currency, issuer
                ORDER BY trades_30d DESC
                """,
                (since,),
            )
            all_rows = cur.fetchall()

    # Rank by trade_count (volume_xrp is unpopulated by walker).
    total_trades = sum(int(r[2] or 0) for r in all_rows)
    labeled_trades = sum(
        int(r[2] or 0) for r in all_rows
        if f"{r[0]}:{r[1]}" in LABELED_KEYS
    )
    unlabeled_rows = [r for r in all_rows if f"{r[0]}:{r[1]}" not in LABELED_KEYS]
    unlabeled_trades = sum(int(r[2] or 0) for r in unlabeled_rows)

    top100 = unlabeled_rows[:100]
    top100_trades = sum(int(r[2] or 0) for r in top100)

    out = {
        "generated_at": int(time.time()),
        "window_hours": WINDOW_HOURS,
        "measure": "trade_count (volume_xrp column is unpopulated by walker)",
        "totals": {
            "total_pairs": len(all_rows),
            "unlabeled_pairs": len(unlabeled_rows),
            "total_trades_30d": total_trades,
            "labeled_trades_30d": labeled_trades,
            "unlabeled_trades_30d": unlabeled_trades,
            "labeled_share_of_total": (labeled_trades / total_trades) if total_trades else 0,
            "unlabeled_share_of_total": (unlabeled_trades / total_trades) if total_trades else 0,
            "top100_share_of_unlabeled": (top100_trades / unlabeled_trades) if unlabeled_trades else 0,
            "top100_share_of_total": (top100_trades / total_trades) if total_trades else 0,
        },
        "rows": [
            {
                "rank": i + 1,
                "currency": r[0],
                "issuer": r[1],
                "trades_30d": int(r[2] or 0),
                "share_of_unlabeled_trades": (int(r[2] or 0) / unlabeled_trades) if unlabeled_trades else 0,
                "share_of_total_trades": (int(r[2] or 0) / total_trades) if total_trades else 0,
            }
            for i, r in enumerate(top100)
        ],
    }

    scratch = os.path.join(ROOT, "scratch")
    os.makedirs(scratch, exist_ok=True)
    outpath = os.path.join(scratch, "d1_unlabeled_top100.json")
    with open(outpath, "w") as f:
        json.dump(out, f, indent=2)

    t = out["totals"]
    print(f"Written: {outpath}")
    print(f"Total 30d /tokens pairs: {t['total_pairs']:,}")
    print(f"Unlabeled pairs: {t['unlabeled_pairs']:,}")
    print(f"Total 30d trades: {t['total_trades_30d']:,}")
    print(f"Labeled share: {t['labeled_share_of_total']*100:.1f}%")
    print(f"Unlabeled share: {t['unlabeled_share_of_total']*100:.1f}%")
    print(f"Top-100 unlabeled covers {t['top100_share_of_unlabeled']*100:.1f}% of unlabeled trades")
    print(f"Top-5 unlabeled preview:")
    for row in out["rows"][:5]:
        print(f"  #{row['rank']}: {row['currency']}:{row['issuer']}  trades={row['trades_30d']:,}  share_of_unlabeled={row['share_of_unlabeled_trades']*100:.2f}%")


if __name__ == "__main__":
    main()

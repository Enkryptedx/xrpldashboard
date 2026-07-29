#!/usr/bin/env python3
"""D1 v2 punch-list #3: per-category 30d trade counts for the labeled cohort.

For each category in token_names.json, sum trade_count over the last 30d
against token_volume. Categories with 0 named tokens are omitted."""
import json
import os
import time
from collections import defaultdict

import psycopg

TOKEN_NAMES = json.load(open("/Users/charliebruce/xrpl_test/token_names.json"))
OUT_PATH = "/Users/charliebruce/xrpl_test/scratch/d1_v2_categories.json"


def main():
    now_bucket = int(time.time()) // 3600
    since = now_bucket - 24 * 30

    # Build (currency,issuer) → category map.
    cat_for_pair = {}
    cat_pair_counts = defaultdict(int)
    for key, meta in TOKEN_NAMES.items():
        cat = meta.get("category") or "no_category"
        cat_for_pair[key] = cat
        cat_pair_counts[cat] += 1

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT currency, issuer, SUM(trade_count)
                FROM token_volume
                WHERE hour_bucket >= %s
                GROUP BY currency, issuer
                """,
                (since,),
            )
            all_rows = cur.fetchall()

    cat_trades = defaultdict(int)
    total_trades = 0
    labeled_trades = 0
    cat_hit_pairs = defaultdict(int)
    for cur_, iss, t in all_rows:
        t = int(t or 0)
        total_trades += t
        key = f"{cur_}:{iss}"
        cat = cat_for_pair.get(key)
        if cat:
            cat_trades[cat] += t
            labeled_trades += t
            cat_hit_pairs[cat] += 1

    # Also include "unlabeled" as a synthetic bucket for context.
    unlabeled_trades = total_trades - labeled_trades

    HERO_5 = {"stablecoin", "fiat", "wrapped_major", "native_utility", "memecoin"}
    ADD_2 = {"rwa", "lp_token"}

    order = sorted(cat_trades.keys(), key=lambda k: -cat_trades[k])
    out = {
        "generated_at": int(time.time()),
        "window_hours": 24 * 30,
        "total_trades_30d": total_trades,
        "labeled_trades_30d": labeled_trades,
        "unlabeled_trades_30d": unlabeled_trades,
        "labeled_share_of_total": labeled_trades / total_trades if total_trades else 0,
        "categories": [
            {
                "category": c,
                "named_pairs_total": cat_pair_counts[c],
                "traded_pairs_30d": cat_hit_pairs[c],
                "trades_30d": cat_trades[c],
                "share_of_total_trades": (cat_trades[c] / total_trades) if total_trades else 0,
                "share_of_labeled_trades": (cat_trades[c] / labeled_trades) if labeled_trades else 0,
                "hero_current_5": c in HERO_5,
                "hero_add_2": c in ADD_2,
            }
            for c in order
        ],
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)

    print(f"Written: {OUT_PATH}")
    print(f"Total 30d trades:     {total_trades:,}")
    print(f"Labeled trades:       {labeled_trades:,} ({labeled_trades/total_trades*100:.1f}%)")
    print()
    print(f"{'category':<20} {'pairs (named/traded)':<22} {'trades 30d':>12} {'% total':>8} {'% labeled':>10} {'hero'}")
    for row in out["categories"]:
        badge = "H5" if row["hero_current_5"] else ("+2" if row["hero_add_2"] else "  ")
        pairs = f"{row['named_pairs_total']}/{row['traded_pairs_30d']}"
        print(
            f"{row['category']:<20} {pairs:<22} "
            f"{row['trades_30d']:>12,} "
            f"{row['share_of_total_trades']*100:>7.2f}% "
            f"{row['share_of_labeled_trades']*100:>9.1f}% "
            f"{badge}"
        )


if __name__ == "__main__":
    main()

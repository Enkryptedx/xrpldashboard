#!/usr/bin/env python3
"""D1 v4: fetch the three values Fable's final spec needs.

1. Bridge (Axelar) 30d trade share — sum trades across ALL currencies
   minted by rfmS3zqrQrka8wVyhXifEeyTwe8AMz2Yhw, divided by total 30d
   trades. Per-currency breakdown included.

2. RWA TVL — for the 38 rwa pairs in token_names.json, attempt to price
   via read_token_prices_map(). For unpriced, report outstanding supply
   from gateway_balances so we can honestly show coverage.

3. LP_token TVL — LP tokens' value = pool value. Look up each named
   lp_token's issuer (which is the AMM account) in amm_ranked_pools,
   sum XRP-equivalent pool values, convert to USD via a live XRP/USD
   reference.

Writes scratch/d1_v4_missing_three.json + prints summary.
"""
import json
import os
import time
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Load env
env_path = os.path.expanduser("~/.config/xrpldashboard/env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("export ") and "=" in line:
                k, _, v = line[len("export "):].partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import psycopg
import db as pgbridge

TOKEN_NAMES = json.load(open(os.path.join(ROOT, "token_names.json")))
OUT_PATH = os.path.join(ROOT, "scratch/d1_v4_missing_three.json")
AXELAR = "rfmS3zqrQrka8wVyhXifEeyTwe8AMz2Yhw"


def hex_to_display(cur):
    if not cur or len(cur) != 40:
        return cur
    try:
        s = bytes.fromhex(cur).decode("ascii", errors="replace").rstrip("\x00").strip()
        if s and all(c.isprintable() for c in s):
            return s
    except ValueError:
        pass
    return cur


def get_xrp_usd():
    """Prefer the same source app.py uses — check db.py for a helper,
    else fall back to a cached fetch. Returns (usd_per_xrp, source)."""
    # Try a Neon-stored recent XRP price if the dashboard tracks one.
    try:
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='daily_snapshots' LIMIT 30")
                cols = [r[0] for r in cur.fetchall()]
                if "xrp_usd" in cols:
                    cur.execute("SELECT xrp_usd FROM daily_snapshots WHERE xrp_usd IS NOT NULL ORDER BY snapshot_ts DESC LIMIT 1")
                    row = cur.fetchone()
                    if row and row[0]:
                        return float(row[0]), "daily_snapshots"
    except Exception as e:
        print(f"  daily_snapshots probe failed: {e}", file=sys.stderr)
    # Fall back: try coingecko (no key required)
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.coingecko.com/api/v3/simple/price?ids=ripple&vs_currencies=usd",
            headers={"User-Agent": "xrpldashboard/d1-v4"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return float(data["ripple"]["usd"]), "coingecko"
    except Exception as e:
        print(f"  coingecko fallback failed: {e}", file=sys.stderr)
    return None, "unavailable"


def pull1_axelar_trade_share():
    now_bucket = int(time.time()) // 3600
    since = now_bucket - 24 * 30
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT SUM(trade_count) FROM token_volume WHERE hour_bucket >= %s",
                (since,),
            )
            total_trades = int(cur.fetchone()[0] or 0)

            cur.execute(
                "SELECT currency, SUM(trade_count) FROM token_volume "
                "WHERE issuer=%s AND hour_bucket >= %s "
                "GROUP BY currency ORDER BY SUM(trade_count) DESC",
                (AXELAR, since),
            )
            per_currency = [
                {
                    "currency_hex": cur_,
                    "currency_display": hex_to_display(cur_),
                    "trades_30d": int(tc),
                }
                for cur_, tc in cur.fetchall()
            ]
            axelar_total = sum(r["trades_30d"] for r in per_currency)

    return {
        "issuer": AXELAR,
        "issuer_label": "Axelar bridge",
        "total_30d_trades_all_tokens": total_trades,
        "axelar_30d_trades": axelar_total,
        "share_of_total": axelar_total / total_trades if total_trades else 0,
        "per_currency": per_currency,
    }


def pull2_rwa_tvl(prices_map, xrp_usd):
    rwa_pairs = [
        (k.split(":")[0], meta.get("issuer"), meta.get("name") or "")
        for k, meta in TOKEN_NAMES.items()
        if meta.get("category") == "rwa" and meta.get("issuer")
    ]
    priced = []
    unpriced = []
    for cur_hex, iss, name in rwa_pairs:
        row = {
            "currency_hex": cur_hex,
            "currency_display": hex_to_display(cur_hex),
            "issuer": iss,
            "name": name,
        }
        xrp_price = prices_map.get((cur_hex, iss))
        if xrp_price is not None and xrp_price > 0:
            row["xrp_price"] = xrp_price
            priced.append(row)
        else:
            unpriced.append(row)

    return {
        "total_rwa_named": len(rwa_pairs),
        "priced_count": len(priced),
        "unpriced_count": len(unpriced),
        "priced_pairs": priced,
        "unpriced_pairs_sample": unpriced[:5],
        "note": (
            "RWA TVL requires outstanding-supply × price. Priced-count is "
            "the ceiling on how many entries can be TVL-computed at all. "
            "Unpriced entries have no XRP pool above the 2500 XRP dust gate "
            "in token_prices, so we cannot honestly value them without an "
            "off-chain reference. Reporting coverage instead of a "
            "fabricated total."
        ),
    }


def pull3_lp_token_tvl(xrp_usd):
    lp_pairs = [
        (k.split(":")[0], meta.get("issuer"))
        for k, meta in TOKEN_NAMES.items()
        if meta.get("category") == "lp_token" and meta.get("issuer")
    ]
    lp_by_issuer = {iss: cur_hex for cur_hex, iss in lp_pairs}

    pool_rows = pgbridge.read_amm_ranked_pools() or []
    if pool_rows:
        sample_keys = list(pool_rows[0].keys())
    else:
        sample_keys = []

    matched = []
    for pool in pool_rows:
        amm_acct = pool.get("amm_account")
        if amm_acct in lp_by_issuer:
            tvl_usd = pool.get("tvl_usd")
            tvl_status = pool.get("tvl_status")
            matched.append({
                "amm_account": amm_acct,
                "lp_currency_hex": lp_by_issuer[amm_acct],
                "pair": pool.get("pair"),
                "tvl_usd": float(tvl_usd) if tvl_usd is not None else None,
                "tvl_status": tvl_status,
                "amount_a": pool.get("amount_a"),
                "amount_b": pool.get("amount_b"),
                "asset_a": pool.get("asset_a"),
                "asset_b": pool.get("asset_b"),
            })

    matched.sort(key=lambda m: (m["tvl_usd"] or 0), reverse=True)
    priced = [m for m in matched if m["tvl_usd"] is not None]
    total_usd_direct = sum(m["tvl_usd"] for m in priced)
    unpriced_matched = [m for m in matched if m["tvl_usd"] is None]

    return {
        "named_lp_token_count": len(lp_pairs),
        "matched_pool_count": len(matched),
        "unmatched_lp_token_count": len(lp_pairs) - len(matched),
        "priced_matched_count": len(priced),
        "unpriced_matched_count": len(unpriced_matched),
        "sample_amm_pool_row_keys": sample_keys,
        "total_tvl_usd_direct": total_usd_direct,
        "matched_pools": matched,
    }


def pull4_axelar_full_issuer():
    """Follow-up: token_names.json only categorizes SOIL as bridge, but
    the Axelar issuer mints 9 currencies. Report both readings:
    (a) bridge-category-only = SOIL only (matches v3 category table)
    (b) whole-Axelar-issuer = all 9 currencies (broader interpretation)"""
    axelar_categorized = {}
    for k, meta in TOKEN_NAMES.items():
        if meta.get("issuer") == AXELAR:
            cur = k.split(":")[0]
            axelar_categorized[cur] = {
                "category": meta.get("category"),
                "name": meta.get("name"),
            }
    return {"categorized_in_token_names": axelar_categorized}


def main():
    print("=" * 60)
    print("D1 v4: three missing values for Fable's final spec")
    print("=" * 60)

    xrp_usd, xrp_usd_src = get_xrp_usd()
    print(f"XRP/USD reference: {xrp_usd} (source: {xrp_usd_src})")
    print()

    print("--- Pull 1: Axelar 30d trade share ---")
    pull1 = pull1_axelar_trade_share()
    print(f"  Total 30d trades (all tokens): {pull1['total_30d_trades_all_tokens']:,}")
    print(f"  Axelar 30d trades:             {pull1['axelar_30d_trades']:,}")
    print(f"  Share of total:                {pull1['share_of_total']*100:.3f}%")
    print(f"  Per-currency breakdown:")
    for r in pull1["per_currency"]:
        print(f"    {r['currency_display'][:16]:<16}  {r['currency_hex']}  trades={r['trades_30d']:,}")
    print()

    print("--- Pull 2: RWA TVL ---")
    prices_map = pgbridge.read_token_prices_map() or {}
    print(f"  token_prices cache size: {len(prices_map)}")
    pull2 = pull2_rwa_tvl(prices_map, xrp_usd)
    print(f"  RWA named pairs:  {pull2['total_rwa_named']}")
    print(f"  Priced (has XRP pool > dust gate): {pull2['priced_count']}")
    print(f"  Unpriced:                          {pull2['unpriced_count']}")
    if pull2["priced_pairs"]:
        print("  Priced RWA sample:")
        for r in pull2["priced_pairs"][:5]:
            print(f"    {r['currency_display'][:20]:<20}  {r['name'][:30]:<30}  xrp_price={r['xrp_price']}")
    print(f"  Note: {pull2['note']}")
    print()

    print("--- Pull 3: LP_token TVL ---")
    pull3 = pull3_lp_token_tvl(xrp_usd)
    print(f"  Named LP tokens:                 {pull3['named_lp_token_count']}")
    print(f"  Matched to amm_ranked_pools:     {pull3['matched_pool_count']}")
    print(f"  Priced (has tvl_usd):            {pull3['priced_matched_count']}")
    print(f"  Unpriced matched:                {pull3['unpriced_matched_count']}")
    print(f"  Unmatched:                       {pull3['unmatched_lp_token_count']}")
    print(f"  Total TVL USD (direct from pools): ${pull3['total_tvl_usd_direct']:,.2f}")
    print(f"  Top matched pools:")
    for m in pull3["matched_pools"][:10]:
        print(f"    pair={m['pair'][:35] if m['pair'] else '?':<35}  tvl_usd=${m['tvl_usd'] or 0:>12,.2f}  status={m['tvl_status']}")
    print()

    print("--- Pull 4: Axelar breakdown ---")
    pull4 = pull4_axelar_full_issuer()
    print(f"  Currencies of Axelar issuer in token_names.json: {len(pull4['categorized_in_token_names'])}")
    for c, m in pull4["categorized_in_token_names"].items():
        try:
            disp = bytes.fromhex(c).decode('ascii','replace').rstrip('\x00').strip() if len(c)==40 else c
        except:
            disp = c
        print(f"    {disp[:10]:<10}  category={m['category']}")
    print()

    out = {
        "generated_at": int(time.time()),
        "xrp_usd_reference": xrp_usd,
        "xrp_usd_source": xrp_usd_src,
        "pull1_axelar_trade_share": pull1,
        "pull2_rwa_tvl": pull2,
        "pull3_lp_token_tvl": pull3,
        "pull4_axelar_category_breakdown": pull4,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"Written: {OUT_PATH}")


if __name__ == "__main__":
    main()

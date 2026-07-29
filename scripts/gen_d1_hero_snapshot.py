#!/usr/bin/env python3
"""Extract the D1 v5 spec-locked hero snapshot into a tracked JSON file.

Reads from scratch/ (dev-only) and writes d1_hero_snapshot.json at repo root
so the /tokens hero can render the same numbers in prod without needing the
scratch/ directory to be deployed.

Refresh this snapshot only when Charlie ships a new D1 spec version. The
30-day walker re-anchor at 2026-08-02 16:17 EDT will trigger the next
regen against fresh worker data.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

V4_PATH = os.path.join(ROOT, "scratch", "d1_v4_missing_three.json")
V2_ROW_PATH = os.path.join(ROOT, "scratch", "d1_v2_row_level.json")
OUT_PATH = os.path.join(ROOT, "d1_hero_snapshot.json")

AXELAR_ISSUER = "rfmS3zqrQrka8wVyhXifEeyTwe8AMz2Yhw"


def main():
    with open(V4_PATH) as f:
        v4 = json.load(f)
    with open(V2_ROW_PATH) as f:
        v2 = json.load(f)

    lp = v4["pull3_lp_token_tvl"]
    total_lp_usd = float(lp["total_tvl_usd_direct"])
    lp_pools = []
    for p in lp["matched_pools"]:
        tvl = p.get("tvl_usd")
        if tvl is None:
            continue
        lp_pools.append({
            "pair": p["pair"],
            "tvl_usd": round(float(tvl), 2),
            "share_pct": round(float(tvl) / total_lp_usd * 100, 2),
            "tvl_status": p["tvl_status"],
        })
    lp_pools.sort(key=lambda x: x["tvl_usd"], reverse=True)

    axelar = v4["pull1_axelar_trade_share"]
    tier_lookup = {}
    for r in v2["rows"]:
        cur = r.get("currency_hex")
        iss = r.get("issuer")
        tier = r.get("tier")
        if cur and iss and tier:
            tier_lookup[f"{cur}|{iss}"] = tier

    out = {
        "generated_at": v4["generated_at"],
        "source": "scratch/d1_v4_missing_three.json + scratch/d1_v2_row_level.json",
        "v5_spec": "D1_DATA_RESULTS_v5_build_ready.md",
        # Zone A supplements
        "axelar_bridge": {
            "issuer": AXELAR_ISSUER,
            "share_of_total_pct": round(
                axelar["share_of_total"] * 100, 3
            ),  # 0.798%
            "trades_30d": axelar["axelar_30d_trades"],
            "total_30d_trades_all_tokens": axelar["total_30d_trades_all_tokens"],
            "currencies": [
                {
                    "currency_hex": c["currency_hex"],
                    "display": c["currency_display"],
                    "trades_30d": c["trades_30d"],
                }
                for c in axelar["per_currency"]
            ],
        },
        # Zone B
        "lp_zone": {
            "total_tvl_usd": round(total_lp_usd, 2),  # $5,802,013
            "pool_count": len(lp_pools),
            "pools": lp_pools,  # sorted desc by tvl_usd, each has share_pct
        },
        # RWA caption
        "rwa_caption": {
            "named_count": v4["pull2_rwa_tvl"]["total_rwa_named"],  # 38
            "priced_count": v4["pull2_rwa_tvl"]["priced_count"],    # 0
            "text": (
                "real-world-asset tokens issued on XRPL — held on-chain, "
                "not actively traded, no on-chain market anchor available."
            ),
        },
        # Attestation tiers (v3 §4a, top-100 pairs)
        "tiers": {
            "counts": v2["summary"]["tier_counts"],  # V/SD/DO/ANON
            "total_pairs": v2["summary"]["total_pairs"],
            "lookup": tier_lookup,  # "currency|issuer" → tier
        },
        # v3 §8 PRAGMATIC floor
        "floor": {
            "pct": 20.5,
            "reading": "PRAGMATIC",
            "definition": (
                "ANONYMOUS-tier share of total 30d trades. "
                "SELF_DESCRIBED + DOMAIN_ONLY count as nameable."
            ),
        },
    }

    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2, sort_keys=False)

    print(f"wrote {OUT_PATH}")
    print(f"  lp_tvl_total: ${out['lp_zone']['total_tvl_usd']:,.2f}")
    print(f"  lp_pools:     {out['lp_zone']['pool_count']}")
    print(f"  axelar_share: {out['axelar_bridge']['share_of_total_pct']}%")
    print(f"  rwa_count:    {out['rwa_caption']['named_count']}")
    print(f"  tier_counts:  {out['tiers']['counts']}")
    print(f"  tier_lookup:  {len(tier_lookup)} pairs")
    print(f"  floor:        {out['floor']['pct']}%")


if __name__ == "__main__":
    main()

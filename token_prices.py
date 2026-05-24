"""
Token XRP-price derivation.

Reads the in-memory ranked AMM pool list (or amm_ranked.json on a
standalone run) and writes one row per XRP-paired token to the
token_prices Postgres table. Each token's price is derived from its
single XRP-paired AMM pool — the XRPL AMM protocol enforces uniqueness
(exactly one AMM per asset pair), so there is no multi-pool tie-breaking
to do. The investigation pass confirmed this against 22,197 XRP-paired
pools: every token had exactly one XRP-paired pool.

Dust-pool gate
--------------
64.8% of XRP-paired pools have less than 100 XRP of reserve. At that
depth a single small swap moves the constant-product price 30%+, so the
"implied price" stops reflecting market consensus and starts reflecting
whoever placed the last $5 trade. We refuse to publish prices below
MIN_POOL_XRP_RESERVE (default 2,500 XRP reserve). The absence of a row
IS the signal — consumers must not backfill those tokens with stale
data; they need to render "—" / "thin liquidity" instead.

The floor is editorial. 2,500 XRP per side is the depth at which
arbitrage gravity reliably pulls the implied price toward external
reference: a ~10% price move requires ~$340 of trade flow, well
within retail arb capacity. Pools below this drift visibly (e.g. a
1,085-XRP "LTC" pool implied 39 XRP/tok when the market was ~66).
Override via the MIN_POOL_XRP_RESERVE env var when sweeping. The
1,000-XRP starting floor was raised to 2,500 after the LTC/ASTEROID
visible-error cases on 2026-05-24; 5,000 was tested but cut 73% of
priceable tokens including top-15 actives (SENT, GiB) whose pools
are shallow-but-honest.

Integration
-----------
v1 piggybacks on rank_amms.main(): after the final amm_ranked_pools
mirror, the in-memory `ranked` list is passed in and prices are
upserted. Same cadence as the ranker, no separate launchd plist, no
schedule drift, and the price snapshot is always paired with the pool
snapshot it was derived from. A __main__ entrypoint is preserved for
ad-hoc runs against amm_ranked.json on the Mac.
"""

import json
import logging
import os
import sys
import time

import db

MIN_POOL_XRP_RESERVE = float(os.environ.get("MIN_POOL_XRP_RESERVE", "2500"))
RANKED_PATH = os.path.join(os.path.dirname(__file__), "amm_ranked.json")


def derive_prices_from_pools(pools, snapshot_ts):
    """Compute XRP-equivalent prices for each XRP-paired token whose pool
    reserve clears MIN_POOL_XRP_RESERVE. Pure function (no I/O); accepts
    the in-memory pool list shape used by rank_amms / amm_ranked.json.

    Returns (rows, skipped_dust, skipped_other) — rows is ready for
    db.write_token_prices; skipped_* counters drive the run summary."""
    rows = []
    skipped_dust = 0
    skipped_other = 0
    for p in pools:
        kind = p.get("kind")
        if kind not in ("xrp_first", "xrp_second"):
            continue
        a = p.get("asset_a") or {}
        b = p.get("asset_b") or {}
        if a.get("currency") == "XRP":
            xrp_amt = p.get("amount_a")
            tok_amt = p.get("amount_b")
            tok_asset = b
        else:
            xrp_amt = p.get("amount_b")
            tok_amt = p.get("amount_a")
            tok_asset = a
        if not xrp_amt or not tok_amt or tok_amt <= 0:
            skipped_other += 1
            continue
        if xrp_amt < MIN_POOL_XRP_RESERVE:
            skipped_dust += 1
            continue
        currency = tok_asset.get("currency")
        issuer = tok_asset.get("issuer")
        if not currency or not issuer:
            skipped_other += 1
            continue
        rows.append({
            "currency": currency,
            "issuer": issuer,
            "snapshot_ts": snapshot_ts,
            "xrp_price": xrp_amt / tok_amt,
            "pool_amm_account": p.get("amm_account"),
            "pool_xrp_reserve": xrp_amt,
            "pool_token_reserve": tok_amt,
            "derivation_method": "direct_xrp_pool",
        })
    return rows, skipped_dust, skipped_other


def run_once(pools=None, snapshot_ts=None):
    """Derive + write token prices. If pools is None, loads
    amm_ranked.json. Silent no-op when PG isn't configured.

    Returns (written, skipped_dust, skipped_other)."""
    log = logging.getLogger("token_prices")
    if pools is None:
        if not os.path.exists(RANKED_PATH):
            log.warning(
                "no pools provided and %s not present; nothing to do",
                RANKED_PATH,
            )
            return (0, 0, 0)
        with open(RANKED_PATH) as f:
            pools = json.load(f)
    if snapshot_ts is None:
        snapshot_ts = int(time.time())
    rows, skipped_dust, skipped_other = derive_prices_from_pools(
        pools, snapshot_ts
    )
    if not rows:
        log.warning(
            "derived 0 rows from %d pools — not writing", len(pools)
        )
        return (0, skipped_dust, skipped_other)
    written = db.write_token_prices(rows)
    log.info(
        "token_prices: written=%d dust_skipped=%d other_skipped=%d floor=%g XRP",
        written, skipped_dust, skipped_other, MIN_POOL_XRP_RESERVE,
    )
    return (written, skipped_dust, skipped_other)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    t0 = time.time()
    try:
        written, dust, other = run_once()
    except Exception:
        logging.getLogger("token_prices").exception("run failed")
        sys.exit(1)
    elapsed = round(time.time() - t0, 2)
    print(
        f"[token_prices] written={written} dust_skipped={dust} "
        f"other_skipped={other} floor={MIN_POOL_XRP_RESERVE:g} XRP "
        f"elapsed={elapsed}s",
        flush=True,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()

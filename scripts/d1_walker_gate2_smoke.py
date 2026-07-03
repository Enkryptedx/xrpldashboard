#!/usr/bin/env python3
"""Walker value-fix Gate 2 smoke test.

Verifies the modified xrpl_stream.token_event_handler:
  1. read_token_prices_map() returns a populated cache from Neon
     (proves the cache source works).
  2. Fabricated Payment txs against known-priced tokens produce
     non-zero volume_xrp in an isolated SQLite (proves the amount
     × price math + INSERT path).
  3. Fabricated Payment tx against an unpriced token produces
     volume_xrp = 0.0 (proves the honest-gap invariant).
  4. Fabricated AMMDeposit produces trade_count += 1, volume_xrp += 0.0
     (proves AMM legs still increment count but never fabricate value).

Does NOT touch prod volumes.db or prod Postgres. Monkey-patches
xrpl_stream._volumes_conn to a tempfile SQLite and pgbridge.upsert_token_volume
to a shim that logs but doesn't write to Neon. Prod xrpl_stream launchd
worker continues untouched.
"""
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Load env (Neon DATABASE_URL) the same way the launchd runner does.
env_path = os.path.expanduser("~/.config/xrpldashboard/env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("export ") and "=" in line:
                k, _, v = line[len("export "):].partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import db as pgbridge
import xrpl_stream


def step1_verify_price_cache_source():
    print("=" * 60)
    print("Step 1: verify token_prices reader returns Neon rows")
    print("=" * 60)
    prices = pgbridge.read_token_prices_map()
    if not prices:
        print("FAIL — read_token_prices_map returned empty. Check Neon "
              "connectivity or that token_prices has been populated.")
        return None
    print(f"OK — {len(prices)} priced tokens in cache")
    sample = list(prices.items())[:3]
    for (cur, iss), xrp_price in sample:
        print(f"  {cur:>8} / {iss[:20]}... = {xrp_price} XRP")
    return prices


def step2_smoke_handler(prices):
    print()
    print("=" * 60)
    print("Step 2-4: fabricated txs through the modified handler")
    print("=" * 60)

    # Isolate: swap _volumes_conn to a temp SQLite matching prod schema.
    tmpdir = tempfile.mkdtemp(prefix="d1_gate2_")
    scratch_db = os.path.join(tmpdir, "volumes.db")
    conn = sqlite3.connect(scratch_db)
    conn.execute(
        "CREATE TABLE token_volume ("
        "  currency TEXT NOT NULL, "
        "  issuer TEXT NOT NULL, "
        "  hour_bucket INTEGER NOT NULL, "
        "  volume_xrp REAL NOT NULL DEFAULT 0.0, "
        "  trade_count INTEGER NOT NULL DEFAULT 0, "
        "  PRIMARY KEY (currency, issuer, hour_bucket))"
    )
    conn.commit()
    xrpl_stream._volumes_conn = conn

    # Neutralise the PG-write path so nothing hits Neon.
    pg_upserts = []
    def fake_upsert(cur, iss, hb, trade_delta=1, volume_xrp_delta=0.0):
        pg_upserts.append((cur, iss, hb, trade_delta, volume_xrp_delta))
    pgbridge.upsert_token_volume = fake_upsert

    # Force the cache to a known snapshot rather than waiting for TTL.
    xrpl_stream._TOKEN_PRICE_CACHE = prices
    xrpl_stream._TOKEN_PRICE_CACHE_LAST_REFRESH = time.time()

    def make_payment(cur, iss, value):
        return {
            "transaction": {
                "TransactionType": "Payment",
                "Amount": {"currency": cur, "issuer": iss, "value": str(value)},
            },
        }

    def make_amm_deposit(cur_a, iss_a, cur_b, iss_b):
        return {
            "transaction": {
                "TransactionType": "AMMDeposit",
                "Asset": {"currency": cur_a, "issuer": iss_a} if cur_a != "XRP" else {"currency": "XRP"},
                "Asset2": {"currency": cur_b, "issuer": iss_b} if cur_b != "XRP" else {"currency": "XRP"},
            },
        }

    # Pick 2 priced tokens for Payment tests.
    priced_pairs = list(prices.items())[:2]
    state = {}

    for (cur, iss), xrp_price in priced_pairs:
        msg = make_payment(cur, iss, "100")
        xrpl_stream.token_event_handler(msg, state)
        print(f"  Payment  100 {cur:>8} (priced @ {xrp_price} XRP) → "
              f"expect volume_xrp += {100 * xrp_price}")

    # Payment for unpriced token → expect volume_xrp += 0
    unpriced_cur = "ZZZUNPRICED12345"
    unpriced_iss = "rTestUnpriced" + "A" * 20
    msg = make_payment(unpriced_cur, unpriced_iss, "500")
    xrpl_stream.token_event_handler(msg, state)
    print(f"  Payment  500 {unpriced_cur[:8]} (UNPRICED) → expect volume_xrp += 0.0 (honest-gap)")

    # AMM deposit for a priced token → expect trade_count += 1, volume_xrp += 0
    (amm_cur, amm_iss), _ = priced_pairs[0]
    msg = make_amm_deposit(amm_cur, amm_iss, "XRP", "")
    xrpl_stream.token_event_handler(msg, state)
    print(f"  AMMDeposit for {amm_cur} → expect trade_count += 1, volume_xrp += 0.0")

    # Read the scratch SQLite and print rows.
    print()
    print("Scratch SQLite state after fabricated txs:")
    print(f"  {'currency':>12} {'issuer':<45} {'trade_count':>12} {'volume_xrp':>14}")
    ok_priced = 0
    ok_unpriced_zero = False
    ok_amm_zero_value = False
    for row in conn.execute("SELECT currency, issuer, trade_count, volume_xrp FROM token_volume ORDER BY volume_xrp DESC"):
        cur, iss, tc, vxrp = row
        print(f"  {cur[:12]:>12} {iss[:45]:<45} {tc:>12} {vxrp:>14.6f}")
        if (cur, iss) in dict(priced_pairs) and vxrp > 0:
            ok_priced += 1
        if cur == unpriced_cur and vxrp == 0.0 and tc == 1:
            ok_unpriced_zero = True

    # Check the AMM row for value=0 (the priced token had 2 events: 1 Payment + 1 AMM)
    (amm_cur_check, amm_iss_check), amm_price = priced_pairs[0]
    row = conn.execute(
        "SELECT trade_count, volume_xrp FROM token_volume WHERE currency=? AND issuer=?",
        (amm_cur_check, amm_iss_check),
    ).fetchone()
    if row:
        tc_amm, vxrp_amm = row
        expected_from_payment_only = 100 * amm_price
        # Payment contributed 100*price; AMM contributed 0. Both rolled up.
        if abs(vxrp_amm - expected_from_payment_only) < 1e-6 and tc_amm == 2:
            ok_amm_zero_value = True

    print()
    print("Verdict:")
    print(f"  Priced payments produced non-zero volume_xrp:      "
          f"{'PASS' if ok_priced == len(priced_pairs) else 'FAIL'} ({ok_priced}/{len(priced_pairs)})")
    print(f"  Unpriced payment produced volume_xrp = 0.0:        "
          f"{'PASS' if ok_unpriced_zero else 'FAIL'}")
    print(f"  AMM deposit added trade_count only, value stayed:  "
          f"{'PASS' if ok_amm_zero_value else 'FAIL'}")
    print(f"  PG upsert calls captured:                          {len(pg_upserts)}")
    print()
    print(f"Scratch SQLite: {scratch_db}")

    return ok_priced == len(priced_pairs) and ok_unpriced_zero and ok_amm_zero_value


def main():
    prices = step1_verify_price_cache_source()
    if not prices:
        sys.exit(2)
    ok = step2_smoke_handler(prices)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

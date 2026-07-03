#!/usr/bin/env python3
"""Walker value-fix Gate 2b smoke — regression coverage after Gate 4 caught
1.66e+95 XRP writes.

Covers four tx shapes that must all behave correctly BEFORE re-kickstart:

  A. Normal priced Payment (delivered_amount present, dict)
       → volume_xrp += value * price (non-zero, sane)

  B. tfPartialPayment where Amount.value is an inflated upper-bound and
     meta.delivered_amount is the true smaller delivered
       → volume_xrp derived from delivered_amount, NOT Amount

  C. Max-IOU-exponent extreme value that would produce xrp_delta > 1e11
     (the 100B XRP total-supply ceiling)
       → volume_xrp += 0.0 (skipped by ceiling), trade_count += 1

  D. Unpriced token (missing from cache)
       → volume_xrp += 0.0 (honest gap), trade_count += 1

  E. Cross-currency Payment with SendMax set and NO delivered_amount
       → skipped entirely (Amount is only an upper bound; SendMax present
         forbids using Amount without delivered_amount)

Isolated: temp SQLite + monkey-patched pgbridge.upsert_token_volume so
nothing hits prod volumes.db or Neon.
"""
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

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


def make_payment(cur, iss, amount_value, delivered=None, send_max=False):
    """Build a Payment stream message.

    - amount_value: goes into tx.Amount.value (the possibly-inflated field)
    - delivered:    if not None, goes into meta.delivered_amount (dict for
                    IOU or string for XRP drops)
    - send_max:     if True, adds a placeholder SendMax to mark cross-currency
    """
    tx = {
        "TransactionType": "Payment",
        "Amount": {"currency": cur, "issuer": iss, "value": str(amount_value)},
    }
    if send_max:
        tx["SendMax"] = "1000000"  # arbitrary XRP drops upper bound
    msg = {"transaction": tx}
    if delivered is not None:
        msg["meta"] = {"delivered_amount": delivered}
    return msg


def run():
    print("=" * 72)
    print("Gate 2b smoke — walker value-fix regression coverage")
    print("=" * 72)

    prices = pgbridge.read_token_prices_map()
    if not prices:
        print("FAIL — read_token_prices_map returned empty")
        return 2
    print(f"OK — {len(prices)} priced tokens in cache")

    # Isolate SQLite + neutralise PG upserts.
    tmpdir = tempfile.mkdtemp(prefix="d1_gate2b_")
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

    pg_upserts = []
    def fake_upsert(cur, iss, hb, trade_delta=1, volume_xrp_delta=0.0):
        pg_upserts.append((cur, iss, hb, trade_delta, volume_xrp_delta))
    pgbridge.upsert_token_volume = fake_upsert

    # Seed cache instead of TTL wait.
    xrpl_stream._TOKEN_PRICE_CACHE = prices
    xrpl_stream._TOKEN_PRICE_CACHE_LAST_REFRESH = time.time()

    # ---- Case A: normal priced Payment with delivered_amount ---------------
    priced_pairs = list(prices.items())[:2]
    (a_cur, a_iss), a_price = priced_pairs[0]
    a_value = 100.0
    a_expected = a_value * float(a_price)
    msg_a = make_payment(
        a_cur, a_iss, amount_value=a_value,
        delivered={"currency": a_cur, "issuer": a_iss, "value": str(a_value)},
    )
    xrpl_stream.token_event_handler(msg_a, {})
    print(f"\nA. Normal priced Payment: 100 {a_cur} × {a_price} XRP "
          f"→ expect volume_xrp ≈ {a_expected:.6f}")

    # ---- Case B: tfPartialPayment (Amount inflated, delivered is the truth)
    (b_cur, b_iss), b_price = priced_pairs[1]
    b_inflated = "999999999999"  # what a partial-payment upper-bound might look like
    b_real = 50.0
    b_expected = b_real * float(b_price)
    msg_b = make_payment(
        b_cur, b_iss, amount_value=b_inflated,
        delivered={"currency": b_cur, "issuer": b_iss, "value": str(b_real)},
    )
    xrpl_stream.token_event_handler(msg_b, {})
    print(f"B. Partial payment: Amount={b_inflated} but delivered={b_real} "
          f"{b_cur} × {b_price} → expect volume_xrp ≈ {b_expected:.6f} "
          f"(NOT {float(b_inflated) * float(b_price):.3e})")

    # ---- Case C: max-IOU-exponent extreme value → ceiling should catch ----
    (c_cur, c_iss), c_price = priced_pairs[0]  # reuse case-A pair
    c_extreme = "1e80"  # max IOU exponent territory; delta would be ~1e80 XRP
    msg_c = make_payment(
        c_cur, c_iss, amount_value=c_extreme,
        delivered={"currency": c_cur, "issuer": c_iss, "value": c_extreme},
    )
    hits_before = xrpl_stream._XRP_DELTA_CEILING_HITS
    xrpl_stream.token_event_handler(msg_c, {})
    hits_after = xrpl_stream._XRP_DELTA_CEILING_HITS
    print(f"C. Extreme value: delivered={c_extreme} {c_cur} × {c_price} "
          f"→ ceiling should catch, volume_xrp += 0.0 "
          f"(ceiling hits {hits_before}→{hits_after})")

    # ---- Case D: unpriced token → honest gap ------------------------------
    d_cur = "ZZZUNPRICED12345"
    d_iss = "rTestUnpriced" + "A" * 20
    msg_d = make_payment(
        d_cur, d_iss, amount_value=500,
        delivered={"currency": d_cur, "issuer": d_iss, "value": "500"},
    )
    xrpl_stream.token_event_handler(msg_d, {})
    print(f"D. Unpriced token 500 {d_cur[:8]}... → expect volume_xrp += 0.0")

    # ---- Case E: cross-currency Payment with SendMax + no delivered_amount
    e_cur, e_iss = "EEE", "rTestSendMax" + "A" * 21
    msg_e = make_payment(e_cur, e_iss, amount_value=1000, send_max=True)
    # No delivered_amount — this Payment should be SKIPPED entirely.
    before_e = conn.execute("SELECT COUNT(*) FROM token_volume").fetchone()[0]
    xrpl_stream.token_event_handler(msg_e, {})
    after_e = conn.execute("SELECT COUNT(*) FROM token_volume").fetchone()[0]
    print(f"E. SendMax + no delivered_amount → expect skipped entirely "
          f"(row count {before_e}→{after_e})")

    # ---- Read + verify ----------------------------------------------------
    print()
    print("Scratch SQLite state after fabricated txs:")
    print(f"  {'currency':>16} {'issuer':<45} {'trade_count':>12} {'volume_xrp':>18}")
    for row in conn.execute(
        "SELECT currency, issuer, trade_count, volume_xrp FROM token_volume "
        "ORDER BY volume_xrp DESC"
    ):
        cur, iss, tc, vxrp = row
        print(f"  {cur[:16]:>16} {iss[:45]:<45} {tc:>12} {vxrp:>18.6f}")

    verdicts = {}

    # A: sane non-zero for case-A pair, no case-C contamination.
    row_a = conn.execute(
        "SELECT trade_count, volume_xrp FROM token_volume WHERE currency=? AND issuer=?",
        (a_cur, a_iss),
    ).fetchone()
    # Case-A + Case-C share the same (cur, iss). C should have contributed
    # trade_count += 1, volume_xrp += 0.0 (ceiling caught it).
    # So expect trade_count == 2 and volume_xrp ≈ a_expected.
    tc_a, vxrp_a = row_a
    verdicts["A: normal priced payment writes sane non-zero"] = (
        tc_a == 2 and abs(vxrp_a - a_expected) < 1e-6
    )

    # B: partial-payment used delivered, NOT inflated Amount.
    row_b = conn.execute(
        "SELECT trade_count, volume_xrp FROM token_volume WHERE currency=? AND issuer=?",
        (b_cur, b_iss),
    ).fetchone()
    tc_b, vxrp_b = row_b
    verdicts["B: partial-payment used delivered_amount"] = (
        tc_b == 1 and abs(vxrp_b - b_expected) < 1e-6
        and vxrp_b < 1e10  # far below what inflated Amount would produce
    )

    # C: ceiling fired at least once.
    verdicts["C: extreme value caught by ceiling"] = (hits_after > hits_before)

    # D: unpriced writes 0.0 + counts trade.
    row_d = conn.execute(
        "SELECT trade_count, volume_xrp FROM token_volume WHERE currency=? AND issuer=?",
        (d_cur, d_iss),
    ).fetchone()
    tc_d, vxrp_d = row_d
    verdicts["D: unpriced token honest gap"] = (tc_d == 1 and vxrp_d == 0.0)

    # E: SendMax-no-delivered skipped completely (no row for (e_cur, e_iss)).
    row_e = conn.execute(
        "SELECT trade_count, volume_xrp FROM token_volume WHERE currency=? AND issuer=?",
        (e_cur, e_iss),
    ).fetchone()
    verdicts["E: SendMax without delivered skipped"] = (row_e is None)

    print()
    print("Verdict:")
    all_pass = True
    for name, ok in verdicts.items():
        mark = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"  {mark}  {name}")
    print()
    print(f"PG upsert calls captured: {len(pg_upserts)}")
    print(f"Scratch SQLite: {scratch_db}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(run())

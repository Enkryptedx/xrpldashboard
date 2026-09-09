"""Forced-fallback + sourcing-aggregation tests for the /wallet data layer.

Part of the 2026-09-09 wallet_data tunnel-first migration (mirrors the
network_pulse / lending SovereignFetcher pattern). These exercise the
sovereign→public cascade end-to-end WITHOUT a real tunnel or DB:

  1. tunnel healthy       → envelope sourcing == "sovereign", zero
                            walker_node_fallback rows written.
  2. tunnel forced-fail   → cascade executes cleanly (data still returns),
                            envelope sourcing == "fallback-public-rpc"
                            (this is the value the wallet.html banner and
                            the billing-pause middleware both key off), and
                            exactly ONE walker_node_fallback row for the whole
                            render (Charlie ruling 2026-09-09), with the
                            cascading branches listed in the reason field.
                            For a holdings-empty wallet the cascading branches
                            are main + escrow + offer + mpt (the LP pool is
                            skipped when there are no LP holdings), so the row
                            reads
                            "branches=main,escrow,offer,mpt reason=tunnel_http_502".
  3. concurrent-branch taint → a fallback isolated to ONE Phase-2 branch
                            still surfaces on the page envelope via
                            worse_sourcing().

Run standalone (bypasses tests/conftest.py, which imports app.py):
    ./venv/bin/python tests/test_wallet_data_sourcing.py
"""
import os
import sys

# Repo root importable directly (bypass conftest.py → app.py → flask_smorest)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sovereign_tunnel_client as stc
import db
import wallet_data as wd


TEST_ADDRESS = "rTESTwalletADDRESSforForcedFallback000000"


# ── Canned JSON-RPC results, keyed by method ──────────────────────────
# Minimal but shape-valid so fetch_wallet_data() runs to completion with a
# holdings-empty wallet (no LP enrichment fan-out, empty Phase-2 sections).
def _canned_result(payload):
    method = payload.get("method")
    if method == "server_info":
        return {"info": {"validated_ledger": {
            "reserve_base_xrp": 10, "reserve_inc_xrp": 2}}}
    if method == "account_info":
        return {"account_data": {"Balance": "125000000", "OwnerCount": 3}}
    if method == "account_lines":
        return {"lines": []}
    if method == "account_tx":
        return {"transactions": [], "marker": None}
    if method == "account_objects":
        return {"account_objects": []}
    if method == "amm_info":
        return {"amm": {}}
    return {}


class _WriteRecorder:
    """Stands in for db.write_walker_node_fallback — records every row."""
    def __init__(self):
        self.rows = []
    def __call__(self, walker_name, reason):
        self.rows.append((walker_name, reason))
        return True


def _install(monkey_tunnel_ok, recorder):
    """Patch the SovereignFetcher internals + db writer. monkey_tunnel_ok
    True → tunnel serves every call (no cascade); False → tunnel always
    fails so call() cascades to public. Returns a restore() thunk."""
    saved = {
        "TUNNEL_CONFIGURED": stc.TUNNEL_CONFIGURED,
        "TUNNEL_NODE": stc.TUNNEL_NODE,
        "_CF_CLIENT_ID": stc._CF_CLIENT_ID,
        "_CF_CLIENT_SECRET": stc._CF_CLIENT_SECRET,
        "_try_tunnel": stc.SovereignFetcher._try_tunnel,
        "_try_public": stc.SovereignFetcher._try_public,
        "write_walker_node_fallback": db.write_walker_node_fallback,
    }
    # Make every fresh fetcher start in the SOVEREIGN state.
    stc.TUNNEL_CONFIGURED = True
    stc.TUNNEL_NODE = "https://tunnel.test.invalid"
    stc._CF_CLIENT_ID = "test-id"
    stc._CF_CLIENT_SECRET = "test-secret"

    if monkey_tunnel_ok:
        def fake_tunnel(self, payload):
            return _canned_result(payload), None
    else:
        def fake_tunnel(self, payload):
            return None, "tunnel_http_502"

    def fake_public(self, payload):
        return _canned_result(payload)

    stc.SovereignFetcher._try_tunnel = fake_tunnel
    stc.SovereignFetcher._try_public = fake_public
    db.write_walker_node_fallback = recorder

    # Clear cross-test module caches so each case does real fetches.
    wd._reserve_cache.clear()
    wd._cache.clear()
    wd._pool_metrics_cache.clear()

    def restore():
        stc.TUNNEL_CONFIGURED = saved["TUNNEL_CONFIGURED"]
        stc.TUNNEL_NODE = saved["TUNNEL_NODE"]
        stc._CF_CLIENT_ID = saved["_CF_CLIENT_ID"]
        stc._CF_CLIENT_SECRET = saved["_CF_CLIENT_SECRET"]
        stc.SovereignFetcher._try_tunnel = saved["_try_tunnel"]
        stc.SovereignFetcher._try_public = saved["_try_public"]
        db.write_walker_node_fallback = saved["write_walker_node_fallback"]
    return restore


def _test_healthy_tunnel_is_sovereign():
    rec = _WriteRecorder()
    restore = _install(monkey_tunnel_ok=True, recorder=rec)
    try:
        data = wd.fetch_wallet_data(TEST_ADDRESS)
    finally:
        restore()
    problems = []
    if data.get("error") is not None:
        problems.append(f"unexpected error: {data.get('error')}")
    if data.get("sourcing") != stc.SOURCING_SOVEREIGN:
        problems.append(f"sourcing={data.get('sourcing')}, expected sovereign")
    if data.get("balance_xrp") != 125.0:
        problems.append(f"balance_xrp={data.get('balance_xrp')}, expected 125.0")
    if rec.rows:
        problems.append(f"unexpected fallback rows: {rec.rows}")
    return (not problems), ("; ".join(problems) or "ok")


def _test_forced_fallback_cascades_cleanly():
    rec = _WriteRecorder()
    restore = _install(monkey_tunnel_ok=False, recorder=rec)
    try:
        data = wd.fetch_wallet_data(TEST_ADDRESS)
    finally:
        restore()
    problems = []
    # Cascade must not break the render — data still comes through public.
    if data.get("error") is not None:
        problems.append(f"cascade broke render: {data.get('error')}")
    if data.get("balance_xrp") != 125.0:
        problems.append(f"balance_xrp={data.get('balance_xrp')}, expected 125.0 (public served)")
    # The banner-/billing-driving field must flip to fallback.
    if data.get("sourcing") != stc.SOURCING_FALLBACK:
        problems.append(f"sourcing={data.get('sourcing')}, expected fallback-public-rpc")
    # Exactly ONE row for the whole render (2026-09-09 flip), with the
    # cascading branches listed in the reason. Holdings-empty wallet → the
    # LP pool never runs, so branches = main,escrow,offer,mpt.
    if len(rec.rows) != 1:
        problems.append(f"fallback rows={len(rec.rows)}, expected 1 (one per page load)")
    if any(name != "wallet_data" for name, _ in rec.rows):
        problems.append(f"unexpected walker_name in rows: {rec.rows}")
    expected_reason = "branches=main,escrow,offer,mpt reason=tunnel_http_502"
    if rec.rows and rec.rows[0][1] != expected_reason:
        problems.append(
            f"reason={rec.rows[0][1]!r}, expected {expected_reason!r}"
        )
    return (not problems), ("; ".join(problems) or "ok")


def _test_before_after_envelope_diff():
    """Same address, tunnel healthy then failing — prove the envelope
    sourcing is the only thing that flips (data payload is identical)."""
    rec_ok = _WriteRecorder()
    restore = _install(monkey_tunnel_ok=True, recorder=rec_ok)
    try:
        before = wd.fetch_wallet_data(TEST_ADDRESS)
    finally:
        restore()
    rec_bad = _WriteRecorder()
    restore = _install(monkey_tunnel_ok=False, recorder=rec_bad)
    try:
        after = wd.fetch_wallet_data(TEST_ADDRESS)
    finally:
        restore()
    problems = []
    if before.get("sourcing") != stc.SOURCING_SOVEREIGN:
        problems.append(f"before sourcing={before.get('sourcing')}")
    if after.get("sourcing") != stc.SOURCING_FALLBACK:
        problems.append(f"after sourcing={after.get('sourcing')}")
    # Everything except the sourcing field should be identical.
    b = {k: v for k, v in before.items() if k != "sourcing"}
    a = {k: v for k, v in after.items() if k != "sourcing"}
    if b != a:
        diff_keys = [k for k in b if b.get(k) != a.get(k)]
        problems.append(f"payload changed beyond sourcing: {diff_keys}")
    print(f"    before.sourcing={before.get('sourcing')!r}  "
          f"after.sourcing={after.get('sourcing')!r}  "
          f"fallback_rows: before={len(rec_ok.rows)} after={len(rec_bad.rows)}")
    return (not problems), ("; ".join(problems) or "ok")


TESTS = [
    ("healthy_tunnel_is_sovereign", _test_healthy_tunnel_is_sovereign),
    ("forced_fallback_cascades_cleanly", _test_forced_fallback_cascades_cleanly),
    ("before_after_envelope_diff", _test_before_after_envelope_diff),
]


def main():
    pass_count = 0
    fail_count = 0
    for name, fn in TESTS:
        ok, detail = fn()
        print(f"  {'PASS' if ok else 'FAIL'} {name}: {detail}")
        pass_count += ok
        fail_count += not ok
    print(f"\n== {pass_count} PASS / {fail_count} FAIL ==")
    return fail_count


if __name__ == "__main__":
    sys.exit(main())

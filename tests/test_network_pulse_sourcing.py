"""Forced-fallback + sourcing tests for network_pulse.fetch_pulse().

GAP-1 (Charlie 2026-09-25): the nav-chip pulse reads own-node-first via
SovereignFetcher (own node primary, PUBLIC_NODES labeled fallback, sourcing
flag + ONE walker_node_fallback row per fetch on real cascade). This was the
single largest public-RPC source in the /health render path. The code fix
landed in a5af6f6 + 4ba836c; these tests lock the sovereignty property so a
future edit can't silently reintroduce a raw public read without turning a
test red.

Mirrors tests/test_wallet_data_sourcing.py exactly (same _install harness,
same _WriteRecorder), differing only in the canned RPC shapes fetch_pulse()
needs (server_info + two ledger headers) and the one-row-per-fetch assertion.

Run standalone (bypasses tests/conftest.py, which imports app.py):
    ./venv/bin/python tests/test_network_pulse_sourcing.py
"""
import os
import sys

# Repo root importable directly (bypass conftest.py → app.py → flask_smorest)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sovereign_tunnel_client as stc
import db
import network_pulse as np


# ── Canned JSON-RPC results, keyed by method ──────────────────────────
# Minimal but shape-valid so fetch_pulse() runs to completion: a server_info
# with a validated_ledger, plus two ledger headers 1000 seq apart whose
# close_time delta / seq delta gives a clean 3.5s avg close (healthy).
_CURRENT_SEQ = 107_500_000
_TARGET_SEQ = _CURRENT_SEQ - np.CLOSE_TIME_SAMPLE_LEDGERS
# 3.5s/ledger over the sample window.
_CURRENT_CLOSE = 800_000_000
_TARGET_CLOSE = _CURRENT_CLOSE - int(np.CLOSE_TIME_SAMPLE_LEDGERS * 3.5)


def _canned_result(payload):
    method = payload.get("method")
    if method == "server_info":
        return {"info": {
            "validated_ledger": {
                "seq": _CURRENT_SEQ,
                "age": 2,
                "base_fee_xrp": 0.00001,
                "reserve_base_xrp": 1,
                "reserve_inc_xrp": 0.2,
            },
            "validation_quorum": 5,
            "load_factor": 1,
            "complete_ledgers": f"32570-{_CURRENT_SEQ}",
        }}
    if method == "ledger":
        params = (payload.get("params") or [{}])[0]
        idx = params.get("ledger_index")
        if idx == _CURRENT_SEQ:
            return {"ledger": {"ledger_index": _CURRENT_SEQ,
                               "close_time": _CURRENT_CLOSE}}
        return {"ledger": {"ledger_index": _TARGET_SEQ,
                           "close_time": _TARGET_CLOSE}}
    return {}


class _WriteRecorder:
    """Stands in for db.write_walker_node_fallback — records every row."""
    def __init__(self):
        self.rows = []

    def __call__(self, walker_name, reason):
        self.rows.append((walker_name, reason))
        return True


def _install(monkey_tunnel_ok, recorder):
    """Patch SovereignFetcher internals + db writer. monkey_tunnel_ok True →
    tunnel serves every call (no cascade); False → tunnel always fails so
    call() cascades to public. Returns a restore() thunk. Also clears the
    pulse module cache so each case does a real fetch."""
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

    # Clear pulse cache so each case does a real fetch.
    np._cache_state["data"] = None
    np._cache_state["fetched_at"] = 0.0

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
        p = np.fetch_pulse()
    finally:
        restore()
    problems = []
    if p.get("error") is not None:
        problems.append(f"unexpected error: {p.get('error')}")
    if p.get("sourcing") != stc.SOURCING_SOVEREIGN:
        problems.append(f"sourcing={p.get('sourcing')}, expected sovereign")
    if p.get("ledger_index") != _CURRENT_SEQ:
        problems.append(f"ledger_index={p.get('ledger_index')}, expected {_CURRENT_SEQ}")
    # 3.5s avg close feeds the healthy classification.
    if p.get("avg_close_seconds") != 3.5:
        problems.append(f"avg_close_seconds={p.get('avg_close_seconds')}, expected 3.5")
    if p.get("status") != "operating_normally":
        problems.append(f"status={p.get('status')}, expected operating_normally")
    if rec.rows:
        problems.append(f"unexpected fallback rows: {rec.rows}")
    return (not problems), ("; ".join(problems) or "ok")


def _test_forced_fallback_cascades_cleanly():
    rec = _WriteRecorder()
    restore = _install(monkey_tunnel_ok=False, recorder=rec)
    try:
        p = np.fetch_pulse()
    finally:
        restore()
    problems = []
    # Cascade must not break the panel — data still comes through public.
    if p.get("error") is not None:
        problems.append(f"cascade broke pulse: {p.get('error')}")
    if p.get("ledger_index") != _CURRENT_SEQ:
        problems.append(f"ledger_index={p.get('ledger_index')}, expected {_CURRENT_SEQ} (public served)")
    if p.get("avg_close_seconds") != 3.5:
        problems.append(f"avg_close_seconds={p.get('avg_close_seconds')}, expected 3.5 (public served)")
    # The banner-driving field must flip to fallback.
    if p.get("sourcing") != stc.SOURCING_FALLBACK:
        problems.append(f"sourcing={p.get('sourcing')}, expected fallback-public-rpc")
    # Exactly ONE row for the whole fetch, despite 3 RPC calls cascading
    # (server_info + 2 ledger headers) — sticky per-fetcher downgrade.
    if len(rec.rows) != 1:
        problems.append(f"fallback rows={len(rec.rows)}, expected 1 (one per fetch)")
    if any(name != "network_pulse" for name, _ in rec.rows):
        problems.append(f"unexpected walker_name in rows: {rec.rows}")
    if rec.rows and rec.rows[0][1] != "tunnel_http_502":
        problems.append(f"reason={rec.rows[0][1]!r}, expected 'tunnel_http_502'")
    return (not problems), ("; ".join(problems) or "ok")


def _test_before_after_envelope_diff():
    """Same fetch, tunnel healthy then failing — prove the sourcing field is
    the only thing that flips (the pulse data payload is identical)."""
    rec_ok = _WriteRecorder()
    restore = _install(monkey_tunnel_ok=True, recorder=rec_ok)
    try:
        before = np.fetch_pulse()
    finally:
        restore()
    rec_bad = _WriteRecorder()
    restore = _install(monkey_tunnel_ok=False, recorder=rec_bad)
    try:
        after = np.fetch_pulse()
    finally:
        restore()
    problems = []
    if before.get("sourcing") != stc.SOURCING_SOVEREIGN:
        problems.append(f"before sourcing={before.get('sourcing')}")
    if after.get("sourcing") != stc.SOURCING_FALLBACK:
        problems.append(f"after sourcing={after.get('sourcing')}")
    # Everything except sourcing/timestamp/node should be identical
    # (timestamp is wall-clock; node reflects tunnel-vs-public display).
    skip = {"sourcing", "timestamp", "node"}
    b = {k: v for k, v in before.items() if k not in skip}
    a = {k: v for k, v in after.items() if k not in skip}
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

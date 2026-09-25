"""Forced-fallback + sourcing tests for the AMM ranking path (GAP-5).

GAP-5 (Charlie 2026-09-25): rank_amms (the hourly Mac walker behind /pools)
reads amm_info own-node-first via xrpl_client.XrplClient, PUBLIC_NODES as the
labeled fallback, ONE walker_node_fallback row per pass via
xrpl_client.RunFallbackSink, and records `sourcing` in the amm_ranker
heartbeat extra that /pools keys its banner off. Pre-fix it built a bare
JsonRpcClient on XRPL_NODE with a hardcoded s1.ripple.com default.

Also locks the Render-side side-finding: wallet_explainer._build_amm_lookup
used to run a LIVE amm_info sweep against hardcoded s1 from Render; it now
reads the walker-cached amm_ranked_pools rows.

Same harness shape as tests/test_bridge_signer_sourcing.py. Hermetic: no DB,
no network. pytest-collected; also runs standalone:
    ./venv/bin/python tests/test_amm_ranking_sourcing.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xrpl.models.requests import AMMInfo

import db
import xrpl_client as xc
import rank_amms as ra
import amm_scan_pools as asp
import wallet_explainer as we


_RLUSD_HEX = "524C555344000000000000000000000000000000"
_RLUSD_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
_ENTRIES = [
    {"Account": "rAMM1", "Asset": {"currency": "XRP"},
     "Asset2": {"currency": _RLUSD_HEX, "issuer": _RLUSD_ISSUER}},
    {"Account": "rAMM2", "Asset": {"currency": "XRP"},
     "Asset2": {"currency": "USD", "issuer": "rISSUER2"}},
    {"Account": "rGONE", "Asset": {"currency": "XRP"},
     "Asset2": {"currency": "USD", "issuer": "rDELETED"}},
]


class _Resp:
    def __init__(self, result):
        self.result = result


def _canned(req):
    assert isinstance(req, AMMInfo)
    issuer = (req.asset2 or {}).get("issuer") if isinstance(req.asset2, dict) else None
    if issuer == "rDELETED":
        return {"error": "actNotFound"}
    return {"amm": {
        "account": "rAMM_" + str(issuer),
        "amount": "1000000000",            # 1,000 XRP in drops-as-string? rank uses float()
        "amount2": {"currency": "X", "issuer": issuer, "value": "1500"},
        "trading_fee": 500,
        "lp_token": {"value": "42"},
    }}


class _WriteRecorder:
    def __init__(self):
        self.rows = []

    def __call__(self, walker_name, reason):
        self.rows.append((walker_name, reason))
        return True


class _RpcRecorder:
    def __init__(self, local_ok):
        self.local_ok = local_ok
        self.urls = []

    def __call__(self, url, req):
        self.urls.append(url)
        if url == xc.LOCAL_NODE and not self.local_ok:
            raise ConnectionError("refused")
        return _Resp(_canned(req))


def _install(local_ok, recorder):
    saved = {
        "_probe_local": xc._probe_local,
        "_post_rpc": xc._post_rpc,
        "_health": dict(xc._health),
        "write_walker_node_fallback": db.write_walker_node_fallback,
        "sleep": xc.time.sleep,
        "ra_sleep": ra.time.sleep,
        "_sink": ra._sink,
        "_rpc_made": ra._rpc_made_this_run,
    }
    rpc = _RpcRecorder(local_ok)
    xc._probe_local = (lambda: (True, "full")) if local_ok else \
        (lambda: (False, "unreachable:ConnectError"))
    xc._post_rpc = rpc
    xc._health.update({"checked_at": 0.0, "ok": False, "reason": "uninitialized"})
    db.write_walker_node_fallback = recorder
    xc.time.sleep = lambda _s: None
    ra.time.sleep = lambda _s: None
    ra._rpc_made_this_run = False

    def restore():
        xc._probe_local = saved["_probe_local"]
        xc._post_rpc = saved["_post_rpc"]
        xc._health.update(saved["_health"])
        db.write_walker_node_fallback = saved["write_walker_node_fallback"]
        xc.time.sleep = saved["sleep"]
        ra.time.sleep = saved["ra_sleep"]
        ra._sink = saved["_sink"]
        ra._rpc_made_this_run = saved["_rpc_made"]
    return rpc, restore


def _rank_pass(local_ok):
    """Rank the canned index the way main() does per entry, with the
    module-level sink __main__ would have created."""
    rec = _WriteRecorder()
    rpc, restore = _install(local_ok, rec)
    try:
        ra._sink = xc.RunFallbackSink()
        client = xc.get_client(ra.WALKER_NAME, fallback_sink=ra._sink)
        rows = [ra.rank_one(client, e, pegs={}, verified_brands={}) for e in _ENTRIES]
        sourcing = ra._run_sourcing()
        reason = ra._sink.reason
    finally:
        restore()
    return rows, sourcing, reason, rec, rpc


# ── Tests ─────────────────────────────────────────────────────────────
def test_healthy_own_node_is_sovereign():
    rows, sourcing, _, rec, rpc = _rank_pass(local_ok=True)
    assert sourcing == "sovereign"
    assert rec.rows == [], rec.rows
    assert set(rpc.urls) == {xc.LOCAL_NODE}, set(rpc.urls)
    assert len(rpc.urls) == 3
    priced = [r for r in rows if r is not None]
    assert len(priced) == 2 and rows[2] is None      # actNotFound skipped cleanly
    assert all(r["tvl_status"] in ("exact", "estimated") for r in priced), rows
    assert priced[0]["fee_pct"] == 0.5


def test_forced_fallback_cascades_cleanly_one_row_per_pass():
    rows, sourcing, reason, rec, rpc = _rank_pass(local_ok=False)
    # Data still lands via public; same rows as the healthy pass.
    priced = [r for r in rows if r is not None]
    assert len(priced) == 2 and rows[2] is None
    assert all(r["tvl_status"] in ("exact", "estimated") for r in priced), rows
    assert sourcing == "fallback-public-rpc"
    # Exactly ONE row despite 3 cascading amm_info calls.
    assert len(rec.rows) == 1, rec.rows
    assert rec.rows[0] == ("rank_amms", "unreachable:ConnectError")
    assert reason == "unreachable:ConnectError"
    public = [u for u in rpc.urls if u != xc.LOCAL_NODE]
    assert len(public) == 3 and set(public) == {xc.PUBLIC_NODES[0]}, rpc.urls
    assert "s1.ripple.com" in xc.PUBLIC_NODES[0]


def test_before_after_envelope_diff():
    rows_ok, s_ok, _, rec_ok, _ = _rank_pass(local_ok=True)
    rows_bad, s_bad, _, rec_bad, _ = _rank_pass(local_ok=False)
    assert (s_ok, s_bad) == ("sovereign", "fallback-public-rpc")
    assert rows_ok == rows_bad, "ranked rows must be identical; only provenance differs"
    assert (len(rec_ok.rows), len(rec_bad.rows)) == (0, 1)
    print(f"    before.sourcing={s_ok!r}  after.sourcing={s_bad!r}  "
          f"fallback_rows: before={len(rec_ok.rows)} after={len(rec_bad.rows)}")


def test_no_rpc_pass_carries_previous_heartbeat_sourcing():
    """The 'already finished' early return makes no RPC; the heartbeat it
    rewrites must carry the previous pass's sourcing, not claim sovereign."""
    saved_read = db.read_heartbeat
    saved_sink, saved_flag = ra._sink, ra._rpc_made_this_run
    try:
        db.read_heartbeat = lambda worker: {"extra": {"sourcing": "fallback-public-rpc"}}
        ra._sink = xc.RunFallbackSink()
        ra._rpc_made_this_run = False
        assert ra._run_sourcing() == "fallback-public-rpc"
        db.read_heartbeat = lambda worker: None
        assert ra._run_sourcing() == "sovereign"
    finally:
        db.read_heartbeat = saved_read
        ra._sink, ra._rpc_made_this_run = saved_sink, saved_flag


def test_wallet_explainer_lookup_reads_cached_rows_not_live_rpc():
    """Render-side lock: no live amm_info sweep; the map comes from the
    walker-written amm_ranked_pools rows."""
    saved = db.read_amm_ranked_pools
    rpc_calls = []
    saved_post = xc._post_rpc
    xc._post_rpc = lambda url, req: rpc_calls.append(url)
    try:
        db.read_amm_ranked_pools = lambda: [
            {"amm_account": "rA", "pair": "XRP/RLUSD"},
            {"amm_account": None, "pair": "XRP/JUNK"},
            {"amm_account": "rB", "pair": "XRP/USD"},
        ]
        out = we._build_amm_lookup()
        db.read_amm_ranked_pools = lambda: (_ for _ in ()).throw(RuntimeError("pg down"))
        empty = we._build_amm_lookup()
    finally:
        db.read_amm_ranked_pools = saved
        xc._post_rpc = saved_post
    assert out == {"rA": "XRP/RLUSD", "rB": "XRP/USD"}
    assert empty == {}
    assert rpc_calls == [], "wallet explainer must not make live RPC"
    src = open(we.__file__, encoding="utf-8").read()
    body = src.split("def _build_amm_lookup")[1].split("\ndef ")[0]
    # The docstring may NAME the old call as history; the body must not
    # import or invoke the scan module.
    assert "from amm_scan_pools import" not in body
    assert "scan_all_pools_cached()" not in "\n".join(
        l for l in body.splitlines() if not l.strip().startswith(("#", "amm_scan_pools.")))


def test_scan_module_reports_label_and_sourcing():
    rec = _WriteRecorder()
    rpc, restore = _install(local_ok=False, recorder=rec)
    saved_tokens = asp.KNOWN_TOKENS
    saved_lc = asp.LedgerCurrent
    try:
        asp.KNOWN_TOKENS = [{"name": "RLUSD", "currency": _RLUSD_HEX,
                             "issuer": _RLUSD_ISSUER, "usd_peg": 1.0}]
        # LedgerCurrent isn't AMMInfo; route it through the same fake.
        def fake_post(url, req):
            rpc.urls.append(url)
            if url == xc.LOCAL_NODE:
                raise ConnectionError("refused")
            if isinstance(req, AMMInfo):
                return _Resp(_canned(req))
            return _Resp({"ledger_current_index": 107_500_000})
        xc._post_rpc = fake_post
        data = asp.scan_all_pools()
    finally:
        asp.KNOWN_TOKENS = saved_tokens
        asp.LedgerCurrent = saved_lc
        restore()
    assert data["error"] is None
    assert data["sourcing"] == "fallback-public-rpc"
    assert data["node"].startswith("public fallback (") and "://" in data["node"]
    assert len(rec.rows) == 1 and rec.rows[0][0] == "amm_scan_pools"


def test_sources_have_no_bare_public_default():
    import scan_all_amms as saa
    import amm_test as at
    for mod, allowed_s2 in ((ra, 0), (asp, 0), (at, 0), (saa, 1)):
        src = open(mod.__file__, encoding="utf-8").read()
        lines = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
        code = "\n".join(lines)
        # Docstrings may mention public hosts as history; the lock is on
        # ASSIGNMENTS (a hardcoded default) and imports.
        assigns = [l for l in lines if "=" in l]
        assert not any("s1.ripple.com" in l for l in assigns), mod.__name__
        assert sum("s2.ripple.com" in l for l in assigns) == allowed_s2, mod.__name__
        assert not any("JsonRpcClient" in l for l in lines if "import" in l or "(" in l), mod.__name__
        assert not any("XRPL_NODE" in l for l in assigns), mod.__name__
        assert "xrpl_client.get_client(" in code, mod.__name__
    # app.py must not import the scan module's client/node/live scan.
    app_src = open(os.path.join(os.path.dirname(ra.__file__), "app.py"), encoding="utf-8").read()
    head = app_src[:4000]
    assert "JsonRpcClient," not in head and "XRPL_NODE," not in head
    assert "scan_all_pools_cached," not in head and "fetch_pool," not in head


TESTS = [
    ("healthy_own_node_is_sovereign", test_healthy_own_node_is_sovereign),
    ("forced_fallback_cascades_cleanly_one_row_per_pass",
     test_forced_fallback_cascades_cleanly_one_row_per_pass),
    ("before_after_envelope_diff", test_before_after_envelope_diff),
    ("no_rpc_pass_carries_previous_heartbeat_sourcing",
     test_no_rpc_pass_carries_previous_heartbeat_sourcing),
    ("wallet_explainer_lookup_reads_cached_rows_not_live_rpc",
     test_wallet_explainer_lookup_reads_cached_rows_not_live_rpc),
    ("scan_module_reports_label_and_sourcing", test_scan_module_reports_label_and_sourcing),
    ("sources_have_no_bare_public_default", test_sources_have_no_bare_public_default),
]


def main():
    pass_count = fail_count = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  PASS {name}")
            pass_count += 1
        except AssertionError as e:
            print(f"  FAIL {name}: {e}")
            fail_count += 1
    print(f"\n== {pass_count} PASS / {fail_count} FAIL ==")
    return fail_count


if __name__ == "__main__":
    sys.exit(main())

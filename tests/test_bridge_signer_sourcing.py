"""Forced-fallback + sourcing tests for bridge_signer_walker (GAP-2).

GAP-2 (Charlie 2026-09-25): the walker reads the Axelar gateway own-node-first
via xrpl_client.XrplClient (LOCAL_NODE with retry, then PUBLIC_NODES as the
labeled fallback) and collapses every cascade in a run into ONE
walker_node_fallback row through _RunFallbackSink. Pre-fix it built a bare
JsonRpcClient on XRPL_NODE with a hardcoded s1.ripple.com default.

Mirrors tests/test_network_pulse_sourcing.py in spirit (healthy / forced
fallback / before-after envelope diff), differing in the primitive under
test: here the own-node client is xrpl_client (Mac walker side), not
SovereignFetcher (Render tunnel side), so the patch points are
xrpl_client._probe_local + xrpl_client._post_rpc.

Hermetic: no DB (scan runs dry_run=True, conn=None), no network. Collected by
pytest as test_* functions; also runs standalone:
    ./venv/bin/python tests/test_bridge_signer_sourcing.py
"""
import os
import sys

# Repo root importable directly (standalone run bypasses conftest.py)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xrpl.models.requests import AccountObjects, AccountTx, Ledger

import db
import xrpl_client as xc
import bridge_signer_walker as bsw


# ── Canned ledger state ──────────────────────────────────────────────
_VALIDATED = 107_500_000
_FROM = _VALIDATED - 1_200          # one hourly window
_ROTATION_LEDGER = _FROM + 700
_ROTATION_HASH = "A" * 64
_SIGNERS = [{"SignerEntry": {"Account": f"r{'S' * 20}{i:03d}", "SignerWeight": 65535}}
            for i in range(3)]


class _Resp:
    """Minimal stand-in for the xrpl-py Response xrpl_client._post_rpc returns."""
    def __init__(self, result):
        self.result = result


def _canned(req):
    """Shape-valid results for every request the steady-state + bootstrap
    paths issue. account_tx pages three times (marker on pages 1 and 2) so a
    forced cascade exercises several calls per run."""
    if isinstance(req, Ledger):
        if req.ledger_index == "validated":
            return {"ledger_index": _VALIDATED,
                    "ledger": {"ledger_index": _VALIDATED,
                               "close_time_iso": "2026-09-25T21:00:00Z"}}
        return {"ledger": {"ledger_index": req.ledger_index,
                           "close_time_iso": "2026-09-25T20:30:00Z"}}
    if isinstance(req, AccountTx):
        page = req.marker or 0
        txs = [{"tx": {"TransactionType": "Payment", "hash": "B" * 64,
                       "ledger_index": _FROM + 10 + page, "date": 843_000_000},
                "meta": {"TransactionResult": "tesSUCCESS"},
                "ledger_index": _FROM + 10 + page, "hash": "B" * 64}]
        if page == 1:
            txs.append({
                "tx": {"TransactionType": "SignerListSet", "hash": _ROTATION_HASH,
                       "SignerQuorum": 131070, "SignerEntries": _SIGNERS,
                       "ledger_index": _ROTATION_LEDGER, "date": 843_000_100},
                "meta": {"TransactionResult": "tesSUCCESS"},
                "ledger_index": _ROTATION_LEDGER, "hash": _ROTATION_HASH,
            })
            # A failed rotation must NOT count.
            txs.append({
                "tx": {"TransactionType": "SignerListSet", "hash": "C" * 64,
                       "SignerQuorum": 1, "SignerEntries": _SIGNERS,
                       "ledger_index": _ROTATION_LEDGER + 1, "date": 843_000_200},
                "meta": {"TransactionResult": "tecNO_PERMISSION"},
                "ledger_index": _ROTATION_LEDGER + 1, "hash": "C" * 64,
            })
        out = {"transactions": txs}
        if page < 2:
            out["marker"] = page + 1
        return out
    if isinstance(req, AccountObjects):
        return {"ledger_index": _VALIDATED,
                "account_objects": [{"LedgerEntryType": "SignerList",
                                     "SignerQuorum": 131070,
                                     "SignerEntries": _SIGNERS}]}
    return {}


class _WriteRecorder:
    """Stands in for db.write_walker_node_fallback — records every row."""
    def __init__(self):
        self.rows = []

    def __call__(self, walker_name, reason):
        self.rows.append((walker_name, reason))
        return True


class _RpcRecorder:
    """Stands in for xrpl_client._post_rpc — records which URL served each
    call. local_ok=False makes LOCAL_NODE raise (transport failure)."""
    def __init__(self, local_ok):
        self.local_ok = local_ok
        self.urls = []

    def __call__(self, url, req):
        self.urls.append(url)
        if url == xc.LOCAL_NODE and not self.local_ok:
            raise ConnectionError("refused")
        return _Resp(_canned(req))


def _install(local_ok, recorder):
    """Patch xrpl_client's probe + transport, the db fallback writer, and
    reset the module health cache so each case starts cold. Returns
    (rpc_recorder, restore_thunk)."""
    saved = {
        "_probe_local": xc._probe_local,
        "_post_rpc": xc._post_rpc,
        "_health": dict(xc._health),
        "write_walker_node_fallback": db.write_walker_node_fallback,
        "sleep": xc.time.sleep,
    }
    rpc = _RpcRecorder(local_ok)
    xc._probe_local = (lambda: (True, "full")) if local_ok else \
        (lambda: (False, "unreachable:ConnectError"))
    xc._post_rpc = rpc
    xc._health.update({"checked_at": 0.0, "ok": False, "reason": "uninitialized"})
    db.write_walker_node_fallback = recorder
    xc.time.sleep = lambda _s: None  # no real backoff waits in tests

    def restore():
        xc._probe_local = saved["_probe_local"]
        xc._post_rpc = saved["_post_rpc"]
        xc._health.update(saved["_health"])
        db.write_walker_node_fallback = saved["write_walker_node_fallback"]
        xc.time.sleep = saved["sleep"]
    return rpc, restore


def _run_walker_pass():
    """One steady-state pass the way main() does it, minus Postgres:
    resolve validated ledger, dry-run scan the window. Returns
    (to_ledger, events, sink, client)."""
    sink = bsw._RunFallbackSink()
    client = xc.get_client(bsw.WALKER_NAME, fallback_sink=sink)
    to_ledger = bsw.current_validated_ledger(client)
    events = bsw.scan_signerlistset_events(client, None, _FROM, to_ledger,
                                           dry_run=True)
    return to_ledger, events, sink, client


# ── Tests ─────────────────────────────────────────────────────────────
def test_healthy_own_node_is_sovereign():
    rec = _WriteRecorder()
    rpc, restore = _install(local_ok=True, recorder=rec)
    try:
        to_ledger, events, sink, client = _run_walker_pass()
    finally:
        restore()
    assert to_ledger == _VALIDATED
    assert events == 1, f"expected exactly the tesSUCCESS rotation, got {events}"
    assert sink.sourcing == "sovereign"
    assert client.sourcing == "sovereign"
    assert rec.rows == [], f"unexpected fallback rows: {rec.rows}"
    # Every call went to our own node, none to a public URL.
    assert rpc.urls and set(rpc.urls) == {xc.LOCAL_NODE}, rpc.urls
    assert len(rpc.urls) == 4, f"expected ledger + 3 account_tx pages, got {rpc.urls}"


def test_forced_fallback_cascades_cleanly_one_row_per_run():
    rec = _WriteRecorder()
    rpc, restore = _install(local_ok=False, recorder=rec)
    try:
        to_ledger, events, sink, client = _run_walker_pass()
    finally:
        restore()
    # Cascade must not break the scan — data still comes through public.
    assert to_ledger == _VALIDATED
    assert events == 1
    # The disclosure-driving flag flips.
    assert sink.sourcing == "fallback-public-rpc"
    assert client.sourcing == "fallback-public-rpc"
    # Exactly ONE walker_node_fallback row for the whole run, despite 4 RPCs
    # cascading (ledger + 3 account_tx pages).
    assert len(rec.rows) == 1, f"fallback rows={len(rec.rows)}, expected 1: {rec.rows}"
    assert rec.rows[0][0] == "bridge_signer_walker"
    assert rec.rows[0][1] == "unreachable:ConnectError"
    assert sink.rows_written == 1
    # Public served every call; the first PUBLIC_NODES entry took them.
    public = [u for u in rpc.urls if u != xc.LOCAL_NODE]
    assert len(public) == 4, rpc.urls
    assert set(public) == {xc.PUBLIC_NODES[0]}, public


def test_before_after_envelope_diff():
    """Same pass, own node healthy then failing — the sourcing flag is the
    only thing that changes; the scan result is identical."""
    rec_ok = _WriteRecorder()
    _, restore = _install(local_ok=True, recorder=rec_ok)
    try:
        before = _run_walker_pass()
    finally:
        restore()
    rec_bad = _WriteRecorder()
    _, restore = _install(local_ok=False, recorder=rec_bad)
    try:
        after = _run_walker_pass()
    finally:
        restore()
    assert before[0] == after[0]          # to_ledger
    assert before[1] == after[1] == 1     # events
    assert before[2].sourcing == "sovereign"
    assert after[2].sourcing == "fallback-public-rpc"
    assert len(rec_ok.rows) == 0 and len(rec_bad.rows) == 1
    print(f"    before.sourcing={before[2].sourcing!r}  after.sourcing={after[2].sourcing!r}  "
          f"fallback_rows: before={len(rec_ok.rows)} after={len(rec_bad.rows)}")


def test_bootstrap_path_shares_the_run_sink():
    """The bootstrap read (account_objects + a Ledger close_time lookup)
    goes through the same client, so a cascade there is also one row."""
    rec = _WriteRecorder()
    rpc, restore = _install(local_ok=False, recorder=rec)
    try:
        sink = bsw._RunFallbackSink()
        client = xc.get_client(bsw.WALKER_NAME, fallback_sink=sink)
        snap = bsw.fetch_current_signer_list(client)
    finally:
        restore()
    assert snap and snap["ledger_index"] == _VALIDATED
    assert snap["signer_count"] == 3 and snap["quorum"] == 131070
    assert sink.sourcing == "fallback-public-rpc"
    assert len(rec.rows) == 1, rec.rows
    assert len(rpc.urls) >= 2  # account_objects + ledger close_time


def test_source_has_no_bare_public_default():
    """Lock: the walker file must not carry a hardcoded public node or a
    bare JsonRpcClient — the only public reference lives in
    xrpl_client.PUBLIC_NODES."""
    src = open(bsw.__file__, encoding="utf-8").read()
    # Strip the GAP-2 comment block that explains the old default.
    code_lines = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    assert "s1.ripple.com" not in code
    assert "s2.ripple.com" not in code
    assert "JsonRpcClient" not in code
    assert "XRPL_NODE" not in code
    assert "xrpl_client.get_client(WALKER_NAME, fallback_sink=sink)" in code


def test_default_client_behavior_unchanged_without_sink():
    """Regression guard for every OTHER walker: with no fallback_sink,
    XrplClient still writes a row per cascading call, exactly as before."""
    rec = _WriteRecorder()
    _, restore = _install(local_ok=False, recorder=rec)
    try:
        client = xc.get_client("some_other_walker")
        client.request(Ledger(ledger_index="validated"))
        client.request(Ledger(ledger_index="validated"))
    finally:
        restore()
    assert len(rec.rows) == 2, rec.rows
    assert all(name == "some_other_walker" for name, _ in rec.rows)
    assert client.sourcing == "fallback-public-rpc"


TESTS = [
    ("healthy_own_node_is_sovereign", test_healthy_own_node_is_sovereign),
    ("forced_fallback_cascades_cleanly_one_row_per_run",
     test_forced_fallback_cascades_cleanly_one_row_per_run),
    ("before_after_envelope_diff", test_before_after_envelope_diff),
    ("bootstrap_path_shares_the_run_sink", test_bootstrap_path_shares_the_run_sink),
    ("source_has_no_bare_public_default", test_source_has_no_bare_public_default),
    ("default_client_behavior_unchanged_without_sink",
     test_default_client_behavior_unchanged_without_sink),
]


def main():
    pass_count = 0
    fail_count = 0
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

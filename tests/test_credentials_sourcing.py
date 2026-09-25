"""Forced-fallback + sourcing tests for credentials_state (GAP-3).

GAP-3 (Charlie 2026-09-25): the credentials walker reads own-node-first via
xrpl_client.XrplClient (LOCAL_NODE with retry, then the public Clio s2 → s1
as the labeled fallback) and collapses every cascade in a run into ONE
walker_node_fallback row via xrpl_client.RunFallbackSink. The page-level
`sourcing` is persisted in the snapshot so /credentials renders its banner
off the LAST refresh. Pre-fix every read was a raw httpx.post to
XRPL_FULL / XRPL_CLIO with hardcoded s1 / s2 defaults.

Same harness shape as tests/test_bridge_signer_sourcing.py (patch
xrpl_client._probe_local + _post_rpc, the db fallback writer, and here also
the two credentials_snapshot db calls so run_once() is hermetic).

Hermetic: no DB, no network. pytest-collected; also runs standalone:
    ./venv/bin/python tests/test_credentials_sourcing.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xrpl.asyncio.clients.utils import request_to_json_rpc

import db
import xrpl_client as xc
import credentials_state as cs


_HEAD = 107_500_000
_ISSUER = "rLGRav6ziCMTpQGeobWXz9gAs8kBstr2jU"   # in SEED_ACCOUNTS_BOOTSTRAP
_SUBJECT = "rSUBJECTsubjectSUBJECTsubject0001"  # discovered via the walk
_CRED = {
    "LedgerEntryType": "Credential", "index": "F" * 64,
    "Issuer": _ISSUER, "Subject": _SUBJECT,
    "CredentialType": "4B5943",  # "KYC"
    "Flags": cs.LSF_ACCEPTED,
}


class _Resp:
    def __init__(self, result):
        self.result = result


def _canned(method, params):
    if method == "feature":
        return {"features": {"A" * 64: {"name": "Credentials", "enabled": True,
                                        "supported": True}}}
    if method == "account_objects":
        acct = params.get("account")
        if acct in (_ISSUER, _SUBJECT):
            return {"ledger_index": _HEAD, "account_objects": [_CRED]}
        return {"ledger_index": _HEAD, "account_objects": []}
    if method == "ledger_current":
        return {"ledger_current_index": _HEAD}
    if method == "ledger":
        li = params.get("ledger_index")
        txs = [{"TransactionType": "Payment"}]
        if li == _HEAD - 2:
            txs.append({"TransactionType": "CredentialCreate"})
        return {"ledger": {"ledger_index": li, "close_time": 843_000_000 - (_HEAD - li) * 4,
                           "transactions": txs}}
    if method == "ledger_data":
        return {"ledger_index": _HEAD, "state": [_CRED]}
    return {}


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
        self.methods = []

    def __call__(self, url, req):
        self.urls.append(url)
        payload = request_to_json_rpc(req)
        method = payload["method"]
        params = (payload.get("params") or [{}])[0]
        self.methods.append(method)
        if url == xc.LOCAL_NODE and not self.local_ok:
            raise ConnectionError("refused")
        return _Resp(_canned(method, params))


class _SnapshotStore:
    """Stands in for db.read/write_credentials_snapshot."""
    def __init__(self, existing=None):
        self.existing = existing
        self.written = None

    def read(self):
        return self.existing

    def write(self, payload):
        self.written = payload


def _install(local_ok, recorder, store):
    saved = {
        "_probe_local": xc._probe_local,
        "_post_rpc": xc._post_rpc,
        "_health": dict(xc._health),
        "write_walker_node_fallback": db.write_walker_node_fallback,
        "read_credentials_snapshot": db.read_credentials_snapshot,
        "write_credentials_snapshot": db.write_credentials_snapshot,
        "sleep": xc.time.sleep,
        "CUM": cs.CUMULATIVE_BUDGET_SECONDS,
        "REC": cs.RECENT_BUDGET_SECONDS,
    }
    rpc = _RpcRecorder(local_ok)
    xc._probe_local = (lambda: (True, "full")) if local_ok else \
        (lambda: (False, "unreachable:ConnectError"))
    xc._post_rpc = rpc
    xc._health.update({"checked_at": 0.0, "ok": False, "reason": "uninitialized"})
    db.write_walker_node_fallback = recorder
    db.read_credentials_snapshot = store.read
    db.write_credentials_snapshot = store.write
    xc.time.sleep = lambda _s: None
    cs.CUMULATIVE_BUDGET_SECONDS = 5
    cs.RECENT_BUDGET_SECONDS = 0.25   # a few hundred fake ledgers, fast
    cs._run["client"] = None
    cs._run["sink"] = None
    # credentials_state keeps a module-level _state; the walker is a
    # one-shot process so it never carries over in prod, but tests share
    # the module — start each case with an empty in-memory state.
    for key in ("amendment", "cumulative", "recent"):
        cs._state[key] = None

    def restore():
        xc._probe_local = saved["_probe_local"]
        xc._post_rpc = saved["_post_rpc"]
        xc._health.update(saved["_health"])
        db.write_walker_node_fallback = saved["write_walker_node_fallback"]
        db.read_credentials_snapshot = saved["read_credentials_snapshot"]
        db.write_credentials_snapshot = saved["write_credentials_snapshot"]
        xc.time.sleep = saved["sleep"]
        cs.CUMULATIVE_BUDGET_SECONDS = saved["CUM"]
        cs.RECENT_BUDGET_SECONDS = saved["REC"]
        cs._run["client"] = None
        cs._run["sink"] = None
    return rpc, restore


def _run(local_ok, existing=None):
    rec = _WriteRecorder()
    store = _SnapshotStore(existing)
    rpc, restore = _install(local_ok, rec, store)
    try:
        sourcing = cs.run_once()
    finally:
        restore()
    return sourcing, store.written, rec, rpc


# ── Tests ─────────────────────────────────────────────────────────────
def test_healthy_own_node_is_sovereign():
    sourcing, payload, rec, rpc = _run(local_ok=True)
    assert sourcing == "sovereign"
    assert payload["sourcing"] == "sovereign"
    cum = payload["cumulative"]
    assert cum["count"] == 1 and cum["exhausted"] is True
    assert _SUBJECT in cum["seed_accounts"], "walk must auto-expand the seed set"
    assert cum["node"] == cs.OWN_NODE_LABEL and cum["sourcing"] == "sovereign"
    assert payload["amendment"]["enabled"] is True
    assert payload["amendment"]["node"] == cs.OWN_NODE_LABEL
    assert payload["recent"]["creates"] >= 1
    assert payload["recent"]["node"] == cs.OWN_NODE_LABEL
    assert rec.rows == [], rec.rows
    assert set(rpc.urls) == {xc.LOCAL_NODE}, set(rpc.urls)
    assert {"feature", "account_objects", "ledger_current", "ledger"} <= set(rpc.methods)
    # No raw URL leaks into anything the page renders.
    for sec in ("amendment", "cumulative", "recent"):
        assert "://" not in str(payload[sec]["node"])


def test_forced_fallback_cascades_cleanly_one_row_per_run():
    sourcing, payload, rec, rpc = _run(local_ok=False)
    # Data still lands via the public cascade.
    assert payload["cumulative"]["count"] == 1
    assert payload["amendment"]["enabled"] is True
    assert payload["recent"]["creates"] >= 1
    # Page-level + section-level flags flip.
    assert sourcing == "fallback-public-rpc"
    assert payload["sourcing"] == "fallback-public-rpc"
    for sec in ("amendment", "cumulative", "recent"):
        assert payload[sec]["sourcing"] == "fallback-public-rpc", sec
        assert payload[sec]["node"] == cs._public_label()
        assert "s2.ripple.com" in payload[sec]["node"]
    # Exactly ONE row for the whole run despite many cascading calls
    # (feature + N account_objects pages + ledger_current + M ledgers).
    assert len(rec.rows) == 1, f"{len(rec.rows)} rows: {rec.rows[:3]}"
    assert rec.rows[0] == ("credentials_walker", "unreachable:ConnectError")
    public = [u for u in rpc.urls if u != xc.LOCAL_NODE]
    assert len(public) >= 5, rpc.methods
    # s2 (public Clio) is the FIRST fallback, not s1.
    assert set(public) == {cs.PUBLIC_FALLBACK_URLS[0]}, set(public)
    assert "s2.ripple.com" in cs.PUBLIC_FALLBACK_URLS[0]


def test_before_after_envelope_diff():
    s_ok, p_ok, rec_ok, _ = _run(local_ok=True)
    s_bad, p_bad, rec_bad, _ = _run(local_ok=False)
    assert (s_ok, s_bad) == ("sovereign", "fallback-public-rpc")
    # Ledger-state payload identical; only provenance fields differ.
    assert p_ok["cumulative"]["count"] == p_bad["cumulative"]["count"]
    assert p_ok["cumulative"]["seed_accounts"] == p_bad["cumulative"]["seed_accounts"]
    assert p_ok["amendment"]["hash"] == p_bad["amendment"]["hash"]
    assert (len(rec_ok.rows), len(rec_bad.rows)) == (0, 1)
    print(f"    before.sourcing={s_ok!r}  after.sourcing={s_bad!r}  "
          f"fallback_rows: before={len(rec_ok.rows)} after={len(rec_bad.rows)}")


def test_carried_over_section_keeps_its_sourcing():
    """A section reused from the existing snapshot (this run produced none)
    keeps its recorded sourcing, and it taints the page (symmetry rule)."""
    existing = {
        "amendment": {"enabled": True, "sourcing": "fallback-public-rpc"},
        "cumulative": {"count": 3, "seed_set_size": 3, "seed_accounts": [],
                       "sourcing": "sovereign"},
        "recent": None,
    }
    rec = _WriteRecorder()
    store = _SnapshotStore(existing)
    rpc, restore = _install(local_ok=True, recorder=rec, store=store)
    try:
        # Simulate a run whose amendment fetch failed everywhere: feature
        # returns None → amendment section carried over from `existing`.
        orig = cs._fetch_amendment_status
        cs._fetch_amendment_status = lambda: None
        try:
            sourcing = cs.run_once()
        finally:
            cs._fetch_amendment_status = orig
    finally:
        restore()
    assert sourcing == "fallback-public-rpc"
    assert store.written["amendment"]["sourcing"] == "fallback-public-rpc"
    assert store.written["cumulative"]["sourcing"] == "sovereign"
    assert rec.rows == []


def test_source_has_no_bare_public_default():
    src = open(cs.__file__, encoding="utf-8").read()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "s1.ripple.com" not in code
    assert "XRPL_CLIO_NODE" not in code and "XRPL_NODE" not in code
    assert "httpx.post" not in code and "import httpx" not in code
    # The ONLY public literal is the labeled Clio fallback default.
    assert code.count("s2.ripple.com") == 1
    assert "xrpl_client.get_client(" in code and "public_urls=PUBLIC_FALLBACK_URLS" in code


TESTS = [
    ("healthy_own_node_is_sovereign", test_healthy_own_node_is_sovereign),
    ("forced_fallback_cascades_cleanly_one_row_per_run",
     test_forced_fallback_cascades_cleanly_one_row_per_run),
    ("before_after_envelope_diff", test_before_after_envelope_diff),
    ("carried_over_section_keeps_its_sourcing", test_carried_over_section_keeps_its_sourcing),
    ("source_has_no_bare_public_default", test_source_has_no_bare_public_default),
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

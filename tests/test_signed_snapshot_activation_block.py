"""Activation-day regression tests for the signed leaf's amendments_block.

Covers (Charlie ruling 2026-09-29):
  * a recognized amendment that ACTIVATES (leaves Majorities, appears in the
    ledger enabled set) is recorded in per_amendment as enabled — it used to
    be dropped silently;
  * entry keys: observed_enabled_in_leaf {date, as_of_unix}, enable_ledger_index,
    enable_close_iso (null unless read on-ledger), majority_reached_ledger/_iso;
    the mislabeled first_seen_enabled_ledger must NOT exist;
  * eligibility: only amendments previously tracked (earlier leaf or majority
    history) — long-enabled watchlist items never get an entry;
  * carry-forward of observed_enabled_in_leaf across consecutive leaves, the
    earlier-date-only rule (same-date re-sign), gaps, and the 30-day drop;
  * the EnableAmendment parser on a REAL captured pseudo-transaction, and the
    lookup's null-not-raise behaviour on malformed / not-found / timeout inside
    its 3 s bound;
  * amendments_state exposes recognized_enabled but NOT an `enabled` key, so the
    MCP get_amendment_status public output is unchanged.
All in-memory; no network, no DB, no shared cache writes.
"""
import datetime as dt
import json
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import amendments_state as A  # noqa: E402
import amendments_network_votes as NV  # noqa: E402
import signed_snapshot as S  # noqa: E402

FIX = "fixBatchV1_2"
HASHES = {
    "PermissionDelegationV1_1": "0F48FF56E8D6E1B2A3C4D5E6F70819202122232425262728293031323334353",
    "fixBatchV1_2": "14A2B45E48A4A124D1BBA657AC7B0DC3D5EA8C256C89E8F0D8142D32960A7944",
    "BatchV1_1": "9F287AED2E8D6E1B2A3C4D5E6F7081920212223242526272829303132333401",
}
OLD_ENABLED = ["fixNFTokenDirV1", "fixNFTokenNegOffer", "NonFungibleTokensV1"]


class FakeFetcher:
    sourcing = "sovereign"

    def __init__(self, features, node):
        self.f, self.n = features, node

    def call(self, method, params=None):
        if method == "feature":
            return {"features": self.f, "ledger_index": 99999999}
        if method == "ledger_entry":
            return {"node": self.n, "ledger_index": 99999999}
        return None


def build_state(activate):
    feats = {}
    for i in range(90):
        feats[f"{i:064X}"] = {"name": f"Amd{i}", "enabled": True, "supported": True}
    for i, nm in enumerate(OLD_ENABLED):
        feats[f"AA{i:062X}"] = {"name": nm, "enabled": True, "supported": True}
    majorities = []
    for nm, h in HASHES.items():
        en = nm == activate
        feats[h] = {"name": nm, "enabled": en, "supported": True}
        if not en:
            majorities.append({"Majority": {"Amendment": h, "CloseTime": 810000000}})
    node = {"Amendments": [h for h, f in feats.items() if f["enabled"]],
            "Majorities": majorities}
    return feats, node


@pytest.fixture
def patched(monkeypatch):
    """Patch the two cached fetches the signer reads + the chain + majority
    history; restore everything after. Returns a builder(activate, chain, date)."""
    monkeypatch.setattr(NV, "fetch_network_vote_tallies_cached",
                        lambda: {"data": {}, "source_url": None, "as_of_iso": None,
                                 "status": "test", "cached_age_seconds": 0.0})
    monkeypatch.setattr(S, "_lookup_enable_amendment", lambda *a, **k: (None, None))
    import app as _app
    monkeypatch.setattr(_app, "_load_amendment_majority_history", lambda: [
        {"hash": HASHES[FIX], "name": FIX, "first_seen_ledger": 107227652,
         "first_seen_close_iso": "2026-09-25T14:13:10Z",
         "activation_eta_iso": "2026-10-09T14:12:51Z"},
    ])

    import tempfile
    sdir = tempfile.mkdtemp(prefix="act_leaves_")
    monkeypatch.setattr(S, "SNAPSHOTS_DIR", sdir)

    def build(activate, chain_leaves, date_str="2026-10-09"):
        # chain_leaves: list of (date, block) written as per-date files, the
        # real on-disk shape (chain.json holds dates only; metrics live in the
        # per-date file).
        for f in os.listdir(sdir):
            os.remove(os.path.join(sdir, f))
        for d, blk in chain_leaves:
            with open(os.path.join(sdir, f"{d}.json"), "w") as f:
                json.dump({"snapshot_date_utc": d,
                           "metrics": [{"name": "amendments_block", "value": blk}]}, f)
        monkeypatch.setattr(S, "load_chain", lambda: {
            "leaves": [{"date": d, "leaf_hash": "00", "ledger_index": 1} for d, _ in chain_leaves]})
        feats, node = build_state(activate)
        st = dict(A.fetch_amendments_state(fetcher=FakeFetcher(feats, node)))
        st["cached_age_seconds"] = 0.0
        monkeypatch.setattr(A, "fetch_amendments_state_cached", lambda ttl=None: dict(st))
        now = dt.datetime.fromisoformat(date_str + "T21:00:00+00:00")
        return S._assemble_amendments_block(now), st
    return build


def leaf(date_str, blk):
    return (date_str, blk)


def test_state_exposes_recognized_enabled_not_enabled():
    feats, node = build_state(FIX)
    st = A.fetch_amendments_state(fetcher=FakeFetcher(feats, node))
    assert "recognized_enabled" in st and len(st["recognized_enabled"]) == 94
    assert "enabled" not in st, "would change MCP get_amendment_status output"
    assert st["enabled_count"] == 94


def test_activated_amendment_recorded_with_correct_keys(patched):
    blk, _ = patched(FIX, [])
    pa = blk["per_amendment"]
    assert FIX in pa
    e = pa[FIX]
    assert e["enabled"] is True and e["hash"] == HASHES[FIX]
    assert e["observed_enabled_in_leaf"] == {"date": "2026-10-09", "as_of_unix": int(
        dt.datetime(2026, 10, 9, 21, tzinfo=dt.timezone.utc).timestamp())}
    assert e["majority_reached_ledger"] == 107227652
    assert e["majority_reached_iso"] == "2026-09-25T14:13:10Z"
    assert e["enable_ledger_index"] is None and e["enable_close_iso"] is None
    assert "first_seen_enabled_ledger" not in e
    # the other two are still in flight
    assert pa["PermissionDelegationV1_1"]["enabled"] is False
    assert pa["BatchV1_1"]["enabled"] is False
    assert "warnings" not in blk


def test_eligibility_excludes_long_enabled_watchlist(patched):
    blk, _ = patched(FIX, [])
    for nm in OLD_ENABLED:
        assert nm not in blk["per_amendment"]
    # a never-tracked activation (no prior leaf, no majority history) is NOT recorded
    blk2, _ = patched("BatchV1_1", [])
    # BatchV1_1 activated but has no majority-history row and no prior leaf ->
    # not eligible -> no entry at all (it is no longer in flight either)
    assert "BatchV1_1" not in blk2["per_amendment"]
    # ...but once an earlier leaf tracked it, it IS recorded
    prev = {"per_amendment": {"BatchV1_1": {"hash": HASHES["BatchV1_1"], "enabled": False}}}
    blk3, _ = patched("BatchV1_1", [leaf("2026-10-08", prev)], "2026-10-09")
    assert blk3["per_amendment"]["BatchV1_1"]["enabled"] is True
    assert blk3["per_amendment"]["BatchV1_1"]["majority_reached_ledger"] is None


def test_carry_forward_same_date_gap_and_drop(patched):
    b1, _ = patched(FIX, [], "2026-10-09")
    d1 = b1["per_amendment"][FIX]["observed_enabled_in_leaf"]
    chain = [leaf("2026-10-09", b1)]
    b2, _ = patched(FIX, chain, "2026-10-10")
    chain.append(leaf("2026-10-10", b2))
    b3, _ = patched(FIX, chain, "2026-10-11")
    assert b2["per_amendment"][FIX]["observed_enabled_in_leaf"] == d1
    assert b3["per_amendment"][FIX]["observed_enabled_in_leaf"] == d1
    # same-date re-sign: prior leaf of the same date is ignored -> self-anchors
    b_re, _ = patched(FIX, [leaf("2026-10-09", b1)], "2026-10-09")
    assert b_re["per_amendment"][FIX]["observed_enabled_in_leaf"]["date"] == "2026-10-09"
    # gap: Oct 9 leaf, next leaf Oct 12 -> carries Oct 9
    b_gap, _ = patched(FIX, [leaf("2026-10-09", b1)], "2026-10-12")
    assert b_gap["per_amendment"][FIX]["observed_enabled_in_leaf"] == d1
    # 30-day drop
    b_old, _ = patched(FIX, [leaf("2026-10-09", b1)], "2026-11-09")
    assert FIX not in b_old["per_amendment"]


def test_missing_tracked_amendment_is_warning_not_block(patched):
    # previous leaf tracked "Ghost" which is now neither in flight nor enabled
    prev = {"per_amendment": {"Ghost": {"hash": "00", "enabled": False}}}
    blk, _ = patched(FIX, [leaf("2026-10-08", prev)], "2026-10-09")
    assert any("Ghost" in w for w in blk.get("warnings", []))
    assert isinstance(blk["per_amendment"], dict)  # block still produced


# ---------------- EnableAmendment parser / lookup ----------------

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "enableamendment_fixCleanup3_3_0.json")


def _real():
    fx = json.load(open(FIXTURE))
    led = {"ledger": {"ledger_index": fx["ledger_index"], "close_time": fx["close_time"],
                      "transactions": [fx["tx"]]}}
    return fx, led, fx["tx"]["Amendment"].upper()


def test_parser_real_pseudo_tx():
    fx, led, h = _real()
    assert S._parse_enable_amendment(led, h) == (106911489, "2026-09-11T11:29:00Z")
    assert fx["ledger_index"] % 256 == 1, "enable lands at flag ledger + 1"
    assert fx["tx"]["Account"] == "rrrrrrrrrrrrrrrrrrrrrhoLvTp" and "Flags" not in fx["tx"]


def test_parser_rejects_majority_flag_variant_and_garbage():
    fx, led, h = _real()
    import copy
    maj = copy.deepcopy(led)
    maj["ledger"]["transactions"][0]["Flags"] = 0x00010000
    assert S._parse_enable_amendment(maj, h) == (None, None)
    assert S._parse_enable_amendment(led, "AB" * 32) == (None, None)
    for bad in [None, "x", {"ledger": "x"}, {"ledger": {"transactions": [None, 5, {"tx_json": 1}]}},
                {"ledger": {"transactions": [{"TransactionType": "EnableAmendment",
                                              "Amendment": h, "Flags": "no"}]}}]:
        assert S._parse_enable_amendment(bad, h) == (None, None)


def test_lookup_timeout_error_notfound_are_null_inside_bound():
    fx, led, h = _real()

    class Slow:
        def request(self, r):
            time.sleep(10)

    t = time.time()
    assert S._lookup_enable_amendment(h, timeout_s=1.0, client=Slow()) == (None, None)
    assert time.time() - t < 1.5

    class Err:
        def request(self, r):
            raise RuntimeError("boom")
    assert S._lookup_enable_amendment(h, timeout_s=1.0, client=Err()) == (None, None)

    class Empty:
        def request(self, r):
            return {"ledger": {"ledger_index": 106912000, "close_time": 842443000, "transactions": []}}
    assert S._lookup_enable_amendment(h, eta_iso="2026-09-11T11:20:00Z", timeout_s=1.0,
                                      client=Empty()) == (None, None)


def test_lookup_finds_real_enable_via_eta_probe():
    fx, led, h = _real()

    class Node:
        def request(self, req):
            li = getattr(req, "ledger_index", None)
            if li == "validated":
                return {"ledger": {"ledger_index": 106912512, "close_time": 842445300}}
            if li == fx["ledger_index"]:
                return led
            return {"ledger": {"ledger_index": li, "close_time": 842441340, "transactions": []}}
    assert S._lookup_enable_amendment(h, eta_iso="2026-09-11T11:25:00Z", timeout_s=3.0,
                                      client=Node()) == (106911489, "2026-09-11T11:29:00Z")

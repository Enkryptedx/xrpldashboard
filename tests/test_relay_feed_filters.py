"""Milestone 1 tests for Option B relay feed filters.

Verifies the Python ports in relay_feed_filters.py are byte-for-byte faithful
to the browser-side filters, using synthetic envelopes with the exact field
paths the templates read, plus a corpus check against real captured envelopes
(loaded from tests/fixtures/relay_envelopes.jsonl if present — a snapshot of
PG events.raw_json, tx_json shape).
"""
import json
import os

import pytest

import relay_feed_filters as F


AMM = "rAMMPoolAccount000000000000000000"
RLUSD_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
RLUSD_CUR = "524C555344000000000000000000000000000000"


def env(tx, meta=None, validated=True, type_="transaction"):
    return {
        "type": type_,
        "validated": validated,
        "meta": meta or {"TransactionResult": "tesSUCCESS", "AffectedNodes": []},
        "tx_json": tx,
    }


# ── gate ──

def test_gate_requires_validated_success():
    assert F.is_validated_success(env({"TransactionType": "Payment"}))
    assert not F.is_validated_success(env({}, validated=False))
    assert not F.is_validated_success(env({}, type_="ledgerClosed"))
    assert not F.is_validated_success(
        env({}, meta={"TransactionResult": "tecUNFUNDED", "AffectedNodes": []}))


# ── §3a amm ──

def test_amm_direct_account():
    assert F.matches_amm(env({"Account": AMM, "TransactionType": "AMMDeposit"}), {AMM})

def test_amm_direct_destination():
    assert F.matches_amm(env({"Account": "rX", "Destination": AMM,
                              "TransactionType": "Payment"}), {AMM})

def test_amm_via_affected_nodes_finalfields():
    e = env({"Account": "rX", "Destination": "rY", "TransactionType": "Payment"},
            meta={"TransactionResult": "tesSUCCESS", "AffectedNodes": [
                {"ModifiedNode": {"LedgerEntryType": "AccountRoot",
                                  "FinalFields": {"Account": AMM}}}]})
    assert F.matches_amm(e, {AMM})

def test_amm_no_match():
    e = env({"Account": "rX", "Destination": "rY", "TransactionType": "Payment"})
    assert not F.matches_amm(e, {AMM})


# ── §3b token top100 ──

def test_token_top100_deliver_max_object():
    e = env({"TransactionType": "Payment",
             "DeliverMax": {"currency": RLUSD_CUR, "issuer": RLUSD_ISSUER, "value": "5"}})
    assert F.matches_token_top100(e, {(RLUSD_CUR, RLUSD_ISSUER)})

def test_token_top100_legacy_amount_object():
    e = env({"TransactionType": "Payment",
             "Amount": {"currency": RLUSD_CUR, "issuer": RLUSD_ISSUER, "value": "5"}})
    assert F.matches_token_top100(e, {(RLUSD_CUR, RLUSD_ISSUER)})

def test_token_top100_xrp_excluded():
    e = env({"TransactionType": "Payment", "DeliverMax": "1000000"})
    assert not F.matches_token_top100(e, {(RLUSD_CUR, RLUSD_ISSUER)})

def test_token_top100_not_in_set():
    e = env({"TransactionType": "Payment",
             "DeliverMax": {"currency": "USD", "issuer": "rOther", "value": "5"}})
    assert not F.matches_token_top100(e, {(RLUSD_CUR, RLUSD_ISSUER)})


# ── §3c whale ──

def test_whale_above_threshold():
    e = env({"TransactionType": "Payment", "Amount": "100000000000"})  # 100k XRP
    assert F.matches_whale(e, 50_000_000_000)

def test_whale_below_threshold():
    e = env({"TransactionType": "Payment", "Amount": "10000000"})  # 10 XRP
    assert not F.matches_whale(e, 50_000_000_000)

def test_whale_token_payment_excluded():
    e = env({"TransactionType": "Payment",
             "Amount": {"currency": "USD", "issuer": "rX", "value": "1"}})
    assert not F.matches_whale(e, 1)

def test_parse_int_js_semantics():
    assert F._parse_int_js("1000000") == 1000000
    assert F._parse_int_js("123abc") == 123      # JS parseInt takes leading digits
    assert F._parse_int_js("abc") is None
    assert F._parse_int_js(None) is None
    assert F._parse_int_js("") is None


# ── §3d wallet ──

def test_wallet_account_match():
    assert F.matches_wallet(env({"Account": "rWATCH", "TransactionType": "Payment"}), {"rWATCH"})

def test_wallet_destination_match():
    assert F.matches_wallet(env({"Account": "rX", "Destination": "rWATCH",
                                 "TransactionType": "Payment"}), {"rWATCH"})

def test_wallet_affected_node_match():
    e = env({"Account": "rX", "TransactionType": "Payment"},
            meta={"TransactionResult": "tesSUCCESS", "AffectedNodes": [
                {"ModifiedNode": {"FinalFields": {"Account": "rWATCH"}}}]})
    assert F.matches_wallet(e, {"rWATCH"})

def test_wallet_empty_set_never_matches():
    assert not F.matches_wallet(env({"Account": "rWATCH"}), set())


# ── §11.1 address validation ──

def test_valid_classic_address():
    # RLUSD issuer is a real, checksum-valid classic address
    assert F.is_valid_classic_address(RLUSD_ISSUER)

def test_invalid_addresses_rejected():
    assert not F.is_valid_classic_address("notanaddress")
    assert not F.is_valid_classic_address("rShortie")
    assert not F.is_valid_classic_address(RLUSD_ISSUER[:-1] + "X")  # bad checksum
    assert not F.is_valid_classic_address(None)
    assert not F.is_valid_classic_address("")
    assert not F.is_valid_classic_address("XrWrongPrefix000000000000000000")


# ── classify router ──

def test_classify_multi_feed():
    # a whale-sized Payment into an AMM account → both amm + whale
    e = env({"Account": "rX", "Destination": AMM,
             "TransactionType": "Payment", "Amount": "100000000000"})
    feeds = F.classify(e, {"pool_accounts": {AMM}, "top100": set(),
                           "whale_tier_drops": 1_000_000_000})
    assert "amm_transactions" in feeds
    assert "whale_transactions" in feeds

def test_classify_gate_blocks_unvalidated():
    e = env({"Account": AMM, "TransactionType": "AMMDeposit"}, validated=False)
    assert F.classify(e, {"pool_accounts": {AMM}}) == []


# ── real-corpus check (no crash across captured envelopes) ──

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "relay_envelopes.jsonl")

@pytest.mark.skipif(not os.path.exists(FIXTURE), reason="no captured fixture")
def test_real_corpus_no_crash_and_shape():
    pools = set()
    top100 = set()
    n = matched = 0
    for line in open(FIXTURE):
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        n += 1
        # must never raise on real data
        feeds = F.classify(data, {"pool_accounts": pools, "top100": top100,
                                  "whale_tier_drops": 50_000_000_000})
        assert isinstance(feeds, list)
        # wallet filter must handle real envelopes too
        assert F.matches_wallet(data, {"rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"}) in (True, False)
        if feeds:
            matched += 1
    assert n > 0, "fixture was empty"

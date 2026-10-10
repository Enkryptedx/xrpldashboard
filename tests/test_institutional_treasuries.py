"""Public-companies-holding-XRP treasury card (branch
institutional-treasuries-2026-10-09).

Fakes only — the balance reader is injected, so nothing here touches our
node, a public XRPL server, or a DB. No production DATABASE_URL (owner
rule 2026-10-08).

The load-bearing rule under test: the 2026-10-09 Evernorth filings name a
CUSTODIAN but ZERO wallet addresses, so no row may ever be labelled
"confirmed by Evernorth filing". If a future filing publishes an address,
that promotion must be a deliberate code change with a test, never a
silent default.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import institutional_treasuries as T  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 20, 0, 0, tzinfo=dt.timezone.utc)


def _balances(**over):
    b = {addr: 1_000_000 for addr, _ in T.WALLETS}   # 1 XRP each
    for k, v in over.items():
        b[k] = v
    return b


# ── source tiers ──

def test_tier_labels_are_the_approved_strings():
    assert T.TIER_LABELS[T.TIER_FILING] == "confirmed by Evernorth filing"
    assert T.TIER_LABELS[T.TIER_ATTRIBUTED] == (
        "attributed by XRPScan / xrp-insights \u2014 not confirmed by Evernorth")


def test_every_wallet_row_is_attributed_not_filing_confirmed():
    """The 2026-10-09 filings contain no addresses, so nothing is confirmed."""
    rows = T.build_rows(_balances())
    assert len(rows) == 13
    assert {r["tier"] for r in rows} == {T.TIER_ATTRIBUTED}
    for r in rows:
        assert r["tier_label"] == T.TIER_LABELS[T.TIER_ATTRIBUTED]
        assert "not confirmed by Evernorth" in r["tier_label"]


def test_no_row_claims_filing_confirmation_anywhere():
    rows = T.build_rows(_balances())
    assert not any("confirmed by Evernorth filing" == r["tier_label"]
                   for r in rows)


# ── balances: never fabricate ──

def test_unreadable_balance_stays_none_not_zero():
    addr = T.WALLETS[0][0]
    rows = T.build_rows(_balances(**{addr: None}))
    row = next(r for r in rows if r["address"] == addr)
    assert row["balance_xrp"] is None


def test_total_excludes_unreadable_rows():
    addr = T.WALLETS[0][0]
    rows = T.build_rows(_balances(**{addr: None}))
    total, readable = T.total_xrp(rows)
    assert readable == 12
    assert total == 12.0          # 12 readable x 1 XRP, missing one not zeroed


def test_total_is_none_when_nothing_readable():
    rows = T.build_rows({a: None for a, _ in T.WALLETS})
    total, readable = T.total_xrp(rows)
    assert total is None and readable == 0


def test_drops_to_xrp_conversion():
    addr = T.WALLETS[0][0]
    rows = T.build_rows(_balances(**{addr: 45_620_881_800_000}))
    row = next(r for r in rows if r["address"] == addr)
    assert row["balance_xrp"] == 45_620_881.8


def test_garbage_balance_is_treated_as_unreadable():
    addr = T.WALLETS[0][0]
    rows = T.build_rows(_balances(**{addr: "not-a-number"}))
    assert next(r for r in rows if r["address"] == addr)["balance_xrp"] is None


# ── filing comparison ──

def test_filing_minimum_matches_the_424b3_figure():
    assert T.FILING_MIN_XRP == 473_276_430


def test_filing_delta_positive_is_consistent_with_at_least():
    assert T.filing_delta(473_276_565.78) == 135.78
    assert T.filing_delta(None) is None


def test_every_filing_fact_carries_a_citation_url():
    for f in T.EVERNORTH["facts"]:
        assert f["url"].startswith("https://www.sec.gov/Archives/edgar/")
        assert f["source"] in {"8-K", "424B3"}
        assert f["label"] and f["value"]


def test_custodian_fact_states_no_address_is_published():
    c = next(f for f in T.EVERNORTH["facts"] if f["label"] == "Custodian")
    assert "BitGo" in c["value"]
    assert "do not publish any wallet address" in c["detail"]


def test_facts_contain_no_xrpl_address():
    """A filing fact must never carry an address, since none were filed."""
    import json
    import re
    blob = json.dumps(T.EVERNORTH)
    assert not re.search(r"\br[1-9A-HJ-NP-Za-km-z]{24,34}\b", blob)


# ── cache: light node load ──

def test_snapshot_ttl_is_fifteen_minutes():
    assert T.SNAPSHOT_TTL == 900


def test_reader_is_called_once_within_the_ttl_window():
    T.reset_cache()
    calls = []

    def reader(addrs):
        calls.append(tuple(addrs))
        return _balances(), {}

    a = T.fetch_treasury_snapshot(reader=reader, now=NOW)
    b = T.fetch_treasury_snapshot(reader=reader,
                                  now=NOW + dt.timedelta(seconds=899))
    assert len(calls) == 1, "second render must hit cache, not the node"
    assert a is b


def test_reader_is_called_again_after_the_ttl_expires():
    T.reset_cache()
    calls = []

    def reader(addrs):
        calls.append(1)
        return _balances(), {}

    T.fetch_treasury_snapshot(reader=reader, now=NOW)
    T.fetch_treasury_snapshot(reader=reader,
                              now=NOW + dt.timedelta(seconds=901))
    assert len(calls) == 2


def test_reader_failure_degrades_and_never_raises():
    T.reset_cache()

    def boom(addrs):
        raise RuntimeError("node down")

    snap = T.fetch_treasury_snapshot(reader=boom, now=NOW)
    assert snap["total_xrp"] is None
    assert snap["readable_count"] == 0
    assert len(snap["rows"]) == 13
    # filing facts still render when the node is unreachable
    assert len(snap["facts"]) == len(T.EVERNORTH["facts"])


def test_snapshot_without_reader_still_returns_facts():
    T.reset_cache()
    snap = T.fetch_treasury_snapshot(reader=None, now=NOW)
    assert snap["wallet_count"] == 13
    assert snap["total_xrp"] is None
    assert snap["facts"]


def test_snapshot_shape_with_real_world_numbers():
    T.reset_cache()
    live = {
        "rsT3yYMkuicxW1hYsy787mg5XHhkz2uQRk": 45_620_881_800_000,
        "rKXXrAgpkHQN8m4HxAQCYmDCPPUByc9mVq": 42_263_815_200_000,
    }
    bal = {a: live.get(a) for a, _ in T.WALLETS}

    snap = T.fetch_treasury_snapshot(reader=lambda a: (bal, {}), now=NOW)
    assert snap["readable_count"] == 2
    assert snap["total_xrp"] == 87_884_697.0
    assert snap["filing_min_xrp"] == 473_276_430
    assert snap["fetched_at_iso"] == "2026-10-09T20:00:00Z"

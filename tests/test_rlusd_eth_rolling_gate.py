"""Boundary gate for the Ethereum RLUSD rolling-24h mints/burns figures.

Companion to tests/test_rlusd_rolling_gate.py.

A dated-band gate once lived here too: the implied start supply had to sit
inside the band of the last 3 dated eth_supply rows, widened 2%. It was
REMOVED on 2026-10-06 alongside its XRPL twin, which had rejected a correct
+$57,881,457.58 on the day a real +62,000,000 XRPL mint landed mid-afternoon.

The reasoning carries over unchanged to this chain: a rolling window STARTS
MID-DAY while the dated rows are end-of-day snapshots, so any large legitimate
mid-day mint or burn pushes the true start supply outside that band. The check
therefore fires hardest exactly on the days the figure matters most.

What remains is the block-timestamp gate. Hermetic: the Etherscan surface is
stubbed. No network, no DB.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rlusd_etherscan as e  # noqa: E402

ETH_SUPPLY_NOW = 1_280_769_529.22

# A real observed shape: a large mid-day burn with small mints.
BIG_BURN_MINTS = 280_000.00
BIG_BURN_BURNS = 48_860_000.00

NOW = 1_760_000_000
START_BASE = 21_000_000
END_BLOCK = 21_007_200          # ~24h of 12s blocks later


def _stub(monkeypatch, *, timestamps, mints, burns,
          start_base=START_BASE, end_block=END_BLOCK):
    """Point the module's Etherscan surface at canned values."""
    def fake_block_at(ts):
        return start_base if ts < NOW - 1 else end_block
    monkeypatch.setattr(e, "block_number_at_or_before", fake_block_at)
    monkeypatch.setattr(e, "block_timestamp", lambda b: timestamps.get(b))
    monkeypatch.setattr(e, "_fetch_transfers", lambda sb, eb: [])
    monkeypatch.setattr(e, "_sum_mints_burns", lambda txs: (mints, burns))


def _healthy_timestamps():
    # start_block is the block AFTER the boundary (exclusive start)
    return {START_BASE + 1: NOW - 86_400 + 8, END_BLOCK: NOW - 5}


# --------------------------------------------------------- the regression

def test_large_midday_burn_is_published_not_withheld(monkeypatch):
    """THE regression: a big legitimate mid-day burn must reach the page.

    net here is -48,580,000 — the old dated-band gate would have rejected it
    because the implied start sits far outside a band of end-of-day rows.
    """
    _stub(monkeypatch, timestamps=_healthy_timestamps(),
          mints=BIG_BURN_MINTS, burns=BIG_BURN_BURNS)

    out = e.aggregate_rolling_24h(NOW)
    assert out is not None, (
        "a large legitimate mid-day burn must be PUBLISHED, not withheld")
    mints, burns = out
    assert mints == pytest.approx(BIG_BURN_MINTS, abs=0.01)
    assert burns == pytest.approx(BIG_BURN_BURNS, abs=0.01)
    assert (mints - burns) == pytest.approx(-48_580_000.00, abs=0.01)


def test_large_midday_mint_is_published_not_withheld(monkeypatch):
    """Symmetric case: a big mint must also survive."""
    _stub(monkeypatch, timestamps=_healthy_timestamps(),
          mints=62_000_000.00, burns=0.0)
    out = e.aggregate_rolling_24h(NOW)
    assert out is not None
    assert (out[0] - out[1]) == pytest.approx(62_000_000.00, abs=0.01)


def test_no_dated_band_check_remains():
    """The band gate and its margin must be gone, not merely bypassed."""
    assert not hasattr(e, "ROLLING_IMPLIED_SUPPLY_MARGIN")
    import inspect
    params = inspect.signature(e.aggregate_rolling_24h).parameters
    assert "recent_supplies" not in params
    assert "supply_now" not in params
    src = inspect.getsource(e.aggregate_rolling_24h)
    assert "band" not in src.lower()


# ------------------------------------------------- the gate that remains

def test_bad_start_block_weeks_back_yields_no_value(monkeypatch, capsys):
    weeks_off = NOW - 86_400 - 3_000_000
    _stub(monkeypatch,
          timestamps={START_BASE + 1: weeks_off, END_BLOCK: NOW - 5},
          mints=BIG_BURN_MINTS, burns=BIG_BURN_BURNS)
    assert e.aggregate_rolling_24h(NOW) is None
    err = capsys.readouterr().err
    assert "withheld" in err and "off target" in err


def test_normal_case_still_returns_mints_and_burns(monkeypatch):
    _stub(monkeypatch, timestamps=_healthy_timestamps(),
          mints=5_993_285.42, burns=1_000_000.00)
    out = e.aggregate_rolling_24h(NOW)
    assert out is not None
    assert out[0] == pytest.approx(5_993_285.42, abs=0.01)
    assert out[1] == pytest.approx(1_000_000.00, abs=0.01)


def test_unreadable_block_timestamp_withholds(monkeypatch):
    _stub(monkeypatch,
          timestamps={START_BASE + 1: None, END_BLOCK: NOW - 5},
          mints=0.0, burns=0.0)
    assert e.aggregate_rolling_24h(NOW) is None


def test_end_block_far_from_now_withholds(monkeypatch):
    _stub(monkeypatch,
          timestamps={START_BASE + 1: NOW - 86_400 + 8, END_BLOCK: NOW - 50_000},
          mints=0.0, burns=0.0)
    assert e.aggregate_rolling_24h(NOW) is None


def test_blocks_out_of_order_withholds(monkeypatch):
    monkeypatch.setattr(
        e, "block_number_at_or_before",
        lambda ts: END_BLOCK if ts < NOW - 1 else START_BASE)
    monkeypatch.setattr(e, "block_timestamp", lambda b: NOW)
    monkeypatch.setattr(e, "_fetch_transfers", lambda sb, eb: [])
    monkeypatch.setattr(e, "_sum_mints_burns", lambda txs: (0.0, 0.0))
    assert e.aggregate_rolling_24h(NOW) is None


def test_tolerance_matches_the_xrpl_side():
    """Two numbers for the same concept on two chains is how drift starts."""
    import rlusd_xrpl_option_a as x
    assert e.ROLLING_CLOSE_TOLERANCE_S == x.ROLLING_CLOSE_TOLERANCE_S == 900
    # and neither chain carries a band margin any more
    assert not hasattr(e, "ROLLING_IMPLIED_SUPPLY_MARGIN")
    assert not hasattr(x, "ROLLING_IMPLIED_SUPPLY_MARGIN")


def test_calendar_day_path_is_deliberately_ungated():
    """History rows resolve from a fixed date, not a sliding `now`."""
    import inspect
    sig = inspect.signature(e.aggregate_calendar_day)
    assert "recent_supplies" not in sig.parameters
    assert sig.return_annotation == "tuple[float, float]"


# ---------------------------------------------------------------- rendering

TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "rlusd.html")


def test_eth_net_requires_both_mints_and_burns():
    """A half-present ETH pair must null the ETH leg, not half-count it."""
    with open(TEMPLATE, encoding="utf-8") as f:
        src = f.read()
    mm = re.search(r"\{%\s*set\s+eth_net\s*=\s*(.+?)%\}", src, re.S)
    assert mm, "eth_net assignment not found in templates/rlusd.html"
    expr = " ".join(mm.group(1).split())
    assert "eth_mints is not none" in expr and "eth_burns is not none" in expr
    assert "else None" in expr

"""Rolling-24h verification gate for the Ethereum RLUSD mints/burns figures.

Companion to tests/test_rlusd_rolling_gate.py. The XRPL incident (2026-10-06)
was a boundary resolver silently returning a ledger weeks off target, so
gateway_balances read a real-but-ancient supply and /rlusd published +$61.6M.
rlusd_etherscan had the SAME unguarded shape: block_number_at_or_before names
a boundary block with no check that it sits near the requested instant, and
nothing cross-checked the resulting mints/burns against the dated eth_supply
rows. These tests lock the two gates that now withhold such a figure.

Hermetic: the Etherscan surface is stubbed. No network, no DB.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rlusd_etherscan as e  # noqa: E402

# Real figures from the 2026-10-06 live read.
ETH_SUPPLY_NOW = 1_280_769_529.2214172
ETH_PREV = 1_274_776_243.80142
ETH_OLDER = 1_276_394_360.80142
REAL_BAND = [ETH_SUPPLY_NOW, ETH_PREV, ETH_OLDER]

GOOD_MINTS = 5_993_285.42
GOOD_BURNS = 1_000_000.00

NOW = 1_760_000_000
START_BLOCK_BASE = 21_000_000
END_BLOCK = 21_007_200          # ~24h of 12s blocks later


def _stub(monkeypatch, *, timestamps, mints=GOOD_MINTS, burns=GOOD_BURNS,
          start_base=START_BLOCK_BASE, end_block=END_BLOCK):
    """Point the module's Etherscan surface at canned values."""
    def fake_block_at(ts):
        return start_base if ts < NOW - 1 else end_block
    monkeypatch.setattr(e, "block_number_at_or_before", fake_block_at)
    monkeypatch.setattr(e, "block_timestamp", lambda b: timestamps.get(b))
    monkeypatch.setattr(e, "_fetch_transfers", lambda sb, eb: [])
    monkeypatch.setattr(e, "_sum_mints_burns", lambda txs: (mints, burns))


def test_bad_start_block_weeks_back_yields_no_value(monkeypatch, capsys):
    """Gate 1: a start block from weeks back must withhold the figure."""
    weeks_off = NOW - 86_400 - 3_000_000
    _stub(monkeypatch, timestamps={
        START_BLOCK_BASE + 1: weeks_off,
        END_BLOCK: NOW - 5,
    })
    out = e.aggregate_rolling_24h(
        NOW, recent_supplies=REAL_BAND, supply_now=ETH_SUPPLY_NOW)
    assert out is None, f"expected None for a weeks-old start block, got {out}"
    err = capsys.readouterr().err
    assert "withheld" in err and "off target" in err, (
        f"suppression must leave a stderr trace; got {err!r}")


def test_normal_case_still_returns_mints_and_burns(monkeypatch):
    """A well-formed window must still publish, unchanged."""
    _stub(monkeypatch, timestamps={
        START_BLOCK_BASE + 1: NOW - 86_400 + 8,   # exclusive start, just after
        END_BLOCK: NOW - 5,
    })
    out = e.aggregate_rolling_24h(
        NOW, recent_supplies=REAL_BAND, supply_now=ETH_SUPPLY_NOW)
    assert out is not None, "a healthy window must not be withheld"
    mints, burns = out
    assert mints == pytest.approx(GOOD_MINTS, abs=0.01)
    assert burns == pytest.approx(GOOD_BURNS, abs=0.01)


def test_implausible_net_is_rejected(monkeypatch, capsys):
    """Gate 2: a net that implies an off-band start supply is withheld.

    Timestamps are FINE here, so this proves Gate 2 catches what Gate 1
    cannot — exactly the shape of the XRPL incident.
    """
    _stub(monkeypatch, timestamps={
        START_BLOCK_BASE + 1: NOW - 86_400 + 8,
        END_BLOCK: NOW - 5,
    }, mints=60_000_000.0, burns=0.0)
    out = e.aggregate_rolling_24h(
        NOW, recent_supplies=REAL_BAND, supply_now=ETH_SUPPLY_NOW)
    assert out is None, f"a +$60M net must be withheld, got {out}"
    assert "outside recent dated band" in capsys.readouterr().err


def test_missing_dated_rows_withholds_rather_than_guesses(monkeypatch, capsys):
    _stub(monkeypatch, timestamps={
        START_BLOCK_BASE + 1: NOW - 86_400 + 8,
        END_BLOCK: NOW - 5,
    })
    for band in (None, []):
        out = e.aggregate_rolling_24h(
            NOW, recent_supplies=band, supply_now=ETH_SUPPLY_NOW)
        assert out is None, f"band={band!r} must withhold, got {out}"
    assert "no recent dated eth_supply rows" in capsys.readouterr().err


def test_unreadable_block_timestamp_withholds(monkeypatch):
    _stub(monkeypatch, timestamps={
        START_BLOCK_BASE + 1: None,
        END_BLOCK: NOW - 5,
    })
    assert e.aggregate_rolling_24h(
        NOW, recent_supplies=REAL_BAND, supply_now=ETH_SUPPLY_NOW) is None


def test_end_block_far_from_now_withholds(monkeypatch):
    """A stale end block is as disqualifying as a stale start block."""
    _stub(monkeypatch, timestamps={
        START_BLOCK_BASE + 1: NOW - 86_400 + 8,
        END_BLOCK: NOW - 50_000,
    })
    assert e.aggregate_rolling_24h(
        NOW, recent_supplies=REAL_BAND, supply_now=ETH_SUPPLY_NOW) is None


def test_blocks_out_of_order_withholds(monkeypatch):
    monkeypatch.setattr(
        e, "block_number_at_or_before",
        lambda ts: END_BLOCK if ts < NOW - 1 else START_BLOCK_BASE)
    monkeypatch.setattr(e, "block_timestamp", lambda b: NOW)
    monkeypatch.setattr(e, "_fetch_transfers", lambda sb, eb: [])
    monkeypatch.setattr(e, "_sum_mints_burns", lambda txs: (0.0, 0.0))
    assert e.aggregate_rolling_24h(
        NOW, recent_supplies=REAL_BAND, supply_now=ETH_SUPPLY_NOW) is None


def test_unknown_current_supply_withholds(monkeypatch, capsys):
    """Without a current supply there is no implied start to check."""
    _stub(monkeypatch, timestamps={
        START_BLOCK_BASE + 1: NOW - 86_400 + 8,
        END_BLOCK: NOW - 5,
    })
    monkeypatch.setattr(
        e, "current_supply",
        lambda: (_ for _ in ()).throw(RuntimeError("tokensupply down")))
    out = e.aggregate_rolling_24h(NOW, recent_supplies=REAL_BAND)
    assert out is None
    assert "current ETH supply unknown" in capsys.readouterr().err


def test_tolerances_match_the_xrpl_side():
    """Two numbers for the same concept on two chains is how drift starts."""
    import rlusd_xrpl_option_a as x
    assert e.ROLLING_CLOSE_TOLERANCE_S == x.ROLLING_CLOSE_TOLERANCE_S == 900
    assert (e.ROLLING_IMPLIED_SUPPLY_MARGIN
            == x.ROLLING_IMPLIED_SUPPLY_MARGIN == 0.02)


def test_calendar_day_path_is_deliberately_ungated():
    """History rows resolve from a fixed date, not a sliding `now`.

    Locked so a future reader doesn't 'helpfully' gate it and blank the
    dated history, which is the reconciliation baseline the gates rely on.
    """
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
    assert "eth_mints is not none" in expr and "eth_burns is not none" in expr, (
        f"ETH leg must guard on BOTH components; got: {expr}")
    assert "else None" in expr, f"ETH leg must fall to None, not 0; got: {expr}"

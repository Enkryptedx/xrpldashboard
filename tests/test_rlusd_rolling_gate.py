"""Rolling-24h verification gate for the XRPL RLUSD net-supply figure.

Incident (2026-10-06): /rlusd published a two-chain "net supply change · 24h"
of +$66.59M, of which +$61,601,457.58 came from aggregate_rolling_24h. Supply
now (1,219,375,211.58) minus that net implies a start-side reading of
1,157,773,754 — absent from the recent dated history (Oct 2-6 all sit in
1.219-1.227B). The dated calendar row for the same period says -1,418,542.42,
which reconciles exactly. So the rolling figure was wrong, not merely
differently-windowed.

These tests lock the two gates that now withhold such a figure. Hermetic: the
RPC surface is stubbed, no network, no DB.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rlusd_xrpl_option_a as m  # noqa: E402

# The real numbers from the incident.
SUPPLY_NOW = 1_219_375_211.58
SUPPLY_PREV = 1_220_793_754.00
SUPPLY_OLDER = 1_227_175_754.00
BAD_IMPLIED_START = 1_157_773_754.00   # what +61.6M implies
REAL_BAND = [SUPPLY_NOW, SUPPLY_PREV, SUPPLY_OLDER]

NOW = 1_760_000_000


def _stub(monkeypatch, *, start_ledger, end_ledger, closes, obligations):
    """Point the module's RPC surface at canned values."""
    def fake_boundary(target):
        return start_ledger if target < NOW - 1 else end_ledger
    monkeypatch.setattr(m, "find_boundary_ledger", fake_boundary)
    monkeypatch.setattr(m, "_ledger_close_time", lambda idx: closes.get(idx))
    monkeypatch.setattr(m, "_obligations_at", lambda idx: obligations[idx])


def test_bad_start_ledger_weeks_back_yields_no_value(monkeypatch, capsys):
    """Gate 1: a start ledger from weeks back must withhold the figure."""
    start, end = 90_000_000, 99_999_999
    weeks_off = NOW - 86_400 - 3_000_000          # ~5 weeks before target
    _stub(monkeypatch, start_ledger=start, end_ledger=end,
          closes={start: weeks_off, end: NOW - 2},
          obligations={start: BAD_IMPLIED_START, end: SUPPLY_NOW})

    out = m.aggregate_rolling_24h(NOW, recent_supplies=REAL_BAND)
    assert out is None, f"expected None for a weeks-old start ledger, got {out}"
    err = capsys.readouterr().err
    assert "withheld" in err and "off target" in err, (
        f"suppression must leave a trace on stderr; got: {err!r}")


def test_normal_case_still_returns_the_net(monkeypatch):
    """A well-formed window must still publish, unchanged."""
    start, end = 99_000_000, 99_022_000
    _stub(monkeypatch, start_ledger=start, end_ledger=end,
          closes={start: NOW - 86_400 - 2, end: NOW - 1},
          obligations={start: SUPPLY_PREV, end: SUPPLY_NOW})

    out = m.aggregate_rolling_24h(NOW, recent_supplies=REAL_BAND)
    assert out == pytest.approx(SUPPLY_NOW - SUPPLY_PREV, abs=0.01)
    assert out == pytest.approx(-1_418_542.42, abs=0.01), (
        "should match the dated calendar row for the same period")


def test_the_real_incident_value_is_rejected(monkeypatch, capsys):
    """Gate 2: +61.6M implies a start supply outside the dated band."""
    start, end = 99_000_000, 99_022_000
    # Close times are FINE here — only the supply reading is implausible,
    # so this proves Gate 2 catches what Gate 1 cannot.
    _stub(monkeypatch, start_ledger=start, end_ledger=end,
          closes={start: NOW - 86_400 - 2, end: NOW - 1},
          obligations={start: BAD_IMPLIED_START, end: SUPPLY_NOW})

    out = m.aggregate_rolling_24h(NOW, recent_supplies=REAL_BAND)
    assert out is None, (
        f"+{SUPPLY_NOW - BAD_IMPLIED_START:,.2f} must be withheld, got {out}")
    err = capsys.readouterr().err
    assert "outside recent dated band" in err


def test_two_percent_margin_rejects_what_five_percent_would_admit():
    """The margin choice is load-bearing, so lock it.

    1,219,375,211.58 x 0.95 = 1,158,406,450 — the bad implied start
    (1,157,773,754) sits only ~$0.6M below a 5% floor, so 5% would have let
    this incident through. 2% rejects it by ~$37M.
    """
    lo_2pct = min(REAL_BAND) * (1 - 0.02)
    lo_5pct = min(REAL_BAND) * (1 - 0.05)
    assert BAD_IMPLIED_START < lo_2pct, "2% must reject the incident value"
    assert BAD_IMPLIED_START > lo_5pct - 1_000_000, (
        "5% would have been within ~$1M of admitting it — keep the margin tight")
    assert m.ROLLING_IMPLIED_SUPPLY_MARGIN == 0.02


def test_missing_dated_rows_withholds_rather_than_guesses(monkeypatch, capsys):
    """No band to verify against => no published figure. Never guess."""
    start, end = 99_000_000, 99_022_000
    _stub(monkeypatch, start_ledger=start, end_ledger=end,
          closes={start: NOW - 86_400 - 2, end: NOW - 1},
          obligations={start: SUPPLY_PREV, end: SUPPLY_NOW})

    for band in (None, []):
        out = m.aggregate_rolling_24h(NOW, recent_supplies=band)
        assert out is None, f"band={band!r} must withhold, got {out}"
    assert "no recent dated supply rows" in capsys.readouterr().err


def test_unreadable_close_time_withholds(monkeypatch):
    start, end = 99_000_000, 99_022_000
    _stub(monkeypatch, start_ledger=start, end_ledger=end,
          closes={start: None, end: NOW - 1},
          obligations={start: SUPPLY_PREV, end: SUPPLY_NOW})
    assert m.aggregate_rolling_24h(NOW, recent_supplies=REAL_BAND) is None


def test_ledgers_out_of_order_withholds(monkeypatch):
    monkeypatch.setattr(m, "find_boundary_ledger",
                        lambda t: 99_999_999 if t < NOW - 1 else 99_000_000)
    monkeypatch.setattr(m, "_ledger_close_time", lambda idx: NOW)
    monkeypatch.setattr(m, "_obligations_at", lambda idx: SUPPLY_NOW)
    assert m.aggregate_rolling_24h(NOW, recent_supplies=REAL_BAND) is None


def test_end_ledger_far_from_now_withholds(monkeypatch):
    """A stale end ledger is as disqualifying as a stale start ledger."""
    start, end = 99_000_000, 99_022_000
    _stub(monkeypatch, start_ledger=start, end_ledger=end,
          closes={start: NOW - 86_400 - 2, end: NOW - 50_000},
          obligations={start: SUPPLY_PREV, end: SUPPLY_NOW})
    assert m.aggregate_rolling_24h(NOW, recent_supplies=REAL_BAND) is None


# ---------------------------------------------------------------- rendering

TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "rlusd.html")


def _template():
    with open(TEMPLATE, encoding="utf-8") as f:
        return f.read()


def test_total_requires_both_legs_never_a_partial_sum():
    """SSR: the two-chain total must be None unless BOTH legs are present."""
    src = _template()
    mm = re.search(r"\{%\s*set\s+net_24h\s*=\s*(.+?)%\}", src, re.S)
    assert mm, "net_24h assignment not found in templates/rlusd.html"
    expr = " ".join(mm.group(1).split())
    assert "eth_net is not none" in expr and "xrpl_net is not none" in expr, (
        f"total must guard on BOTH legs; got: {expr}")
    assert "else None" in expr, f"total must fall to None, not 0; got: {expr}"


def test_js_total_is_null_safe():
    """Live poller: the total must use the null-propagating helper."""
    src = _template()
    assert re.search(r"const\s+net\s*=\s*sumOrNull\(", src), (
        "JS total must be computed with sumOrNull so one missing leg nulls it")
    mm = re.search(r"function sumOrNull\(a, b\) \{(.+?)\}", src, re.S)
    assert mm and "return null" in mm.group(1), (
        "sumOrNull must return null when either side is null")


def test_missing_values_render_as_dash():
    """fmt_usd/fmt_signed must render an em dash for None, never $0."""
    src = _template()
    for macro in ("fmt_usd", "fmt_signed"):
        mm = re.search(
            r"\{%-?\s*macro\s+" + macro + r"\(.*?\)\s*-?%\}(.+?)\{%-?\s*endmacro",
            src, re.S)
        assert mm, f"{macro} macro not found"
        body = mm.group(1)
        assert "is none" in body and "—" in body, (
            f"{macro} must render an em dash for a missing value; got: {body[:160]}")

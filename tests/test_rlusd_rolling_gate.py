"""Boundary gate for the XRPL RLUSD rolling-24h net-supply figure.

HISTORY, because it matters for reading these tests:

A second gate once lived here. It required the implied start supply to sit
inside the band of the last 3 dated rlusd_supply_history rows, widened 2%.
It was REMOVED on 2026-10-06 because it rejected a CORRECT figure.

Measured directly against s2.ripple.com, the endpoint production uses:

    find_boundary_ledger(now-86400)       -> drift  -4s  (13 probes, 0 fails)
    find_boundary_ledger(15:03 UTC Oct 5) -> drift  +0s
    supply @ 107451884 (15:03 UTC Oct 5)  = 1,158,793,754.00
    supply @ 107460174 (23:58 UTC Oct 5)  = 1,220,793,754.00  (== dated row)
    supply now         (15:03 UTC Oct 6)  = 1,216,675,211.58

RLUSD genuinely minted +62,000,000 on XRPL during the afternoon of Oct 5. A
rolling window STARTS MID-DAY; the dated rows are end-of-day snapshots taken
after the mint landed. So a true intraday start legitimately sits far below
that band, and +57,881,457.58 was correct to the cent.

What remains is the close-time gate, which verifies the boundary ledgers
actually resolved near their targets. Hermetic: RPC stubbed, no network, no DB.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rlusd_xrpl_option_a as m  # noqa: E402

# Real measured figures from 2026-10-06.
SUPPLY_NOW = 1_216_675_211.58
SUPPLY_START_REAL = 1_158_793_754.00      # actual on-ledger value 24h earlier
TRUE_NET = 57_881_457.58                  # the figure the old gate suppressed

NOW = 1_760_000_000


def _stub(monkeypatch, *, start_ledger, end_ledger, closes, obligations):
    """Point the module's RPC surface at canned values."""
    def fake_boundary(target):
        return start_ledger if target < NOW - 1 else end_ledger
    monkeypatch.setattr(m, "find_boundary_ledger", fake_boundary)
    monkeypatch.setattr(m, "_ledger_close_time", lambda idx: closes.get(idx))
    monkeypatch.setattr(m, "_obligations_at", lambda idx: obligations[idx])


# --------------------------------------------------------- the regression

def test_real_mint_day_figure_is_published_not_withheld(monkeypatch):
    """THE regression. 2026-10-06, the day a real +62M mint landed mid-day.

    The removed dated-band gate withheld this. It must now be published.
    """
    start, end = 107_451_884, 107_475_997
    _stub(monkeypatch, start_ledger=start, end_ledger=end,
          closes={start: NOW - 86_400, end: NOW - 2},
          obligations={start: SUPPLY_START_REAL, end: SUPPLY_NOW})

    out = m.aggregate_rolling_24h(NOW)
    assert out is not None, (
        "a true +$57.88M on a real mint day must be PUBLISHED, not withheld")
    assert out == pytest.approx(TRUE_NET, abs=0.01)


def test_no_dated_band_check_remains():
    """The band gate and its margin must be gone, not merely bypassed."""
    assert not hasattr(m, "ROLLING_IMPLIED_SUPPLY_MARGIN"), (
        "the dated-band margin constant must be removed")
    import inspect
    params = inspect.signature(m.aggregate_rolling_24h).parameters
    assert "recent_supplies" not in params, (
        "the dated-rows parameter must be gone from the signature")
    src = inspect.getsource(m.aggregate_rolling_24h)
    assert "band" not in src.lower(), "no band logic may remain in the function"


# ------------------------------------------------- the gate that remains

def test_bad_start_ledger_weeks_back_yields_no_value(monkeypatch, capsys):
    """A start ledger whose close time is weeks off target is withheld."""
    start, end = 90_000_000, 99_999_999
    weeks_off = NOW - 86_400 - 3_000_000
    _stub(monkeypatch, start_ledger=start, end_ledger=end,
          closes={start: weeks_off, end: NOW - 2},
          obligations={start: SUPPLY_START_REAL, end: SUPPLY_NOW})

    assert m.aggregate_rolling_24h(NOW) is None
    err = capsys.readouterr().err
    assert "withheld" in err and "off target" in err


def test_normal_case_still_returns_the_net(monkeypatch):
    start, end = 99_000_000, 99_022_000
    _stub(monkeypatch, start_ledger=start, end_ledger=end,
          closes={start: NOW - 86_400 - 2, end: NOW - 1},
          obligations={start: 1_220_793_754.00, end: SUPPLY_NOW})
    out = m.aggregate_rolling_24h(NOW)
    assert out == pytest.approx(SUPPLY_NOW - 1_220_793_754.00, abs=0.01)


def test_unreadable_close_time_withholds(monkeypatch):
    start, end = 99_000_000, 99_022_000
    _stub(monkeypatch, start_ledger=start, end_ledger=end,
          closes={start: None, end: NOW - 1},
          obligations={start: SUPPLY_START_REAL, end: SUPPLY_NOW})
    assert m.aggregate_rolling_24h(NOW) is None


def test_ledgers_out_of_order_withholds(monkeypatch):
    monkeypatch.setattr(m, "find_boundary_ledger",
                        lambda t: 99_999_999 if t < NOW - 1 else 99_000_000)
    monkeypatch.setattr(m, "_ledger_close_time", lambda idx: NOW)
    monkeypatch.setattr(m, "_obligations_at", lambda idx: SUPPLY_NOW)
    assert m.aggregate_rolling_24h(NOW) is None


def test_end_ledger_far_from_now_withholds(monkeypatch):
    start, end = 99_000_000, 99_022_000
    _stub(monkeypatch, start_ledger=start, end_ledger=end,
          closes={start: NOW - 86_400 - 2, end: NOW - 50_000},
          obligations={start: SUPPLY_START_REAL, end: SUPPLY_NOW})
    assert m.aggregate_rolling_24h(NOW) is None


def test_tolerance_is_a_loose_outer_bound():
    """900s is ~230 ledger intervals; measured drift in practice was 0-4s."""
    assert m.ROLLING_CLOSE_TOLERANCE_S == 900


# ---------------------------------------------------------------- rendering

TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "rlusd.html")


def _template():
    with open(TEMPLATE, encoding="utf-8") as f:
        return f.read()


def test_total_requires_both_legs_never_a_partial_sum():
    src = _template()
    mm = re.search(r"\{%\s*set\s+net_24h\s*=\s*(.+?)%\}", src, re.S)
    assert mm, "net_24h assignment not found in templates/rlusd.html"
    expr = " ".join(mm.group(1).split())
    assert "eth_net is not none" in expr and "xrpl_net is not none" in expr
    assert "else None" in expr


def test_js_total_is_null_safe():
    src = _template()
    assert re.search(r"const\s+net\s*=\s*sumOrNull\(", src)
    mm = re.search(r"function sumOrNull\(a, b\) \{(.+?)\}", src, re.S)
    assert mm and "return null" in mm.group(1)


def test_missing_values_render_as_dash():
    src = _template()
    for macro in ("fmt_usd", "fmt_signed"):
        mm = re.search(
            r"\{%-?\s*macro\s+" + macro + r"\(.*?\)\s*-?%\}(.+?)\{%-?\s*endmacro",
            src, re.S)
        assert mm, f"{macro} macro not found"
        body = mm.group(1)
        assert "is none" in body and "—" in body

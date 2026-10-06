"""RLUSD figures must be labelled by chain and by window.

Two true numbers looked contradictory because neither said what it measured:

  * homepage "What's new" — XRPL issuer obligations only, diffed between two
    signed leaves taken at 21:00 ET, i.e. 9:00 PM ET Oct 4 -> 9:00 PM ET Oct 5.
  * /rlusd "net supply change · 24h" — both chains, rolling trailing 24h
    ending at the moment of the read.

A leaf's own date is its WRITE date: it is taken at 01:00 UTC, which is
21:00 ET the previous evening, so the date sits one calendar day ahead of the
evening whose value it carries. Naming leaf dates in the detail line would
therefore state the wrong days; the instants are named instead.

Hermetic: no DB, no network. Reads the template from disk.
"""

import datetime as dt
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import changes_builder as cb  # noqa: E402

# The real leaf instants behind the -$6.38M homepage item.
LEAF_PREV = int(dt.datetime(2026, 10, 5, 1, 0, tzinfo=dt.timezone.utc).timestamp())
LEAF_CUR = int(dt.datetime(2026, 10, 6, 1, 0, tzinfo=dt.timezone.utc).timestamp())
SUPPLY_PREV = 1_227_175_754.0
SUPPLY_CUR = 1_220_793_754.0


def test_headline_names_the_chain():
    """"RLUSD supply" read as cross-chain; it is XRPL only."""
    assert cb._SHORT_LABEL["rlusd_xrpl_supply"] == "RLUSD XRPL supply"
    line = cb._plain_english_scalar_line(
        "rlusd_xrpl_supply", SUPPLY_PREV, SUPPLY_CUR, "usd")
    assert line.startswith("RLUSD XRPL supply "), line


def test_detail_line_names_instants_not_leaf_dates():
    """The exact approved wording, and no bare leaf date."""
    out = cb._detail_line(SUPPLY_PREV, SUPPLY_CUR, "usd",
                          LEAF_PREV, LEAF_CUR, "rlusd_xrpl_supply")
    assert out == (
        "was $1,227,175,754.00 at 9:00 PM ET Oct 4 (01:00 UTC Oct 5), "
        "now $1,220,793,754.00 at 9:00 PM ET Oct 5 (01:00 UTC Oct 6) "
        "\u2014 XRPL only"
    ), out


def test_detail_line_puts_eastern_first_utc_in_parens():
    out = cb._detail_line(SUPPLY_PREV, SUPPLY_CUR, "usd",
                          LEAF_PREV, LEAF_CUR, "rlusd_xrpl_supply")
    first_et = out.index("ET")
    first_utc = out.index("UTC")
    assert first_et < first_utc, "Eastern must come before UTC"
    assert "(01:00 UTC Oct 5)" in out and "(01:00 UTC Oct 6)" in out


def test_leaf_write_date_is_one_day_ahead_of_the_evening_described():
    """The trap this whole change exists to avoid.

    Leaf 2026-10-06 is taken at 01:00 UTC on Oct 6 = 9:00 PM ET on Oct 5.
    Labelling it "snapshot 2026-10-06" would name the wrong evening.
    """
    inst = cb._fmt_instant(LEAF_CUR)
    assert inst == "9:00 PM ET Oct 5 (01:00 UTC Oct 6)", inst
    assert "Oct 6" in inst.split("(")[1]      # UTC side carries the leaf date
    assert "Oct 5" in inst.split("(")[0]      # ET side carries the real evening


def test_scope_suffix_only_where_declared():
    """XRPL-only metrics say so; others are untouched."""
    assert cb._CHAIN_SCOPE["rlusd_xrpl_supply"] == "XRPL only"
    other = cb._detail_line(10, 12, "count", LEAF_PREV, LEAF_CUR,
                            "amm_pools_count")
    assert "XRPL only" not in other
    assert other.startswith("was 10 at ")


def test_detail_line_falls_back_without_instants():
    """Older envelopes carry no snapshot_taken_unix; must not crash or lie."""
    out = cb._detail_line(SUPPLY_PREV, SUPPLY_CUR, "usd")
    assert out == "was $1,227,175,754.00, now $1,220,793,754.00"
    assert " at " not in out


def test_scalar_slot_carries_a_real_as_of():
    """The as-of key must actually be populated, not silently null.

    build_strip receives the CHANGES envelope, which carries
    generated_at_utc. snapshot_taken_unix is a SIGNED-LEAF field and is not
    present here — an earlier draft read it and produced a dead key.
    """
    envelope = {
        "date": "2026-10-06",
        "generated_at_utc": "2026-10-06T01:21:25Z",
        "changes": [{
            "category": "rlusd",
            "line": "RLUSD XRPL supply shrank by $6.4M (−0.52%)",
            "detail": "was $1,227,175,754.00, now $1,220,793,754.00",
            "prove_url": "/rlusd",
            "source": "signed_snapshot",
            "metric_name": "rlusd_xrpl_supply",
            "metric_type": "usd",
            "before": SUPPLY_PREV,
            "after": SUPPLY_CUR,
            "delta": SUPPLY_CUR - SUPPLY_PREV,
        }],
    }
    strip = cb.build_strip(envelope, k=3)
    scalar = [s for s in strip["slots"]
              if s.get("metric_name") == "rlusd_xrpl_supply"]
    assert scalar, "scalar slot missing from strip"
    assert scalar[0]["as_of_utc"] == "2026-10-06T01:21:25Z"
    assert not hasattr(cb, "_iso_utc"), "dead helper should be removed"


# ---------------------------------------------------------------- template

TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "rlusd.html")


def _template():
    with open(TEMPLATE, encoding="utf-8") as f:
        return f.read()


def test_every_rolling_cell_names_chain_and_window():
    src = _template()
    scopes = re.findall(r'class="scope">([^<]+)<', src)
    assert len(scopes) == 4, f"expected 4 scoped cells, got {len(scopes)}"
    assert "both chains \u00b7 rolling last 24 hours, ending now" in scopes
    assert "XRPL \u00b7 rolling last 24 hours, ending now" in scopes
    assert scopes.count("Ethereum \u00b7 rolling last 24 hours, ending now") == 2
    for s in scopes:
        assert "rolling last 24 hours, ending now" in s, s


def test_scope_line_is_muted_and_wraps():
    """It is a qualifier under the label, not a heading, and must not
    re-introduce the phone overflow fixed in b56f566."""
    src = _template()
    m = re.search(r"\.press-footer \.label \.scope,\s*"
                  r"\.chain-card \.breakdown \.item \.l \.scope \{([^}]*)\}", src)
    assert m, "shared .scope rule not found"
    body = " ".join(m.group(1).split())
    assert "display: block" in body
    assert "overflow-wrap: anywhere" in body
    assert "opacity" in body


def test_phantom_dated_rows_sentence_is_gone():
    """It pointed at rows that are not rendered on this page."""
    src = _template()
    assert "dated history rows below" not in src
    # the surrounding explainer must survive
    assert "These cells show the last 24 hours on a rolling window" in src
    assert "Same underlying source (Etherscan on the Ethereum side" in src

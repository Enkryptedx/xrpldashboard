"""The escrow headline publishes escrows_snapshot, stamped with its ledger.

Regression for 2026-10-14..11-13: supply_escrow_walker was paused pending the
incremental redesign, freezing xrp_supply_block's escrow side at 31.4B XRP /
98 objects. Both the /cold-storage hero and the homepage escrow pill read that
column, so both published a month-old figure while escrows_snapshot -- written
every 30 min by escrow_walker from our own node -- carried the live one.

Locked here:
  1. the stamp composes "as of ledger N - time ET (UTC)" and DEGRADES to a
     fetched_at-only stamp while snapshot_ledger_index is still NULL, instead
     of inventing a ledger number or dropping the stamp entirely;
  2. the headline sums XRP-denominated escrow objects only (a TokenEscrow row
     carries no drops and must never enter an XRP total) and reports the
     OWNER count, which is a third number distinct from either cohort size;
  3. both reader-visible surfaces actually read it -- the discriminating
     check, since 1 and 2 would pass against the pre-change code (which
     composed no stamp at all) while the pages still rendered the frozen
     supply-block figure.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import escrow_headline as H  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 2026-11-13 16:12 ET == 21:12 UTC. Chosen on the winter side of the DST
# switch: the printed label is the site's stable "ET", but the CONVERSION is a
# real America/New_York one, so a broken conversion would show 21:12 or 17:12
# here rather than 4:12.
FETCHED_AT = 1794604320


def _rows(ledger=101234567):
    """Two XRP escrows owned by two accounts, plus one TokenEscrow."""
    return {
        "rows": [
            {"owner": "rA", "denom": "XRP", "amount_drops": 31_000_000_000_000_000},
            {"owner": "rB", "denom": "XRP", "amount_drops": 100_000_000_000_000},
            {"owner": "rC", "denom": "USD", "amount_drops": None},
        ],
        "fetched_at": FETCHED_AT,
        "snapshot_ledger_index": ledger,
        "token_escrow_observed": True,
    }


# --- 1. the stamp ---------------------------------------------------------

def test_stamp_names_the_ledger_and_both_clocks():
    s = H.format_as_of_stamp(101234567, FETCHED_AT)
    assert s.startswith("as of ledger 101,234,567")
    assert "(21:12 UTC)" in s
    # The site's stamp convention is the stable "ET" label (app.py,
    # roll_call_card.py, five "ET (" stamps in templates/) -- never the %Z
    # abbreviation, which would print EST/EDT and read differently here than
    # on every other surface.
    assert "4:12 PM ET (21:12 UTC)" in s, s
    assert "EST" not in s and "EDT" not in s, s


def test_stamp_degrades_to_fetch_time_while_ledger_is_null():
    """The fallback the task names: rows written before the walker captured
    the validated ledger index still get an honest, coarser stamp."""
    s = H.format_as_of_stamp(None, FETCHED_AT)
    assert "ledger" not in s
    assert s == "as of 4:12 PM ET (21:12 UTC)", s


def test_stamp_is_none_when_there_is_nothing_to_stamp():
    assert H.format_as_of_stamp(None, None) is None


# --- 2. the figure --------------------------------------------------------

def test_headline_sums_xrp_only_and_counts_owners():
    h = H.build_escrow_headline(_rows(), escrow_cohort_size=20)
    assert h["total_xrp"] == 31_100_000_000.0
    assert h["object_count"] == 2          # the USD escrow is not an XRP object
    assert h["token_object_count"] == 1
    assert h["owner_count"] == 2           # owners, not the 20/21 cohorts
    assert h["escrow_cohort_size"] == 20
    assert h["ledger_index"] == 101234567
    assert h["has_ledger"] is True
    assert h["age_seconds"] >= 0
    assert h["age_seconds"] <= int(time.time()) - FETCHED_AT


def test_headline_marks_a_null_ledger_without_losing_the_figure():
    h = H.build_escrow_headline(_rows(ledger=None), escrow_cohort_size=20)
    assert h["total_xrp"] == 31_100_000_000.0
    assert h["ledger_index"] is None
    assert h["has_ledger"] is False
    assert h["as_of"] and "ledger" not in h["as_of"]


def test_empty_snapshot_returns_none_so_callers_can_fall_back():
    assert H.build_escrow_headline({"rows": []}) is None
    assert H.build_escrow_headline(None) is None
    # All-token snapshot: no XRP total exists, so publish nothing.
    assert H.build_escrow_headline({
        "rows": [{"owner": "rC", "denom": "USD", "amount_drops": None}],
        "fetched_at": FETCHED_AT, "snapshot_ledger_index": 1,
    }) is None


# --- 3. the surfaces actually read it (discriminating case) ---------------

def test_escrow_snapshot_page_data_carries_the_headline():
    import escrow_snapshot
    assert "build_escrow_headline" in escrow_snapshot.__dict__ or hasattr(
        escrow_snapshot, "build_escrow_headline")
    src = open(os.path.join(HERE, "escrow_snapshot.py")).read()
    assert '"headline": build_escrow_headline(' in src


def test_cold_storage_hero_reads_the_snapshot_headline_not_the_supply_block():
    src = open(os.path.join(HERE, "templates", "cold_storage.html")).read()
    assert "set live = escrows.headline" in src
    # Slice the WHOLE hero block, not up to the first </div> -- the eyebrow
    # closes a div two lines in, and a short slice makes this test fail on a
    # correct template (which is exactly what it did on the first run).
    hero = src.split('class="hero-inner"')[1].split('class="kpi-strip"')[0]
    assert "live.total_xrp" in hero
    # The frozen supply-block figure may only appear as a fallback branch.
    assert hero.index("live.total_xrp") < hero.index("locked.total_xrp")


def test_homepage_pill_reads_the_live_cohort():
    src = open(os.path.join(HERE, "templates", "index.html")).read()
    assert "xrp_distribution.escrow_live" in src
    assert "XRP locked in Ripple escrows" in src


def test_pill_shows_no_frozen_ledger_wide_figure_beside_the_live_one():
    """Charlie's call 2026-11-13: the ledger-wide escrow_all_* line reads
    xrp_supply_block's escrow side, frozen since supply_escrow_walker was
    paused on 2026-10-14. A month-old figure rendered beside a 30-minute-old
    one reads as though both are current, so the line stays out until that
    walker resumes. This is the guard against it being restored by habit."""
    src = open(os.path.join(HERE, "templates", "index.html")).read()
    assert "Ledger-wide escrow, all accounts:" not in src
    pill = src.split("xrp_distribution.escrow_live")[1].split("</a>")[0]
    assert "escrowed_xrp" not in pill, (
        "the live pill must not render the frozen escrow_all_* total")
    assert "escrow_as_of_utc" not in pill


def test_constellation_withholds_every_frozen_escrow_all_figure():
    """Charlie's call 2026-11-13, second pass: the constellation published the
    same frozen escrow_all_* the pill had just dropped -- the IN ESCROW basin,
    its percentage, its ledger stamp, the Ripple sub-line, and the two legend
    chips. Every one is now gated on `escrow_stale`, the existing >60 min age
    check, so they come back by themselves on supply_escrow_walker's first
    fresh write instead of waiting for someone to remember a flag.

    HELD BY WALLETS is gated too: it is total - escrow - AMM, so rendering it
    while escrow is frozen republishes the Oct 14 figure by subtraction.
    """
    src = open(os.path.join(HERE, "templates", "index.html")).read()

    def gated(anchor, gate):
        """The line carrying `anchor` must also carry `gate`. Anchors are whole
        expressions, so this cannot pass on a coincidental substring."""
        lines = [ln for ln in src.splitlines() if anchor in ln]
        assert lines, f"anchor vanished from the template: {anchor}"
        for ln in lines:
            assert gate in ln, f"{anchor!r} renders ungated by {gate!r}: {ln.strip()[:120]}"

    # The basin value and its percentage.
    gated('format(xd.escrowed_xrp/1000000000)', "xd.escrow_stale")
    gated('format(xd.escrowed_pct)', "escrow_stale")
    # The derived wallets basin, on the staler-of-two-inputs rule.
    gated('format(xd.wallets_xrp/1000000000)', "xd.wallets_stale")
    gated('format(xd.wallets_pct)', "wallets_stale")
    # The Ripple monthly-release sub-line (same paused writer).
    gated("xd.escrow_ripple_xrp is not none", "not xd.escrow_stale")
    # The pill's ledger-wide fallback branch.
    gated("xrp_distribution.escrowed_xrp and", "not xrp_distribution.escrow_stale")

    # And the stamp says the figures are withheld rather than dating one.
    assert "escrow and wallet figures withheld: refresh paused since" in src


def test_distribution_payload_exposes_escrow_live_without_moving_escrowed_xrp():
    """The constellation's percentages are computed from escrowed_xrp
    (escrow_all_*). Repointing the headline must not have moved that shared
    value -- only added a sibling."""
    src = open(os.path.join(HERE, "app.py")).read()
    assert '"escrow_live": escrow_live,' in src
    assert '"escrowed_pct": (escrowed_xrp / total_xrp) * 100,' in src

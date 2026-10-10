"""Evernorth card assembly: build_card + the verbatim live line (2026-10-10).

build_card turns already-read Postgres rows into the whole card context. It
is pure, so every property the card promises a reader can be pinned here
without a DB, a node, or a render.

What this pins:

1. The live line is the owner's sentence, VERBATIM. It is assembled in
   Python rather than in a gettext string because a dynamic number inside
   `_()` means %-in-gettext, a known render-killer in these templates.
2. No wallet row carries a third-party name. The xrp-insights naming
   convention ("Custody A", "Staging", ...) stays in WALLETS for matching
   and is never rendered — on a public card it would read as a first-party
   fact about Evernorth's custody structure that no filing supports.
3. The two attribution groups keep their approved headings verbatim and
   stay SEPARATE, each linking to the source that makes its own claim.
   Merging them would launder 11 inferred addresses into the 2 that an
   explorer actually labelled.
4. The strongest tier string ("confirmed by Evernorth filing") is never
   reachable from a row — no filing names any address.
5. Balances are used as stored (XRP), an unreadable wallet is excluded
   rather than counted as zero, and a partial read is visible.
6. With no snapshot at all the card still renders the filing facts — the
   filing half must not depend on our own pipeline being up.

Hermetic: plain dicts, no DB, no network.
"""
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import institutional_treasuries as T  # noqa: E402

# The third-party naming convention that must never reach a rendered row.
BANNED_WORDS = ["Custody", "Staging", "Evernorth 1", "Evernorth 2"]


def _snapshot(balances, date="2026-10-10"):
    return {"date": date, "taken_at": 1791600000, "total_xrp": None,
            "readable_count": len(balances), "wallet_count": len(T.WALLETS),
            "balances": balances}


def _all_readable(per_wallet=36_405_879.0):
    return {addr: per_wallet for addr, _ in T.WALLETS}


# ── 1. the verbatim live line ────────────────────────────────────────────
def test_live_line_is_verbatim():
    """Owner-specified sentence. Do not reword without asking."""
    assert T.live_line(473_276_565.0, 13) == (
        "These 13 wallets total 473,276,565 XRP; "
        "the filing says at least 473,276,430 XRP at closing."
    )


def test_live_line_quotes_the_filing_minimum_exactly():
    line = T.live_line(1.0, 13)
    assert "473,276,430 XRP at closing" in line
    assert T.FILING_MIN_XRP == 473276430


def test_live_line_omitted_when_nothing_readable():
    """Better no line than a total that silently excludes wallets."""
    assert T.live_line(None) is None
    assert T.build_card(_snapshot({}))["live_line"] is None


def test_live_line_wallet_count_matches_the_published_list():
    card = T.build_card(_snapshot(_all_readable()))
    assert card["wallet_count"] == 13
    assert card["live_line"].startswith("These 13 wallets total ")


# ── 2. no third-party names anywhere in a row ───────────────────────────
def test_no_row_carries_a_third_party_name():
    card = T.build_card(_snapshot(_all_readable()))
    for group in card["groups"]:
        for row in group["rows"]:
            blob = " ".join(str(v) for v in row.values())
            for banned in BANNED_WORDS:
                assert banned not in blob, f"{banned!r} leaked into a row"


def test_rows_show_shortened_addresses_with_an_ellipsis():
    card = T.build_card(_snapshot(_all_readable()))
    rows = [r for g in card["groups"] for r in g["rows"]]
    assert len(rows) == 13
    for row in rows:
        assert "\u2026" in row["short"], "a shortened address must keep its ellipsis"
        assert row["short"] != row["address"]
        # The full address is only ever reachable through the explorer link.
        assert row["address"] in row["explorer_url"]


# ── 3. the two groups stay separate, headings verbatim ──────────────────
def test_group_headings_are_the_approved_strings_verbatim():
    card = T.build_card(_snapshot(_all_readable()))
    labels = [g["label"] for g in card["groups"]]
    assert labels == [
        "labeled Evernorth by XRPScan — not confirmed by Evernorth",
        "inferred by xrp-insights from amounts matching the filing "
        "— not confirmed by Evernorth",
    ]


def test_groups_are_two_and_split_2_and_11():
    card = T.build_card(_snapshot(_all_readable()))
    assert len(card["groups"]) == 2
    counts = [len(g["rows"]) for g in card["groups"]]
    assert counts == [2, 11], "2 explorer-labelled, 11 inferred"


def test_each_group_links_to_its_own_source():
    card = T.build_card(_snapshot(_all_readable()))
    xrpscan, inferred = card["groups"]
    assert "xrpscan.com" in xrpscan["source_url"]
    assert "xrp-insights.com" in inferred["source_url"]
    assert xrpscan["source_url"] != inferred["source_url"]


# ── 4. the strongest tier is unreachable ────────────────────────────────
def test_no_row_claims_a_filing_confirmed_an_address():
    card = T.build_card(_snapshot(_all_readable()))
    strongest = T.TIER_LABELS[T.TIER_FILING]
    for group in card["groups"]:
        for row in group["rows"]:
            assert row["tier"] == T.TIER_ATTRIBUTED
            assert strongest not in " ".join(str(v) for v in row.values())


# ── 5. balances, partial reads ──────────────────────────────────────────
def test_balances_are_used_as_stored_xrp_not_re_derived():
    addr = T.WALLETS[0][0]
    card = T.build_card(_snapshot({addr: 1.5}))
    row = next(r for g in card["groups"] for r in g["rows"]
               if r["address"] == addr)
    assert row["balance_xrp"] == 1.5
    assert card["total_xrp"] == 1.5


def test_unreadable_wallet_excluded_not_counted_as_zero():
    a, b = T.WALLETS[0][0], T.WALLETS[1][0]
    card = T.build_card(_snapshot({a: 10.0, b: None}))
    assert card["total_xrp"] == 10.0
    assert card["readable_count"] == 1
    assert card["partial_read"] is True


def test_full_read_is_not_flagged_partial():
    card = T.build_card(_snapshot(_all_readable()))
    assert card["readable_count"] == 13
    assert card["partial_read"] is False


def test_filing_delta_positive_is_consistent_with_at_least():
    card = T.build_card(_snapshot({T.WALLETS[0][0]: float(T.FILING_MIN_XRP + 100)}))
    assert card["filing_delta"] == 100.0


# ── 6. degradation ──────────────────────────────────────────────────────
def test_no_snapshot_still_renders_the_filing_facts():
    card = T.build_card(None)
    assert card["facts"], "filing facts must not depend on our pipeline"
    assert card["total_xrp"] is None
    assert card["live_line"] is None
    assert card["as_of"] is None
    assert len([r for g in card["groups"] for r in g["rows"]]) == 13


def test_moves_and_history_default_to_empty_lists():
    card = T.build_card(None)
    assert card["moves"] == []
    assert card["history"] == []


def test_moves_pass_through_build_moves_newest_first():
    raw = [
        {"hash": "OLD", "iso": "2026-10-09T10:00:00Z", "amount_xrp": 1.0,
         "direction": "in", "counterparty": "rStranger"},
        {"hash": "NEW", "iso": "2026-10-10T10:00:00Z", "amount_xrp": 2.0,
         "direction": "out", "counterparty": "rStranger"},
    ]
    card = T.build_card(None, raw_moves=raw)
    assert [m["hash"] for m in card["moves"]] == ["NEW", "OLD"]
    # Unlabelled counterparty stays bare — never an invented entity name.
    assert card["moves"][0]["counterparty_named"] is False
    assert card["moves"][0]["counterparty_name"] is None


def test_counterparty_is_shortened_first6_last4_not_tail_truncated():
    """A template `truncate` would give 'rStranger12…' — the wrong form."""
    raw = [{"hash": "H", "iso": "2026-10-10T10:00:00Z", "amount_xrp": 1.0,
            "direction": "out",
            "counterparty": "rsT3yYMkuicxW1hYsy787mg5XHhkz2uQRk"}]
    m = T.build_card(None, raw_moves=raw)["moves"][0]
    assert m["counterparty_short"] == "rsT3yY\u2026uQRk"
    assert m["counterparty_short"].endswith("uQRk"), "must keep the tail"
    # The full address stays available for the explorer link only.
    assert m["counterparty"] == "rsT3yYMkuicxW1hYsy787mg5XHhkz2uQRk"


def test_internal_move_has_no_counterparty_short():
    raw = [{"hash": "H", "iso": "2026-10-10T10:00:00Z", "amount_xrp": 1.0,
            "direction": "out", "counterparty": None}]
    m = T.build_card(None, raw_moves=raw)["moves"][0]
    assert m["counterparty_short"] is None


def test_card_carries_a_correction_contact():
    card = T.build_card(None)
    assert "@" in card["correction_contact"]

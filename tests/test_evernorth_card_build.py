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


# ── dust rule + the "XRP" label (2026-10-20) ─────────────────────────────
def _raw(amount, cp="rStranger1111111111111111111111111", iso="2026-10-20T20:12:29Z", hash_="H"):
    return {"hash": hash_, "iso": iso, "amount_xrp": amount, "direction": "in", "counterparty": cp}


def test_moves_under_one_xrp_are_hidden():
    """A dusting bot sent 1 drop to every Evernorth wallet (and 635K other
    accounts). Those are not treasury moves and were filling the block."""
    rows = [_raw(0.000001, hash_="DUST"), _raw(10.0, hash_="TEN"), _raw(0.5, hash_="HALF")]
    out = T.build_moves(rows)
    assert [m["hash"] for m in out] == ["TEN"]


def test_exactly_one_xrp_is_shown():
    assert [m["hash"] for m in T.build_moves([_raw(1.0, hash_="ONE")])] == ["ONE"]


def test_none_amount_is_not_treated_as_dust():
    """amount_xrp None (unreadable) must not be silently dropped as dust."""
    assert len(T.build_moves([_raw(None, hash_="NA")])) == 1


def test_card_discloses_hidden_dust_only_when_there_was_some():
    card = T.build_card(snapshot=None, raw_moves=[_raw(0.000001), _raw(10.0)], history=[], labels={})
    assert card["moves_hidden_dust"] is True
    card = T.build_card(snapshot=None, raw_moves=[_raw(10.0)], history=[], labels={})
    assert card["moves_hidden_dust"] is False


def test_dust_filter_frees_a_row_for_a_real_move():
    """Before: 7 dust rows + 4 real moves = the 10-row cap hid the 10 XRP
    internal Evernorth transfer. After: all real moves fit."""
    dust = [_raw(0.000001, hash_=f"D{i}", iso=f"2026-08-29T0{i}:00:00Z") for i in range(7)]
    real = [_raw(a, hash_=h, iso=i) for a, h, i in (
        (100000, "A", "2026-10-20T20:12:29Z"), (200000, "B", "2026-10-20T20:12:29Z"),
        (200000, "C", "2026-10-20T20:12:29Z"), (10, "TEN", "2026-10-20T18:25:11Z"),
        (200000, "E", "2026-10-03T03:34:02Z"))]
    out = T.build_moves(dust + real, limit=10)
    assert {m["hash"] for m in out} == {"A", "B", "C", "TEN", "E"}


def test_counterparty_slot_is_address_or_curated_name_never_a_currency_word():
    """Regression for the 'XRP' label: named_accounts.json carried
    rHXgaJ... as name="XRP" -- a dusting bot's own TOML block named itself
    after the currency and derive_name() took it verbatim. The card then
    printed "XRP" in the counterparty slot. The entry is removed; this pins
    that no curated label is a bare currency code."""
    import json, pathlib
    named = json.loads((pathlib.Path(__file__).resolve().parent.parent / "named_accounts.json").read_text())
    assert "rHXgaJLpjDe4jsAiF8atNrRfS6eYrjPYPB" not in named
    bad = {a: v.get("name") for a, v in named.items()
           if isinstance(v, dict) and (v.get("name") or "").strip().upper() in {"XRP", "RLUSD", "USD", "BTC", "ETH"}}
    assert bad == {}, bad


def test_template_disclosure_string():
    import pathlib
    t = (pathlib.Path(__file__).resolve().parent.parent / "templates" / "_evernorth_card.html").read_text()
    assert "{{ _('Moves under %(n)s XRP not shown.', n=evernorth.moves_floor_label) }}" in t


def test_disclosed_floor_is_what_the_stream_actually_captures():
    """The card may only promise what the events stream delivers.

    2026-10-20 am: the stream recorded a treasury payment only above the
    whale threshold (50,000 XRP), so the floor had to be 50,000.
    2026-10-20 pm: xrpl_stream.TREASURY_WATCH records every payment touching
    institutional_treasuries.WALLETS, so the floor is the 1 XRP dust guard.
    This test holds the two sides together: if TREASURY_WATCH ever stops
    covering WALLETS, the 1 XRP promise becomes false and this fails."""
    import re, pathlib, importlib, os
    root = pathlib.Path(__file__).resolve().parent.parent
    src = (root / "xrpl_stream.py").read_text()
    assert "from institutional_treasuries import WALLETS" in src, "stream must read the treasury list itself"
    assert re.search(r"from_addr in TREASURY_WATCH or to_addr in TREASURY_WATCH", src), "tagged branch must consult TREASURY_WATCH"
    assert "TREASURY_WATCH" in src.split("event_type = \"tagged\"")[0].rsplit("if event_type is None", 1)[1], "TREASURY_WATCH must gate the tagged branch, not a later one"
    # The code is on main, but the promise is only true once the Lenovo
    # process has been restarted. Owner away, return TBD (2026-10-20).
    if T.STREAM_TREASURY_WATCH_RESTARTED:
        assert T.MOVES_FLOOR_XRP == int(T.MOVES_MIN_XRP) == 1
        assert T.MOVES_FLOOR_LABEL == "1"
    else:
        assert T.MOVES_FLOOR_XRP == T.STREAM_PRE_WATCH_FLOOR_XRP == 50_000
        assert T.MOVES_FLOOR_LABEL == "50,000"
    card = T.build_card(snapshot=None, raw_moves=[], history=[], labels={})
    assert card["moves_floor_label"] == T.MOVES_FLOOR_LABEL


def test_pre_watch_floor_matches_stream_default_threshold():
    """50,000 is not a magic number: it is xrpl_stream's default
    WHALE_XRP_THRESHOLD_XRP, read from source."""
    import re, pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "xrpl_stream.py").read_text()
    m = re.search(r'WHALE_XRP_THRESHOLD_XRP",\s*"(\d+)"', src)
    assert m and int(m.group(1)) == T.STREAM_PRE_WATCH_FLOOR_XRP


def test_stream_treasury_watch_is_the_wallet_list(monkeypatch):
    """Import the stream's loader in isolation and check it returns exactly
    WALLETS -- no hand-copied list that can drift."""
    import importlib.util, pathlib, sys
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    src = (root / "xrpl_stream.py").read_text()
    # lift just the loader so the stream's side-effectful module body never runs
    start = src.index("def _load_treasury_watch():"); end = src.index("TREASURY_WATCH = _load_treasury_watch()")
    ns = {"sys": sys}
    exec(src[start:end], ns)
    got = ns["_load_treasury_watch"]()
    assert got == frozenset(a for a, _ in T.WALLETS)
    assert len(got) == 13

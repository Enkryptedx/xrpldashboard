"""/whales shows the two attribution labels, never the names (2026-10-10).

The Evernorth 13 are attributed by a third party — an explorer's curation
or a research site's reconciliation — and named in no SEC filing. Standing
owner ruling: the third party's NAMES are not published on ANY surface.
A row therefore keeps its bare short address and carries the GROUP's
attribution string, which is the whole evidentiary claim.

What this pins:

1. Both approved group strings are reachable from /whales, verbatim and
   identical to the ones the /institutional card uses — one table feeds
   both surfaces, so they cannot drift apart.
2. The third party's wallet names never reach a rendered row, checked
   field by field over every event key.
3. An address in neither group gets no attribution at all — the label is
   not sprayed onto unrelated accounts.
4. The strongest string ("confirmed by Evernorth filing") stays
   unreachable: no filing names any address.
5. The chip stays INSIDE its own <a class="addr"> and above its own
   address. A one-sided chip that escapes its anchor reads as the wrong
   side of the arrow — the bug fixed in PR #15.
6. Rows show short addresses, with the full address only in href values.

Hermetic: _resolve_event is called directly with fake rows, and the page
is rendered from an injected context. No DB, no node.
"""
import json
import os
import re
import sys
import time

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import app as app_module  # noqa: E402
import institutional_treasuries as T  # noqa: E402

XRPSCAN_ADDR = "rsT3yYMkuicxW1hYsy787mg5XHhkz2uQRk"   # explorer-labelled
INFERRED_ADDR = "rGy4zJtGfGtF7dtjZmBraQTcfZSQgwqpaa"  # inferred only
STRANGER = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"

XRPSCAN_LABEL = "labeled Evernorth by XRPScan — not confirmed by Evernorth"
INFERRED_LABEL = ("inferred by xrp-insights from amounts matching the filing "
                  "— not confirmed by Evernorth")

BANNED_NAMES = ["Custody A", "Custody B", "Custody C", "Custody D",
                "Custody E", "Custody F", "Custody G", "Custody H",
                "Custody I", "Custody J", "Staging",
                "Evernorth 1", "Evernorth 2"]


def _row(from_addr, to_addr, drops=60_000_000_000):
    """_resolve_event unpacks a positional TUPLE, not a dict - a dict here
    unpacks into its KEYS and fails inside _format_xrp with 'str / int'.
    Order: tx_hash, ledger_index, ts, type, from_addr, to_addr,
    amount_drops, currency, issuer, raw_json.

    amount_display is read out of raw_json (the dedicated columns are
    sparse for some tx shapes), so Amount has to be in the payload for the
    row to show an amount at all.
    """
    raw = {"transaction": {"TransactionType": "Payment",
                           "Amount": str(drops)}}
    return (
        "A" * 64, 107_500_000, int(time.time()) - 60, "large_xfer",
        from_addr, to_addr, drops, None, None, json.dumps(raw),
    )


def _resolve(row, named=None):
    return app_module._resolve_event(row, named or {}, {}, tier_lookup={})


# ── 1. both strings reachable, verbatim, shared with the card ───────────
def test_xrpscan_group_label_renders_verbatim():
    ev = _resolve(_row(XRPSCAN_ADDR, STRANGER))
    assert ev["from_attribution"] == XRPSCAN_LABEL


def test_inferred_group_label_renders_verbatim():
    ev = _resolve(_row(STRANGER, INFERRED_ADDR))
    assert ev["to_attribution"] == INFERRED_LABEL


def test_whales_uses_the_same_strings_as_the_card():
    """One table, two surfaces — they must not drift apart."""
    card = T.build_card(None)
    card_labels = {g["label"] for g in card["groups"]}
    whales_labels = {
        _resolve(_row(XRPSCAN_ADDR, STRANGER))["from_attribution"],
        _resolve(_row(INFERRED_ADDR, STRANGER))["from_attribution"],
    }
    assert whales_labels == card_labels


def test_each_group_carries_its_own_source_url():
    a = _resolve(_row(XRPSCAN_ADDR, STRANGER))
    b = _resolve(_row(INFERRED_ADDR, STRANGER))
    assert "xrpscan.com" in a["from_attribution_url"]
    assert "xrp-insights.com" in b["from_attribution_url"]
    assert a["from_attribution_url"] != b["from_attribution_url"]


# ── 2. no third-party names in any field ────────────────────────────────
@pytest.mark.parametrize("addr", [XRPSCAN_ADDR, INFERRED_ADDR])
def test_no_third_party_name_in_any_event_field(addr):
    ev = _resolve(_row(addr, STRANGER))
    blob = " ".join(str(v) for v in ev.values())
    for name in BANNED_NAMES:
        assert name not in blob, f"{name!r} leaked into a /whales row"


def test_attributed_address_still_has_no_label_chip():
    """Attribution is not a name: the label pipeline must stay empty."""
    ev = _resolve(_row(XRPSCAN_ADDR, STRANGER))
    assert ev["from_label"] is None


# ── 3 + 4. scope and the unreachable strongest claim ────────────────────
def test_unattributed_address_gets_no_attribution():
    ev = _resolve(_row(STRANGER, "rLNaPoKeeNjNFBPdHJaKLWLKxQpqEjCJMz"))
    assert ev["from_attribution"] is None
    assert ev["to_attribution"] is None
    assert ev["from_attribution_url"] is None


def test_filing_confirmed_string_never_reachable():
    for addr in (XRPSCAN_ADDR, INFERRED_ADDR):
        ev = _resolve(_row(addr, STRANGER))
        blob = " ".join(str(v) for v in ev.values())
        assert T.TIER_LABELS[T.TIER_FILING] not in blob


def test_all_thirteen_wallets_are_attributed():
    for addr, _name in T.WALLETS:
        label, url = T.attribution_for(addr)
        assert label in (XRPSCAN_LABEL, INFERRED_LABEL)
        assert url


# ── 5 + 6. rendered markup: chip ownership, short addresses ─────────────
def _render_whales(events):
    ctx = {
        "events": events, "filter_type": None, "tier": "50k",
        "tier_label": "50K+", "tier_drops": 50_000_000_000,
        "threshold_xrp": 50_000, "named_accounts_count": 74,
        "radar_blips": [], "data_age_label": "live",
        "type_counts": {"_total": len(events), "large_xfer": len(events),
                        "tagged": 0, "trustset": 0},
        "radar_stats": {"last_24h": 0, "last_label": "none"},
    }
    from flask import render_template
    with app_module.app.test_request_context("/whales"):
        return render_template("whales.html", **ctx)


def test_chip_stays_inside_its_own_anchor():
    """A chip outside its <a class="addr"> reads as the wrong side of the
    arrow — the positional bug fixed in PR #15."""
    html = _render_whales([_resolve(_row(XRPSCAN_ADDR, STRANGER))])
    anchors = re.findall(r'<a class="addr.*?</a>', html, re.S)
    owning = [a for a in anchors if XRPSCAN_LABEL in a]
    assert len(owning) == 1, "attribution chip must sit in exactly one anchor"
    assert T.short_address(XRPSCAN_ADDR) in owning[0]
    # And the arrow must not fall inside the anchor that owns the chip.
    assert "arrow" not in owning[0]


def test_chip_is_stacked_above_its_own_address():
    html = _render_whales([_resolve(_row(XRPSCAN_ADDR, STRANGER))])
    anchor = next(a for a in re.findall(r'<a class="addr.*?</a>', html, re.S)
                  if XRPSCAN_LABEL in a)
    assert anchor.index("addr-chip") < anchor.index("addr-id")


def test_rendered_row_shows_short_address_not_full():
    html = _render_whales([_resolve(_row(XRPSCAN_ADDR, STRANGER))])
    text = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<style.*?</style>", " ", html))
    assert T.short_address(XRPSCAN_ADDR) in text
    assert XRPSCAN_ADDR not in text, "full address must not be visible text"


def test_rendered_row_carries_no_third_party_name():
    html = _render_whales([
        _resolve(_row(XRPSCAN_ADDR, STRANGER)),
        _resolve(_row(STRANGER, INFERRED_ADDR)),
    ])
    for name in BANNED_NAMES:
        assert name not in html, f"{name!r} leaked into rendered /whales HTML"


def test_both_group_strings_render_on_one_page():
    html = _render_whales([
        _resolve(_row(XRPSCAN_ADDR, STRANGER)),
        _resolve(_row(STRANGER, INFERRED_ADDR)),
    ])
    assert XRPSCAN_LABEL in html
    assert INFERRED_LABEL in html

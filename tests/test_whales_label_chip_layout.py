"""/whales label-chip ownership layout (Charlie 2026-10-05).

Incident: ledger 107450166, `rwpTh9DDa5…` -> `rMdG3ju8pg…`. The receiver
carried an "Uphold" chip and the sender carried none, so the row rendered as

    rwpTh9…mQVP  →  [Uphold] rMdG3j…6Fxns

and the only chip on the row sat immediately after the arrow, reading as if
it belonged to the SENDER. Root cause was purely positional: the chip was an
inline span emitted BEFORE its address inside the same `<a class="addr">`,
with a uniform 10px `.event-flow` gap on its left and only 6px of its own
`margin-right` on its right.

Fix (templates/whales.html only, inline CSS): each party is one vertical
unit — the chip stacks directly ABOVE its own address, left-aligned to it —
and the arrow gets extra horizontal margin so no chip can read as touching
it. An unlabeled address simply shows no chip.

These tests assert at the layer the fix lives in: the rendered template
structure and the inline CSS rules. No DB, no network.
"""

import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TEMPLATE = os.path.join(REPO, "templates", "whales.html")

SENDER = "rwpTh9DDa52XkM9nTKp2QrJuCGV5d1mQVP"
RECEIVER = "rMdG3ju8pgyVh29ELPWaDuA74CpWW6Fxns"


def _event(from_label, to_label):
    """One resolved whale row, shaped exactly like app._resolve_event output."""
    return {
        "tx_hash": "A1B2C3D4E5F6A1B2C3D4E5F6",
        "tx_hash_short": "A1B2C3D4E5…",
        "ledger": 107450166,
        "age": "2m",
        "type": "large_xfer",
        "type_display": "whale",
        "tx_type": "Payment",
        "from_addr": SENDER,
        "from_addr_short": "rwpTh9…mQVP",
        "from_label": from_label,
        "from_label_tier": None,
        "from_label_source": None,
        "from_attested_domain": None,
        "from_address_tier": "labeled" if from_label else "bare",
        "from_address_tier_display": "Labeled",
        "from_ofac_sanctioned": False,
        "to_addr": RECEIVER,
        "to_addr_short": "rMdG3j…6Fxns",
        "to_label": to_label,
        "to_label_tier": None,
        "to_label_source": None,
        "to_attested_domain": None,
        "to_address_tier": "labeled" if to_label else "bare",
        "to_address_tier_display": "Labeled",
        "to_ofac_sanctioned": False,
        "amount_display": "250,000 XRP",
        "row_type_pill": "whale",
        "xrpscan_url": "https://xrpscan.com/tx/A1B2C3D4E5F6A1B2C3D4E5F6",
        "token_tier": None,
        "token_tier_display": None,
    }


def _render(events):
    """Render whales.html from an injected context (no DB, no live events)."""
    import app as A
    from flask import render_template

    A.app.config["TESTING"] = True
    ctx = dict(
        events=events,
        filter_type="",
        tier="100k",
        tier_label="≥100K XRP",
        tier_drops=100_000 * 1_000_000,
        type_counts={
            "_total": len(events),
            "large_xfer": len(events),
            "tagged": 0,
            "trustset": 0,
        },
        threshold_xrp=100_000,
        named_accounts_count=74,
        radar_blips=[],
        radar_stats={"last_24h": 0, "last_label": "—"},
        data_age_label="live",
    )
    with A.app.test_request_context("/whales"):
        return render_template("whales.html", **ctx)


def _flow(html, nth=0):
    """Return the nth `<div class="event-flow">…</div>` block."""
    blocks = re.findall(
        r'<div class="event-flow">(.*?)</div>', html, re.S
    )
    assert len(blocks) > nth, f"expected >{nth} event-flow blocks, got {len(blocks)}"
    return blocks[nth]


def _parties(flow):
    """Split one flow into (sender_markup, receiver_markup) at the arrow."""
    arrow = re.search(r'<span class="arrow">', flow)
    assert arrow, "flow has no arrow span"
    return flow[: arrow.start()], flow[arrow.end():]


# ---------------------------------------------------------------- CSS rules

def _css():
    with open(TEMPLATE, encoding="utf-8") as f:
        return f.read()


def test_addr_unit_stacks_chip_above_address():
    """`.addr` must be a column flex so the chip sits ABOVE its address,
    left-aligned to it — not inline beside it."""
    css = _css()
    rule = re.search(r"\n\s*\.addr\s*\{(.*?)\}", css, re.S)
    assert rule, ".addr rule not found"
    body = rule.group(1)
    assert "flex-direction: column" in body, (
        ".addr must stack its children vertically (chip above address); got: "
        + " ".join(body.split())
    )
    assert "align-items: flex-start" in body, (
        ".addr children must be left-aligned to each other; got: "
        + " ".join(body.split())
    )


def test_chip_is_not_attached_sideways_to_the_address():
    """The old inline layout put the chip to the LEFT of the address via
    `margin-right: 6px`. Stacked, that side margin must be gone."""
    css = _css()
    rule = re.search(r"\.addr \.label\s*\{(.*?)\}", css, re.S)
    assert rule, ".addr .label rule not found"
    assert "margin-right" not in rule.group(1), (
        ".addr .label must not keep a right margin — it no longer sits beside "
        "the address"
    )


def test_arrow_has_extra_space_on_both_sides():
    """Extra horizontal margin on the arrow, so no chip can read as touching
    it. `margin: 0 Npx` applies to both sides with N > 0."""
    css = _css()
    rule = re.search(r"\n\s*\.arrow\s*\{(.*?)\}", css, re.S)
    assert rule, ".arrow rule not found"
    body = " ".join(rule.group(1).split())
    m = re.search(r"margin:\s*0\s+(\d+)px", body)
    assert m, f".arrow must set a horizontal margin; got: {body}"
    assert int(m.group(1)) > 0, f".arrow horizontal margin must be > 0; got: {body}"


def test_address_rows_share_a_baseline_with_the_arrow():
    """With one side two rows tall and the other one row, the flow must align
    on the address line (the last row of each unit), not on centre."""
    css = _css()
    rule = re.search(r"\.event-flow\s*\{(.*?)\}", css, re.S)
    assert rule, ".event-flow rule not found"
    assert "align-items: flex-end" in rule.group(1), (
        ".event-flow must align on the address baseline (flex-end) so an "
        "unlabeled side does not knock the row out of alignment"
    )


# ------------------------------------------------------- rendered structure

def test_each_chip_is_nested_inside_its_own_address_anchor():
    """The ownership guarantee: a chip may only appear INSIDE the `<a
    class="addr">` of the address it describes, wrapped in `.addr-chip`."""
    html = _render([_event("Coinbase", "Uphold")])
    flow = _flow(html)

    # Every chip occurrence must be inside an addr anchor, never a bare
    # sibling of the arrow.
    for m in re.finditer(r'<span class="label">', flow):
        before = flow[: m.start()]
        # the nearest preceding anchor open must not already be closed
        last_open = before.rfind('<a class="addr')
        assert last_open != -1, "chip rendered outside any address anchor"
        assert "</a>" not in before[last_open:], (
            "chip escaped its address anchor — it would read as belonging to "
            "the other side of the arrow"
        )
        assert 'class="addr-chip"' in before[last_open:], (
            "chip must be wrapped in .addr-chip inside its address unit"
        )


def test_labeled_receiver_chip_belongs_to_receiver_not_sender():
    """The exact incident row: sender unlabeled, receiver labeled 'Uphold'.
    The chip must live on the RECEIVER's side of the arrow."""
    html = _render([_event(None, "Uphold")])
    sender, receiver = _parties(_flow(html))

    assert "Uphold" not in sender, (
        "receiver's chip leaked onto the sender side of the arrow — this is "
        "the reported bug"
    )
    assert "Uphold" in receiver, "receiver's chip is missing from its own side"
    assert 'class="addr-chip"' not in sender, (
        "unlabeled sender must render no chip row at all"
    )
    assert 'class="addr-chip"' in receiver


def test_both_labeled_each_chip_on_its_own_side():
    html = _render([_event("Coinbase", "Uphold")])
    sender, receiver = _parties(_flow(html))
    assert "Coinbase" in sender and "Uphold" not in sender
    assert "Uphold" in receiver and "Coinbase" not in receiver


def test_neither_labeled_renders_no_chip():
    html = _render([_event(None, None)])
    flow = _flow(html)
    assert 'class="addr-chip"' not in flow, (
        "an unlabeled address must simply show no chip"
    )
    assert 'class="label"' not in flow


def test_every_address_is_wrapped_in_an_addr_id_row():
    """The address (+ OFAC flag + tier pill) is the unit's own bottom row, so
    the chip above it has something to be left-aligned against."""
    for ev in (_event("Coinbase", "Uphold"), _event(None, "Uphold"),
               _event(None, None)):
        flow = _flow(_render([ev]))
        assert flow.count('class="addr-id"') == 2, (
            "both sender and receiver need an .addr-id row; got "
            f"{flow.count('class=\"addr-id\"')}"
        )


@pytest.mark.parametrize("from_label,to_label", [
    ("Coinbase", "Uphold"),
    (None, "Uphold"),
    (None, None),
])
def test_chip_count_matches_label_count(from_label, to_label):
    flow = _flow(_render([_event(from_label, to_label)]))
    expected = sum(1 for v in (from_label, to_label) if v)
    assert flow.count('class="addr-chip"') == expected

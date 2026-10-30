"""Wallet Pulse "top by value", and issuer-only capability pills on /wallet.

Two /wallet changes, both reported on rK21t6nGLy4MNDqhvy3pGwcLFy25qgfzMx
(0 trustlines, no tokens issued, 11 txs, one 185,017,485.5 XRP inflow from
rDAE53VfMvftPB4ogpWGWvzkQxfht6JPxr).

1. THE "top" STAT WAS THE BUSIEST, NOT THE BIGGEST

   `top_label` took `nodes[0]` -- the ACTIVITY ranking -- and `nodes[0]
   ['pct']`, which is a share of TX COUNT. On this wallet that reads
   "top: Unknown account · 9.1%" while the same graph holds a 185M XRP
   inflow, because 1 of 11 txs is 9.1% of the activity. It now names the
   highest-VALUE counterparty and states its share of VALUE.

   Note for anyone re-reading this fixture: after PR #108 the activity
   tiebreak is value DESC, so on THIS wallet `nodes[0]` already happens
   to BE the big sender. What changes here is the PERCENTAGE (9.1% ->
   79.2%). `test_value_leader_wins_when_it_is_not_the_activity_leader`
   is the case that proves the SELECTION changed too -- without it these
   tests would pass against the old code.

2. ISSUER-ONLY CAPABILITY PILLS RENDERED ON A PLAIN WALLET

   Freeze / Clawback / Transfer fee describe what an ISSUER can do to
   holders of its token. On an account that issued nothing and holds no
   trustline there is no such token and no such holder, so
   "This issuer retains the ability to freeze trustlines" reads as a
   finding about a plain wallet. Filtered at the wallet_data call site
   only; `check_data._capability_signals` is untouched, so /token and
   /check still render the full list.

Hermetic: synthetic account_tx envelopes and literal AccountRoot dicts.
No node, no DB, no network, no clock dependence beyond `time.time()` for
tx dates (ages are not asserted).
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wallet_data as wd  # noqa: E402
from check_data import _capability_signals  # noqa: E402


OWNER = "rK21t6nGLy4MNDqhvy3pGwcLFy25qgfzMx"
BIG = "rDAE53VfMvftPB4ogpWGWvzkQxfht6JPxr"   # 185,017,485.5 XRP, ONE tx
CHATTY = "rCHATTYdddddddddddddddddddddddddd"
RIPPLE_EPOCH = wd.RIPPLE_EPOCH

# Ledger flag bits, duplicated as literals on purpose: importing the
# private constants would make this test agree with check_data by
# construction instead of checking the encoding it actually reads.
FLAG_GLOBAL_FREEZE = 0x00400000
FLAG_NO_FREEZE = 0x00200000
FLAG_CLAWBACK = 0x02000000
FLAG_DISABLE_MASTER = 0x00100000


# ── fixtures ─────────────────────────────────────────────────────────────

def _payment(sender, drops, ripple_date):
    return {
        "tx": {
            "TransactionType": "Payment",
            "Account": sender,
            "Destination": OWNER,
            "Amount": str(int(drops)),
            "date": ripple_date,
        },
        "meta": {"delivered_amount": str(int(drops))},
        "validated": True,
    }


def _trustset(counterparty, ripple_date):
    """A zero-value counterparty -- the shape of the five 0.0-XRP rows."""
    return {
        "tx": {
            "TransactionType": "TrustSet",
            "Account": counterparty,
            "LimitAmount": {"currency": "USD", "issuer": OWNER, "value": "1"},
            "date": ripple_date,
        },
        "meta": {},
        "validated": True,
    }


def _rk21t6_shape():
    """11 txs / 10 counterparties, every counterparty at tx_count=1.

    Mirrors the live read: five zero-volume trustsets, four mid-size
    inflows, the 185M inflow OLDEST, plus one self AccountSet that has no
    counterparty at all -- which is why 11 txs yield 10 counterparties.
    """
    now_ripple = int(time.time()) - RIPPLE_EPOCH
    txs = []
    for i in range(5):
        txs.append(_trustset(
            f"rZero{i}AAAAAAAAAAAAAAAAAAAAAAAAAAAA", now_ripple - 60 * (i + 1)))
    for i, drops in enumerate([14_693_052_500_000, 13_140_680_400_000,
                               11_955_157_600_000, 8_655_567_100_000]):
        txs.append(_payment(
            f"rMid{i}BBBBBBBBBBBBBBBBBBBBBBBBBBBBB", drops,
            now_ripple - 3600 * (i + 1)))
    txs.append(_payment(BIG, 185_017_485_500_000, now_ripple - 14 * 3600))
    txs.append({
        "tx": {"TransactionType": "AccountSet", "Account": OWNER,
               "date": now_ripple - 30},
        "meta": {}, "validated": True,
    })
    return txs


def _graph(txs):
    cps = wd._build_counterparty_graph(txs, OWNER, wd.LOOKBACK_DAYS)
    return cps, wd._layout_nodes(cps, len(txs))


def _addr_of(node):
    return (node.get("detail") or {}).get("addressFull")


# ── 1. top by value ──────────────────────────────────────────────────────

def test_fixture_matches_the_measured_wallet():
    """Guard the fixture itself: 11 txs, 10 counterparties, all tx_count=1."""
    txs = _rk21t6_shape()
    assert len(txs) == 11
    cps_all = {}
    for tx in txs:
        cp = wd._tx_counterparty(tx, OWNER)
        if cp is None:
            continue
        cps_all.setdefault(cp[0], 0)
        cps_all[cp[0]] += 1
    assert len(cps_all) == 10, f"expected 10 counterparties, got {len(cps_all)}"
    assert set(cps_all.values()) == {1}, "every counterparty must have tx_count=1"
    assert BIG in cps_all


def test_top_by_value_names_the_185m_sender():
    cps, nodes = _graph(_rk21t6_shape())
    _label, addr = wd._top_counterparty_label(nodes, cps)
    assert addr == BIG, f"top by value should be the 185M sender, got {addr}"


def test_top_share_is_a_value_share_not_a_tx_count_share():
    """The number next to the name must be value, not activity.

    1 of 11 txs = 9.1%; 185,017,485.5 of 233,461,943.1 XRP = 79.2%.
    """
    cps, nodes = _graph(_rk21t6_shape())
    label, _addr = wd._top_counterparty_label(nodes, cps)
    assert "79.2%" in label, f"expected the value share in {label!r}"
    assert "9.1%" not in label, "still reporting the tx-count share"
    # and the tx-count share really is what the old code would have used
    assert nodes[0]["pct"] == 9.1


def test_value_leader_wins_when_it_is_not_the_activity_leader():
    """The discriminating case: a chatty dust counterparty is the activity
    leader, so `nodes[0]` is NOT the value leader. Without this test the
    suite would pass against the old `nodes[0]` code.
    """
    now_ripple = int(time.time()) - RIPPLE_EPOCH
    txs = [_payment(CHATTY, 1_000_000, now_ripple - 10 * i) for i in range(40)]
    txs.append(_payment(BIG, 185_017_485_500_000, now_ripple - 14 * 3600))
    cps, nodes = _graph(txs)
    assert _addr_of(nodes[0]) == CHATTY, "fixture must make the dust node busiest"
    _label, addr = wd._top_counterparty_label(nodes, cps)
    assert addr == BIG, "40 dust payments outranked a 185M XRP inflow"


def test_name_comes_straight_from_the_node():
    """No second label lookup: the name is whatever _layout_nodes already
    got from _classify_address."""
    cps, nodes = _graph(_rk21t6_shape())
    ranked = wd._top_counterparty_by_value(cps)
    assert ranked is not None
    label, _addr = wd._top_counterparty_label(nodes, cps)
    assert label.startswith(nodes[ranked[0]]["name"] + " \u00b7 ")


def test_falls_back_to_activity_leader_when_nothing_moved_value():
    """A wallet whose every counterparty moved 0 keeps the old behaviour
    rather than inventing a 0% leader."""
    now_ripple = int(time.time()) - RIPPLE_EPOCH
    txs = [_trustset(f"rZ{i}CCCCCCCCCCCCCCCCCCCCCCCCCCCCCC", now_ripple - 60 * i)
           for i in range(12)]
    cps, nodes = _graph(txs)
    assert wd._top_counterparty_by_value(cps) is None
    label, addr = wd._top_counterparty_label(nodes, cps)
    assert label == f"{nodes[0]['name']} \u00b7 {nodes[0]['pct']}%"
    assert addr == _addr_of(nodes[0])


def test_empty_graph_keeps_the_em_dash():
    label, addr = wd._top_counterparty_label([], [])
    assert label == "\u2014"
    assert addr is None


def test_value_helper_picks_usd_scale_when_priced():
    """USD and drops must never be mixed into one percentage."""
    cps = [
        ("rA", {"volume_in_usd": 90.0, "volume_out_usd": 0.0,
                "volume_in_drops": 1, "volume_out_drops": 0}),
        ("rB", {"volume_in_usd": 10.0, "volume_out_usd": 0.0,
                "volume_in_drops": 999_999_999, "volume_out_drops": 0}),
    ]
    assert wd._top_counterparty_by_value(cps) == (0, 90.0)


def test_value_helper_falls_back_to_drops_without_an_oracle():
    cps = [
        ("rA", {"volume_in_usd": 0.0, "volume_out_usd": 0.0,
                "volume_in_drops": 1, "volume_out_drops": 0}),
        ("rB", {"volume_in_usd": 0.0, "volume_out_usd": 0.0,
                "volume_in_drops": 3, "volume_out_drops": 0}),
    ]
    assert wd._top_counterparty_by_value(cps) == (1, 75.0)


def test_template_renders_the_top_by_value_label():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "templates", "wallet.html"), encoding="utf-8") as f:
        src = f.read()
    assert "{{ _('top by value:') }}" in src
    assert "{{ _('top:') }}" not in src, "bare 'top:' label still present"


# ── 2. issuer-only capability pills ──────────────────────────────────────

ISSUER_ONLY = {"Freeze capability", "Clawback capability", "Transfer fee"}


def _issuer_ish_account_data():
    """Flags that make _capability_signals emit all three issuer pills
    plus two account-security pills that must never be filtered."""
    return {
        "Flags": FLAG_CLAWBACK | FLAG_DISABLE_MASTER,
        "TransferRate": 1_005_000_000,          # 0.5%
        "RegularKey": "rREGULARkeyAAAAAAAAAAAAAAAAAAAAAA",
    }


def test_capability_signals_emits_all_three_before_filtering():
    """Guard the guard: if the source list stops containing these pills
    the filter tests below prove nothing."""
    labels = {c["label"] for c in _capability_signals(_issuer_ish_account_data())}
    assert ISSUER_ONLY <= labels, f"source list missing issuer pills: {labels}"


def test_issuer_pills_hidden_for_a_plain_wallet():
    """rK21t6's shape: nothing issued, no trustlines."""
    caps = _capability_signals(_issuer_ish_account_data())
    out = wd._filter_issuer_only_capabilities(caps, [], 0)
    labels = {c["label"] for c in out}
    assert not (labels & ISSUER_ONLY), f"issuer pills still shown: {labels}"


def test_account_security_pills_are_never_filtered():
    caps = _capability_signals(_issuer_ish_account_data())
    out = wd._filter_issuer_only_capabilities(caps, [], 0)
    labels = {c["label"] for c in out}
    assert "Master key" in labels
    assert "RegularKey" in labels


def test_issuer_pills_kept_for_a_real_issuer():
    caps = _capability_signals(_issuer_ish_account_data())
    out = wd._filter_issuer_only_capabilities(
        caps, [{"currency": "USD", "holders": 3}], 0)
    assert [c["label"] for c in out] == [c["label"] for c in caps]
    assert ISSUER_ONLY <= {c["label"] for c in out}


def test_issuer_pills_kept_when_the_account_holds_trustlines():
    """A trustline makes the freeze pill meaningful even with nothing
    issued, so BOTH conditions are required to filter."""
    caps = _capability_signals(_issuer_ish_account_data())
    out = wd._filter_issuer_only_capabilities(caps, [], 4)
    assert ISSUER_ONLY <= {c["label"] for c in out}


def test_no_copy_edits_surviving_pills_are_identical_objects():
    """The filter chooses which dicts appear; it never rewrites one."""
    caps = _capability_signals(_issuer_ish_account_data())
    out = wd._filter_issuer_only_capabilities(caps, [], 0)
    for c in out:
        assert any(c is original for original in caps), "a pill was rebuilt"
    kept = [c for c in caps if c["label"] not in ISSUER_ONLY]
    assert out == kept


def test_filter_is_a_noop_on_an_account_with_no_issuer_pills():
    caps = _capability_signals({"Flags": FLAG_DISABLE_MASTER | FLAG_NO_FREEZE})
    out = wd._filter_issuer_only_capabilities(caps, [], 0)
    assert "Master key" in {c["label"] for c in out}
    # the no-freeze variant is still a Freeze pill, so it is filtered
    assert "Freeze capability" not in {c["label"] for c in out}


def test_global_freeze_variant_is_also_filtered():
    """All three Freeze states share the label, so none can leak through."""
    caps = _capability_signals({"Flags": FLAG_GLOBAL_FREEZE})
    assert {c["label"] for c in caps} == {"Freeze capability"}
    assert wd._filter_issuer_only_capabilities(caps, [], 0) == []


def test_none_trustline_count_is_treated_as_zero():
    caps = _capability_signals(_issuer_ish_account_data())
    out = wd._filter_issuer_only_capabilities(caps, [], None)
    assert not ({c["label"] for c in out} & ISSUER_ONLY)


def test_check_and_token_surfaces_are_untouched():
    """The filter lives at the wallet_data call site only. check_data's
    own function must still return the unfiltered list -- that is what
    /token and /check render.
    """
    caps = _capability_signals(_issuer_ish_account_data())
    assert ISSUER_ONLY <= {c["label"] for c in caps}
    import check_data
    src_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "check_data.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    assert "_ISSUER_ONLY_CAPABILITY_LABELS" not in src, (
        "the filter leaked into check_data.py, which /token and /check use")
    assert hasattr(check_data, "_capability_signals")

"""The largest counterparty must not be ranked out of the /wallet graph.

Incident (2026-10-30), measured live on
rK21t6nGLy4MNDqhvy3pGwcLFy25qgfzMx:

    distinct counterparties BEFORE cut: 10   (TOP_N_COUNTERPARTIES = 8)
    every single counterparty had tx_count = 1
    ...
     9. rDQSFBVK7Nrh2tTC..  in=          0.0
    10. rDAE53VfMvftPB4o..  in=185,017,485.5   <== cut
    VERDICT: RANKED OUT by the top-N cut

`sorted(by_addr.items(), key=lambda x: -x[1]["tx_count"])` was a TOTAL TIE
across all ten, so Python's stable sort fell back to dict insertion order.
`account_tx` returns newest-first, so the wallet's OLDEST counterparty
sorted last and was cut first -- and the oldest is typically the funding
payment, i.e. the most significant one. Five counterparties with 0.0 XRP
of volume made the cut while a 185,017,485.5 XRP inflow was dropped.

These tests are hermetic: synthetic account_tx envelopes, no node, no DB,
no network. They prove the RANKING LOGIC admits a single-transaction
high-value counterparty -- which is the durable guarantee. (The live
confirmation on the real wallet was a one-off read, not something CI can
or should repeat.)
"""

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wallet_data as wd  # noqa: E402


OWNER = "rK21t6nGLy4MNDqhvy3pGwcLFy25qgfzMx"
BIG = "rDAE53VfMvftPB4ogpWGWvzkQxfht6JPxr"          # 185,017,485.5 XRP, 1 tx
RIPPLE_EPOCH = wd.RIPPLE_EPOCH


def _payment(sender, drops, ripple_date):
    """An inbound XRP Payment envelope in account_tx shape."""
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
    """A zero-value counterparty: someone trustlines TO us (we're issuer).
    This is the shape of the five 0.0-volume rows in the live measurement.
    """
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
    """Reproduce the measured wallet: 10 counterparties, ALL tx_count=1,
    newest-first, with the 185M inflow OLDEST (hence last-inserted)."""
    now_ripple = int(time.time()) - RIPPLE_EPOCH
    txs = []
    # five zero-volume trustsets, newest
    for i in range(5):
        txs.append(_trustset(f"rZero{i}AAAAAAAAAAAAAAAAAAAAAAAAAAAA", now_ripple - 60 * (i + 1)))
    # four mid-size inflows
    for i, drops in enumerate([14_693_052_500_000, 13_140_680_400_000,
                               11_955_157_600_000, 8_655_567_100_000]):
        txs.append(_payment(f"rMid{i}BBBBBBBBBBBBBBBBBBBBBBBBBBBBB", drops,
                            now_ripple - 3600 * (i + 1)))
    # the big one LAST == oldest, exactly as account_tx returned it
    txs.append(_payment(BIG, 185_017_485_500_000, now_ripple - 14 * 3600))
    return txs


# ── the regression ───────────────────────────────────────────────────────

def test_largest_counterparty_survives_the_cut():
    txs = _rk21t6_shape()
    cps = wd._build_counterparty_graph(txs, OWNER, wd.LOOKBACK_DAYS)
    addrs = [a for a, _ in cps]
    assert len(cps) == wd.TOP_N_COUNTERPARTIES
    assert BIG in addrs, (
        f"the 185M XRP counterparty was ranked out; got {addrs}"
    )


def test_fixture_actually_reproduces_the_old_bug():
    """Guard the guard: under the OLD ranking this fixture must DROP the
    big counterparty. If this fails the fixture no longer reproduces the
    incident and the test above proves nothing.
    """
    txs = _rk21t6_shape()
    by_addr = {}
    for tx in txs:
        cp = wd._tx_counterparty(tx, OWNER)
        if cp is None:
            continue
        by_addr.setdefault(cp[0], {"tx_count": 0})
        by_addr[cp[0]]["tx_count"] += 1
    old = sorted(by_addr.items(), key=lambda x: -x[1]["tx_count"])
    old_kept = [a for a, _ in old[:wd.TOP_N_COUNTERPARTIES]]
    assert len(by_addr) > wd.TOP_N_COUNTERPARTIES
    assert BIG not in old_kept, "fixture no longer reproduces the bug"


def test_all_five_large_inflows_are_shown():
    """Every counterparty that actually moved value should make the cut
    ahead of zero-volume ones."""
    txs = _rk21t6_shape()
    cps = dict(wd._build_counterparty_graph(txs, OWNER, wd.LOOKBACK_DAYS))
    movers = [a for a, r in cps.items()
              if (r["volume_in_drops"] + r["volume_out_drops"]) > 0]
    assert len(movers) == 5, f"expected all 5 value movers, got {movers}"


# ── ordering must be deterministic, never insertion order ────────────────

def test_ranking_is_independent_of_insertion_order():
    """The bug was insertion-order dependence. Shuffling the tx list must
    not change which counterparties are shown."""
    base = wd._build_counterparty_graph(_rk21t6_shape(), OWNER, wd.LOOKBACK_DAYS)
    base_set = {a for a, _ in base}
    rng = random.Random(1234)
    for _ in range(8):
        txs = _rk21t6_shape()
        rng.shuffle(txs)
        got = {a for a, _ in wd._build_counterparty_graph(txs, OWNER, wd.LOOKBACK_DAYS)}
        assert got == base_set, "ranking changed with input order"


# ── the reservation must not misfire ─────────────────────────────────────

def test_zero_value_counterparty_never_takes_a_reserved_slot():
    """Reserved slots are for significance; 0.0 is not significant. With
    only zero-volume counterparties the result is pure activity order."""
    now_ripple = int(time.time()) - RIPPLE_EPOCH
    txs = [_trustset(f"rZ{i}CCCCCCCCCCCCCCCCCCCCCCCCCCCCCC", now_ripple - 60 * i)
           for i in range(12)]
    cps = wd._build_counterparty_graph(txs, OWNER, wd.LOOKBACK_DAYS)
    assert len(cps) == wd.TOP_N_COUNTERPARTIES
    for _addr, rec in cps:
        assert rec["volume_in_drops"] + rec["volume_out_drops"] == 0


def test_busy_wallet_keeps_activity_primacy():
    """tx_count stays the PRIMARY key, so a chatty counterparty still
    outranks a quiet one. Only the reserved slots change behaviour."""
    now_ripple = int(time.time()) - RIPPLE_EPOCH
    txs = []
    for i in range(40):
        txs.append(_payment("rCHATTYdddddddddddddddddddddddddd", 1_000_000,
                            now_ripple - 10 * i))
    txs.append(_payment(BIG, 185_017_485_500_000, now_ripple - 14 * 3600))
    cps = wd._build_counterparty_graph(txs, OWNER, wd.LOOKBACK_DAYS)
    addrs = [a for a, _ in cps]
    assert addrs[0] == "rCHATTYdddddddddddddddddddddddddd"
    assert BIG in addrs, "a high-value single-tx mover must still be shown"


def test_no_cut_when_under_the_limit():
    now_ripple = int(time.time()) - RIPPLE_EPOCH
    txs = [_payment(f"rOnly{i}eeeeeeeeeeeeeeeeeeeeeeeeeee", 5_000_000,
                    now_ripple - 60 * i) for i in range(3)]
    cps = wd._build_counterparty_graph(txs, OWNER, wd.LOOKBACK_DAYS)
    assert len(cps) == 3


def test_value_helper_falls_back_to_drops_without_an_oracle():
    """With no price oracle every usd is 0.0, so drops must carry the
    ordering -- otherwise a large XRP mover ties at zero with dust."""
    big = {"volume_in_usd": 0.0, "volume_out_usd": 0.0,
           "volume_in_drops": 185_017_485_500_000, "volume_out_drops": 0}
    dust = {"volume_in_usd": 0.0, "volume_out_usd": 0.0,
            "volume_in_drops": 1, "volume_out_drops": 0}
    assert wd._counterparty_value(big) > wd._counterparty_value(dust)
    assert wd._counterparty_value({}) == (0.0, 0)


# ── direction labels ─────────────────────────────────────────────────────

def _wallet_html():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "templates", "wallet.html"), encoding="utf-8") as f:
        return f.read()


def test_direction_labels_name_both_ends():
    src = _wallet_html()
    assert "'sent ' + usdInTxt + ' to this wallet'" in src
    assert "'received ' + usdOutTxt + ' from this wallet'" in src
    assert "'sent ' + txCount + noun + ' to this wallet'" in src
    assert "'received ' + txCount + noun + ' from this wallet'" in src


def test_inverted_labels_are_gone():
    """`direction === 'in'` means the counterparty SENT to this wallet, so
    a bare "received" on its node reads backwards."""
    src = _wallet_html()
    assert "statText = 'received ' + usdInTxt;" not in src
    assert "statText = 'sent ' + usdOutTxt;" not in src
    assert "statText = 'received ' + txCount + noun;" not in src
    assert "statText = 'sent ' + txCount + noun;" not in src


def test_both_direction_case_names_the_verbs():
    src = _wallet_html()
    assert "'sent ' + (usdInTxt || '$0') + ' \u00b7 received ' + (usdOutTxt || '$0')" in src
    assert "' in \u00b7 '" not in src, "ambiguous 'in · out' phrasing still present"

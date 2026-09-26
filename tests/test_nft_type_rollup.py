"""nft_type_rollup — pure merge tests (no DB).

Founding incident 2026-09-26: `--mode summary` recomputed the all-time
COUNT(*) / GROUP BY tx_type over 7.77M nft_activity rows every 5 min and
tripped the 25 s statement cap four times in one morning. The rollup folds
only rows past an id watermark; these tests pin the merge semantics.
"""
from __future__ import annotations
import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import nft_type_rollup as R  # noqa: E402

T0 = dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc)
T1 = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.timezone.utc)
T2 = dt.datetime(2026, 9, 26, 13, 0, tzinfo=dt.timezone.utc)


def test_seed_from_empty():
    s = R.advance(R.empty_state(), [
        ("Mint", 100, 95_000_000, 107_000_000, T0, T1, 5000),
        ("AcceptOffer", 40, 96_000_000, 106_999_999, T0, T1, 4990),
    ])
    assert s["total_events"] == 140
    assert s["counts_all"] == {"Mint": 100, "AcceptOffer": 40}
    assert s["range_start_ledger"] == 95_000_000 and s["range_end_ledger"] == 107_000_000
    assert s["range_start_close"] == T0 and s["range_end_close"] == T1
    assert s["through_id"] == 5000


def test_incremental_fold_extends_only_forward():
    s0 = R.advance(R.empty_state(), [("Mint", 100, 95_000_000, 107_000_000, T0, T1, 5000)])
    s1 = R.advance(s0, [
        ("Mint", 3, 107_000_001, 107_000_050, T1, T2, 5010),
        ("Burn", 1, 107_000_020, 107_000_020, T1, T1, 5007),
    ])
    assert s1["total_events"] == 104
    assert s1["counts_all"] == {"Mint": 103, "Burn": 1}
    assert s1["range_end_ledger"] == 107_000_050 and s1["range_end_close"] == T2
    assert s1["range_start_ledger"] == 95_000_000 and s1["range_start_close"] == T0
    assert s1["through_id"] == 5010
    # input state untouched (advance returns a copy)
    assert s0["total_events"] == 100 and s0["counts_all"] == {"Mint": 100}


def test_backfill_rows_with_older_ledgers_lower_the_start_only():
    s0 = R.advance(R.empty_state(), [("Mint", 10, 100_000_000, 107_000_000, T1, T1, 900)])
    s1 = R.advance(s0, [("Mint", 5, 90_000_000, 90_000_100, T0, T0, 950)])
    assert s1["range_start_ledger"] == 90_000_000 and s1["range_start_close"] == T0
    assert s1["range_end_ledger"] == 107_000_000 and s1["range_end_close"] == T1
    assert s1["through_id"] == 950


def test_empty_delta_is_a_noop():
    s0 = R.advance(R.empty_state(), [("Mint", 10, 1, 2, T0, T1, 900)])
    assert R.advance(s0, []) == s0


def test_rewinding_watermark_is_rejected():
    s0 = R.advance(R.empty_state(), [("Mint", 10, 1, 2, T0, T1, 900)])
    with pytest.raises(ValueError):
        R.advance(s0, [("Mint", 1, 3, 3, T1, T1, 900)])


def test_equals_full_count_reports_differences():
    s = R.advance(R.empty_state(), [("Mint", 10, 1, 2, T0, T1, 900)])
    ok, diffs = R.equals_full_count(s, s)
    assert ok and diffs == []
    ok, diffs = R.equals_full_count(s, {**s, "total_events": 11, "counts_all": {"Mint": 11}})
    assert not ok and len(diffs) == 2

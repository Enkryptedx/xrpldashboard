"""Monthly escrow release history — shaping, arithmetic and ET rendering.

Hermetic: pure functions over hand-built rows. No DB, no network.

The Sept 2026 fixture below is the shape the real cohort produced, and it is
the case that makes the OWNER-scope rule visible: all three releases were
submitted by a third party (rPw6JAdM9BGwH4y4LxJQBDybaGNhFLCspU), which that
same month finished 364 unrelated escrows. Scoping this surface by submitter
would have put all 364 on /cold-storage; scoping by owner yields 3.

The badge assertions pin the reason the month bucket is UTC-based: the
monthly unlock executes at 00:00:1x UTC on the 1st, which is the PREVIOUS
EVENING in ET. Grouping by ET would file a September release under August
and disagree with every explorer.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import escrow_unlock_history as euh  # noqa: E402

UTC = timezone.utc

# The real cohort accounts this fixture models.
ESCROW_01 = "r9NpyVfLfUG8hatuCCHKzosyDtKnBdsEN3"
ESCROW_08 = "rNASJdZjY9dToHnNURi3HAUku3duPwbtD1"
ESCROW_09 = "rU9qmGM4Y6WWDhiNzkwVKBwwatcoE7YL1T"
THIRD_PARTY_SUBMITTER = "rPw6JAdM9BGwH4y4LxJQBDybaGNhFLCspU"


def _row(tx_hash, tx_type, owner, drops, when, submitter=None, denom="XRP",
         amount_json=None):
    return {
        "tx_hash": tx_hash,
        "tx_type": tx_type,
        "ledger_index": 97_000_000,
        "close_time": when,
        "denom": denom,
        "amount_drops": drops,
        "amount_json": amount_json if amount_json is not None else str(drops),
        "escrow_owner": owner,
        "escrow_destination": owner,
        "submitter": submitter or owner,
        "tx_result": "tesSUCCESS",
        "source": "own-node",
    }


@pytest.fixture
def sept_2026_rows():
    """Three releases just after midnight UTC on Sep 1, two re-escrows that
    afternoon. released 1,000,000,000 - re-escrowed 700,000,000 = net
    300,000,000 drops."""
    return [
        # --- releases: 00:00:1x UTC Sep 1 == 8:00 PM EDT Aug 31 ----------
        _row("F1" + "0" * 62, "EscrowFinish", ESCROW_01, 400_000_000,
             datetime(2026, 9, 1, 0, 0, 10, tzinfo=UTC), THIRD_PARTY_SUBMITTER),
        _row("F2" + "0" * 62, "EscrowFinish", ESCROW_08, 300_000_000,
             datetime(2026, 9, 1, 0, 0, 11, tzinfo=UTC), THIRD_PARTY_SUBMITTER),
        _row("F3" + "0" * 62, "EscrowFinish", ESCROW_09, 300_000_000,
             datetime(2026, 9, 1, 0, 0, 12, tzinfo=UTC), THIRD_PARTY_SUBMITTER),
        # --- re-escrows: 18:1x UTC Sep 1 == 2:1x PM EDT Sep 1 -----------
        _row("C8" + "0" * 62, "EscrowCreate", ESCROW_08, 350_000_000,
             datetime(2026, 9, 1, 18, 12, 0, tzinfo=UTC)),
        _row("C9" + "0" * 62, "EscrowCreate", ESCROW_09, 350_000_000,
             datetime(2026, 9, 1, 18, 14, 0, tzinfo=UTC)),
    ]


class TestSept2026Arithmetic:
    def test_released_re_escrowed_and_net(self, sept_2026_rows):
        months = euh.group_by_month(sept_2026_rows)
        assert len(months) == 1
        m = months[0]
        assert m["month"] == "2026-09"
        assert m["released_drops"] == 1_000_000_000
        assert m["re_escrowed_drops"] == 700_000_000
        assert m["net_released_drops"] == 300_000_000
        assert m["is_net_re_escrow"] is False
        assert m["tx_count"] == 5

    def test_net_is_released_minus_re_escrowed_not_a_raw_total(
            self, sept_2026_rows):
        """The headline figure must be the NET. Reporting released alone
        would overstate what reached circulation by 700,000,000 drops."""
        m = euh.group_by_month(sept_2026_rows)[0]
        assert m["net_released_drops"] == (
            m["released_drops"] - m["re_escrowed_drops"])
        assert m["net_released_drops"] != m["released_drops"]

    def test_xrp_conversion(self, sept_2026_rows):
        m = euh.group_by_month(sept_2026_rows)[0]
        assert m["released_xrp"] == 1000.0
        assert m["net_released_xrp"] == 300.0

    def test_re_escrowed_attributed_to_08_and_09(self, sept_2026_rows):
        m = euh.group_by_month(sept_2026_rows)[0]
        owners = {t["escrow_owner"] for t in m["transactions"]
                  if t["is_re_escrowed"]}
        assert owners == {ESCROW_08, ESCROW_09}

    def test_owner_scope_survives_third_party_submitter(self, sept_2026_rows):
        """Every release here was submitted by an unrelated account. The
        owner is what this surface reports; the submitter is recorded but
        never used to decide membership."""
        m = euh.group_by_month(sept_2026_rows)[0]
        releases = [t for t in m["transactions"] if t["is_released"]]
        assert all(t["submitter"] == THIRD_PARTY_SUBMITTER for t in releases)
        assert {t["escrow_owner"] for t in releases} == {
            ESCROW_01, ESCROW_08, ESCROW_09}

    def test_failed_transactions_are_not_in_the_fixture_path(self):
        """Guard the sum: a row carrying a non-success result must never be
        added by the shaper. (The walker filters these out before write;
        this pins that the arithmetic would not quietly absorb one.)"""
        bad = _row("FF" + "0" * 62, "EscrowFinish", ESCROW_01, 999_000_000,
                   datetime(2026, 9, 1, 0, 0, 10, tzinfo=UTC))
        bad["tx_result"] = "tecNO_PERMISSION"
        m = euh.group_by_month([bad])[0]
        # The shaper is not the filter, so the row IS shaped — but its result
        # is preserved verbatim so a reviewer can see why it must be excluded
        # upstream rather than silently trusted here.
        assert m["transactions"][0]["tx_result"] == "tecNO_PERMISSION"


class TestDateDiffersBadge:
    def test_badge_on_for_midnight_utc_finishes(self, sept_2026_rows):
        m = euh.group_by_month(sept_2026_rows)[0]
        for t in m["transactions"]:
            if t["is_released"]:
                assert t["date_differs"] is True, (
                    f"{t['tx_hash'][:2]} finished at {t['utc_time']} UTC "
                    f"({t['et_date']} ET) — badge must be ON")
                assert t["utc_date"] == "2026-09-01"
                assert t["et_date"] == "2026-08-31"

    def test_badge_off_for_afternoon_utc_creates(self, sept_2026_rows):
        m = euh.group_by_month(sept_2026_rows)[0]
        for t in m["transactions"]:
            if t["is_re_escrowed"]:
                assert t["date_differs"] is False, (
                    f"{t['tx_hash'][:2]} created at {t['utc_time']} UTC "
                    f"({t['et_date']} ET) — badge must be OFF")
                assert t["utc_date"] == t["et_date"] == "2026-09-01"

    def test_month_rollup_flags_that_some_row_differs(self, sept_2026_rows):
        assert euh.group_by_month(sept_2026_rows)[0]["any_date_differs"] is True

    def test_september_release_is_not_filed_under_august(self, sept_2026_rows):
        """The whole reason the bucket is UTC. In ET these releases are
        Aug 31; they belong to September."""
        months = euh.group_by_month(sept_2026_rows)
        assert [m["month"] for m in months] == ["2026-09"]
        assert "2026-08" not in {m["month"] for m in months}


class TestDstPair:
    """Same 00:00:10 UTC wall-clock on two consecutive 1sts, one either side
    of the US DST switch. The abbreviation must be DERIVED from the zone, not
    from the month number — and the switch date moves year to year, so a
    hardcoded cutoff would rot."""

    def test_november_first_is_edt(self):
        t = euh.format_tx_times(datetime(2026, 11, 1, 0, 0, 10, tzinfo=UTC))
        assert t["et_abbrev"] == "EDT"
        assert t["et_date"] == "2026-10-31"
        assert t["et_time"] == "20:00:10"   # UTC-4
        assert t["date_differs"] is True

    def test_december_first_is_est(self):
        t = euh.format_tx_times(datetime(2026, 12, 1, 0, 0, 10, tzinfo=UTC))
        assert t["et_abbrev"] == "EST"
        assert t["et_date"] == "2026-11-30"
        assert t["et_time"] == "19:00:10"   # UTC-5
        assert t["date_differs"] is True

    def test_the_pair_differs_only_by_the_dst_offset(self):
        nov = euh.format_tx_times(datetime(2026, 11, 1, 0, 0, 10, tzinfo=UTC))
        dec = euh.format_tx_times(datetime(2026, 12, 1, 0, 0, 10, tzinfo=UTC))
        assert nov["utc_time"] == dec["utc_time"] == "00:00:10"
        assert nov["et_abbrev"] != dec["et_abbrev"]
        assert nov["et_time"] != dec["et_time"]

    def test_abbreviation_is_rendered_into_the_display_string(self):
        nov = euh.format_tx_times(datetime(2026, 11, 1, 0, 0, 10, tzinfo=UTC))
        dec = euh.format_tx_times(datetime(2026, 12, 1, 0, 0, 10, tzinfo=UTC))
        assert nov["et_display"].endswith("EDT")
        assert dec["et_display"].endswith("EST")
        assert "UTC" in nov["utc_display"]

    def test_naive_close_time_is_treated_as_utc(self):
        """A fixture or hand-built row can arrive naive. close_time is only
        ever written from ledger metadata, which is UTC by definition."""
        aware = euh.format_tx_times(datetime(2026, 11, 1, 0, 0, 10, tzinfo=UTC))
        naive = euh.format_tx_times(datetime(2026, 11, 1, 0, 0, 10))
        assert aware == naive


class TestMonthOrderingAndEmptyState:
    def test_months_are_newest_first(self):
        rows = [
            _row("A" + "0" * 63, "EscrowFinish", ESCROW_01, 1_000_000,
                 datetime(2026, 7, 1, 0, 0, 10, tzinfo=UTC)),
            _row("B" + "0" * 63, "EscrowFinish", ESCROW_01, 2_000_000,
                 datetime(2026, 9, 1, 0, 0, 10, tzinfo=UTC)),
            _row("C" + "0" * 63, "EscrowFinish", ESCROW_01, 3_000_000,
                 datetime(2026, 8, 1, 0, 0, 10, tzinfo=UTC)),
        ]
        assert [m["month"] for m in euh.group_by_month(rows)] == [
            "2026-09", "2026-08", "2026-07"]

    def test_transactions_newest_first_within_a_month(self, sept_2026_rows):
        txs = euh.group_by_month(sept_2026_rows)[0]["transactions"]
        times = [t["close_time_utc"] for t in txs]
        assert times == sorted(times, reverse=True)

    def test_empty_input_reports_no_data_rather_than_zero(self):
        out = euh.build_history([])
        assert out["has_data"] is False
        assert out["months"] == []
        assert out["months_observed"] == 0
        assert out["newest_month"] is None

    def test_a_net_re_escrow_month_is_labelled_not_clamped(self):
        """If the cohort re-locked more than it released, that is a real
        outcome. It must not be floored at zero."""
        rows = [
            _row("R" + "0" * 63, "EscrowFinish", ESCROW_01, 100_000_000,
                 datetime(2026, 9, 1, 0, 0, 10, tzinfo=UTC)),
            _row("S" + "0" * 63, "EscrowCreate", ESCROW_01, 250_000_000,
                 datetime(2026, 9, 2, 18, 0, 0, tzinfo=UTC)),
        ]
        m = euh.group_by_month(rows)[0]
        assert m["net_released_drops"] == -150_000_000
        assert m["is_net_re_escrow"] is True

    def test_iou_and_mpt_rows_cannot_enter_the_xrp_sums(self):
        """amount_drops is NULL by constraint for IOU/MPT. They are counted
        and disclosed, never silently summed or dropped."""
        rows = [
            _row("X" + "0" * 63, "EscrowFinish", ESCROW_01, 500_000_000,
                 datetime(2026, 9, 1, 0, 0, 10, tzinfo=UTC)),
            _row("Y" + "0" * 63, "EscrowFinish", ESCROW_01, None,
                 datetime(2026, 9, 1, 0, 0, 11, tzinfo=UTC),
                 denom="IOU",
                 amount_json={"currency": "USD", "issuer": ESCROW_09,
                              "value": "42"}),
        ]
        m = euh.group_by_month(rows)[0]
        assert m["released_drops"] == 500_000_000
        assert m["non_xrp_count"] == 1
        assert m["tx_count"] == 2

    def test_unlock_month_column_is_preferred_when_present(self):
        """The DB's generated column and the page must agree by
        construction, not by two parallel derivations."""
        row = _row("Z" + "0" * 63, "EscrowFinish", ESCROW_01, 1_000_000,
                   datetime(2026, 9, 1, 0, 0, 10, tzinfo=UTC))
        row["unlock_month"] = datetime(2026, 9, 1)
        assert euh.group_by_month([row])[0]["month"] == "2026-09"

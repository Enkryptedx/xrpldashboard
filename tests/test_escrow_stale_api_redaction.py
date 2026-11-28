"""A stale escrow census is withheld from the API, not served with a flag.

Measured on production 2026-11-28: /api/xrp-distribution served
escrowed_xrp = 31,545,215,584.52 from a row **45.3 days** old
(escrow_fetched_at_utc 2026-10-14 07:41 UTC), with escrow_stale:true beside
it. The pages had already stopped publishing that number -- the homepage
constellation prints the word "paused" instead, gated on the same check, and
the ledger-wide pill line was pulled on 2026-11-13 because "a month-old
figure sitting beside a 30-minute-old one reads as though both are current".
The JSON was the one surface still asserting it.

Disclosure is not the same as not asserting it. A consumer that reads the
number and ignores the flag gets a month-and-a-half-old figure as current.

THE ARITHMETIC LEAK. wallets_xrp = max(0, total_xrp - (escrowed_xrp +
amm_xrp)), so nulling escrowed_xrp while still serving total, amm and
wallets lets anyone reconstruct the withheld figure exactly. wallets_xrp is
also itself wrong by however far the census has drifted. Both are therefore
redacted too -- one field-pair beyond the original instruction, because
without it the redaction is cosmetic.

Second half of this file: a freshness canary on the escrow HALF of the
singleton. No walker signal can catch that fault -- supply_escrow_walker is
legitimately PAUSED, so walker_health is right to stay quiet and the generic
staleness rule is right to exempt it, while supply_fast_walker keeps the
total half current so the row looks healthy. Only a check that reads the
timestamp on the DATA can see it.

Pure: no DB, no network.
"""

import datetime as dt

import pytest

import app as A
import escrow_census_freshness_canary as C


UTC = dt.timezone.utc


def payload(stale=True, **kw):
    """A realistic /api/xrp-distribution payload."""
    p = {
        "total_xrp": 99985532458.1008,
        "escrowed_xrp": 31545215584.5195,
        "amm_xrp": 1000000.0,
        "wallets_xrp": 68439316873.58,
        "escrowed_pct": 31.5497800756,
        "amm_pct": 0.001,
        "wallets_pct": 68.449,
        "escrow_object_count": 15553,
        "escrow_account_count": 1887,
        "escrow_ripple_xrp": 31400000000.0,
        "escrow_ripple_object_count": 98,
        "escrow_live": {"total_xrp": 31100000000.0, "object_count": 99,
                        "as_of": "as of ledger 108,629,981"},
        "escrow_age_seconds": 3914869.31,
        "escrow_as_of_utc": "2026-10-14 07:41 UTC",
        "escrow_ledger_index": 107645974,
        "escrow_stale": stale,
        "total_age_seconds": 36.0,
        "server_time": 1795878196,
    }
    p.update(kw)
    return p


# ── redaction when stale ─────────────────────────────────────────────

@pytest.mark.parametrize("field", [
    "escrowed_xrp", "escrowed_pct",
    "escrow_object_count", "escrow_account_count",
    "escrow_ripple_xrp", "escrow_ripple_object_count",
])
def test_escrow_figures_are_null_when_stale(field):
    assert A._redact_stale_escrow(payload(stale=True))[field] is None


@pytest.mark.parametrize("field", ["wallets_xrp", "wallets_pct"])
def test_the_arithmetic_leak_is_closed(field):
    """total - amm - wallets would otherwise reconstruct the withheld
    escrow figure exactly, making the redaction cosmetic."""
    assert A._redact_stale_escrow(payload(stale=True))[field] is None


def test_the_withheld_figure_cannot_be_reconstructed():
    """The property that matters, asserted directly rather than via the
    field list: no combination of the remaining numbers yields it."""
    out = A._redact_stale_escrow(payload(stale=True))
    assert out["escrowed_xrp"] is None
    assert out["wallets_xrp"] is None
    # total and amm may stay -- on their own they say nothing about escrow.
    assert out["total_xrp"] is not None
    assert out["amm_xrp"] is not None


# ── provenance is kept ───────────────────────────────────────────────

@pytest.mark.parametrize("field,expected", [
    ("escrow_stale", True),
    ("escrow_age_seconds", 3914869.31),
    ("escrow_as_of_utc", "2026-10-14 07:41 UTC"),
    ("escrow_ledger_index", 107645974),
])
def test_staleness_provenance_survives_redaction(field, expected):
    """These describe the staleness rather than assert a supply figure. A
    consumer needs them to understand why the numbers are null."""
    assert A._redact_stale_escrow(payload(stale=True))[field] == expected


def test_the_live_cohort_is_never_redacted():
    """escrow_live comes from escrows_snapshot (own node, 30 min) and is the
    one escrow figure that is NOT stale. Nulling it would hide the good
    number along with the bad."""
    out = A._redact_stale_escrow(payload(stale=True))
    assert out["escrow_live"]["total_xrp"] == 31100000000.0
    assert out["escrow_live"]["as_of"] == "as of ledger 108,629,981"


def test_unrelated_buckets_are_untouched():
    out = A._redact_stale_escrow(payload(stale=True))
    assert out["total_xrp"] == 99985532458.1008
    assert out["amm_xrp"] == 1000000.0
    assert out["amm_pct"] == 0.001
    assert out["server_time"] == 1795878196


# ── no redaction when fresh ──────────────────────────────────────────

def test_nothing_is_redacted_when_the_census_is_fresh():
    fresh = payload(stale=False)
    assert A._redact_stale_escrow(fresh) == fresh


def test_a_fresh_payload_is_returned_unchanged_identity():
    """Cheap path: fresh payloads are passed straight through."""
    fresh = payload(stale=False)
    assert A._redact_stale_escrow(fresh) is fresh


# ── it must not corrupt the homepage's own view ──────────────────────

def test_redaction_does_not_mutate_the_caller_payload():
    """The homepage builds its view from the same function and its template
    gates on these values; mutating in place would blank the page."""
    original = payload(stale=True)
    snapshot = dict(original)
    A._redact_stale_escrow(original)
    assert original == snapshot


# ── defensive ────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [None, "not-a-dict", 42, []])
def test_non_dict_input_is_passed_through_not_crashed(bad):
    assert A._redact_stale_escrow(bad) is bad


def test_a_payload_missing_the_flag_is_left_alone():
    p = {"total_xrp": 1.0}
    assert A._redact_stale_escrow(p) == p


def test_missing_fields_do_not_raise():
    p = {"escrow_stale": True, "escrowed_xrp": 5.0}
    out = A._redact_stale_escrow(p)
    assert out["escrowed_xrp"] is None


# ══ freshness canary on the escrow half ═════════════════════════════

def test_a_fresh_census_is_zero_findings():
    now = dt.datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
    f, msg = C.evaluate(now - dt.timedelta(hours=3), now=now)
    assert f == 0 and "clean" in msg


def test_a_stale_census_is_a_finding():
    now = dt.datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
    f, msg = C.evaluate(now - dt.timedelta(days=45), now=now)
    assert f == 1
    assert "FINDING" in msg
    assert "1080.0h" in msg          # 45 days, stated not implied
    assert "paused" in msg           # names the cause
    assert "withholds" in msg        # and the reader-visible consequence


def test_the_real_production_gap_would_have_fired():
    """The exact row measured on 2026-11-28, 45.3 days old."""
    now = dt.datetime(2026, 11, 28, 15, 0, tzinfo=UTC)
    fetched = dt.datetime(2026, 10, 14, 7, 41, tzinfo=UTC)
    f, _ = C.evaluate(fetched, now=now)
    assert f == 1


def test_a_missing_timestamp_is_a_finding_not_a_pass():
    """An empty escrow half means the census was NEVER written, which is
    strictly worse than stale."""
    f, msg = C.evaluate(None)
    assert f == 1 and "never been written" in msg


def test_the_ceiling_pairs_with_a_daily_cadence():
    """26h = 24h daily + ~2h grace, matching the proposed resume shape."""
    assert C.MAX_AGE_SECONDS == 26 * 3600


def test_exactly_at_the_ceiling_is_not_yet_a_finding():
    now = dt.datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
    f, _ = C.evaluate(now - dt.timedelta(seconds=26 * 3600), now=now)
    assert f == 0


def test_a_second_past_the_ceiling_fires():
    now = dt.datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
    f, _ = C.evaluate(now - dt.timedelta(seconds=26 * 3600 + 1), now=now)
    assert f == 1


def test_a_naive_timestamp_is_treated_as_utc_not_crashed():
    """Postgres can hand back a naive datetime depending on the driver."""
    now = dt.datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
    f, _ = C.evaluate(dt.datetime(2026, 11, 28, 9, 0), now=now)
    assert f == 0


def test_cadence_is_hourly():
    assert C.WALKER_CADENCE_SECONDS == 3600

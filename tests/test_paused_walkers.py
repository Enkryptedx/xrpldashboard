"""A deliberately paused walker must report as PAUSED, never as FAILING.

Incident: `supply_escrow_walker` was paused 2026-10-15 for the escrow
incremental redesign (owner-gated "stage 4"). The project pause
convention leaves `last_run_ok=false` and `consecutive_failures=3` on the
row on purpose -- closing it via `write_walker_health_end` would either
fake a `last_success_at` or manufacture a failure. The 8 PM evening
summary read those two columns as "failing" and reported a 15-day outage
every night for 15 nights (Oct 14-29 2026) on a pause that was intended
and tracked. That is the alert-fatigue failure mode: the one real signal
in the bucket stopped being read.

No DB, no network.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paused_walkers as pw  # noqa: E402


# ── the regression that caused this ──────────────────────────────────────

DECL = {
    "paused_since": "2026-10-15",
    "reason": "a stated reason",
    "tracking": "escrow stage 4 (owner-gated)",
    "coverage": "no current coverage; full-ledger escrow census is unmeasured "
                "pending stage 4",
}


import pytest  # noqa: E402


@pytest.fixture
def paused(monkeypatch):
    """An injected paused walker.

    PAUSED_WALKERS is EMPTY as of 2026-11-28 -- supply_escrow_walker resumed
    on a daily schedule. The mechanism must still be guarded: if a future
    pause stopped classifying as PAUSED, the next one reports as a failure
    every night, which is exactly the 15-night alert-fatigue incident this
    file exists to prevent. So these tests exercise the behaviour with an
    injected declaration rather than asserting a pause that is over.
    """
    monkeypatch.setitem(pw.PAUSED_WALKERS, "a_paused_walker", dict(DECL))
    return "a_paused_walker"


def test_the_exact_incident_row_classifies_as_paused(paused):
    """The incident row shape: ok=False, 3 consecutive failures -- what the
    evening summary mis-reported for 15 nights."""
    assert pw.classify(
        paused, last_run_ok=False, consecutive_failures=3
    ) == pw.PAUSED


def test_nothing_is_paused_any_more():
    assert pw.PAUSED_WALKERS == {}
    assert not pw.is_paused("supply_escrow_walker")


def test_supply_escrow_walker_is_recorded_as_resumed():
    r = pw.RESUMED["supply_escrow_walker"]
    assert r["paused_since"] == "2026-10-15"
    assert r["resumed_on"] == "2026-11-28"


def test_stage_4_is_redefined_as_a_schedule_not_a_rewrite():
    """It was referenced for six weeks with no spec anywhere in the repo.
    Measurement replaced it: the walk never needed to be incremental."""
    s = pw.RESUMED["supply_escrow_walker"]["stage_4"]
    assert "REDEFINED 2026-11-28" in s
    assert "once a day" in s
    assert "71.2 pages/s" in s


def test_the_resume_records_its_slot_and_cap():
    r = pw.RESUMED["supply_escrow_walker"]
    assert "Hour=6" in r["resumed_as"] and "Minute=30" in r["resumed_as"]
    assert "CADENCE_SECONDS=86400" in r["resumed_as"]
    assert "ENUM_HARD_CAP_SECONDS=3600" in r["resumed_as"]
    assert "RunAtLoad" in r["resumed_as"]
    assert "pg_backup" in r["slot_rationale"]


def test_the_old_declaration_fields_still_work_for_a_pause(paused):
    d = pw.declaration(paused)
    assert d["paused_since"] == "2026-10-15"
    assert "stage 4" in d["tracking"]
    # 2026-11-04: was replacement == "escrow_seed_walk". That claim died when
    # escrow_seed_walk was RETIRED on 11-02, and it was always generous --
    # escrow_seed_walk was the stage-2 one-shot, not a standing census.
    # Nothing performs the ledger-wide ledger_data(type=escrow) enumeration
    # this walker was paused from, so the declaration now states an absence.
    assert "replacement" not in d, (
        "a named replacement asserts a working substitute; none exists")
    assert d["coverage"] == (
        "no current coverage; full-ledger escrow census is unmeasured "
        "pending stage 4")


# ── a pause must never mask a real failure ───────────────────────────────

def test_undeclared_walker_with_failed_run_is_failing():
    assert pw.classify(
        "token_issuer_flags_walker", last_run_ok=False, consecutive_failures=1
    ) == pw.FAILING


def test_undeclared_walker_with_consecutive_failures_is_failing():
    assert pw.classify(
        "some_walker", last_run_ok=True, consecutive_failures=4
    ) == pw.FAILING


def test_paused_state_is_reachable_only_through_the_register():
    """No argument other than the name can produce PAUSED.

    Guards the masking risk: if a future edit let a message or a NULL
    cadence imply paused, an undeclared broken walker would go quiet.
    """
    for ok in (True, False, None):
        for cf in (0, 1, 99, None):
            for stale in (True, False):
                state = pw.classify("definitely_not_declared", ok, cf, stale)
                assert state != pw.PAUSED


def test_classify_takes_no_message_argument():
    """Explicit register, not a string match on last_run_message."""
    params = set(inspect.signature(pw.classify).parameters)
    assert params == {
        "walker_name", "last_run_ok", "consecutive_failures", "is_stale"
    }
    assert "message" not in params
    assert "last_run_message" not in params


# ── the other states still work ──────────────────────────────────────────

def test_healthy_walker_is_ok():
    assert pw.classify("escrow_walker", True, 0) == pw.OK


def test_stale_without_failure_is_stale():
    assert pw.classify("escrow_walker", True, 0, is_stale=True) == pw.STALE


def test_failing_outranks_stale():
    assert pw.classify("escrow_walker", False, 1, is_stale=True) == pw.FAILING


# ── report bucketing ─────────────────────────────────────────────────────

def test_summarise_buckets_dict_rows(paused):
    rows = [
        {"walker_name": "a_paused_walker", "last_run_ok": False,
         "consecutive_failures": 3},
        {"walker_name": "token_issuer_flags_walker", "last_run_ok": False,
         "consecutive_failures": 1},
        {"walker_name": "escrow_walker", "last_run_ok": True,
         "consecutive_failures": 0},
    ]
    out = pw.summarise(rows)
    assert len(out[pw.PAUSED]) == 1
    assert out[pw.PAUSED][0].startswith("a_paused_walker")
    assert len(out[pw.FAILING]) == 1
    assert out[pw.FAILING][0].startswith("token_issuer_flags_walker")
    assert out[pw.STALE] == []
    # the healthy walker is not reported at all
    assert not any("escrow_walker (" in s for s in out[pw.FAILING])


def test_summarise_accepts_sequence_rows(paused):
    out = pw.summarise([("a_paused_walker", False, 3, False)])
    assert len(out[pw.PAUSED]) == 1


def test_paused_rows_are_never_silently_dropped(paused):
    """A pause must stay VISIBLE -- reporting it as nothing is how a
    paused walker becomes a permanent blind spot (tools/l1_pager.py
    records supply_escrow_walker wedged 17.5h with nothing watching it).
    """
    out = pw.summarise([
        {"walker_name": "a_paused_walker", "last_run_ok": False,
         "consecutive_failures": 3},
    ])
    assert out[pw.PAUSED], "paused walker vanished from the summary"
    assert "stage 4" in out[pw.PAUSED][0]


def test_describe_names_the_tracking_and_coverage(paused):
    s = pw.describe(paused)
    assert "paused since 2026-10-15" in s
    assert "stage 4" in s
    # The report must not imply a substitute is running.
    assert "no current coverage" in s
    assert "covered by" not in s, s
    assert "escrow_seed_walk" not in s, s


# ── register hygiene ─────────────────────────────────────────────────────

def test_every_declaration_carries_the_required_fields():
    for name, d in pw.PAUSED_WALKERS.items():
        assert d.get("paused_since"), f"{name} missing paused_since"
        assert d.get("reason"), f"{name} missing reason"
        assert d.get("tracking"), f"{name} missing tracking"
        # `coverage` is free text rendered verbatim; `replacement` is a
        # walker NAME rendered as "covered by <x>" and is only legitimate
        # when that walker genuinely does the paused walker's job. They are
        # mutually exclusive so a declaration cannot both name a substitute
        # and disclaim one.
        assert set(d) <= {
            "paused_since", "reason", "tracking", "replacement", "coverage"
        }, f"{name} has unexpected keys"
        assert not (d.get("replacement") and d.get("coverage")), (
            f"{name} declares both replacement and coverage")


def test_declaration_returns_a_copy(paused):
    """Callers must not be able to mutate the register."""
    d = pw.declaration(paused)
    d["tracking"] = "mutated"
    assert pw.PAUSED_WALKERS[paused]["tracking"] != "mutated"


def test_unknown_walker_has_no_declaration():
    assert pw.declaration("no_such_walker") is None
    assert not pw.is_paused("no_such_walker")

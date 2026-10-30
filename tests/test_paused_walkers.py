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

def test_the_exact_incident_row_classifies_as_paused():
    """supply_escrow_walker's real row: ok=False, 3 consecutive failures.

    This is the row the evening summary mis-reported for 15 nights.
    """
    assert pw.classify(
        "supply_escrow_walker", last_run_ok=False, consecutive_failures=3
    ) == pw.PAUSED


def test_supply_escrow_walker_is_declared():
    assert pw.is_paused("supply_escrow_walker")
    d = pw.declaration("supply_escrow_walker")
    assert d["paused_since"] == "2026-10-15"
    assert "stage 4" in d["tracking"]
    assert d["replacement"] == "escrow_seed_walk"


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

def test_summarise_buckets_dict_rows():
    rows = [
        {"walker_name": "supply_escrow_walker", "last_run_ok": False,
         "consecutive_failures": 3},
        {"walker_name": "token_issuer_flags_walker", "last_run_ok": False,
         "consecutive_failures": 1},
        {"walker_name": "escrow_walker", "last_run_ok": True,
         "consecutive_failures": 0},
    ]
    out = pw.summarise(rows)
    assert len(out[pw.PAUSED]) == 1
    assert out[pw.PAUSED][0].startswith("supply_escrow_walker")
    assert len(out[pw.FAILING]) == 1
    assert out[pw.FAILING][0].startswith("token_issuer_flags_walker")
    assert out[pw.STALE] == []
    # the healthy walker is not reported at all
    assert not any("escrow_walker (" in s for s in out[pw.FAILING])


def test_summarise_accepts_sequence_rows():
    out = pw.summarise([("supply_escrow_walker", False, 3, False)])
    assert len(out[pw.PAUSED]) == 1


def test_paused_rows_are_never_silently_dropped():
    """A pause must stay VISIBLE -- reporting it as nothing is how a
    paused walker becomes a permanent blind spot (tools/l1_pager.py
    records supply_escrow_walker wedged 17.5h with nothing watching it).
    """
    out = pw.summarise([
        {"walker_name": "supply_escrow_walker", "last_run_ok": False,
         "consecutive_failures": 3},
    ])
    assert out[pw.PAUSED], "paused walker vanished from the summary"
    assert "stage 4" in out[pw.PAUSED][0]


def test_describe_names_the_tracking_and_replacement():
    s = pw.describe("supply_escrow_walker")
    assert "paused since 2026-10-15" in s
    assert "stage 4" in s
    assert "escrow_seed_walk" in s


# ── register hygiene ─────────────────────────────────────────────────────

def test_every_declaration_carries_the_required_fields():
    for name, d in pw.PAUSED_WALKERS.items():
        assert d.get("paused_since"), f"{name} missing paused_since"
        assert d.get("reason"), f"{name} missing reason"
        assert d.get("tracking"), f"{name} missing tracking"
        assert set(d) <= {
            "paused_since", "reason", "tracking", "replacement"
        }, f"{name} has unexpected keys"


def test_declaration_returns_a_copy():
    """Callers must not be able to mutate the register."""
    d = pw.declaration("supply_escrow_walker")
    d["tracking"] = "mutated"
    assert pw.PAUSED_WALKERS["supply_escrow_walker"]["tracking"] != "mutated"


def test_unknown_walker_has_no_declaration():
    assert pw.declaration("no_such_walker") is None
    assert not pw.is_paused("no_such_walker")

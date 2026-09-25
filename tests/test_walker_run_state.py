"""Tests for the walker_health tri-state convention (Charlie 2026-09-25):
run_in_progress != ok=false.

walker_health stores last_run_ok=False for BOTH the in-flight marker
(start-of-run) and the failure marker (end-of-run failure). A long-running
walker (supply_escrow_walker: ~14.7min full-ledger walk on a 15min cadence)
therefore shows ok=False almost continuously and reads as 'failed' to any
glance. db._walker_run_state derives an honest tri-state without changing the
stored columns.
"""
import datetime as dt

import db


def test_running_when_started_no_end():
    # start-of-run: ok=False, completed IS NULL, message == 'run_in_progress'
    assert db._walker_run_state(False, None, "run_in_progress") == "running"


def test_ok_when_last_run_ok_true():
    assert db._walker_run_state(True, dt.datetime.now(dt.timezone.utc), "done") == "ok"
    # ok wins even if completed is somehow None
    assert db._walker_run_state(True, None, "run_in_progress") == "ok"


def test_failed_when_completed_with_failure():
    # real failure: ok=False AND a completed run
    assert db._walker_run_state(False, dt.datetime.now(dt.timezone.utc), "boom") == "failed"


def test_failed_when_null_completed_but_not_sentinel():
    # ok=False, no completion, but message is NOT the in-progress sentinel →
    # treat as failed (don't let an arbitrary message masquerade as running)
    assert db._walker_run_state(False, None, "some crash message") == "failed"
    assert db._walker_run_state(False, None, None) == "failed"
    assert db._walker_run_state(False, None, "") == "failed"


def test_run_state_present_in_reads():
    # read_walker_health / read_walker_health_all must include run_state when PG
    # is available; skip cleanly without a DB.
    if not db.pg_available():
        import pytest
        pytest.skip("no DB")
    rows = db.read_walker_health_all()
    for r in rows:
        assert "run_state" in r
        assert r["run_state"] in ("ok", "running", "failed")

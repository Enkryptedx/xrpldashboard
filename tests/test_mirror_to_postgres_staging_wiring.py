"""rank_amms._mirror_to_postgres: writes staging always, swaps only on a
finished pass (branch amm-ranked-pools-staging-swap-2026-10-26).

Hermetic: db.replace_amm_ranked_pools_staging / swap_amm_ranked_pools_staging
/ write_heartbeat are monkeypatched to recording stubs — no DB, no network.

Why this exists: the staging table and the swap function are useless if
the one caller, rank_amms._mirror_to_postgres, still upserts straight into
the public table on every checkpoint, or swaps while a pass is still only
partway done. This pins the call sequence for all three call sites:
  * mid-pass checkpoint (state["finished_at"] is None)      -> staging only
  * end-of-pass, finished_at just set this call              -> staging + swap
  * early-return "already finished" (finished_at from a prior run) -> staging + swap
And pins that the OLD live-table writer (`replace_amm_ranked_pools`) is
never called from this path again.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
import rank_amms as ra


@pytest.fixture
def calls(monkeypatch):
    log = []
    monkeypatch.setattr(db, "replace_amm_ranked_pools_staging",
                         lambda rows: log.append(("staging", len(rows))))
    monkeypatch.setattr(db, "swap_amm_ranked_pools_staging",
                         lambda: log.append(("swap",)))
    monkeypatch.setattr(db, "write_heartbeat",
                         lambda *a, **kw: log.append(("heartbeat",)))

    def _boom(rows):
        raise AssertionError(
            "the old live-table writer must never be called from "
            "_mirror_to_postgres after the staging-swap fix")
    monkeypatch.setattr(db, "replace_amm_ranked_pools", _boom)
    # Reset the module-level failure streak so one test's outcome can't
    # bleed into the next.
    monkeypatch.setattr(ra, "_mirror_consec_failures", 0)
    return log


def _state(finished_at=None):
    return {"started_at": "t0", "finished_at": finished_at,
            "cursor": 5, "errors": 0, "skipped": 0}


def test_mid_pass_checkpoint_writes_staging_only_no_swap(calls):
    ra._mirror_to_postgres([{"amm_account": "rA"}], _state(finished_at=None),
                            indexed_count=100)
    kinds = [c[0] for c in calls]
    assert "staging" in kinds
    assert "swap" not in kinds, "a partial sweep must never be promoted"


def test_end_of_pass_writes_staging_then_swaps(calls):
    ra._mirror_to_postgres([{"amm_account": "rA"}],
                            _state(finished_at="2026-10-26T12:00:00Z"),
                            indexed_count=100)
    kinds = [c[0] for c in calls]
    assert kinds.index("staging") < kinds.index("swap"), \
        "must write the complete set to staging BEFORE promoting it"
    assert kinds.count("swap") == 1


def test_already_finished_early_return_path_also_swaps(calls):
    # Mirrors the real call site: main() calls this once more on a tick
    # where a prior run already finished and there's nothing new to rank.
    ra._mirror_to_postgres([{"amm_account": "rA"}, {"amm_account": "rB"}],
                            _state(finished_at="2026-10-25T09:00:00Z"),
                            indexed_count=100)
    kinds = [c[0] for c in calls]
    assert "swap" in kinds, "an already-complete staging table should still get promoted"


def test_staging_write_gets_the_full_ranked_list(calls):
    ranked = [{"amm_account": f"r{i}"} for i in range(37)]
    ra._mirror_to_postgres(ranked, _state(finished_at=None), indexed_count=100)
    staging_call = next(c for c in calls if c[0] == "staging")
    assert staging_call[1] == 37


def test_mirror_failure_does_not_raise(monkeypatch, calls):
    def _boom(rows):
        raise RuntimeError("neon down")
    monkeypatch.setattr(db, "replace_amm_ranked_pools_staging", _boom)
    # Must not raise -- a prod Neon outage must never block local ranking.
    ra._mirror_to_postgres([{"amm_account": "rA"}], _state(finished_at=None),
                            indexed_count=100)


def test_swap_failure_does_not_raise(monkeypatch, calls):
    def _boom():
        raise RuntimeError("neon down mid-rename")
    monkeypatch.setattr(db, "swap_amm_ranked_pools_staging", _boom)
    ra._mirror_to_postgres([{"amm_account": "rA"}],
                            _state(finished_at="2026-10-26T12:00:00Z"),
                            indexed_count=100)

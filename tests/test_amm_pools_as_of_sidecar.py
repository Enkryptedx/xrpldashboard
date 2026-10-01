"""AMM pools as_of sidecar — Charlie ruling 2026-10-01.

rank_amms.py writes amm_ranked_finished_meta.json atomically in the same
breath it promotes amm_ranked_finished.json, holding ONLY that pass's
finished_at. signed_snapshot.py reads that sidecar — never
amm_rank_state.json, whose finished_at is blank for the ~2.5h/night a
pass is running (exactly when the leaf signs).

Three assertions:
  1. Sidecar present + well-formed -> both amm_pools_count and
     amm_pools_total_tvl_usd carry as_of == sidecar's finished_at.
  2. Sidecar missing -> as_of is None on both metrics, and
     "amm_pools_as_of: <ErrorType>" lands in errors. The count/TVL
     metrics are still emitted (sidecar failure gates only as_of, not
     the metric itself).
  3. Sidecar present but malformed JSON -> same fail-closed behavior as
     (2), never a fallback read of amm_rank_state.json.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _write_ranked_finished(path, n=3):
    pools = [
        {"account": f"r{i}", "tvl_usd": 100.0 * (i + 1)}
        for i in range(n)
    ]
    with open(path, "w") as f:
        json.dump(pools, f)
    return pools


def test_as_of_present_when_sidecar_present(tmp_path, monkeypatch):
    import signed_snapshot as ss

    finished_path = tmp_path / "amm_ranked_finished.json"
    meta_path = tmp_path / "amm_ranked_finished_meta.json"
    _write_ranked_finished(finished_path, n=3)
    with open(meta_path, "w") as f:
        json.dump({"finished_at": "2026-10-01T02:38:50Z"}, f)

    monkeypatch.setattr(ss, "AMM_RANKED_FINISHED_PATH", str(finished_path))
    monkeypatch.setattr(ss, "AMM_RANKED_FINISHED_META_PATH", str(meta_path))

    metrics, errors = ss.collect_metrics()
    amm = {m["name"]: m for m in metrics if m["name"] in ("amm_pools_count", "amm_pools_total_tvl_usd")}
    assert amm["amm_pools_count"]["as_of"] == "2026-10-01T02:38:50Z"
    assert amm["amm_pools_total_tvl_usd"]["as_of"] == "2026-10-01T02:38:50Z"
    assert not [e for e in errors if "amm_pools_as_of" in e]


def test_as_of_null_and_logged_when_sidecar_missing(tmp_path, monkeypatch):
    import signed_snapshot as ss

    finished_path = tmp_path / "amm_ranked_finished.json"
    meta_path = tmp_path / "amm_ranked_finished_meta.json"  # never written
    _write_ranked_finished(finished_path, n=3)

    monkeypatch.setattr(ss, "AMM_RANKED_FINISHED_PATH", str(finished_path))
    monkeypatch.setattr(ss, "AMM_RANKED_FINISHED_META_PATH", str(meta_path))

    metrics, errors = ss.collect_metrics()
    amm = {m["name"]: m for m in metrics if m["name"] in ("amm_pools_count", "amm_pools_total_tvl_usd")}
    # Fail closed on as_of only — the metrics themselves are still emitted.
    assert amm["amm_pools_count"]["as_of"] is None
    assert amm["amm_pools_total_tvl_usd"]["as_of"] is None
    assert amm["amm_pools_count"]["value"] == 3
    assert any(e.startswith("amm_pools_as_of:") for e in errors), errors


def test_as_of_null_and_logged_when_sidecar_malformed(tmp_path, monkeypatch):
    import signed_snapshot as ss

    finished_path = tmp_path / "amm_ranked_finished.json"
    meta_path = tmp_path / "amm_ranked_finished_meta.json"
    _write_ranked_finished(finished_path, n=2)
    with open(meta_path, "w") as f:
        f.write("{not valid json")

    monkeypatch.setattr(ss, "AMM_RANKED_FINISHED_PATH", str(finished_path))
    monkeypatch.setattr(ss, "AMM_RANKED_FINISHED_META_PATH", str(meta_path))

    metrics, errors = ss.collect_metrics()
    amm = {m["name"]: m for m in metrics if m["name"] in ("amm_pools_count", "amm_pools_total_tvl_usd")}
    assert amm["amm_pools_count"]["as_of"] is None
    assert amm["amm_pools_total_tvl_usd"]["as_of"] is None
    assert any(e.startswith("amm_pools_as_of:") for e in errors), errors


def test_as_of_never_falls_back_to_state_file(tmp_path, monkeypatch):
    """The state file's finished_at must never be read for as_of, even if
    present and non-null — only the dedicated sidecar counts."""
    import signed_snapshot as ss

    finished_path = tmp_path / "amm_ranked_finished.json"
    meta_path = tmp_path / "amm_ranked_finished_meta.json"  # sidecar missing
    state_path = tmp_path / "amm_rank_state.json"
    _write_ranked_finished(finished_path, n=1)
    # A state file with a non-null finished_at sits right next to it —
    # if the signer ever fell back to it, as_of would wrongly show this
    # value instead of staying null.
    with open(state_path, "w") as f:
        json.dump({"finished_at": "2099-01-01T00:00:00Z"}, f)

    monkeypatch.setattr(ss, "AMM_RANKED_FINISHED_PATH", str(finished_path))
    monkeypatch.setattr(ss, "AMM_RANKED_FINISHED_META_PATH", str(meta_path))
    if hasattr(ss, "STATE_PATH"):
        monkeypatch.setattr(ss, "STATE_PATH", str(state_path))

    metrics, errors = ss.collect_metrics()
    amm = {m["name"]: m for m in metrics if m["name"] in ("amm_pools_count", "amm_pools_total_tvl_usd")}
    assert amm["amm_pools_count"]["as_of"] is None, (
        "as_of must never fall back to amm_rank_state.json's finished_at"
    )

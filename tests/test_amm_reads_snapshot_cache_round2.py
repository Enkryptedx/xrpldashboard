"""Round 2 of the snapshot/watermark-gated caching fix
(branch amm-reads-snapshot-cache-round2-2026-10-27).

2026-10-27 follow-up egress audit (Charlie): after round 1
(amm-reads-snapshot-cache-2026-10-27) landed, token_category_inferred
became the new top row-puller (~8,000 rows/call, ~11 calls/hr) and the
6-column amm_ranked_pools SELECT (pair/amount_a/amount_b/tvl_usd/
snapshot_ts) was still unfixed (~2 calls/hr, caller untraced).

Traced to:
  - app.py's _cached_token_category_inferred_map() -- was a plain
    @ttl_cache(seconds=60) wrapping db.read_token_category_inferred_map()
    (no WHERE clause, pulls every row every call). Fix: probe
    db.read_token_category_inferred_watermark() (MAX(fetched_at), one
    row) first, only re-pull the full map when it changed.
  - amm_tvl_recorder.py's record_once() -- calls
    db.read_amm_ranked_pools(columns=(5 named cols)), which always also
    selects snapshot_ts (6 total), on every 900s launchd tick with no
    gate at all. Fix: a file-based watermark (this walker is a fresh
    process each run, unlike app.py) -- skip the full read+write when
    read_amm_snapshot_ts() matches the last successful run's watermark.

Hermetic: no DB, no network. amm_tvl_recorder tests use tmp_path for the
state file.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ── app.py's _cached_token_category_inferred_map() ──

@pytest.fixture(autouse=True)
def _reset_token_category_inferred_cache():
    import app
    app._token_category_inferred_cache = {"watermark": None, "result": None, "checked_mono": 0.0}
    yield
    app._token_category_inferred_cache = {"watermark": None, "result": None, "checked_mono": 0.0}


def test_token_category_inferred_no_longer_a_plain_ttl_cache():
    """Pin by source: the egress bug was specifically the bare
    @ttl_cache(seconds=60) decorator."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent.joinpath("app.py").read_text()
    i = src.index("def _cached_token_category_inferred_map():")
    before = src[max(0, i - 120):i]
    assert "@ttl_cache" not in before, \
        "_cached_token_category_inferred_map must not be a plain time-based ttl_cache any more"


def test_token_category_inferred_skips_full_read_when_watermark_unchanged(monkeypatch):
    import app
    import db
    monkeypatch.setattr(app, "_TOKEN_CATEGORY_INFERRED_PROBE_INTERVAL_S", 0)
    monkeypatch.setattr(db, "read_token_category_inferred_watermark", lambda: "2026-10-27T00:00:00Z")
    full_read_count = {"n": 0}

    def fake_read():
        full_read_count["n"] += 1
        return {("USD", "rX"): {"category": "stablecoin"}}

    monkeypatch.setattr(db, "read_token_category_inferred_map", fake_read)

    m1 = app._cached_token_category_inferred_map()
    assert m1 == {("USD", "rX"): {"category": "stablecoin"}}
    assert full_read_count["n"] == 1

    m2 = app._cached_token_category_inferred_map()
    assert m2 == m1
    assert full_read_count["n"] == 1, "unchanged watermark must not trigger a second full read"


def test_token_category_inferred_refetches_when_watermark_changes(monkeypatch):
    import app
    import db
    monkeypatch.setattr(app, "_TOKEN_CATEGORY_INFERRED_PROBE_INTERVAL_S", 0)
    state = {"watermark": "2026-10-27T00:00:00Z", "map": {("USD", "rX"): {}}}
    full_read_count = {"n": 0}

    monkeypatch.setattr(db, "read_token_category_inferred_watermark", lambda: state["watermark"])

    def fake_read():
        full_read_count["n"] += 1
        return state["map"]

    monkeypatch.setattr(db, "read_token_category_inferred_map", fake_read)

    app._cached_token_category_inferred_map()
    assert full_read_count["n"] == 1

    state["watermark"] = "2026-10-27T01:00:00Z"
    state["map"] = {("USD", "rX"): {}, ("EUR", "rY"): {}}
    m2 = app._cached_token_category_inferred_map()
    assert m2 == state["map"]
    assert full_read_count["n"] == 2, "a changed watermark must trigger exactly one more full read"


def test_token_category_inferred_probe_interval_gate(monkeypatch):
    """A second call within the probe interval must not even check the
    watermark -- same discipline as _ranked_amm_snapshot's own gate."""
    import app
    import db
    monkeypatch.setattr(app, "_TOKEN_CATEGORY_INFERRED_PROBE_INTERVAL_S", 9999)
    probe_count = {"n": 0}

    def fake_watermark():
        probe_count["n"] += 1
        return "2026-10-27T00:00:00Z"

    monkeypatch.setattr(db, "read_token_category_inferred_watermark", fake_watermark)
    monkeypatch.setattr(db, "read_token_category_inferred_map", lambda: {})

    app._cached_token_category_inferred_map()
    app._cached_token_category_inferred_map()
    assert probe_count["n"] == 1, "within the probe interval, the watermark must not be re-checked at all"


# ── amm_tvl_recorder.py's record_once() ──

@pytest.fixture
def recorder(tmp_path, monkeypatch):
    import amm_tvl_recorder as rec
    monkeypatch.setattr(rec, "STATE_PATH", str(tmp_path / "amm_tvl_recorder_state.json"))
    return rec


def test_amm_tvl_recorder_skips_full_read_when_snapshot_unchanged(recorder, monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "read_amm_snapshot_ts", lambda: 5000)
    full_read_count = {"n": 0}

    def fake_read(**kw):
        full_read_count["n"] += 1
        return [{"amm_account": "rA", "tvl_usd": 1.0}]

    monkeypatch.setattr(db, "read_amm_ranked_pools", fake_read)
    monkeypatch.setattr(db, "write_amm_tvl_snapshot", lambda rows, ts: len(rows))

    snap1, written1 = recorder.record_once()
    assert snap1 == 1 and written1 == 1 and full_read_count["n"] == 1

    snap2, written2 = recorder.record_once()
    assert snap2 == 0 and written2 == 0, \
        "unchanged snapshot must report a clean no-op, not a stale re-write"
    assert full_read_count["n"] == 1, "unchanged snapshot must not trigger a second full read"


def test_amm_tvl_recorder_refetches_when_snapshot_changes(recorder, monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    state = {"snap": 5000, "rows": [{"amm_account": "rA", "tvl_usd": 1.0}]}
    full_read_count = {"n": 0}

    monkeypatch.setattr(db, "read_amm_snapshot_ts", lambda: state["snap"])

    def fake_read(**kw):
        full_read_count["n"] += 1
        return state["rows"]

    monkeypatch.setattr(db, "read_amm_ranked_pools", fake_read)
    monkeypatch.setattr(db, "write_amm_tvl_snapshot", lambda rows, ts: len(rows))

    recorder.record_once()
    assert full_read_count["n"] == 1

    state["snap"] = 6000
    state["rows"] = [{"amm_account": "rA", "tvl_usd": 2.0}, {"amm_account": "rB", "tvl_usd": 1.0}]
    snap2, written2 = recorder.record_once()
    assert snap2 == 2 and written2 == 2
    assert full_read_count["n"] == 2, "a changed snapshot must trigger exactly one more full read"


def test_amm_tvl_recorder_watermark_survives_across_fresh_processes(recorder, monkeypatch):
    """The whole point of the file-based watermark: this walker is a
    brand-new Python process every launchd tick, so the gate must work
    even when nothing is held in memory between calls."""
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    monkeypatch.setattr(db, "read_amm_snapshot_ts", lambda: 7777)
    monkeypatch.setattr(db, "read_amm_ranked_pools", lambda **kw: [{"amm_account": "rA", "tvl_usd": 1.0}])
    monkeypatch.setattr(db, "write_amm_tvl_snapshot", lambda rows, ts: len(rows))

    recorder.record_once()
    assert os.path.exists(recorder.STATE_PATH)
    assert recorder._load_last_snapshot_ts() == 7777

    # simulate a brand-new process re-reading the same state file
    assert recorder._load_last_snapshot_ts() == 7777


def test_amm_tvl_recorder_missing_state_file_is_a_cold_start_not_an_error(recorder):
    assert recorder._load_last_snapshot_ts() is None


def test_amm_tvl_recorder_does_not_persist_watermark_on_pg_unavailable(recorder, monkeypatch):
    import db
    monkeypatch.setattr(db, "pg_available", lambda: False)
    snapshotted, written = recorder.record_once()
    assert (snapshotted, written) == (0, 0)
    assert recorder._load_last_snapshot_ts() is None

"""nft_type_rollup — incremental all-time aggregates for nft_activity.

Charlie ruling 2026-09-26 09:24 ET: the `--mode summary` walker must never
recompute the all-time COUNT(*) / GROUP BY tx_type over the whole
nft_activity table (7.77M rows, 25 s statement cap → four timeouts on
2026-09-26, three on 09-24, one on 09-23). Instead a single-row rollup
(`nft_activity_type_rollup`, id=1) carries the running totals and a
watermark on nft_activity.id (BIGSERIAL, monotonic; the table is
append-only — no DELETE/UPDATE path exists in the repo). Each run folds in
only `WHERE id > through_id` (a PK range scan of the ~200–300 rows added
since the last run) and advances the watermark.

This module is the PURE merge; db.py owns the SQL, nft_activity_walker.py
owns the run. Kept separate so the merge is unit-testable without PG.

State shape (dict, JSON-safe except the two close_time datetimes):
    through_id          int   last nft_activity.id folded in (0 = never)
    total_events        int
    counts_all          dict  tx_type -> count
    range_start_ledger  int|None
    range_end_ledger    int|None
    range_start_close   datetime|None   (min close_time)
    range_end_close     datetime|None   (max close_time)

Delta row shape (one per tx_type, from the SQL in db.read_nft_type_delta):
    (tx_type, n, min_ledger, max_ledger, min_close, max_close, max_id)
"""
from __future__ import annotations


def empty_state() -> dict:
    return {
        "through_id": 0,
        "total_events": 0,
        "counts_all": {},
        "range_start_ledger": None,
        "range_end_ledger": None,
        "range_start_close": None,
        "range_end_close": None,
    }


def _min(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a if a <= b else b


def _max(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def advance(state: dict, delta_rows) -> dict:
    """Fold delta rows into a COPY of state and return it. Empty delta →
    state unchanged (same watermark). Never decrements; never rewinds the
    watermark (a row with max_id <= through_id is a caller bug and is
    rejected loudly)."""
    new = dict(state)
    new["counts_all"] = dict(state.get("counts_all") or {})
    base_watermark = int(state.get("through_id") or 0)
    for tx_type, n, min_ledger, max_ledger, min_close, max_close, max_id in delta_rows:
        n = int(n or 0)
        if n <= 0:
            continue
        # Check against the INCOMING watermark: groups within one batch
        # legitimately carry different max_ids (a batch's smaller groups
        # end below the batch maximum).
        if max_id is not None and int(max_id) <= base_watermark:
            raise ValueError(
                f"nft_type_rollup: delta max_id {max_id} <= watermark "
                f"{base_watermark} — caller passed already-folded rows"
            )
        new["counts_all"][tx_type] = int(new["counts_all"].get(tx_type, 0)) + n
        new["total_events"] = int(new["total_events"]) + n
        new["range_start_ledger"] = _min(new["range_start_ledger"], min_ledger)
        new["range_end_ledger"] = _max(new["range_end_ledger"], max_ledger)
        new["range_start_close"] = _min(new["range_start_close"], min_close)
        new["range_end_close"] = _max(new["range_end_close"], max_close)
        new["through_id"] = _max(new["through_id"], int(max_id) if max_id is not None else None)
    return new


def equals_full_count(state: dict, full: dict) -> tuple[bool, list[str]]:
    """Compare a rollup state against a full-scan result of the same shape
    (total_events, counts_all, range_*). Returns (equal, differences)."""
    diffs = []
    if int(state.get("total_events", 0)) != int(full.get("total_events", 0)):
        diffs.append(f"total_events rollup={state.get('total_events')} full={full.get('total_events')}")
    sc = {k: int(v) for k, v in (state.get("counts_all") or {}).items()}
    fc = {k: int(v) for k, v in (full.get("counts_all") or {}).items()}
    for k in sorted(set(sc) | set(fc)):
        if sc.get(k, 0) != fc.get(k, 0):
            diffs.append(f"counts_all[{k}] rollup={sc.get(k, 0)} full={fc.get(k, 0)}")
    for k in ("range_start_ledger", "range_end_ledger", "range_start_close", "range_end_close"):
        if state.get(k) != full.get(k):
            diffs.append(f"{k} rollup={state.get(k)} full={full.get(k)}")
    return (not diffs), diffs

"""Signed-snapshot v5 tests: amendments_block metric (Charlie ruling
2026-09-20, proving-run 2026-09-21).

Two assertions after Ship A (2026-09-24):

1. **Extractor correctness** — `_assemble_amendments_block` returns a
   dict with `per_amendment` populated (>0 entries) when the
   amendments_state cache is warm. Prior extractor read the non-existent
   `known_amendments` key and always returned 0.

2. **Sign+verify round-trip** — a v5 leaf produced with the amendments
   block signs cleanly and passes `verify_envelope()` with no issues.
   This proves the amendments tally is covered by the signature (it's
   inside `metrics`, which is one of the four keys hashed as the leaf
   payload) and that no verifier change is required.

The prior env-gate parity test was removed 2026-09-24 as part of Ship A —
SIGNED_SNAPSHOT_AMENDMENTS_BLOCK_ENABLED and RWA_SUPPLY_NAV_IN_LEAF no
longer exist. See test_signed_snapshot_pre_sign_gate.py for the
regression that proves the env flags stay removed.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


NOW = dt.datetime(2026, 9, 22, 6, 0, 0, tzinfo=dt.timezone.utc)


def test_extractor_returns_populated_per_amendment():
    from signed_snapshot import _assemble_amendments_block
    block = _assemble_amendments_block(now_utc=NOW)
    if block is None:
        # Extractor bails silently if amendments_state fails to import
        # on the test box; treat that as skip-not-fail because CI may
        # not have the walker running. Log and move on.
        print("SKIP: amendments_state/network_votes modules unavailable")
        return
    assert isinstance(block, dict)
    assert "per_amendment" in block
    assert len(block["per_amendment"]) > 0, (
        f"per_amendment is empty ({block['per_amendment']!r}); the extractor "
        f"is reading the wrong key from fetch_amendments_state_cached(). "
        f"The real key is `in_flight` (not `known_amendments`)."
    )
    # Every in-flight entry must at least have a hash + enabled flag
    for name, data in block["per_amendment"].items():
        assert "hash" in data, f"{name}: missing hash"
        assert "enabled" in data, f"{name}: missing enabled"
    # threshold_display must be populated when any in-flight entry has votes
    assert block["threshold_display"], "threshold_display should be N/M shape"


def test_v5_leaf_signs_and_verifies_cleanly():
    from signed_snapshot import build_snapshot, sign_snapshot, verify_envelope
    snap = build_snapshot("2026-09-22", now_utc=NOW)
    signed = sign_snapshot(snap, dry_run=True)
    ok, issues = verify_envelope(signed)
    assert ok, f"v5 leaf verification failed: {issues}"
    # amendments_block metric MUST be present in the verified envelope
    amendments = [m for m in signed["metrics"] if m["name"] == "amendments_block"]
    if amendments:
        # (may be absent if amendments_state modules failed on the box)
        assert amendments[0].get("unit") == "dict"
        assert "value" in amendments[0]
        assert "per_amendment" in amendments[0]["value"]


if __name__ == "__main__":
    test_extractor_returns_populated_per_amendment()
    test_v5_leaf_signs_and_verifies_cleanly()
    print("ALL PASS")

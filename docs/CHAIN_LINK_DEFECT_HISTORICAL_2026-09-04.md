# Historical chain-link defect (surfaced 2026-09-04)

## Summary

The L2 inspector alerted `snapshot_signature:chain_link_mismatch` for
2026-09-04. Investigation revealed a latent defect in
`signed_snapshot.py:sign_snapshot` that had been mis-computing
`previous_root` on every same-date re-run since inception. Six historical
signed snapshots (in addition to 09-04) carry the same defect. Those
files are already anchored on-chain via anchors #1–#4 and cannot be
re-signed without invalidating those anchors.

## What the defect looked like

`sign_snapshot` read `previous_root` from `chain["current_root"]`
**before** calling `append_or_replace_leaf`. When
`append_or_replace_leaf` REPLACED a same-date leaf (because a prior run
had already written one for the same date), `chain["current_root"]` still
held the first-attempt's chain_root, not the prior day's. The second run
therefore stored `previous_root = <first-attempt-root>`, pointing at an
intermediate chain state that was overwritten and no longer exists in
the append-only leaves array.

The result: `previous_root` on the affected file doesn't match:
- `merkle_root(leaves[0..leaf_index-1])` from the current `chain.json`
- The prior day's `chain_root`

Both mismatch checks fire in the L2 inspector.

## Files with the defect (as of 2026-09-04)

| File                | Anchored via | Notes |
|---------------------|--------------|-------|
| 2026-05-14.json     | Anchor #1    | `leaf_index=0` but `previous_root` populated (extra flavour of the same bug — a re-run on the very first day) |
| 2026-05-15.json     | Anchor #1    | double-run same-day |
| 2026-05-16.json     | Anchor #1    | double-run same-day |
| 2026-06-14.json     | Anchor #2    | double-run same-day |
| 2026-08-12.json     | Anchor #3    | double-run same-day |
| 2026-08-30.json     | Anchor #4    | double-run same-day |
| 2026-09-04.json     | (pre-anchor-#5, caught before stamp) | double-run same-day — re-signed post-fix |

Every other historical file (~100+) passes the new `chain_link` check.

## What the fix does

- `sign_snapshot` now computes `previous_root = merkle_root(all_leaves[:leaf_index])`
  after `append_or_replace_leaf` returns. This is correct for both the
  append case (index = new tail) and the replace case (index = existing
  slot). It reads the chain state as if the leaf being added didn't
  exist yet, which is the true "previous root."
- `verify_envelope` gains a fifth check: `chain_link OK` cross-references
  either (a) `chain.json` leaves 0..N-1 for a recomputed merkle root, or
  (b) the prior day's snapshot file's `chain_root`. Catches locally what
  L2 caught externally.
- `append_or_replace_leaf` returns a `replaced: bool` flag and prints a
  LOUD stderr message when replacing an existing same-date leaf, so
  future double-runs are never silent again.

## Why historical files are left as-is

Anchors #1–#4 committed the chain_roots for those days to XRPL. Re-signing
the historical files would change their leaf_hashes (metric-set drift
between then and now), which cascades merkle root changes through every
subsequent leaf. That would invalidate the roots stamped on-chain in
anchors #1–#4 — a much bigger integrity breach than the chain-link
defect itself.

The honest posture: leave the historical files with their intact
signatures, leaf_hashes, and chain_roots. Document the defect. Every
new snapshot from 09-04 onward has the fix. `verify_snapshot_file` on
any historical defective file will now surface the chain_link issue,
which is correct — the local user should see what auditors would see.

## Test coverage

`tests/test_signed_snapshot_v4.py::test_double_run_previous_root_links_to_prior_day_not_first_attempt`
reproduces the exact scenario (append day N-1, sign day N, re-sign day
N with different metric values) and asserts the fix.

`tests/test_signed_snapshot_v4.py::test_gate_4b_every_historical_v3_snapshot_verifies`
was updated to skip `chain_link` issues on historical files (documented
as expected) while still asserting the other four checks pass on every
historical file.

## Root cause of the double-run pattern

Two triggers coincided on the affected dates:
1. The daily launchd `signed_snapshot` StartInterval firing at ~13:51 EDT.
2. A manual re-invocation (either `launchctl kickstart -k` or a manual
   walker kick) happening in the same UTC day.

`launchctl kickstart -k` kills a running instance then restarts it; if
the launchd was mid-fire or fired a moment later, two full runs could
land in short succession.

## Prevention

- **Code**: the fix + loud stderr on replace makes the defect visible if
  it does recur.
- **Procedure**: the standing pre-stamp checklist gains an item — never
  schedule a manual `kickstart` for `signed_snapshot` without first
  checking the plist's next scheduled fire time and ensuring there's a
  clear window. Manual invocations should target the SCRIPT directly
  (not the launchd job) to avoid any race with the scheduler.

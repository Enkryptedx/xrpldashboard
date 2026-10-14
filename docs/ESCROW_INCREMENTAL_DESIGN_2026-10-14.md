# Escrow supply: incremental stream-tracking design

**Status:** approved 2026-10-14. Stage 1 (this PR) ships the design doc, the
escrow set table and the seed walk. Nothing is wired to the site.

## Problem

`supply_escrow_walker` recomputed XRP escrow supply by enumerating the full
ledger state tree via `ledger_data(type=escrow)`, paginated to completion,
every hour.

The cost scales with **total ledger state size, not escrow count**. Measured
2026-10-14 against our own node: **123 escrow objects in 500 pages** (~0.25
per page, `limit=400`), against a full walk of **~78,215 pages**. So roughly
**99.75% of the traversal work is discarded** — we scan the entire state tree
to find a few thousand objects.

## Why it broke

Throughput collapsed while the ledger barely moved:

| | pages | rate | full walk |
|---|---|---|---|
| 2026-09-24 sampling | 77,906 | ~93 pages/s | 13 m 54 s |
| last successful walk | 78,215 | 90.9 pages/s | 860 s |
| degraded floor | 44,512 (capped) | 23.7 pages/s | did not finish |
| 2026-10-14 measured | 500 sampled | 44.1 pages/s | ~29.6 min projected |

State grew **77,906 → 78,215 pages = +309 pages (+0.4%)** while the rate fell
by 2–4×. **Ledger growth is therefore not the cause.** At 23.7 pages/s the walk
needs ~55 min against a 60 min cadence and a 45 min hard cap, so it stopped
completing and was cut off mid-walk.

Raising the cap is not a fix: at the degraded rate the walker would be
enumerating almost continuously, which is plausibly part of what degraded the
node. That is a feedback loop.

The walker was paused 2026-10-14. The homepage shows an amber
`as of <UTC>` + `refresh paused` stamp (PR #53), so the frozen figure is
disclosed rather than presented as current.

## Design

Event-sourced escrow set, reconciled against periodic full walks.

### Seed (stage 1, this PR)

One full walk builds the authoritative escrow set: owner, destination, drops,
sequence, condition, finish-after / cancel-after, and the ledger index it was
observed at.

**The seed is pinned to a single `ledger_index N`.** Every `ledger_data` call
in the walk passes that fixed `N` — never `validated`. A walk that lets the
ledger advance underneath it produces a set that is a blend of several ledger
states: an escrow finished mid-walk can be missed or double-counted depending
on where the marker was, and the resulting total reconciles against nothing.
Pinning makes the seed a true snapshot of one ledger, which is what the delta
stream needs in order to have a defined starting point.

**The walk must finish while `N` is still in the node's retained history.**
Measured 2026-10-14: `complete_ledgers = 107568362-107658759`, a span of
**90,397 ledgers ≈ 100.4 h** at ~4 s/ledger. Against a projected ~30 min walk
at the current 44.1 pages/s that is ample margin, but it is not unlimited and
it is not guaranteed — online_delete or a node restart can shrink the window.
So the seed:

- records `N` at the start and re-checks it is still inside `complete_ledgers`
  on resume and at completion;
- if `N` has fallen out of retained history, the seed is **abandoned, not
  patched** — a partial set stitched across two pinned ledgers is exactly the
  blended-state bug above. A new `N` is chosen and the walk restarts.

**The seed is resumable.** The marker is checkpointed to the database as the
walk progresses, together with `N` and the page count. A seed interrupted by a
reap, a restart or a crash resumes from its last checkpoint against the same
`N` rather than starting over. This is what makes a ~30 min (or, at the
degraded rate, ~55 min) walk survivable without a hard cap.

**The seed runs off-peak and without the hourly cap.** It is not on the hourly
cadence and has no `ENUM_HARD_CAP_SECONDS` equivalent — the cap is what was
truncating walks mid-flight. Instead it is gated on node load the way
`census_watcher` already gates its census: proceed only while `load_factor` is
low and `server_state` is `full`/`proposing`, and yield otherwise.

### Deltas (stage 2, shadow mode)

`xrpl_stream` already subscribes to every validated transaction with no
account or type filter, and its Phase 1a novelty sensor already reads
`LedgerEntryType` from `meta.AffectedNodes` at zero additional RPC cost. The
escrow lifecycle is exactly three transaction types:

- `EscrowCreate` → `CreatedNode` of `LedgerEntryType: Escrow` → insert
- `EscrowFinish` → `DeletedNode` → delete, credit destination
- `EscrowCancel` → `DeletedNode` → delete, return to owner

Drive off `meta.AffectedNodes`, **not** the transaction body. `AffectedNodes`
carries the actual ledger mutation with exact drops in `FinalFields` /
`PreviousFields`, so variant and partial cases do not need special-casing.

**The stream applies deltas only from `N+1` onward.** Events for ledgers at or
before `N` are already represented in the seed; applying them again would
double-count. Events arriving **during** the seed walk are **buffered**, not
dropped, and replayed in ledger order once the seed completes — otherwise
every escrow created or finished during a 30-minute walk is lost. The buffer
is bounded and its high-water mark is recorded; overflow is a `resync
required` condition, not a silent discard.

The running total then costs O(escrow-touching transactions per ledger)
instead of O(state tree per hour) — a handful of events per ledger against
78,215 pages.

### Correctness policy (owner-approved 2026-10-14)

- **Never serve an unverified incremental total.**
- On any detected gap: mark **`resync required`**, keep showing the **last
  verified figure in amber**, and queue a **full reconcile walk** —
  `load_factor`-gated and off-peak.
- A reconcile mismatch is an **alarm, not a silent correction**. This
  preserves the escrow-lesson property: divergence is a finding, never
  something papered over.

## Staging

1. **This PR** — design doc, escrow set table, seed walk. Not wired to the
   site.
2. Delta handler in the live stream owner (`xrpl_stream:rippled-node`;
   `xrpl_stream:mac` is retired, heartbeat 1,004 h stale), **shadow mode only
   — not on the site**.
3. Compare the shadow total against a full walk over several days, and report.
4. **Only then** switch the homepage to the incremental figure.

## Open items

- Gap detector needs an explicit cursor; the stream already tracks
  `last_ledger`, so the gap signal is cursor discontinuity.
- Confirm whether `entry_type_deltas_flushed_since_last_heartbeat` already
  persists `AffectedNodes` deltas before adding a parallel path.
- Escrows created **and** finished inside the same ledger.
- `escrow_walker` (Ripple monthly-release cohort, ~102 objects via
  `account_objects`) stays a **separate surface** with its own declared scope.

## Separate track

Why the node's escrow walk got 2–4× slower while the ledger grew only 0.4% is
**not addressed here** and is being tracked on its own. This design reduces
exposure to that regression but does not explain it.

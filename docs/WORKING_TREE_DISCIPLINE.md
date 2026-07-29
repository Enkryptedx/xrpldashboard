# Working Tree Discipline

## Rule

At session close, `git status` must show either (a) staged or committed
diffs matching the session's declared work, or (b) nothing. Loose
untracked artifacts have four legal destinations and no fifth:

1. **Committed** — real output of the session's declared work; goes on
   `main` (or the current working branch).
2. **Parked** — real work not ready to ship; goes on `parked/<topic>`
   pushed to origin. Stash is too fragile. Working tree is not a
   parking lot (MEMORY 2026-07-21).
3. **Scratched** — exploratory or diagnostic output; goes in `scratch/`
   (already gitignored). Reserve for one-off dumps, probe results,
   disposable JSON, ad-hoc logs.
4. **Deleted** — no lasting value.

## Enforcement

- Exploratory scripts default their output paths to `scratch/`. When a
  script grows past exploratory (writes something a later commit will
  cite), promote its output path deliberately.
- Every session-close report includes a `git status --short` line and
  a one-line reason for any remaining untracked entries. Zero-loose
  closes.
- Recurrence tally: three prior instances (RLUSD Option A drafts,
  API v1 scaffold near-miss, D1/census pile) triggered this doc.
  A fourth is a defect in the discipline, not the operator.

## Founding case

`parked/d1-census-escrow-analysis` @ `aeeaec3` — thirty files
(D1_DATA_RESULTS iterations, census_escrow_phase1c snapshots, six
d1_pull scripts, two watcher observation jsonls, hero-snapshot
generator). Six weeks of research accumulation, one park.

## Cross-refs

- `parked/api-v1-scaffold` @ `2b5eb76` — API v1 draft, pre-existing example.
- `parked/learn-vaults-draft` @ `b38880a` — parked content page draft.
- MEMORY.md: "working tree is not a parking lot" (2026-07-21).
- MEMORY.md: "session-close persistence sweep" (2026-07-19).

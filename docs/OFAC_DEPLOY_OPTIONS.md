# Nightly OFAC push triggers a full Render rebuild — options

**Status: Option C chosen 2026-11-18 (leave as-is). Revisit B after Friday's
anchor.** Nothing in this doc is implemented.

## The problem

`refresh_ofac_sdn` commits `ofac_sdn_addresses.json` straight to `main`
nightly — observed at 16:03:18, 16:03:30 and 16:03:39 ET on Nov 15/16/17.
`render.yaml:114` has `autoDeploy: true` and **no `buildFilter`**, so a
one-file data commit rebuilds and redeploys the entire service. Each deploy
reopens a cold-start window (that window is what produced a 42.68 s
`/amendments.json` response on 2026-11-18, since fixed by serving the signed
leaf).

## Why "just add a buildFilter" does not work

Investigated read-only 2026-11-18. **The running site cannot pick up a new
OFAC file without a deploy.** Three independent blockers:

1. **No Postgres mirror exists.** `grep -i ofac db.py` returns nothing. The
   committed JSON is the *only* delivery channel to production.
2. **The loader caches once per process, forever.** `check_data.py:239`
   `_OFAC_SNAPSHOT_CACHE` is a module global with no TTL, no mtime check and
   no invalidation. Even if the file changed underneath a running worker, it
   would never be re-read.
3. **No `disk` / `mountPath` in render.yaml.** The filesystem is baked at
   build time, so there is no persistent volume anything could write the new
   list into.

Only `check_data.py` reads the file at runtime. A missing or unreadable file
degrades to `count: 0`, `_load_error: True`, which surfaces as
`couldnt_check` rather than crashing `/check`.

So today the nightly deploy **is** the delivery mechanism. Removing it
without replacing it does not save a build — it breaks sanctions-data
delivery.

## Options

### A. buildFilter alone — **rejected, unsafe**

Add `buildFilter.ignoredPaths: [ofac_sdn_addresses.json]`.

Stops the rebuild. But the live list then **freezes at whatever shipped in
the last image**, and `/check` keeps returning confident "no match" verdicts
from a stale sanctions list, with no staleness signal to the reader. That is
a silent false negative on a compliance surface. Unacceptable, not merely
risky.

### B. PG mirror first, then buildFilter — **recommended**

1. `refresh_ofac_sdn` writes the snapshot to Postgres as well as the file.
2. `check_data.py` reads **PG-first, file fallback** — the exact precedent
   already in the codebase: `_read_signed_envelope` and
   `_list_signed_snapshots` are PG-first *because* "Render has no disk
   access to the Mac-written files".
3. **Add a TTL to `_load_ofac_snapshot` — mandatory, not optional.** Without
   it the PG read is also cached once per process and the staleness bug
   simply moves one layer down.
4. Surface `fetched_at` so `/check` can downgrade to `couldnt_check` when the
   mirror is behind, instead of asserting a clean result from old data.
5. *Only then* add the buildFilter.

Risks: touches a compliance-sensitive read path; more moving parts; needs the
staleness downgrade to be right. All manageable, and step 4 is what keeps a
PG outage honest rather than silently reassuring.

### C. Leave it — **chosen for now**

One wasted deploy per day plus a reopened cold window. Correctness holds, and
the leaf-first fix on `/amendments.json` already removed the worst
canary-visible symptom. Notably the canary did **not** fail in the 16:0x
window on any of Nov 15/16/17, so the nightly deploys have not been causing
visible damage.

### D. PG only, stop committing the file — cleanest end state

Walker writes solely to Postgres; the JSON becomes a dev artifact. Bigger
change, and the file fallback is what currently lets a fresh clone render
`/check` at all, so that fallback would need deliberate handling.

## If B is picked up

Order matters: mirror + TTL + staleness downgrade must all land and be
verified *before* the buildFilter, or there is a window where the deploy no
longer delivers and PG does not yet. The buildFilter is the last step, not
the first.

# Dated permalinks for /amendments — Design Report (2026-09-26)

Status: **BUILT 2026-09-26** (Charlie go, all four recommendations taken:
v1 labels majority events + roll-call "not in the leaf"; unsigned day =
200 "not yet signed"; roll-call day counts included; URL form
`/amendments/YYYY-MM-DD`). Route + `.json` twin in `app.py`
(`_amendments_permalink_payload`), logic in `amendments_permalink.py`,
template `templates/amendments_permalink.html`, tests
`tests/test_amendments_permalink.py` (16, OPEN + CLOSED, no DB — fixture
leaf `tests/fixtures/signed_leaf_2026-09-25.json`). Build (3) of the
2026-09-26 queue.

Found while building: production `/snapshots/verify` had reported
VERIFICATION FAILED for every date because the chain-link step only knew
disk (`chain.json` / prior-day file) and Render has neither; fixed by
completing the chain link from Postgres inside `_verify_snapshot`
(recompute the Merkle root over the PG chain's leaves, else compare the
prior LEAF's chain_root). Correction to this doc: the leaf dated D is
written at **D 01:00 UTC = 21:00 ET on D-1**, not "21:00 ET on D".

## Goal

`/amendments/YYYY-MM-DD` serves that day's **signed** amendment tallies and
the day's majority events with the leaf signature, so a press citation of
"/amendments on <date>" resolves to a tamper-evident record instead of the
live page. `/amendments` gains a **"Cite this day"** link to the newest signed
permalink.

## What already exists (verified 2026-09-26)

| Piece | Where | State |
|---|---|---|
| Daily signed leaf (schema 5) | `signed_snapshot.py`, 21:00 ET; PG `signed_snapshots` (snapshot_date, envelope, leaf_hash, chain_root, leaf_index, pubkey_fp) + disk `signed_snapshots/<date>.json` | live; one leaf per date, never re-signed (chain_job_never_re_signs_a_date) |
| `amendments_block` inside the leaf | `signed_snapshot._assemble_amendments_block` | per amendment: hash, enabled, supported_by_responding_node, network_votes (count / validations / needed), plus `responding_node_source`; part of EXPECTED_METRIC_KEYS_SCHEMA_5 since Ship A (2026-09-24) |
| Verifier | `/snapshots/verify?date=&metric=&value=` → `_verify_snapshot` (PG-first, disk fallback; signature + audit path + chain root) | live |
| Majority events | PG `amendment_majority_history` (gained / lost / regained at flag ledgers; `amendment_majority_walker`) | live; **not in the leaf** |
| Daily VHS-labeled tallies | PG `amendment_tally_reconstructions` (56 rows, per amendment per day) | live; not in the leaf |
| Roll-call rounds | PG `amendment_roll_call_rounds` / `_tallies` (own node, since 2026-09-26 11:44Z) | live; counts-only rule; not in the leaf |
| Known gaps | 2026-09-16/17/18 have no leaf (Mac outage; the gap IS the record) | recorded in /methodology + docs/anchor_history.md |

## Proposed shape

### Route `GET /amendments/<date>` (+ `.json`)

1. Validate `YYYY-MM-DD` (reuse `_safe_date_str`); anything else → 404.
2. Load the envelope for that date exactly as `_verify_snapshot` does
   (PG-first, disk fallback) and run `verify_envelope` on it. Render the
   verifier result on the page — the permalink shows its own proof, not
   just a link to it.
3. Page sections, in this order (human first, machine detail collapsed):
   - **Header:** "Amendments on <date> — signed record". Leaf date, leaf
     index, chain position, `leaf_hash`, `chain_root`, `previous_root`,
     signer fingerprint, signature (collapsed `<details>`), audit path
     (collapsed), one-line verifier verdict with a link to
     `/snapshots/verify?date=<date>&metric=amendments_block`.
   - **Signed tallies:** the leaf's `amendments_block` rendered as a table —
     amendment, hash, enabled, supported by our node, network vote
     count / validations / needed, `responding_node_source`. Every number
     here is inside the signed payload; the page says so.
   - **Majority events that UTC day:** rows from `amendment_majority_history`
     whose gained/lost/regained timestamps fall in the day. **Labeled
     explicitly "ledger-sourced by amendment_majority_walker; not inside
     this day's signed leaf."** (See open question 1.)
   - **Roll-call that day (counts only):** rounds recorded that day —
     number of rounds, heard min/max of UNL size, threshold/needed, and per
     amendment the last round's yes-incl-carry. Same label: not in the
     leaf. Per-validator rows never rendered (standing rule 2026-09-26).
   - **Not-signed states:** (a) date is today or the leaf hasn't landed →
     200 with "No signed record yet for <date> — the leaf is signed at
     21:00 ET; come back after" and NO tallies (nothing unsigned is shown
     as if signed). (b) date inside a known gap (09-16/17/18) → 200 with
     the outage sentence from the known-gaps record. (c) date before the
     first leaf → 404.
4. `Cache-Control`: past signed dates `public, max-age=86400, immutable`
   (a leaf is never re-signed); today / unsigned → `no-store`.
5. `/amendments/<date>.json`: `{date, leaf_hash, chain_root, signature,
   amendments_block, majority_events_utc_day, roll_call_day_counts,
   verify_url, sourcing}` — the machine twin; listed in agents.json and
   llms.txt; added to the sitemap.

### "Cite this day" on `/amendments`

Under the page header: **"Cite this day: /amendments/<newest signed date>"**
with the leaf hash's first 12 hex chars and "signed <date> 21:00 ET". Copy
button optional. When today's leaf has not landed, the link points at
yesterday's and says so ("today's record signs at 21:00 ET").

### Tests (OPEN + CLOSED, per the publish-gate rule)

- valid signed date renders 200 with tallies and verifier `ok` (fixture
  envelope from a real leaf file);
- unsigned/today date → 200 "not yet signed", no tallies markup;
- gap date → 200 with the outage sentence; malformed → 404; pre-first-leaf
  → 404;
- `.json` twin matches the HTML numbers;
- trust-critical list gains `/amendments/2026-09-25` only if a fixture leaf
  ships in the repo (CI has no DB; the route must not 500 without DB —
  verify by test with DATABASE_URL="").

### Size and cost

~150 lines in `app.py` (route + json twin + day-window helpers), one new
template (~120 lines, reuse `signed_snapshots_verify.html` styles), 6–8
tests, one sitemap line, agents.json/llms.txt line. No walker, no schema
change, no Lenovo touch. Half a day.

## Open questions for Charlie (decide before build)

1. **Majority events + roll-call in the leaf?** v1 shows them labeled
   "not in the leaf". Putting them INSIDE the signed record means a schema
   bump (schema 6, `EXPECTED_METRIC_KEYS_SCHEMA_6` in code per
   schema-metrics-are-code-never-env-toggles) and a change to what the
   21:00 walker collects. Recommend: ship v1 labeled now; schema-6 as a
   separate proposal after the roll-call card has a week of rounds.
2. **Unsigned-day behaviour:** 200 "not yet signed" (recommended, keeps
   the URL stable for citations written during the day) vs 404 until the
   leaf lands.
3. **Roll-call day counts on the permalink at all** in v1, or only after
   the card is flag-flipped (2026-09-27 11:44Z)? Recommend: include, since
   the card's gate is about the live surface and the permalink only shows
   recorded rounds.
4. **URL form:** `/amendments/2026-09-25` (recommended, reads as a date)
   vs `/amendments?date=`. The dated path is what press will paste.

## Not in scope

- Re-signing or amending past leaves (never).
- Per-validator anything (standing rule).
- Backfilling `amendments_block` into leaves before 2026-09-24 (they don't
  carry it; the permalink for those dates shows the leaf and says the
  block starts 2026-09-24).

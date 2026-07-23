# is_bot Column Design
**Status:** DESIGN — awaiting Charlie's review before any code

**Authored:** 2026-07-23  
**Acceptance test:** Someone reading this in six months can answer "how do we know the stamps still match the classifier?" from this doc alone, without reading code.

---

## Why

Every `/analytics` query scans the full `page_views` table and evaluates a complex bot filter per row — currently ~400ms per query, ~10s for the full cold render. The filter is correct. The cost is the scan. The fix is to materialize the filter's verdict into the row and let Postgres use a partial index instead.

With the column: each query becomes `WHERE is_bot IS NOT TRUE` over an indexed subset. Target: ~5–20ms per query, cold render < 1s (measured against prod Neon before cutover, not labeled local-only).

SWR (59dade3) already covers users — they never wait. This buys database headroom and kills the 30s warmer-cycle drag.

**What this is not:** a source of truth change. The classifier (`_bot_filter_sql` and its inputs) remains the authority. The column is a cache of its verdicts. The doc's job is ensuring that cache stays honest.

---

## Component 1 — Schema and Semantics

```sql
ALTER TABLE page_views ADD COLUMN is_bot BOOLEAN DEFAULT NULL;
```

Online in Postgres — no table rewrite, no lock beyond a brief metadata update. Safe on Neon free tier.

```sql
CREATE INDEX CONCURRENTLY idx_page_views_human
    ON page_views (ts)
    WHERE is_bot IS NOT TRUE;
```

`CONCURRENTLY` avoids a full table lock at index-build time. Build runs once after the column is added.

**NULL semantics — chosen, not accidental:**

| Value | Meaning |
|-------|---------|
| `TRUE` | Classified as bot by current classifier rules |
| `NULL` | Not yet classified — counted as human |
| `FALSE` | Not written. The index covers it; adding FALSE would double write cost for no query benefit. Coverage is tracked via watermark, not column value. |

`NULL = counted as human` matches the methodology page's disclosed retrospective behavior: "recent counts may drop later as patterns confirm." The column's NULL semantics ARE that disclosure, materialized. The page may need one sentence added (see Component 6).

**Query cutover (after backfill + canary sign-off):**

```python
# human filter — was: AND NOT (full_bot_pred)
"AND is_bot IS NOT TRUE"

# bot filter — was: AND (full_bot_pred)  
"AND is_bot = TRUE"
```

---

## Component 2 — Background Writer

### 2a. Metadata table

```sql
CREATE TABLE page_view_classification_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

Rows used:

| key | value | purpose |
|-----|-------|---------|
| `last_classified_ts` | unix epoch (int) | watermark: all rows with ts ≤ this have been visited |
| `last_classifier_version` | version string | triggers full resync on mismatch |
| `last_cohort_version` | ISO timestamp | max(created_at) seen in burst_cohort_days; triggers date-range resync on advance |
| `backfill_complete` | `true` / `false` | set by the backfill job on completion; canary gates on this |

### 2b. Incremental forward pass (ongoing)

Cadence: every **5 minutes** (launchd plist, `StartInterval 300`).

```
SELECT id, visitor_hash, ip_day_hash, path, user_agent, country, ts
FROM page_views
WHERE ts > (last_classified_ts - 600)   -- 10-min overlap catches boundary races
ORDER BY ts ASC
```

Apply `_bot_filter_sql` (legacy predicate form, always — this is the classifier, not the column reader) to each row. Batch UPDATE in chunks of 1,000. After all chunks, write `last_classified_ts = MAX(ts processed)` to meta.

The 10-minute overlap is deliberate: ip_day_hash session spread means a new bot row can retroactively classify rows in the same session that arrived slightly earlier. The overlap re-checks that boundary window on every pass.

### 2c. One-time historical backfill

Runs once, chunked by day (oldest-first), 1,000 rows per chunk, 1-second sleep between chunks. Writes `backfill_complete = true` when the last chunk finishes and watermark covers all rows.

Progress logged to `walker_health` heartbeat row (`name = 'is_bot_backfill'`) on each day-chunk completion. Loud on failure — no silent truncation.

This is not urgent. The incremental pass classifies forward continuously; the backfill fills the historical tail. Users see no difference (SWR + NULL-counted-as-human means nothing changes at the query layer during the backfill period).

Estimated backfill time at 1,000-row chunks + 1s sleep: ~100 chunks for 100k rows = ~100 seconds. Polite on Neon free tier.

### 2d. Walker standards

- Scope declaration at top of writer script: classifies `page_views.is_bot` via `_bot_filter_sql`; reads `burst_cohort_days`; reads `page_view_bot_hashes` and `page_view_scanner_combos` (already-materialized bot data for efficiency on the incremental pass); writes `page_view_classification_meta`
- `write_heartbeat('is_bot_writer')` on each successful forward pass
- launchd plist under `~/Library/LaunchAgents/` (not just `~/xrpl_test/launchd/` — lesson from nft_activity_walker 19h silence, 2026-07-15)
- Explicit `set -euo pipefail` in the wrapper shell script; env file sourced at top

---

## Component 3 — Reconciliation Obligation

The founding risk, stated verbatim:

> **"burst-cohort adds a cohort day → old rows are bots per the live classifier → their stamps still say human → the page undercounts bots silently forever while classifier audits pass green."**

That is the RLUSD 53-day incident wearing a schema. The column ships with the machinery that prevents it.

### Reconciliation triggers and mechanics

**Trigger 1: New burst_cohort_days rows (daily)**

Detection: on each writer run, query `MAX(created_at) FROM burst_cohort_days`. Compare to `last_cohort_version` in meta.

Action on advance: re-run the classifier over the date range `(burst_day MIN to MAX)` of newly-added cohort rows. Update `is_bot` for affected rows. Update `last_cohort_version` in meta.

This ensures that when the burst-cohort scanner fires overnight and adds yesterday as a burst day, yesterday's rows are re-stamped within one writer cycle (5 minutes).

**Trigger 2: BOT_UA_PATTERNS or BOT_PATH_PATTERNS changes (any deploy)**

Detection: `CLASSIFIER_VERSION` constant in `db.py`, incremented on any change to bot-filter inputs (UA patterns, path patterns, scanner thresholds). Compare to `last_classifier_version` in meta.

Action on mismatch: queue a full-table resync pass (same chunked approach as the backfill). This is intentionally heavyweight — rule changes are rare and correctness requires completeness. Set `last_classifier_version` in meta only after the full resync completes.

**Cheapest reliable CLASSIFIER_VERSION shape:** A plain integer constant in `db.py` (e.g., `BOT_CLASSIFIER_VERSION = 1`), stored per-sync-run in the meta table. Per-row storage would add 4 bytes × 100k rows = 400KB ongoing write amplification and complicates the canary. Per-sync-run is one row in a tiny meta table — effectively free.

**Trigger 3: burst_cohort_days row deleted or modified**

Not handled in v1. Cohort rows are only ever added, not modified or deleted, by `scan_burst_cohorts()`. If that changes, trigger 2 (CLASSIFIER_VERSION bump) should accompany it.

### What reconciliation does NOT cover

- Real-time (<5 minute lag): acceptable. Methodology page already discloses retrospective classification.
- Session spread across the overlap window edge: the 10-minute overlap handles this within one writer cycle.
- ip_day_hash changes to existing rows: ip_day_hash is set at write time and immutable per-row. Not a reconciliation concern.

---

## Component 4 — Drift Canary

Ships in the **same commit** as the column DDL and writer. Not phase-2. Not optional.

### What it checks

Daily, the canary computes human-row counts via two independent paths over the same data and alarms on divergence:

- **Path A (column):** `SELECT COUNT(*) FROM page_views WHERE is_bot IS NOT TRUE AND ts BETWEEN $start AND $end`
- **Path B (live predicate):** same window, with the full legacy `_bot_filter_sql` predicate (the legacy path is retained in `_bot_filter_sql` permanently and is never deleted — it IS the independent check)

### Sample windows

Two windows per daily run:

1. **Trailing 7 days:** `ts` between `now - 7d` and `now - 10min` (the 10-min exclusion is the in-flight window — rows newer than this may not yet be stamped by the writer and are legitimately out of sync)

2. **Rotating historical week:** deterministic, not random. Week selected as `(ISO week number % 4) + 2` weeks ago. This gives a 4-position rotation over the month, covering four different historical windows without needing a random seed. Canary run on a Thursday covers data that is 2–5 weeks old on a 4-week rotation.

### Alarm threshold

**Exact match required** in both windows, outside the in-flight exclusion. The tolerance is zero — not a percentage. Column and predicate are computing the same classifier over the same rows; any divergence is a reconciliation failure.

The single exception: rows where `backfill_complete = false` and `ts < last_classified_ts` — if the backfill hasn't reached a particular historical row, Path A (column) will count it as human (NULL) while Path B (predicate) may count it as bot. The canary gates on `backfill_complete = true` before running the historical-week window.

### Alarm delivery

Named `walker_health` failure: `name = 'is_bot_canary'`. Follows the same health-row format as all other walkers — `last_success`, `last_run`, `error_detail`. This wires into the answer_plausibility framework (Layer 3 audit) rather than inventing a new alert path.

The canary's failure message should include: window checked, Path A count, Path B count, delta, and which reconciliation trigger is the likely cause (e.g., "burst_cohort_days advanced since last canary run: check rows in affected date range").

---

## Component 5 — Cutover and Rollback

### Cutover sequence

1. **Column added + index built** (one deploy, no query changes)
2. **Writer starts** — forward pass classifies new rows; backfill runs in background
3. **Canary starts** — trailing-7d window only until `backfill_complete = true`
4. **Backfill completes** — canary begins checking historical window as well
5. **Canary runs clean for 3 days** — covers at least 3 burst-cohort scanner cycles and 3 writer cadence cycles. Three days is the minimum meaningful signal on a daily-running canary.
6. **Queries flip** — `_bot_filter_sql` gains a new top tier: `if _is_bot_column_ready: return "AND is_bot IS NOT TRUE"` (similar to `_bot_hash_table_ready` gate). `_is_bot_column_ready` is set True only after external confirmation (manual flag or a deploy-time check that canary has been clean).
7. **Legacy predicate stays** — `_bot_filter_sql` retains the full legacy path forever as the canary's comparison source. It is never deleted.

### Rollback

One-line change: flip `_is_bot_column_ready = False` (or remove the column-path branch). Column data stays inert. No data loss. Predicate path was never removed.

### Expected post-cutover numbers (to be measured against prod Neon, not labeled local-only)

- Per-query time: target 5–20ms (index scan over human rows only)
- Cold render: target <1s (22 queries × ~15ms + overhead)
- Warmer cycle: rebuild time trivial; warmer can reduce its cadence or serve as pure freshness insurance

The identical-output diff (column path vs predicate path over the same data snapshot) is the ship gate — same bar as #3. A cutover that shifts any count is a drift-canary failure waiting to fire; catch it in the diff before the flip.

---

## Component 6 — What This Does Not Change

**Classifier authority:** `_bot_filter_sql` and its inputs remain the source of truth. The column is a performance cache. Any disagreement between column and predicate is an error in the column, never in the predicate.

**Methodology page:** Likely one sentence added to the bot-filter explanation section — something like: *"Classification verdicts are materialized for query performance and reconciled continuously against the live classifier; counts always reflect current rules applied to all historical data."* Check whether the existing language already covers this (the retrospective-disclosure banner probably does); if yes, no change needed.

**Public numbers at cutover:** Zero change. Identical-output diff (column path vs predicate path on same data) is a hard ship gate. If any number shifts, the canary should have caught it first — if it didn't, the canary spec failed and the cutover doesn't happen.

**Queue behind this (unchanged):** Lenovo doc (RAM pending), /check v2, NFT anomaly scan ~Aug 5. Fleet watch on Sunday agenda — if tonight's blocked count shows UA adaptation, that outranks this work.

---

## Open Questions for Charlie

**None blocking design.** The three pre-review callouts (FALSE value, N=3 days, 10-minute canary exclusion) are decided in this doc. The design is ready to build on your word.

---

## Appendix — Files This Touches

| File | Change |
|------|--------|
| `db.py` | `BOT_CLASSIFIER_VERSION` constant; `_is_bot_column_ready` flag; new top tier in `_bot_filter_sql`; `refresh_classification_meta()` helper |
| `app.py` | `_is_bot_column_ready` check in `analytics()`; warmer loop unchanged (writer is separate process) |
| `scripts/is_bot_writer.py` | New — forward pass + backfill logic |
| `launchd/run_is_bot_writer.sh` | New — env wrapper |
| `launchd/com.charliebruce.xrpldashboard.is_bot_writer.plist` | New — 5-min cadence |
| `scripts/is_bot_canary.py` | New — daily drift check, writes walker_health |
| `launchd/run_is_bot_canary.sh` + plist | New — daily cadence |
| `docs/IS_BOT_COLUMN_DESIGN.md` | This file |
| `scripts/claims_check.sh` / `CLAIMS.yaml` | Methodology claim about bot filter may need one-sentence update |

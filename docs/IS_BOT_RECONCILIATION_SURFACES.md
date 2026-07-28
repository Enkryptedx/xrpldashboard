# is_bot Reconciliation Surfaces

**Authored:** 2026-07-28
**Status:** APPROVED — ships this commit
**Parent design:** `docs/IS_BOT_COLUMN_DESIGN.md`
**Cousin:** `docs/IS_BOT_SCANNER_MEMORY_FIX_2026-07-26.md`

**Acceptance test:** Someone reading this in six months can answer, without reading code:
1. Why cohort + scanner + bot_hashes are all instances of the same class, not three unrelated bugs.
2. Why the fix for bot_hashes is a trigger and not a BOT_CLASSIFIER_VERSION bump.
3. Why the version scheme is content-hash and not timestamp.
4. Why the trigger is expected to fire most cycles, and why that's fine.
5. What was decided vs. what remains open.

---

## 1. The law

**Predicate evidence tables are reconciliation surfaces.** Any table the live classifier reads must carry an advance-trigger in the writer that forces a re-stamp (TRUE-only, forward-only) when its content changes. Without one, the writer's stamps drift behind the classifier's memory, and the canary reports mismatch on rows the writer's forward window never revisits.

The class was hidden while cohort's trigger existed-but-was-dead (`created_at` typo swallowed by `try/except pass`, since ~however-many-days). Fixing that one bug surfaced two more instances of the same shape. Three members in one week isn't a coincidence — it's a design law the system taught us three times.

---

## 2. Three founding instances

| # | Table | Arm | Trigger status (2026-07-28 EOD) | Version scheme |
|---|-------|-----|----------------------------------|----------------|
| 1 | `burst_cohort_days` | `cohort_pred` | **Alive** — revived in `12f2c94`. TRUE-only re-stamp over `[MIN(burst_day), MAX(burst_day)]` when `MAX(classified_at)` advances. Founding failure = the `created_at` typo swallowed by try/except pass. | `MAX(classified_at)::TEXT` |
| 2 | `page_view_scanner_combos_confirmed` | `scanner_pred` | **Alive** — built in `12f2c94` (sibling added at same time as #1's revival). TRUE-only combo-targeted re-stamp when `MAX(confirmed_at)` advances. No time window (combo-identity). | `MAX(confirmed_at)::TEXT` |
| 3 | `page_view_bot_hashes` | `session_pred` (visitor + ip_day arms) | **Alive** — this doc's commit. TRUE-only hash-targeted re-stamp when the table's content-hash changes. Instance masked earlier the same day by hammer `eb6c2f1` (BOT_CLASSIFIER_VERSION v2→v3 full resync). | `MD5(string_agg(hash_type\|\|':'\|\|hash, ',' ORDER BY hash_type, hash))` |

`row_pred` is intentionally excluded from the class: static patterns baked into code, no evidence table. A change to `BOT_PATH_PATTERNS` or `BOT_UA_PATTERNS` is the case where `BOT_CLASSIFIER_VERSION` *is* the correct mechanism — bump on pattern change, full resync fires. That's what `BOT_CLASSIFIER_VERSION` exists for.

---

## 3. Why a trigger, not a hammer

The `BOT_CLASSIFIER_VERSION` bump is the right tool for **static** classifier changes (pattern edits, threshold changes, arm additions/removals) — it fires the bidirectional `CASE-WHEN` full-resync, which can both stamp TRUE and NULL a stale TRUE. It is the correct hammer for a semantic change to the predicate itself.

It is the *wrong* tool for **dynamic** evidence-table changes (new hash lands, new confirmed combo lands, new burst-cohort lands). These are additive: evidence is revealed, existing TRUE stamps don't become invalid. TRUE-only re-stamp is the correct shape, and it needs to fire on every trigger event — not once per version bump.

Reaching for the version bump every time an evidence table advances is the anti-pattern the class hides. It:
- Rewrites 115,665 rows per bump for an issue that touches 26.
- Fires the bidirectional `CASE-WHEN` — the ELSE-NULL landmine (see `IS_BOT_SCANNER_MEMORY_FIX_2026-07-26.md` §3) — over the full history.
- Trains the on-call reflex "canary flips → bump the version," which is the hammer-not-mechanism trap this doc names.

Version bumps stay for pattern edits. Triggers stay for evidence tables. One is a semantic change; the other is a memory update.

---

## 4. bot_hashes trigger — implementation spec

### 4a. The versioning problem

`page_view_bot_hashes` has no timestamp column. Schema is `(hash_type TEXT, hash TEXT, PRIMARY KEY (hash_type, hash))`. `refresh_bot_hash_tables()` `TRUNCATE`s and re-populates the whole table every writer cycle. There is no `classified_at` or `updated_at` to compare against.

The version is therefore a function of table *content*, not row age.

### 4b. Content-hash choice

**Chosen: `MD5(string_agg(hash_type||':'||hash, ',' ORDER BY hash_type, hash))`.** Deterministic (ORDER BY guarantees stable output across executions), catches add/remove/replace (a `COUNT + MAX` composite would miss replacements that preserve both count and max), cheap at current table size (13,110 rows; O(N) sort dominated by network, ~1-3ms in practice).

Stored as TEXT in `classification_meta.last_bot_hashes_version`. Same shape as cohort/scanner versions.

### 4c. Trigger placement + code shape

Insert as `§5c` in `is_bot_writer.py run()`, immediately after `§5b` (scanner-combos-confirmed advance check). Single UPDATE with OR across both hash types — verified via `EXPLAIN ANALYZE` on prod Neon to be one seq scan of `page_views` + hash-join against each `hash_type` subset, ~30ms total (vs ~72ms for split-arm UPDATEs).

```python
# ── 5c. Check page_view_bot_hashes content advance ─────────────
# Third sibling in the reconciliation-surface class (see
# docs/IS_BOT_RECONCILIATION_SURFACES.md §2 for the law).
# refresh_bot_hash_tables() at step 2 TRUNCATE+repopulates this
# table every cycle from trailing session data. When a new
# (hash_type, hash) lands, historical page_views rows matching
# that hash would otherwise not be re-classified until the next
# BOT_CLASSIFIER_VERSION bump — hence the 07-28 +25 residue
# that eb6c2f1 cleared instance-wise. TRUE-only + hash-targeted:
# no time window (hashes are combo-identity like scanner combos).
# Already-TRUE rows skipped (idempotent).
#
# EXPECTED TO FIRE MOST CYCLES. ip_day_hash entries are day-scoped
# (a hash is unique per ip × day), so a new UTC day creates fresh
# hashes and old ones age out of the trailing session window; the
# content-hash therefore changes nearly every cycle. The UPDATE
# is TRUE-only + idempotent so correctness holds regardless of
# fire frequency; the concern is cost. Duration is logged on every
# fire so week-one cost stays visible. Prod EXPLAIN ANALYZE:
# ~30ms/fire against 116k page_views rows, 13k bot_hashes rows.
# At 5-min cadence that's ~360ms/hour of DB work — accepted at
# current scale. Escape hatch if measurement shows real cost:
# switch to additions-only versioning via set-difference against
# the prior hash set (deferred; not built).
with conn.cursor() as cur:
    cur.execute(
        "SELECT MD5(string_agg(hash_type || ':' || hash, ',' "
        "                      ORDER BY hash_type, hash)) "
        "FROM page_view_bot_hashes"
    )
    row = cur.fetchone()
    current_bot_hashes_v = (row[0] or "") if row else ""

if current_bot_hashes_v and current_bot_hashes_v != last_bot_hashes_v:
    import time as _time
    _fire_start = _time.monotonic()
    log.info(
        "page_view_bot_hashes advanced (%s→%s) — TRUE-only hash re-stamp",
        (last_bot_hashes_v or "<empty>")[:12],
        current_bot_hashes_v[:12],
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE page_views p SET is_bot = TRUE "
            "WHERE ("
            "  (p.visitor_hash IS NOT NULL AND p.visitor_hash IN ("
            "    SELECT hash FROM page_view_bot_hashes "
            "    WHERE hash_type = 'visitor'))"
            "  OR (p.ip_day_hash IS NOT NULL AND p.ip_day_hash IN ("
            "    SELECT hash FROM page_view_bot_hashes "
            "    WHERE hash_type = 'ip_day'))"
            ") "
            "AND (p.is_bot IS NULL OR p.is_bot = FALSE)"
        )
        count = cur.rowcount
    conn.commit()
    _fire_ms = int((_time.monotonic() - _fire_start) * 1000)
    log.info("bot_hashes-resync (TRUE-only): rows=%d duration_ms=%d",
             count, _fire_ms)
    db.set_classification_meta({"last_bot_hashes_version": current_bot_hashes_v})
```

Add `last_bot_hashes_v = meta.get("last_bot_hashes_version", "")` in `§3` alongside `last_cohort_v` and `last_scanner_v`.

Update the module docstring "Reconciliation triggers checked on every run:" block to list the third trigger.

### 4d. Fire frequency + measurement discipline

**Expected to fire most cycles.** `ip_day_hash` entries are day-scoped: each hash represents one (ip, UTC-day) pair. A new UTC day therefore creates fresh hashes; old hashes age out of the trailing session window used by `refresh_bot_hash_tables()`. The content-hash MD5 changes on any add, remove, or replacement — so it changes nearly every 5-min cycle in normal operation.

**Why that's fine at current scale:**
- The UPDATE is TRUE-only + `is_bot IS NOT TRUE` filter → idempotent. Firing every cycle re-checks the same rows; the ones already stamped TRUE are skipped by the filter, and Postgres doesn't touch pages it doesn't have to.
- Prod `EXPLAIN ANALYZE` (2026-07-28, 43MB page_views / 116k rows / 13k bot_hashes): 29.9ms execution, 5,645 shared buffers hit, actual write rows = 139 (only truly-new matches on the sample cycle). Cost is dominated by the seq scan of `page_views` (~5-6ms) and the hash-join build (~3ms), not by the UPDATE itself.
- At 5-min cadence: 12 cycles/hour × ~30ms = ~360ms/hour of DB work dedicated to this trigger. Comparable to the existing forward-pass UPDATE.

**Measurement discipline:** every fire logs `rows=<n> duration_ms=<n>`. Week-one cost stays visible in `launchd_logs/is_bot_writer.err.log`. Sunday audit reads these lines and flags growth outside expected range (baseline ~30ms, rowcount typically ≤200 excluding first-run).

**Escape hatch (deferred, not built):** if measurement shows real cost (duration climbing with `page_views` growth, e.g. crossing ~500ms/fire), switch to additions-only versioning. Sketch: persist the prior `set-of-hashes` in a side table; on each cycle, compute `new_hashes = current \ prior`; only re-stamp rows matching `new_hashes`; store new prior. Fires only when there's genuinely new evidence to reconcile. Not built now because current measurement doesn't justify the extra table + set-diff logic. Revisit if week-one logs show growth.

### 4e. First-run behavior

On first run after ship, `last_bot_hashes_v = ""` and `current_bot_hashes_v` is a real MD5 → trigger fires. That single first-run event does the equivalent of a targeted-arm resync for the historical instances that today's `eb6c2f1` cleared for `session_pred`. All subsequent runs skip only if the content-hash matches — which per §4d is uncommon.

**Expected first-run rowcount: near zero.** The `eb6c2f1` hammer (v2→v3 full resync) already re-stamped all 115,665 rows earlier today. First-run rowcount ≠ 0 would indicate rows the hammer's CASE-WHEN pass missed — which is itself a signal worth surfacing.

**No `BOT_CLASSIFIER_VERSION` bump needed with this commit.** The trigger's first-run behavior IS the resync for this arm. Bumping in the same commit would be belt-and-suspenders masking the trigger's real behavior.

### 4f. Indexes verified

`page_views` has two composite indexes covering the hash arms:
- `page_views_visitor_idx (visitor_hash, ts DESC)`
- `page_views_ip_day_idx (ip_day_hash, ts DESC)`

Both from the `2dee018` precomputed-hash work. `EXPLAIN ANALYZE` shows the planner picks Hash Join (not index probes) for the ~9k-value IN-subquery — correct choice given the set size; index probes would only win with much smaller IN sets. Indexes remain valuable for the live `/analytics` query path and for any future single-hash lookups. No new indexes required for this trigger.

### 4g. Failure mode: fail loud, per the founding law

No `try/except` around the SELECT. If the query errors (column rename, table drop, whatever), the exception propagates to `run()`'s outer `try/except` → `walker_health.ok = False` + exception message. That's the direct lesson of `12f2c94` — a dead trigger must look identical to a broken trigger, and both must look loudly different from a live trigger.

---

## 5. Deploy order

Simpler than the scanner-confirmed migration because no table is created or backfilled:

1. Commit doc + trigger code + docstring update + `IS_BOT_COLUMN_DESIGN.md` addendum. One commit. Push.
2. Manual writer invocation → first-run trigger fires automatically (§4e).
3. Manual canary immediately after → labeled "post-ship confirmation." Expect `delta=0` both windows.
4. **If the post-ship canary shows anything but `0/0`, STOP and report.** That's a signal per §4e — the trigger should be a no-op against the currently-clean state; any perturbation means either the first-run touched rows the hammer missed (worth investigating) or the trigger's shape differs from the hammer's classifier in some way not covered by the LANDMINE discipline.
5. Tomorrow 06:00 EDT scheduled canary = **soak day 1** on the finished system. All three soak days test the complete machinery — the arm the trigger closes AND the trigger itself.
6. Wed/Thu clean scheduled runs → **Friday flip-eligibility** holds.

**Rollback:** revert the commit. `last_bot_hashes_version` in the meta table becomes orphaned but harmless (writer ignores unknown keys). No schema migration to reverse.

---

## 6. Design-doc addendum

Insert into `docs/IS_BOT_COLUMN_DESIGN.md`, dated 2026-07-28:

> **Predicate evidence tables are reconciliation surfaces.** Any table the live classifier reads must carry an advance-trigger in the writer that forces re-stamp (TRUE-only, forward-only) when its content changes. Three founding instances (2026-07-28): `burst_cohort_days` (dead trigger, `created_at` typo — fixed `12f2c94`), `page_view_scanner_combos_confirmed` (built Sunday alongside the amnesia-gap fix), `page_view_bot_hashes` (content-hash versioned — see `docs/IS_BOT_RECONCILIATION_SURFACES.md`). The version scheme adapts to the table: append-only ledgers use `MAX(timestamp)`; snapshot tables use content-hash. Static classifier changes (patterns, thresholds, arm additions) remain the domain of `BOT_CLASSIFIER_VERSION` — bumping the version for an evidence-table advance is the hammer-not-mechanism trap this rule closes.

---

## 7. Decided vs open

**Decided (this doc's scope):**
- The law itself, catalogued with three founding instances.
- bot_hashes trigger goes into `is_bot_writer.py` §5c, single UPDATE with OR across both hash arms.
- Version scheme: content-hash via `MD5(string_agg(..., ORDER BY ...))`.
- Meta key name: `last_bot_hashes_version`.
- No `BOT_CLASSIFIER_VERSION` bump in this ship. First-run behavior IS the resync for this arm.
- No dwell period, no time window — TRUE-only + hash-identity + already-TRUE-skipped is idempotent.
- Ship timing: **tonight**, before tomorrow 06:00 EDT scheduled canary. Reason on record: the soak certifies the system that runs post-flip; ship before day 1 so all three soak days test the complete machinery.
- Failure mode: fail loud through `walker_health`. No `try/except` swallow.
- Fire frequency: expected most cycles due to `ip_day_hash` day-scoping. Cost accepted at current scale, measured on every fire, revisit if week-one logs show growth.
- Escape hatch (deferred): additions-only versioning via set-difference against prior hash set.
- Design-doc addendum verbatim into `IS_BOT_COLUMN_DESIGN.md`.

**Open (later scope, not this doc):**
- Additions-only versioning escape hatch — only if measurement justifies it. Sunday audit reads the duration/rowcount lines; a duration trending past ~500ms/fire is the trigger for revisiting.
- Removals-never-un-stamp — explicit non-goal. If we ever want removal-driven un-stamping, it goes through `BOT_CLASSIFIER_VERSION` bump (bidirectional resync), not through this trigger. The content-hash DOES detect removals (any set change flips the MD5); the re-stamp SQL just doesn't act on them. That's the LANDMINE discipline preserved by construction.

---

## 8. Acceptance test — answered

1. **Why three instances, not three bugs:** Every arm of `_bot_filter_sql` that reads a mutable evidence table needs a re-stamp trigger. Cohort had a broken one; scanner didn't have one; bot_hashes doesn't have one. Same shape three times means the rule was implicit — this doc makes it explicit. §1, §2.
2. **Why trigger not bump:** Version bumps are for classifier semantic changes (patterns, thresholds). Evidence advances are additive and continuous — triggers on every change. Bumping for every evidence advance is the hammer trap. §3.
3. **Why content-hash not timestamp:** The table has no timestamp column and is `TRUNCATE`+repopulated each cycle. Content-hash is the only version scheme that survives the table's shape. §4a-b.
4. **Why fire-most-cycles is fine:** TRUE-only + `is_bot IS NOT TRUE` filter is idempotent; measured cost ~30ms/fire against 116k rows; escape hatch exists if measurement changes. §4d.
5. **What was decided vs open:** §7.

# is_bot Scanner-Memory Fix — Scratch Note

**Authored:** 2026-07-26
**Status:** SCOPED — build in progress
**Parent design:** `docs/IS_BOT_COLUMN_DESIGN.md`

**Acceptance test:** Someone reading this in six months can answer, without reading code:
1. Why the canary drifted −43 rows on 2026-07-26 after the writer-lock incident.
2. Why the natural-looking fix (widen writer's UPDATE window) is a data-loss landmine.
3. Why the confirmed-ledger fix does NOT violate the canary's independence property.
4. What was decided vs. what remains open.

---

## 1. Mechanism (established from DB evidence, not re-derivation)

**Incident timeline:** on 2026-07-26, after the writer-stall recovery (commit `57ac3cd` + wrapper hardening `6bd2010`), the manual canary reported `delta = −43` (later drifted to −45 over the same day, essentially stable). Not a fresh divergence — a persistent residual the writer's recovery pass didn't touch.

**DB check (executed 2026-07-26 against Neon):**

- All 45 disputed rows are `CheckHost` probe traffic on `/` and `/analytics`, timestamps `2026-07-19 → 2026-07-23`, from HU/GB/US/IR/NL/CH.
- Ground truth: these are automated diagnostic probes (some or all invoked by us during the July analytics-timeout and DNS incidents; check-host.net is a public service, so a stranger's probe would land in the same bucket). Classification-correct as bots regardless of invoker.
- 0/5 sampled disputed `visitor_hash` values in `page_view_bot_hashes[visitor]`.
- 0/5 sampled disputed `ip_day_hash` values in `page_view_bot_hashes[ip_day]`.
- 0/5 sampled disputed `(path, ua)` in `page_view_scanner_combos`.
- 0/6 sampled disputed `visitor_hash` values have EVER appeared with a row matching `row_pred` (i.e. they could not have entered `bot_hashes` even historically — the CheckHost UA doesn't match any BOT_UA_PATTERN and `/` `/analytics` don't match any BOT_PATH_PATTERN).

**Only possible mechanism:** at some earlier writer pass — during a CheckHost probe burst — `page_view_scanner_combos` transiently contained `('/analytics', 'CheckHost...')` and `('/', 'CheckHost...')`. The writer stamped those rows `is_bot=TRUE` via the scanner arm. Later refreshes evicted the combos as the probe burst decayed. The rows retained the TRUE stamps. The writer's recent post-recovery UPDATE window covered `[2026-07-23 11:44 UTC → now]` — just after the disputed rows' timestamps end — so none were re-touched.

**Four-arm scope check** (which arms have the forgetting problem?):

| Arm | Refresh basis | Forgetting? |
|-----|---------------|-------------|
| `row_pred` | static patterns | no |
| `session_pred` (via `page_view_bot_hashes`) | unbounded scan of page_views for row_pred matches; append-only | no |
| `scanner_pred` (via `page_view_scanner_combos`) | `ts > now − 7d` — time-bounded | **YES** — the only forgetting arm |
| `cohort_pred` (via `burst_cohort_days`) | append-only, written by burst walker | no |

The fix is scoped to scanner_pred only. Do not over-apply the pattern to arms that don't forget.

---

## 2. Deploy-order theorem (canary-specific migration discipline)

**Deploying read-paths against an empty confirmed table triggers a fresh mass-drift event with the same shape as the one we're closing.**

If the writer/canary code updates to read from `page_view_scanner_combos_confirmed` before that table is populated, every historical TRUE stamp that relied on since-evicted scanner combos becomes an immediate canary residual. The delta explodes from −45 into the thousands.

**Sequence — law, not suggestion:**

1. Migration: create `page_view_scanner_combos_confirmed` (empty).
2. Run backfill script: walks history, emits review file (JSON) with candidate confirmed rows + governance fields.
3. Human review of the review file. Ambiguous entries left unconfirmed.
4. Populate `page_view_scanner_combos_confirmed` from the reviewed set.
5. THEN deploy code that reads from the confirmed table.
6. First scheduled canary run.

If backfill surprises or review takes longer, the code waits. Table completeness gates the deploy, period.

---

## 3. Un-stamp landmine (dormant, marked but not disarmed)

The writer's `_bot_update_sql` produces `CASE WHEN pred THEN TRUE ELSE NULL END`. The `ELSE NULL` semantic is CORRECT for the designed forward window: `NULL = not-yet-classified` is the column's honest state for fresh rows, and the snapshot barely moves within the ~10-minute window so verdict stability holds.

**But:** the moment the UPDATE window's `ts_start` is widened to cover historical rows, `ELSE NULL` becomes silent verdict-erasure. Every row whose original TRUE stamp came from a since-evicted snapshot gets set to NULL. The canary agrees (both arms now see NULL = human). Delta becomes 0 not by fixing the classifier but by destroying the history of its correct verdicts.

**Why we leave the code as-is:** the mechanism is correct for its designed use. Rewriting the CASE expression would harm the forward window.

**Two cheap locks, applied at build time:**

1. Comment block on the ts-window definition line (`window_start = max(0, last_ts - FORWARD_OVERLAP_SECS)` in `is_bot_writer.py` `run()`) stating: "Widening this window activates the ELSE NULL path against historical rows and erases stamps the current predicate can't re-derive. See `docs/IS_BOT_SCANNER_MEMORY_FIX_2026-07-26.md` §3. Do not widen without the confirmed-ledger in place."
2. Corresponding line in `docs/IS_BOT_COLUMN_DESIGN.md` addendum (see §5 of this note).

Warnings belong on triggers, not mechanisms. The ts-window is the trigger; the CASE expression is the mechanism.

---

## 4. Fix: `page_view_scanner_combos_confirmed` (persistent classifier memory)

### 4a. Table shape

```sql
CREATE TABLE page_view_scanner_combos_confirmed (
    path TEXT NOT NULL,
    user_agent TEXT NOT NULL,
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_by TEXT NOT NULL,       -- 'auto' | 'reviewed' | 'manual'
    evidence_ratio NUMERIC,           -- hits / distinct_visitors at confirmation
    evidence_row_count INTEGER,       -- hits at confirmation
    evidence_window_start BIGINT,     -- ts_start of the 7d window that triggered
    evidence_window_end BIGINT,       -- ts_end of that window
    last_seen_at TIMESTAMPTZ,         -- most recent refresh that saw this combo hot
    notes TEXT,                       -- reviewer notes for 'reviewed' entries
    PRIMARY KEY (path, user_agent)
);
```

### 4b. Read paths

Both writer's `_bot_update_sql` and canary's `_legacy_bot_pred` read scanner classifications from `page_view_scanner_combos_confirmed`. Neither reads from the trailing 7-day `page_view_scanner_combos` anymore.

### 4c. Write path (auto-ratchet, transactional)

`refresh_bot_hash_tables()` computes the trailing-7d scanner detection as today, then in the SAME transaction UPSERTs any detected combos into `page_view_scanner_combos_confirmed` with:

- `confirmed_by = 'auto'`
- `evidence_ratio`, `evidence_row_count`, `evidence_window_start`, `evidence_window_end` filled from the current detection
- `last_seen_at = NOW()` (ON CONFLICT: update last_seen_at only; original confirmation evidence is preserved)

No dwell period. **Dwell-before-ratchet is rejected: a dwell window is just eviction risk wearing a prudence costume — a burst could decay during its own dwell period, re-creating the amnesia in miniature.** Auto-ratchet + governance-removal beats manual-gate.

### 4d. `confirmed_by` distinction (weekly-audit hook)

| Value | Source | Governance |
|-------|--------|------------|
| `reviewed` | Backfill's review file, human-approved | Reviewed once at ratchet; changes require explicit decision |
| `auto` | Ongoing writer refresh, ratcheted at first detection | Audited weekly (Sunday) as "what did the machine convict this week"; reversal is one explicit decision |
| `manual` | Explicit decision (add or override) | The escape hatch |

The Sunday audit gets a standing one-liner: `SELECT path, user_agent, confirmed_at FROM page_view_scanner_combos_confirmed WHERE confirmed_by = 'auto' AND confirmed_at > now() - interval '7 days'`. Human oversight is preserved as *review*, not as *gate*: the machine acts immediately, the human audits weekly, reversal is always available.

### 4e-bis. Ledger scope — analytics-only, not blocking

Grep (2026-07-26) confirmed: no CF challenge, WAF rule, rate-limiter, 429 path, or middleware reads `page_view_scanner_combos_confirmed` or `is_bot`. The ledger feeds `_bot_filter_sql` (analytics classification) exclusively. **The ledger records what things are; it does not decide how we treat them.** This distinction is load-bearing: a "welcome bot" (ChatGPT-User, Bingbot) can appear in the confirmed set with `confirmed_by='reviewed'` — a correct classification — without any change to how the site serves them (no blocking, no challenge, no rate-limit). Any future component that reads `is_bot` for a treatment decision (block/challenge/throttle) must be surfaced as a separate design change and revalidated against the citation-moat rule.

### 4f. Backfill review — anomaly heuristics (from the 2026-07-26 candidate walk)

Two shapes surfaced in the 17-candidate review that neither the ratio+floor rule nor governance fields alone flag. Both are permanent classification-review discipline, not one-time notes:

- **Edge-ratio + all-windows-qualifying = category anomaly, always human review.** A combo that qualifies in EVERY daily window over the full history AND sits at ratio ≈ 1.05 (right at the ceiling) is baseline traffic wearing a burst mask, not a burst. Real bursts are episodic — a combo present continuously since day 1 is either a scraper permanent in the corpus or a real client segment; the ratio can't distinguish. Escalate to secondary checks (frozen-UA-string category, identity recurrence, timing shape) before ratifying. The founding case: iPhone iOS 13_2_3 exact UA, 73/73 windows qualified, ratio 1.05. Left permanently unconfirmed after review disputed the classification.
- **Frozen exact UA string with zero version spread is category evidence, not one-more-pillar evidence.** A single copy-pasted UA claiming 1,000+ distinct visitor_hashes with no iOS/Chrome version scatter is one client program wearing a costume — the mobile analog of the Chrome/142 shape. Geographic and referrer signals cannot overcome this alone; frozen-UA + high-hash-count-with-single-page-depth is fleet-shaped regardless of the pillars around it. Charlie 2026-07-26: "iOS 13.2.3 is a 2019 operating system, and this is one exact UA string — real user segments show iOS 16/17/18 scatter."

### 4g. ChatGPT-User is a welcome bot — the ledger is agnostic

Two ChatGPT-User entries appear in the ratified 16 with `confirmed_by='reviewed'`. Ratifying them is correct: they *are* bots (self-identifying, high hits/visitor with correct crawl pattern). They are also citation-moat traffic — one of the exact segments the site benefits from being read by. **The ledger records that they are bots; it does not decide how we treat them.** No blocking, no challenge, no rate-limit is wired to `is_bot=TRUE`. This distinction is why the ratchet is safe: convicting a welcome bot has no user-visible consequence. If that ever changes — if a future component reads `is_bot` for a treatment decision — the ChatGPT-User class needs an explicit carve-out before the change ships, not after.

### 4e. Retire `page_view_scanner_combos` — DEFERRED (three readers, not two)

Grep at build time (2026-07-26) shows three readers of `page_view_scanner_combos`:

1. `scripts/is_bot_writer.py:84` — the writer's `_bot_update_sql` (repointing to confirmed table as part of this scope).
2. `db.py:4911` — `_bot_filter_sql`, called by `/analytics` and other live queries in the pre-column-flip state (`_is_bot_column_ready=False`).
3. Nothing else. (Canary does NOT read this table — it rebuilds scanner_pred live via subquery.)

Because `_bot_filter_sql` is a third reader — powering the live `/analytics` render until the column flip — the retire-in-same-migration criterion isn't met. **Table stays.** Repointing `_bot_filter_sql` to the confirmed table would be a live-filter semantic change (`/analytics` would classify old bursts as bot even after decay), which is arguably the right semantic under the "conviction is permanent" doctrine but wants explicit Charlie sign-off, not a side-effect of this migration.

Follow-up decision (out of this scope): repoint `_bot_filter_sql` to the confirmed table too, then retire `page_view_scanner_combos`. Ship after the confirmed ledger has soaked and the column-flip is either done or imminent.

---

## 5. Design-doc addendum (goes into `docs/IS_BOT_COLUMN_DESIGN.md`)

Verbatim insert, dated 2026-07-26:

> **Comparison arms must share input SCOPE AND input MEMORY.** A classifier whose evidence tables forget confirmed verdicts will disagree with every correct historical stamp — not because its logic is wrong, but because the arm's memory is shorter than the thing it's checking. The founding case: the CheckHost scanner-combo residual of 2026-07-26 (`docs/IS_BOT_SCANNER_MEMORY_FIX_2026-07-26.md`). Fix: any classifier arm whose evidence is time-bounded (currently: `scanner_pred` only) writes its verdicts to a persistent confirmed ledger, and both writer and canary read from that ledger.
>
> **The evidence-table-not-stamp-table distinction (independence preservation).** The canary's independence rule ("must never be replaced by a table-subquery or column path") forbids the canary reading the writer's *verdicts*. It does NOT forbid the canary reading shared *evidence*. The distinction: BOT_UA_PATTERNS is a shared input to both arms' independent derivations — nobody would argue the canary must maintain its own copy of the pattern list to preserve independence. The confirmed scanner-combo ledger is the same kind of shared input: an evidence table, not a stamp table. Both arms still derive each row's classification via their own row/session/scanner/cohort logic; the ledger is what "did this combo ever meet the threshold" resolves against. The failure modes the canary catches (writer's `_bot_update_sql` diverging from canonical rules, refresh loop broken, CLASSIFIER_VERSION mismatch not resyncing) remain caught.

---

## 6. What was decided vs. what remains open

**Decided:**
- Fix option: (a-real-2) persistent confirmed ledger.
- Backfill approach: (i) definitive full-history walk, chunked/off-peak; NOT (ii) version-bump. Reason: (ii) inherits the amnesia it's meant to fix.
- Un-stamp landmine: comment + doc, not code change. Warning on the ts-window line.
- Auto-ratchet, immediate, reversible via governance.
- `confirmed_by` distinction: `reviewed` (backfill) vs `auto` (ongoing) vs `manual` (explicit).
- Sunday-audit hook on `confirmed_by = 'auto'` entries from the past week.
- Retire `page_view_scanner_combos`: **deferred** — grep found a third reader (`_bot_filter_sql` in db.py:4911, powering pre-column-flip `/analytics`). Table stays. See §4e.
- Deploy order: migration → backfill → review → populate → code → first canary run. Sequenced, not merge-and-observe.

**Open (build-time or later):**
- Follow-up: repoint `_bot_filter_sql` to the confirmed table and retire `page_view_scanner_combos`. Ship after this scope soaks and column-flip is done or imminent. Explicit Charlie decision required — it's a live-filter semantic change.
- `/methodology` page paragraph wording (same shape as burst-cohort disclosure — "detection is transient; conviction is permanent, reversible by explicit decision").

**Soak clock:** starts on the first clean scheduled canary PASS after ship. Three days. Then the `_is_bot_column_ready` flip is a separate deploy step. This does NOT start today; today closed the diagnostic layer, not the soak.

---

## 7. Scoring the arc

The is_bot arc has now caught three distinct lie-classes:

1. **Supervision lie** — walker's wrapper reporting healthy while the writer stalled (2026-07-26 morning; fixed in commit `6bd2010`, wrapper stale-lock recovery with visible walker_health failure).
2. **Comparison-arm amnesia** — canary's scanner arm losing memory of verdicts the writer stamped from earlier snapshots (2026-07-26 afternoon; being fixed by this note's scope).
3. **Dormant erasure landmine** — writer's `CASE WHEN ... ELSE NULL` gated only by the ts-window; a one-line optimization away from silent history-destruction (2026-07-26; marked by the comment + this doc, not code change).

Each was caught, named, and either closed or plumbed against. The arc closes when the confirmed ledger ships and the first clean scheduled canary PASS lands.

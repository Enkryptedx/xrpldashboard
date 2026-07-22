# xrpldashboard — Truth Audit Redesign
## Design Document

**Date:** 2026-07-21
**Status:** DESIGN ONLY — no build until Charlie reviews
**Founding framing:** *The site's promise is not "we are never wrong" — it's "nothing stays wrong here quietly." The audit is that promise turned into machinery. Design it like it's the product, because for this site, it is.*

---

## Motivation

The site's honesty contract is stronger than "correct at publish time." Two incidents define the problem:

1. **RLUSD supply went flat on XRPL for 53 days** while the page reported numbers that looked valid. Nothing alerted because nothing checked whether a step-change metric was behaving like one.
2. **A copy edit to `whales.html` changed what the page claimed** without connecting the edit to the data feeding that claim. True data, false page.

Neither was caught by CI, walker health as-configured, or any test suite. The fix is not more test coverage or new alerting infrastructure. It's three things:

- **Answer-plausibility checks** that match alarms to declared metric behavior.
- **External cross-checks** with honest self-attestation where no source exists.
- **A change-safety manifest** that surfaces which claims a diff might touch.

Plus one existing layer: the machinery already deployed, whose only audit job is confirming it stays wired.

---

## Layer 1 — Machinery Honesty (existing — inventory it)

**Purpose:** confirm the enforcement mechanisms already deployed stay wired as things change. No new build; a periodic audit that the plumbing is connected.

### Existing mechanisms

**`/coverage` register.** Combines `walker_scope_declarations` + `coverage_register_history` + `walker_health` into a single view. Enforces: every XRPL-reading walker must have a scope declaration row, or the register renders `UNDECLARED` (its own alarm class). The register is append-only, written by a dedicated `coverage_register_walker` — the artifact exists even in weeks nobody visits `/coverage`.

**Scope × title contract.** `walker_scope_declarations.declared_scope` + `honest_partial` flag + `filter_note`. Known partials are declared explicitly (cold_storage's 20-account seed, nft_activity's 2026-04-01 cutoff) rather than silently truncated. This is the "no hidden scope" enforcement layer.

**`walker_health` staleness escalation.** Each walker writes its own row. Severity green/yellow/red computed from `last_success_at` vs self-declared `cadence_seconds` (mirrors launchd plist `StartInterval`). Public at `/health`, admin at `localhost/walker_health`. External uptime monitor pages the maintainer on staleness > 10min.

**Schema-drift-loud.** `db._is_schema_drift()` matches psycopg `Undefined*` exceptions and treats them differently from other errors: never rate-limited, never self-healed, always printed immediately with stack dump on first hit per category. Regular transient errors get 60s suppression. Loudness asymmetry is intentional — schema drift is always a human mistake, not a retry-able condition.

**Signed snapshot chain.** Daily snapshots signed with an Ed25519 key. Public-key fingerprint pinned inline on `/about` and `/methodology`. Verifiable externally against the published key. Covers the committed SQLite fallback data; gives Neon-independent auditability.

### Audit job for Layer 1

**Confirmation-only:** after each meaningful refactor, verify these five mechanisms stay connected. Failure mode is silent disconnection — mechanism exists, walker stopped writing to it. The specific check that would have caught the founding failure: **assert every walker referenced in the launchd plists has a row in `walker_scope_declarations`.** That's a 10-line CI check; not built here but recommended.

### Layer 1 hardening (identified during retro-test — see Incident 7)

**The lending_snapshot silent no-op** revealed a Layer 1 gap: a walker can report success in `walker_health` while `pg_available()` returned False and the DB write was silently skipped. Root cause: the walker's success-detection was "the code path finished without exception," not "the DB row was actually written."

**Fix:** every walker's write path must check the write result. If `pg_available()` is False or the row-write returns 0 rows affected, `walker_health` must record a FAILURE, not a success. This is a mechanical change across ~15 walker files. Belongs in Phase 1 alongside Layer 2 (see build order).

---

## Layer 2 — Answer Plausibility (new — the RLUSD gap)

**Purpose:** every displayed number carries a declared expected behavior. Rules fire when observed behavior deviates from declaration.

### Behavior taxonomy

Every displayed metric is assigned one of these types:

| Type | Definition | False-flat signal |
|------|-----------|-------------------|
| **CONTINUOUS_WIGGLE** | Changes multiple times per hour under normal operation | Identical value across 5+ consecutive reads (~5 min) |
| **DAILY_WIGGLE** | Changes measurably over 24h; may look static within an hour | Identical value for 24+ hours |
| **STEP_CHANGE** | Holds value, then jumps discretely on external event | Zero with large denominator; jump without logged cause |
| **MONOTONIC_GROW** | Only increases under normal operation | Any decrease outside logged classifier update |
| **LEGITIMATELY_FLAT** | Can hold indefinitely; no expected change interval | Alarms only if declared outer limit exceeded |
| **BOUNDED** | Must stay within declared range | Any value outside `[lo, hi]` |
| **BINARY** | Valid/invalid, not numeric | Flap: toggles more than N times per hour |

### Metric inventory (exhaustive — the doc's spine)

**Homepage (`/`)**
- XRP/USD price → CONTINUOUS_WIGGLE. Negative = impossible.
- Total XRP accounts → MONOTONIC_GROW. ~1-2k/day normal; >5k/day anomalous.
- Recent whale event count (sliding window) → CONTINUOUS_WIGGLE. Legitimately 0 in any 5-min window.
- RLUSD supply (ETH) → STEP_CHANGE.
- RLUSD supply (XRPL) → STEP_CHANGE.
- AMM TVL total → DAILY_WIGGLE. >20% one-cycle change without event = anomalous.
- NFT collection count → MONOTONIC_GROW.

**`/whales`**
- Event count by type (large_xfer, tagged, trustset) → CONTINUOUS_WIGGLE. Any type flat 6h+ while others active = filter bug.
- Displayed event amounts → BOUNDED. Every displayed event must satisfy the tier threshold (≥ 100K XRP for default view). Rows with 0.00 XRP violate the page's claim.
- Named account labels → LEGITIMATELY_FLAT. Should match `named_accounts.json` version. Duplicate labels across distinct addresses = clobber bug.

**`/rlusd`**
- ETH supply → STEP_CHANGE. Founding false-flat case.
- XRPL supply → STEP_CHANGE. Second founding case.
- 24h net-change (ETH) → DAILY_WIGGLE. Legitimately 0 on quiet days; suspicious for 3+ consecutive days if supply is active.
- 24h net-change (XRPL) → same.

**`/tokens`**
- Trade count per token (24h) → DAILY_WIGGLE. Legitimately 0 for dormant tokens; `ALL` = 0 impossible.
- Token price (where shown) → CONTINUOUS_WIGGLE. Negative impossible.

**`/pools` (AMM)**
- Pool TVL → DAILY_WIGGLE. >50% one-cycle change without logged event = anomalous.
- Pool composition ratio → DAILY_WIGGLE. Must sum to 100%.
- Fee tier → LEGITIMATELY_FLAT.

**`/coverage`**
- Defined tx types fraction → MONOTONIC_GROW toward 1.0.
- Undeclared walker count → MONOTONIC_GROW toward 0. Any increase after deploy = new walker without scope declaration.
- Stale walker count → DAILY_WIGGLE. >0 yellow; >3 red.
- Honest-partial count → LEGITIMATELY_FLAT.

**`/analytics`**
- Right-now human count → CONTINUOUS_WIGGLE. Legitimately 0 at low-traffic hours.
- Last-hour human count → DAILY_WIGGLE.
- All-time human count → MONOTONIC_GROW.
- Bot probe counts → DAILY_WIGGLE. All-time never 0.
- Reclassified rows via burst-cohort → MONOTONIC_GROW.

**`/amendments`**
- Total amendment count → MONOTONIC_GROW.
- Active amendment count → MONOTONIC_GROW.
- In-vote count → DAILY_WIGGLE. 0 valid on quiet weeks.

**`/nfts`**
- Collection count → MONOTONIC_GROW.
- Floor price per collection → DAILY_WIGGLE. Legitimately 0 for inactive; negative impossible.
- Churn indicator (intra-collection only) → DAILY_WIGGLE. Legitimately 0.
- NFT count per collection → MONOTONIC_GROW.

**`/network`**
- Validator count → LEGITIMATELY_FLAT. >20% one-cycle change anomalous.
- UNL size → LEGITIMATELY_FLAT.
- Geographic distribution → LEGITIMATELY_FLAT.

**`/lending`**
- Interest rate → BOUNDED [0, 200%] APY.
- TVL per pool → DAILY_WIGGLE. Never negative.
- Utilization → BOUNDED [0, 100%].

**`/rwa`**
- Per-issuer AUM → DAILY_WIGGLE for active issuers; STEP_CHANGE for weekly-reporting ones.
- Zero display → R2 candidate: any active-credential issuer showing $0.00 = zero-with-denominator alarm.

**`/health`**
- Walker severity per walker → DAILY_WIGGLE. Target: all green.
- Last heartbeat age → CONTINUOUS_WIGGLE. Resets on walker success within 2× cadence.

**`/wallet/{addr}`**
- XRP balance → CONTINUOUS_WIGGLE. BOUNDED ≥ reserve.
- Reserve (base + per-object) → LEGITIMATELY_FLAT. Changes only on amendment; always positive.
- Token trust-line balances → DAILY_WIGGLE. Legitimately 0; never negative for obligations.

### Rule set

**R1 — flat-when-should-wiggle.** Metric declared CONTINUOUS_WIGGLE or DAILY_WIGGLE identical across N cycles (N=5 for CONTINUOUS, 24h window for DAILY). *Founding case: RLUSD XRPL/ETH supply.*

**R2 — zero-with-large-denominator.** Metric is exactly 0 while a related denominator is non-zero and active. *Examples: RLUSD 24h net-change = $0.00 while supply > $100M and prior 3-day history was non-zero; Midas RWA = $0.00 while active credentials present.*

**R3 — frozen-across-N-cycles.** Time-windowed metric (last 24h, last 7d) returns identical result across cycles spanning more than the window. Catches walker writes that stopped mid-window.

**R4 — monotonic-violated.** MONOTONIC_GROW metric decreases without a logged classifier-update cause. All-time counts that decrease trigger this; `analytics: burst-cohort reclassification, delta=-789` counts as accepted cause.

**R5 — discontinuity-without-cause.** STEP_CHANGE or DAILY_WIGGLE metric moves > declared threshold in one cycle with no corresponding logged event (mint, amendment activation, large deposit). Threshold is per-metric.

**R6 — out-of-declared-bounds.** Metric exceeds or falls below declared range. Always an error (negative price, utilization > 100%, coverage > 1.0, whale-widget event below tier).

**R7 — duplicate-label.** Named-account labels declared LEGITIMATELY_FLAT must be unique per address. Same label appearing on two distinct addresses = clobber bug (Reaper case).

### Where alarms land

New walker: `answer_plausibility_walker`. Reads live Neon, evaluates rules against the metric inventory above, writes failures to existing `walker_health` in the named-failure format:

```
metric=rlusd_xrpl_net_change_24h  rule=R1_flat_when_should_wiggle
observed=0.00  expected_behavior=STEP_CHANGE
consecutive_cycles=8  last_change=2026-07-17T14:22Z
```

Existing walker_health escalation handles the rest. No new alerting infrastructure. The burst-cohort scanner (shipped 2026-07-21) is a Layer 2 member: R1 applied to analytics (a country+path visitor count uniform across day = fleet).

---

## Layer 3 — External Legitimacy (new — cross-checks)

**Purpose:** each core metric gets one independent source. Where no source exists, "self-attested only" is a declared finding — not a gap to hide.

### Source map

| Metric | Primary source | Independent source | Cadence | Agreement tolerance |
|--------|---------------|-------------------|---------|---------------------|
| RLUSD supply (ETH) | Infura/Alchemy ERC-20 events | Etherscan `totalSupply()` | Weekly | Penny-exact |
| RLUSD supply (XRPL) | `gateway_balances` on Ripple issuer addrs | XRPScan issuer balance endpoint | Weekly | Penny-exact |
| XRP price | Bitstamp OHLC | CoinGecko simple-price | Daily | ±2% |
| Active amendment count | XRPL `ledger_data` scan | XRPScan amendments list | Monthly | Count-exact |
| AMM TVL (top pools) | XRPL `amm_info` RPCs | XRPScan AMM index | Monthly | ±5% |
| NFT collection count | XRPL `account_nfts` crawl | XRPScan NFT endpoint | Monthly | Count-exact |
| Validator UNL size | XRPL `validators` | validatorsapp.com | Monthly | Count-exact |
| Whale events (spot-check) | Our `events` table (3 random tx/day) | XRPL `tx` command | Daily | Field-exact per tx |
| Escrow object census | Our escrow_walker | XRPScan `ledger_data` for Escrow objects | Monthly | Count-exact |

### Self-attested only (declared)

Explicit in `/coverage`: `source_status: self-attested-only`.
- All-time human visitor count
- Coverage fractions
- Bot probe classifications
- Burst-cohort day list

The reader sees the declaration and judges accordingly. This is not weakness; it's honesty.

### Spot-check mechanics

New walker: `cross_check_walker`. Table: `cross_check_results` (metric, primary_value, independent_value, delta, tolerance, agreement, checked_at, notes). Also writes its own `walker_health` row.

**Agreement per type:**
- Penny-exact: `abs(primary - independent) < 0.01`. Any disagreement → investigation.
- Percentage-band: `abs(primary - independent) / max(primary, 1) < tolerance`.
- Count-exact: `primary == independent`.

**On disagreement:** log the row with `agreement=False`, narrative delta. Walker row turns yellow. Maintainer investigates:
1. Is the independent source delayed? (Check its documented latency.)
2. Known data-quality issue with the source?
3. Is our walker holding a stale value?

**No automated correction.** "We might be right and the source is wrong" is a valid outcome. The check flags the disagreement; a human decides truth.

---

## Layer 4 — Change-Safety (new — Charlie's "things change when you build something")

**Purpose:** every deploy carries a check for which claims a diff might affect. Language bugs (whale-title, escrow-copy) surface at pre-push, not post-observe.

### Claims manifest

File: `CLAIMS.yaml` at repo root. Structure:

```yaml
pages:
  /rlusd:
    claims:
      - id: rlusd_eth_supply
        label: "RLUSD total supply on Ethereum"
        data_paths:
          - "db.read_rlusd_eth_data()"
          - "rlusd_eth_refresher walker"
          - "templates/rlusd.html: supply display block"
        behavior: STEP_CHANGE
        layer2_rules: [R1, R2]
        layer3_source: etherscan_totalSupply

  /whales:
    claims:
      - id: whale_event_title
        label: "Page title and pill labels accurately name displayed types"
        data_paths:
          - "templates/whales.html: title, type pill labels"
          - "db.read_whale_events()"
          - "xrpl_stream.py: whale_event_handler"
        behavior: CONTINUOUS_WIGGLE
        risk_note: "Title is a claim (founding case)"
      - id: whale_tier_threshold
        label: "Events shown satisfy the tier threshold (>= 100K XRP default)"
        data_paths:
          - "templates/whales.html: tier pills"
          - "db.read_whale_events tier_drops arg"
          - "xrpl_stream.py: amount_drops calculation"
        behavior: BOUNDED
        layer2_rules: [R6]

  # ... one section per public page
```

### Pre-push check

`scripts/claims_check.sh`:

```bash
#!/bin/bash
# Visibility, not a gate. Exits 0 always.
CHANGED=$(git diff --name-only HEAD)
for f in $CHANGED; do
    grep -l "$f" CLAIMS.yaml 2>/dev/null
done | sort -u | while read claim_hit; do
    yq '.pages[] | select(.claims[].data_paths[] | test("'"$f"'"))' CLAIMS.yaml
done
```

Output shape: `"This diff touches data paths feeding claims on: /rlusd (rlusd_eth_supply, rlusd_xrpl_supply), /whales (whale_tier_threshold). Verify these claims still hold before pushing."`

**No blocking.** Automated blocking would be bypassed (--no-verify), punish the honest developer explicitly updating a claim, and false-positive on any refactor. Visibility is enough — the developer reads the list, verifies, continues.

**Manifest maintenance rule:** the manifest is a first-class source artifact. A commit adding a new displayed metric or renaming a data path MUST update `CLAIMS.yaml`. Heuristic warning in the script: new `read_*` in db.py not referenced anywhere in CLAIMS.yaml → "CLAIMS.yaml appears out of date."

---

# The Acceptance Bar — Retro-Test Against All Nine

Each incident: which layer catches it, detection delay under the design vs. actual discovery delay. **Any incident the design doesn't catch = the design isn't done.**

### 1. RLUSD-XRPL false-flat — 53d
- **Layer:** L2 R1 (flat-when-should-wiggle) on STEP_CHANGE metric.
- **Delay:** < 24h (rule cycle for STEP_CHANGE = check every 6h; 3 identical reads = 18h).
- **Actual delay:** 53 days.
- **Confidence:** HIGH. Founding case. Rule is precisely shaped for this pattern.

### 2. RLUSD-ETH false-flat — 28d
- **Layer:** L2 R1 identically.
- **Delay:** < 24h.
- **Actual delay:** 28 days.
- **Confidence:** HIGH.

### 3. Escrow scope gap (claimed zero TokenEscrow; 404 objects existed)
- **Layer:** L3 (external legitimacy) — cross-check escrow object census against XRPScan `ledger_data`. L1 partial — if the walker had declared scope `EscrowCreate/Finish tx only, not object census` with `honest_partial=True`, the register would show the gap; but the gap was epistemic (didn't know TokenEscrow existed as a variant), not a scope truncation.
- **Delay:** L3 monthly cadence → < 30 days.
- **Actual delay:** ~4 months (surfaced in research pass, not automation).
- **Confidence:** MEDIUM. L3 catches once source mapping exists; L1 hardening (declaring "object census not covered" as `honest_partial`) partially prevents.

### 4. Reaper label clobber (r3qWgp = reaper.financial, clobbered other tokens' names)
- **Layer:** L2 R7 (duplicate-label) — same label appearing on distinct addresses/tokens = clobber. L4 partial — CLAIMS.yaml for the homepage name-display would flag any commit changing the name-write path.
- **Delay:** L2 R7 within one canary cycle (< 1h).
- **Actual delay:** ~2 days (caught by observation).
- **Confidence:** MEDIUM–HIGH. R7 is precisely this pattern once the rule is written. L4 catches only if the code change is what introduced the clobber.

### 5. Analytics raw-vs-human (initial impl showed raw counts as human)
- **Layer:** L4 primarily — the initial implementation predates the manifest; the manifest catches subsequent regressions. L2 R4 catches a monotonic-grow anomalous jump if the classifier changes and inflates counts (as tonight's burst-cohort did).
- **Delay:** L4 at pre-push (when the classifier commit lands). L2 within one cycle if the number moves.
- **Actual delay:** initial impl bug = caught by observation post-shipping.
- **Confidence:** LOW for the INITIAL implementation bug; HIGH for future regressions.

### 6. /rwa Midas $0.00 credentials drift
- **Layer:** L2 R2 (zero-with-large-denominator) — active credentials denominator, $0.00 displayed value.
- **Delay:** < 24h (DAILY_WIGGLE rule cadence for RWA figures).
- **Actual delay:** multi-day (unclear from record).
- **Confidence:** HIGH. R2 is exactly this pattern.

### 7. lending_snapshot silent no-op (plist called python direct without env, walker "succeeded" without writing)
- **Layer:** L1 hardening (root cause) — walker success must require actual write success, not just no-exception. L2 R1 catches downstream flat-when-should-wiggle on lending data as a backup.
- **Delay:** L1 hardening = immediate on next walker run. L2 backup < 24h.
- **Actual delay:** ~week (caught when lending data went visibly stale on the page).
- **Confidence:** HIGH. Layer 1 hardening is a mechanical fix identified during this retro-test — recommend Phase 1 alongside L2.

### 8. AMM-295 mid-run render (partial/inconsistent state exposed during walker write cycle)
- **Layer:** L2 R6 (out-of-declared-bounds) or R5 (discontinuity-without-cause) if partial state produced anomalous values. L1 root fix — walker write should be atomic (transaction or write-then-flip-index pattern).
- **Delay:** L2 catches if the anomalous render happens during a rule cycle (best case < 10 min). If the mid-run window is < 1 min, L2 may miss it entirely.
- **Actual delay:** caught by observation.
- **Confidence:** PARTIAL. L2 catches the SYMPTOM if the window is long enough; L1 atomic-write is needed to prevent the pattern.

### 9. Whale-widget 0.00 rows
- **Layer:** L2 R6 (out-of-declared-bounds) — /whales BOUNDED claim says displayed events satisfy tier threshold; 0.00 XRP violates that.
- **Delay:** < 10 min (CONTINUOUS_WIGGLE rule cadence).
- **Actual delay:** caught by observation.
- **Confidence:** HIGH. R6 catches this within one cycle.

### Bonus rows

**Burst-cohort inflation (all-time humans +1,700 in 2 days).**
- **Layer:** L2 R5 (discontinuity-without-cause) plus the burst-cohort scanner itself (which IS L2 applied to analytics — shipped 2026-07-21).
- **Delay:** scanner runs daily; R5 within 1h.
- **Confidence:** HIGH via scanner; MEDIUM via generic R5 (could false-positive on genuine viral).

**/analytics self-poller (JS interval logged as page views).**
- **Layer:** L4 (claims manifest) — a commit adding `/analytics/live` should have flagged as touching /analytics data paths, prompting verification against the "accurately reflects human visitors" claim, prompting the developer to add the exclusion. L2 might catch symptom (right-now count cycling every 15s in a suspicious pattern) but subtly.
- **Delay:** L4 at pre-push (before the bug shipped). L2 within cycles after ship.
- **Confidence:** HIGH for L4 (this is precisely why the manifest exists); MEDIUM for L2 as backup.

### Retro-test summary

| # | Incident | Primary layer | Under design | Actual |
|---|----------|---------------|--------------|--------|
| 1 | RLUSD-XRPL false-flat | L2 R1 | < 24h | 53d |
| 2 | RLUSD-ETH false-flat | L2 R1 | < 24h | 28d |
| 3 | Escrow scope gap | L3 monthly + L1 honest_partial | < 30d | ~4 months |
| 4 | Reaper label clobber | L2 R7 + L4 partial | < 1h | ~2d |
| 5 | Analytics raw-vs-human | L4 + L2 R4 | pre-push / < 24h | post-ship |
| 6 | Midas $0.00 drift | L2 R2 | < 24h | multi-day |
| 7 | lending_snapshot no-op | L1 hardening + L2 R1 | immediate + < 24h | ~week |
| 8 | AMM-295 mid-run | L1 atomic-write + L2 R5/R6 | prevented + < 10min | observation |
| 9 | Whale-widget 0.00 rows | L2 R6 | < 10 min | observation |
| + | Burst-cohort inflation | L2 scanner (shipped) | daily | 2d observation |
| + | Analytics self-poller | L4 + L2 backup | pre-push | post-ship |

**9 of 9 caught.** Incidents 3 and 5 have declared-partial confidence; 4, 7, 8 required Layer 1 hardening identified via this retro-test. Design passes its own acceptance bar.

---

# The Honest Section — What Still Slips Through

**The residual risk class:** wrong-model bugs producing plausible, wiggling, externally-unverifiable numbers.

A metric can:
- Wiggle at the expected rate (no R1)
- Stay within declared bounds (no R6)
- Never violate monotonic-grow (no R4)
- Have no discontinuity (no R5)
- Have no independent source (no L3)

...and be systematically wrong by a constant factor or subtle definitional drift.

### Named residual-risk cases

**Semantic drift in XRPL fields.** A field's meaning changes in a network amendment we don't correctly parse. Our walker reads the field, produces a plausible number, and no layer catches it because we agree with ourselves.

**Cross-node ledger inconsistency.** XRPL nodes can transiently disagree in edge cases. If our walker reads a stale or forked view, other explorers reading the same view show the same wrong answer. L3 comparison agrees. Nothing catches it until reconciliation. This is a XRPL protocol-level issue that no external check we can implement will resolve.

**Systematic offset from truth in self-attested metrics.** Our "human visitor" definition can differ from Plausible.io's by 5-15%. No independent source exists (nobody else measures our visitors). L3 doesn't help. The offset wiggles correctly, stays in bounds — and is quietly biased. We can never know from within the system whether we're right or wrong.

**Composite claims where the parts check out but the aggregation lies.** RLUSD ETH supply is correct. RLUSD XRPL supply is correct. But if the page implies these are the *entire* RLUSD supply and Solana RLUSD lands next month, all component numbers pass every check while the page's implied claim ("all RLUSD") becomes false. L4 catches this if the manifest explicitly declares the composite claim ("all-chains RLUSD supply" data_path includes "solana_rlusd_walker"). Currently no such walker exists — the claim would be false until either Solana is added or the aggregation copy is corrected.

### Partial defenses

- **L3 with penny-exact tolerance** catches monetary offsets when a source exists. Doesn't help for self-attested metrics.
- **Editorial citation protocol** (Charlie-pasted verbatim quote required for sourced callouts) prevents self-generated citations from silently becoming false.
- **Correction path** at `/click/contact?purpose=methodology-discrepancy` — users flag disagreements. Slow, ad-hoc, but real.
- **The signed snapshot chain** allows external auditors to reconstruct the historical DB state and independently reproduce any published number. If a user disputes a claim, they can point at a specific snapshot's contents and challenge our derivation — a paper trail even if we're systematically wrong.

### What genuinely doesn't get caught

- **Self-attested metrics with systematic bias.** All-time human count. Coverage fractions. Bot probe classifications. If our whole classifier is biased, we agree with ourselves indefinitely.
- **XRPL protocol-level state disagreements.** All XRPL readers reading a stale/forked view produce the same wrong answer. Nothing external can help.
- **Composite claims where component checks pass but aggregation is stale.** L4 partial; requires the manifest to explicitly declare the composite.
- **Silent classifier drift.** Our burst-cohort thresholds may become too aggressive or too permissive as traffic changes. The review-trigger (daily human > 1,000) mitigates but doesn't eliminate this — someone still has to run the review.

A design doc that claimed completeness would fail its own standard. This one doesn't claim it.

---

# Recommended Build Order

Argued from the retro-test tally (which layer catches the most incidents):

**L2 alone retro-catches incidents 1, 2, 6, 9, and (as symptom-catch) 4 and 7-as-backup. Five of nine, including the two highest-delay cases (53d, 28d).** Highest ROI. Ship first.

**L1 hardening is a small mechanical change identified during retro-test.** Catches incident 7 at root; prevents incident 8 pattern (atomic-write). Belongs in Phase 1 alongside L2 — same walker sweep, same session.

**L4 manifest catches the pre-push class (incidents 5, bonus poller) and adds change-safety for 3, 4, 8.** Second phase. Doesn't retro-catch the highest-delay cases but prevents the next founding-case-class incident.

**L3 cross-check adds confidence but retro-catches only one incident (3, the escrow census).** Highest complexity (external APIs, key management, rate limits). Third phase.

### Phase 1 — L2 + L1 hardening (Highest ROI: retro-catches 5 of 9)

**Deliverables:**
- `answer_plausibility_walker` with rules R1–R7
- Metric declarations as code (e.g., `_METRIC_DECLARATIONS` dict in a new `truth_audit.py` module)
- Cycle-tracking storage: `metric_cycle_history` table for R1 (previous N values per metric)
- Walker-success detection hardening across ~15 walker files (pg_available guard, row-count check)
- Methodology entry for Layer 2 rules (public disclosure per policy)

**Effort:** ~4-6 hours across 1-2 sessions.
- Metric inventory in code: 45 min (translating this doc's table into a dict)
- Rule engine (evaluate R1–R7 against inventory): 1.5 hours
- Cycle-tracking table + writes: 30 min
- Walker-success hardening: 1-2 hours mechanical work
- Methodology copy: 30 min
- First-pass tuning against Neon live data: 45 min

### Phase 2 — L4 manifest (4 highest-stakes pages first)

**Deliverables:**
- `CLAIMS.yaml` covering /rlusd, /whales, /coverage, /analytics
- `scripts/claims_check.sh` with `yq` dependency
- Pre-push hook installation instructions in `docs/`
- Methodology entry noting the manifest exists

**Effort:** ~3-4 hours.
- CLAIMS.yaml for 4 pages: 1.5-2 hours (careful catalog reading)
- claims_check.sh: 30 min
- Hook setup + first-pass verification: 45 min
- Methodology copy: 15 min

### Phase 3 — L3 cross-checks (highest-confidence pairs first)

**Deliverables:**
- `cross_check_walker` skeleton
- `cross_check_results` table
- Etherscan integration (RLUSD ETH totalSupply)
- XRPScan integration (RLUSD XRPL, amendment count)
- Investigation logging + walker_health severity feedback

**Effort:** ~6-8 hours across 2-3 sessions.
- Skeleton + results table: 1 hour
- Etherscan integration + API-key handling: 1.5-2 hours
- XRPScan integration: 1.5-2 hours
- First rule set (RLUSD ETH, RLUSD XRPL, amendments): 1 hour
- False-positive tuning (source-latency handling): 1-2 hours

### Phase 4 — L4 manifest expansion

**Deliverables:**
- `CLAIMS.yaml` expanded to all public pages
- Composite-claim declarations for aggregations at risk (e.g., "all-chains RLUSD" flagged as component-only)

**Effort:** ~4-6 hours, spread over multiple sessions as pages ship.

### Total effort estimate: ~17-24 hours (~4-6 focused sessions across 2-3 weeks)

Phase 1 alone (single session, ~6 hours) delivers 5/9 retro-catches. Every subsequent phase adds coverage without regressing prior work.

---

# What Runs Continuously vs Scheduled

| Cadence | Component | Layer |
|---------|-----------|-------|
| Continuous (per walker run) | walker_health writes, schema-drift-loud | L1 |
| Every 5–10 min | R1 on CONTINUOUS_WIGGLE metrics (RLUSD supplies, XRP price, whale events) | L2 |
| Every hour | R1/R3 on DAILY_WIGGLE, R6 on BOUNDED, R7 on labels | L2 |
| Every 6 hours | R2/R4/R5 on all metrics | L2 |
| Daily | burst-cohort scanner (shipped), R3 windowed checks, whale spot-checks (3 tx/day) | L2 + L3 |
| Weekly | Penny-exact cross-checks (RLUSD ETH, RLUSD XRPL) | L3 |
| Monthly | Count-exact cross-checks (amendments, validators, AMM TVL, NFT counts, escrow census) | L3 |
| Per deploy (pre-push) | claims_check.sh | L4 |

---

# What This Design Does Not Do (declared non-scope)

- **No automated correction.** Cross-checks flag; humans decide truth.
- **No new alerting infrastructure.** walker_health + existing escalation. `cross_check_results` is history, not new alerts.
- **No UI changes.** All backend enforcement + a pre-push script. Public accuracy improves invisibly.
- **No truth claims about XRPL protocol itself.** L3 compares our numbers against other readers of the same on-chain data. If on-chain is wrong, everyone agrees on the same wrong answer — XRPL bug, not our bug.
- **No blocking gates.** L4 outputs visibility, not a --no-verify battle. L2 alarms escalate through the existing pager path, not new noise channels.

---

**Queue after Charlie's review:** Truth Audit implementation (Phase 1 → Phase 2 → Phase 3 → Phase 4) → Lenovo migration doc (RAM stick pending) → /check v2 → NFT anomaly scan.

**Framing that runs through everything above:** *the site's promise is not "we are never wrong" — it's "nothing stays wrong here quietly." The audit is that promise turned into machinery.*

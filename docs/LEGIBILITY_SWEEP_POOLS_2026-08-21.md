# Legibility Sweep — /pools (entry 1 of 13)

**Filed:** 2026-08-21 (overnight sitting, turn 4) — **REFILED 19:37 EDT** as a legibility-sweep artifact, NOT the /pools assignment.
**Author:** JJ 🦞
**Status:** Draft for Charlie's morning review. Do not ship without ack.
**Scope:** Applies the 4-part scope-note structure from `feedback_scope_notes_human_first.md` to /pools. First of 13 pages in the post-cert legibility batch.
**Sibling files:** `docs/PHASE2_MEMORY_AWARE_CACHE_DESIGN.md`, `docs/LIVE_FETCH_AMENDMENTS_DESIGN.md`, `docs/OAUTH_CREDENTIAL_INVENTORY_DESIGN.md`, `docs/SATURDAY_QUEUE_2026-08-22.md`

---

## Correction banner (2026-08-21 19:37 EDT)

This file was originally landed as `POOLS_FULL_WRITEUP_2026-08-21.md` against Charlie's Friday-night queue item "/pools full writeup." **That was a scope drift on my part.** The real POOLS-COMETS assignment is the live-mode graphic upgrade design pack, not a legibility rewrite:

- **Phase A** — directional comets. `xrpl_stream.py:767` and `:773` compute `abs()` on deltas, discarding sign. `pools.html:830` deliberately won't fake direction downstream. Fix: preserve signed delta end-to-end.
- **Phase B** — pool→pool arcs. Requires multi-emit for path payments (one event → multiple pool touches).
- **Phase C** — live top-10 refresh. Star set is currently frozen at page-render; needs `/api/pools/top10` + diff-render on the client.

Source: my own gap-analysis Thursday morning. Assignment was carried on Friday's manifest.

This artifact is useful and stays on disk — refiled as legibility-sweep entry 1. The real /pools pack drafts in Saturday's fresh session. See `docs/SATURDAY_QUEUE_2026-08-22.md`.

---

## 1. Purpose of this doc

The `/pools` page has grown organically from "list every AMM sorted by TVL" into a substrate feature: the constellation hero fires live comets on real on-chain events, TVL is bucketed by trust status (`exact` / `estimated` / `non_xrp_pair`), spoof-side detection surfaces `⚠` markers on top-100 rows, and the page is now the single source of truth that the homepage AMM card + Top-pools reads from (`app.py:1667`, `1925-1926`).

That growth has out-run the page's own methodology surface. Trust-adjacent copy is scattered across six `explainer()` tooltips and two callouts — good coverage in aggregate, no single "can I trust this page?" answer for a skeptical reader landing cold. The 4-part scope-note structure ratified 2026-08-18 (`feedback_scope_notes_human_first.md`) has never been applied to /pools.

This writeup: (a) inventories what /pools currently is and shows, (b) enumerates the honest limits it should say out loud, (c) drafts the 4-part scope note, (d) files the backlog of near-term substrate work (pool history, order-book gap, freshness surface, spoof-detection story).

**What this doc is NOT:** a build order. Nothing here ships without Charlie's ruling. This is the writeup Charlie asked for as an editorial+design foundation for /pools work that follows.

---

## 2. Current state — what /pools is today

### 2.1 Route + data path

- Route: `app.py:5285-5410` (`@app.route("/pools")`)
- Data source: `_ranked_amm_snapshot()` → `amm_ranked.json` produced by `rank_amms.py` on a ~4h `launchd` cadence
- Postgres mirror: `db.replace_amm_ranked_pools()` truncate-and-replace singleton (`rank_amms.py:346-437`)
- Template: `templates/pools.html` (1,244 lines)
- Tiers: `?tier=100` (default) / `?tier=500` / paginated browse-all at 500/page
- Constellation live-stream: `/api/pools/recent_events` (`app.py:6341-6360`)

### 2.2 What it shows

- **Hero:** top 10 by TVL rendered as star constellation, star radius ∝ share-of-top-10, live comets fire on real deposit/withdraw/swap events via WebSocket
- **Stats strip:** total TVL (XRP-paired only), pools ranked count, exact count, estimated count, token-token count, spoof-side count (conditional)
- **Table:** tiered view of ranked rows with `⚠` markers on flagged spoof sides
- **Snapshot chip:** `snapshot_age_label` on TVL stat (`Ns/Nm/Nh/Nd ago`)
- **Empty state:** honest — tells reader the bootstrap scan indexed N AMMs but ranking hasn't run yet
- **Callout block:** "Why TVL has different statuses" + "Catalog is still expanding"

### 2.3 What it does well already

- **TVL trust bucketing is honest.** `exact` (stablecoin-anchored) vs `estimated · 2× XRP` vs `token-token pair` is a real trust distinction, not cosmetic. Non-XRP pairs sink to the bottom rather than being priced with a guess. Total-TVL headline excludes the non-XRP pairs by construction (`app.py:5358-5362`).
- **Spoof-side detection surfaces the ⚠.** Sides where currency code matches a TOML-attested brand but issuer wallet does not match get the marker (`app.py:5346-5351`). Same substrate `/rwa` uses to gate verified-family inclusion — cross-page consistency.
- **Empty state doesn't lie.** If ranking hasn't run, page says so and prints the command to run.
- **`ranked_count` is math-consistent by construction.** Comment at `app.py:5340-5344`: `ranked_count = exact + estimated + non_xrp_pair`, `error` rows excluded — no visible-total-vs-breakdown mismatch.

### 2.4 What it hides or under-serves

- **Freshness gap is barely visible.** `snapshot_age_label` is a small chip next to TVL. If rank_amms hasn't completed a cycle in 8h, the reader has to notice a "8h ago" chip vs the normal "3h ago." No red/amber threshold.
- **Order-book DEX not counted.** The `coverage-note` lede says it once, then it's gone. Reader reaches the total-TVL headline and can easily read it as "XRPL DEX TVL," which it is not — it's AMM-only.
- **"Indexed" vs "ranked" wording split.** The empty-state uses `indexed_count`, the stats strip uses `ranked_count`, the callout mentions catalog expansion. Three related numbers, no single spot that says: indexed = discovered on ledger, ranked = we've fetched pool state and priced it.
- **Spoof-side count is off-page-fold.** Only conditionally shown (`if spoof_count_all > 0`). Currently populated on real pools but sits BELOW the top-100 band, so most default-view readers never see one.
- **No pool-level history.** Every visit reads the singleton `amm_ranked_pools`. No sparkline, no "TVL 24h delta," no "was this pool drained today?" See `project_xrpldashboard_backlog_amm_pool_history.md` (87d old — still current, table not built).
- **No "meaningful pool" surface.** BITx-style spam-bloom expands `pool_count` while `meaningful_pool_count` stays flat. Reader has no way to see the split. `/pools` currently shows only ranked pools, so spam is invisible here (good) but that also means the reader can't see the ratio (loss).
- **`walker_health` for rank_amms isn't linked from /pools.** If rank_amms is failing, /pools happily shows stale snapshot with no visible surface.
- **Constellation is dazzling; scope is thin.** Bottom of page has "swaps and TVL stream in live via WebSocket; pool index seeded from `amm_info`" — 1 sentence for a page that makes serious quantitative claims about the XRPL AMM ecosystem.

---

## 3. The honest-limits inventory

The 4-part scope note requires enumerating what's permanently missing. The full list, unfiltered:

**Coverage limits (permanent):**
1. AMM pools only. Order-book DEX offers, executions, and depth are separately tracked and NOT summed here.
2. Non-XRP pairs (token-token) are counted for transparency but their dollar value is not included in the headline TVL number.
3. `estimated` pools use 2× XRP-side, a deterministic model. It is model-derived, not exchange-observed.
4. Spoof-side detection covers only currency codes with a TOML-attested canonical issuer. New-issuer spam without a canonical brand to spoof is invisible to the `⚠` layer.

**Freshness limits (cadence):**
5. Ranking runs on ~4h `launchd` cadence. If it hasn't run in 8h, the snapshot is 8h stale. Chip surfaces the age; no red/amber gating today.
6. Live-stream comets require the WebSocket connection to `xrplcluster.com` — if the socket drops, the constellation goes quiet, but historical events are not backfilled visually.

**Data-source limits (source of truth):**
7. Pool index seeded from public `amm_info` on the local rippled + s1.ripple.com fallback. Bootstrap scan walks ledger by AMM object type. New pools created between rank cycles do not appear until the next cycle.
8. USD pricing on the XRP side comes from `price_oracle.xrp_usd()` — see `/about` for source stack.
9. `rank_amms` writes to Postgres (`amm_ranked_pools`) as a truncate-and-replace singleton. No history is preserved between cycles.

**Scope-of-page limits (what /pools is NOT):**
10. Not a wallet page (see `/wallet/<address>`).
11. Not a token page (see `/token/<currency>/<issuer>`).
12. Not a whale-activity page (see `/whales`).
13. Not order-book DEX (see `/tokens`, coming).
14. Not real-time depth (constellation is event-level, not order-book depth).
15. Not financial advice.

---

## 4. Draft 4-part scope note (per legibility rule)

To be placed as a `<details>` block near the page footer, above the "Not financial advice" line, or folded into the callout above it. Draft copy below.

### 4.1 What we watch

> Every AMM (automated market maker) pool we've discovered on the XRP Ledger, ranked by total value parked inside it. An AMM pool is like a shared vending machine that holds two tokens and lets anyone swap between them — the price you get depends on how much of each token is sitting in the pool.

### 4.2 Where the data comes from

> We walk the ledger to find every AMM, then re-read each pool's live state every four hours to see how much is in it. For pools with a dollar-pegged token on one side (like RLUSD or USDC), the dollar value is exact — it comes straight from the pegged side. For pools with XRP on one side, we double the XRP-side value in dollars, which is deterministic because AMM pools always balance 50/50. Pools that pair two other tokens (like RLUSD/USDC) are tracked and counted but not priced in the headline — we'd be guessing.

### 4.3 What's permanently missing

> - **The order-book DEX.** XRP Ledger has two ways to trade: AMM pools (here) and an order-book DEX (like a traditional exchange). This page is only the AMM side. Order-book liquidity is tracked separately and coming.
> - **Older snapshots.** Right now, every visit shows the most recent four-hour snapshot. We're not yet keeping a history — "was this pool drained yesterday?" is a question we can't answer here today.
> - **New pools created in the last few hours.** Anything created since the last scan cycle isn't in the ranking yet, though the live constellation at the top does fire comets on brand-new activity as it happens.
> - **Non-XRPL AMMs.** Only pools on the XRP Ledger itself. Sidechain AMMs and off-XRPL protocols aren't tracked here.

### 4.4 What this page is NOT

> - Not a wallet detail page. For an individual AMM wallet, click any row.
> - Not the order-book DEX. That's a separate view.
> - Not real-time depth. The constellation fires on actual events, but it's not an order-book depth chart.
> - Not financial advice.

### 4.5 Technical details (behind disclosure)

Collapsed `<details>` block, machine-precision:

```
- Pool index: bootstrap scan via amm_info RPC on the public XRPL node
- Ranking cadence: launchd every 4h (rank_amms.py, RPS pacing 4-8)
- Snapshot storage: amm_ranked_pools table (Postgres, truncate-and-replace)
- Snapshot artifact: amm_ranked.json (JSON payload the /pools route reads)
- TVL statuses: exact | estimated | non_xrp_pair | error (error hidden from breakdown)
- Spoof-side detection: TOML-attested brand vs canonical issuer wallet match
- Live constellation: xrplcluster.com WebSocket, subscribed to top-10 AMM accounts
- Recent-event polling: /api/pools/recent_events (Postgres-backed in prod)
- XRP-USD pricing: price_oracle.xrp_usd() (see /about for source stack)
- Snapshot freshness: exposed via snapshot_age_label on TVL stat
- Walker health: rank_amms row in walker_health table (not currently linked from /pools)
```

---

## 5. Backlog (near-term substrate work)

Filed here so /pools work has a canonical home. None of these are blocking for the legibility rewrite in §4; they are the layer *below* the rewrite.

### 5.1 Pool history table (see `project_xrpldashboard_backlog_amm_pool_history.md`)

- Schema: `amm_pool_history(snapshot_ts, amm_account, lp_token_value, reserve_xrp, reserve_other, tvl_usd)`, PK on `(snapshot_ts, amm_account)`
- Write: append inside the same transaction as the truncate-and-replace in `db.replace_amm_ranked_pools`
- Retention: 90d rolling, DELETE tail in `rank_amms` or a separate `launchd` job
- Volume estimate: ~16M rows in 90d, ~2-3GB with indexes
- First consumers: 24h TVL delta chip on /pools row, spam-bloom alert on `/health`, LP-growth sparkline on `/token`
- Blocking: none — filed 87d ago, still not built, remains not-blocking

### 5.2 Freshness surface upgrade

- Current: `snapshot_age_label` is a small chip that never turns amber or red
- Proposed: age > 6h → amber, age > 12h → red + explanatory tooltip pointing at rank_amms walker_health
- Cost: pure template + tiny CSS
- Blocking: nothing; ships with the legibility rewrite

### 5.3 Order-book DEX link-out

- Current: `coverage-note` lede sentence says "coming soon" with no destination
- Proposed: hard link to `/tokens` (order-book side, once shipped) — until then, honest phrasing "tracked separately, not yet publicly surfaced"
- Blocking: /tokens page work (separate track)

### 5.4 Indexed-vs-ranked wording pass

- Current: three related numbers (indexed_count, ranked_count, meaningful subset via `rank_amms` internals) with no single explainer
- Proposed: fold "indexed = discovered on ledger, ranked = state fetched + priced, meaningful = not zeroed-out spam" into either the empty-state copy or a single tooltip on the stats strip
- Cost: copy work, one commit
- Blocking: none

### 5.5 Spoof-side story fold-out

- Current: `⚠` marker + count in stats strip, one explainer tooltip
- Gap: no landing page for "what's a spoof?" — the marker fires, the reader has nowhere to click for the full story
- Proposed: fold into the trust-hub build (queued post-cert), OR add a section on `/rwa` methodology page that /pools links to
- Blocking: trust-hub scope decision

### 5.6 rank_amms walker_health link

- Current: /pools doesn't surface if rank_amms is failing
- Proposed: small heartbeat pill near snapshot chip: green = healthy, amber = last run > 2× cadence, red = last run > 4× cadence or last-run failed
- Blocking: none; template + one DB read

---

## 6. Editorial posture questions Charlie owes a ruling on

Filed as questions, not answers. Ranked by blocking-ness.

- **Q1 (blocks §4 ship):** Where does the 4-part scope note live on the page? Options: (a) `<details>` above the "Not financial advice" line, (b) fold into the existing callout block, (c) collapse into an updated `/methodology#pools` section and link to it from a single "See methodology" link. Recommendation: (a) — inline, disclosure-open, matches the sibling-page pattern the legibility rule contemplates.
- **Q2 (soft-blocks §5.2):** What are the freshness thresholds? Recommendation: amber at 6h, red at 12h. Cadence is 4h, so 6h = one late cycle, 12h = two-cycles-late = walker likely wedged.
- **Q3 (blocks §5.5):** Trust-hub or /rwa for the spoof-story landing page? Recommendation: trust-hub, once built. Interim: link `⚠` explainer tooltip to `/about#spoof-detection` and write a 3-para section there.
- **Q4 (blocks nothing):** Do we want a "browse spam" tier that shows the invisible tail — the 20K+ pools below the ranked band? Argument for: full-transparency ethic. Argument against: performance + it invites low-quality inbound queries. Recommendation: NO, but expose count.

---

## 7. Success criteria for the rewrite

The legibility rewrite is done when a skeptical reader landing on /pools cold can, in under 30 seconds:

- Understand what an AMM pool is (already handled by `explainer("amm")`, keep)
- Understand what the total TVL number counts and does not count (partially handled by `explainer("tvl-xrp")`, keep + reinforce via §4.1-4.3)
- See the source of the data without reading `<code>amm_info</code>` (§4.2 draft handles)
- See the honest limits (§4.3 draft handles — including order-book gap that today is easy to miss)
- Know what the page is NOT (§4.4 draft handles — new)

Machine-precision remains available for readers who want it (§4.5), behind disclosure. No honest limit loses in the rewrite.

---

## 8. What lands next after this writeup

**Original §8 text (SUPERSEDED — see correction banner at top). Left in for audit trail.**

~~Per Charlie's queue, sequence continues: v4 filed version (this doc → tightened), Legibility 13 (apply this same 4-part structure across the remaining 12 pages), Contact-filter sketch.~~

**Corrected §8 (2026-08-21 19:37 EDT):**

This is legibility-sweep entry 1 of 13. It does NOT need a "v4 tighten pass" — the misread of Friday's queue conflated this writeup with anchor schema v4 (a separate assignment, `signed_snapshot.py` five-leaf design, ~175 LOC, post-cert build, debuts anchor #4 or #5).

Next-up sequencing lives in `docs/SATURDAY_QUEUE_2026-08-22.md`. This file's own follow-on is the remaining 12 entries of the legibility sweep (whales, cold-storage, tokens, MPTs, RWA, RLUSD, lending, NFTs, network, amendments, health, regulation), gated post-cert per `feedback_scope_notes_human_first.md`.

End of v1.

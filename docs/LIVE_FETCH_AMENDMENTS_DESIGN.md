# Live-Fetch Amendments — Design Pack

**Status:** DRAFT (disk-first, first-pass complete).
**Author:** JJ 🦞
**Date started:** 2026-08-21
**Reviewer:** Charlie
**Deadline:** design pack owed 2026-08-22 (per stability-clock memo); code Friday.
**Scope ruling (Charlie, 2026-08-21):** ALL in-flight amendments get live vote counts — not just those in active news cycles. Confirmed by XLS-75 detour: the `INTERIM_VOTE_NOTES` dict was a stopgap for LendingProtocol + SingleAssetVault; the XLS-75 Permission Delegation surge showed we'd have to patch the dict per news beat, which is untenable.

---

## 1. Purpose

The `/amendments` page shows every in-flight amendment on XRPL mainnet — supported by rippled but not yet enabled — plus enabled, superseded, and in-development. **Vote counts are the primary signal that separates a "just merged" amendment from an "activating in 14 days" one.**

Today the vote count on the page reflects only the responding node's OWN validator vote, not the trusted-validator network tally. See `amendments_state.py:149`:
> "Rippled `feature` RPC only returns the responding node's OWN validator vote in count/threshold — it never surfaces the trusted-validator network tally."

Two amendments (LendingProtocol, SingleAssetVault) currently receive a manually-maintained honest-vote-count note from `INTERIM_VOTE_NOTES` (`amendments_state.py:161-174`), sourced from `xrpscan.com/api/v1/amendments`. That dict is a stopgap.

**This pack replaces the manual dict with live-fetched network vote counts for every in-flight amendment, on the same 300s cache TTL as the base state, with an honest-partial envelope + fallback semantics for when the aggregator is unavailable.**

## 2. Non-goals

- **Not** running our own validator sweep. Fetching from an aggregator is 1 HTTP call; a sweep is N validator polls per refresh + trust-list management.
- **Not** predicting activation — 14-day majority window is already computed by base state (`amendments_state.py:282-294`); this pack adds the vote count, not activation logic.
- **Not** adding new amendment metadata (summaries, dependencies). `IN_DEVELOPMENT_AMENDMENTS` is the surface for that.
- **Not** a redesign of the /amendments UI. Adds a small "network vote: N/M validators" element to each in-flight entry; no structural change.
- **Not** cross-network coverage. Mainnet only. Testnet/devnet have different validator sets and are out of scope.

## 3. Constraints

- **Aggregator dependency is a wound.** If xrpscan.com is down, our live-fetch is down. Design must serve stale + fail honest, never fabricate.
- **Rate discipline.** xrpscan.com publishes no explicit rate limits but the polite ceiling is one call per cache TTL (300s = 12 req/hr / 288/day). Well inside any reasonable free-tier bar.
- **Attribution required.** Every vote count on the page must cite source + as_of timestamp. Standing rule: `feedback_ledger_facts_before_narrative.md` — verify on-chain facts before writing narrative; the corollary is show your sources.
- **Honest-partial is the contract.** If we have counts for 4 of 6 in-flight, we show 4 with counts and 2 marked "network vote not fetched" — never fill missing with 0/N or omit them entirely.
- **Cache TTL cannot weaken freshness.** 300s matches base state; aligned so a single /amendments render is either fully-fresh or fully-stale (no partial mismatch).

## 4. Sources evaluated

### 4.1 xrpscan.com/api/v1/amendments (STANDBY — pending written consent)
- **Endpoint:** `https://api.xrpscan.com/api/v1/amendments` (the source previously cited in `INTERIM_VOTE_NOTES` through 2026-08-20).
- **Response shape (verified 2026-08-20):** JSON array; each object has `amendment` (hash), `name`, `count` (validator votes yes — UNL-scoped), `validations` (trusted-validator count = denominator), `threshold` (80% of validations), `enabled` (bool), `supported` (bool), `deprecated` (bool).
- **Cadence:** xrpscan itself polls the validator set continuously; the `/amendments` endpoint reflects near-real-time.
- **Cost:** free tier 10,000 requests/day; PAYG 100k/day at 0.0001 XRP/req; Enterprise $4,999/mo.
- **Failure modes:** DNS/HTTPS outage, JSON schema change, rate cap, stale data (unlikely but possible during xrpscan-side incident).
- **Terms (verified 2026-08-22 from `docs.xrpscan.com/api-documentation/introduction.md` and `.../help/terms-of-service.md`):** API operates under **Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License**. ToS states: *"You may use XRPScan content for personal or non-commercial educational use"* and *"Bulk reproduction, resale, or redistribution in commercial or enterprise contexts is strictly prohibited without prior written consent"*. Attribution required: *"Source: XRPScan (https://xrpscan.com)"*.
- **Verdict:** **STANDBY-PENDING-CONSENT.** xrpldashboard.com operates a paid tier (MCP /connect) so the "non-commercial educational use" clause does not cover us on its face. Written consent request sent to support@xrpscan.com on 2026-08-22. If granted, xrpscan becomes cross-check source alongside primary; if refused or silent, xrpscan remains out of the fetch path.

### 4.5 data.xrpl.org / vhs.prod.ripplex.io (CHOSEN — 2026-08-22)
- **Endpoint:** `https://data.xrpl.org/v1/network/amendments/vote/main` (canonical alias) or `https://vhs.prod.ripplex.io/v1/network/amendments/vote/main` (RippleX-hosted mirror; identical payload, same ETag).
- **Service:** Validator History Service (VHS) — the aggregation backend that powers the XRPL Foundation explorer at `livenet.xrpl.org`. Endpoint discovered by grepping the explorer SPA bundle (`index-DGVof9nL.js`).
- **Response shape (verified 2026-08-22):** JSON object `{result, count, amendments: [{id, name, threshold: "N/M", consensus: "PP.PP%", voted: {count, validators: [{signing_key, ledger_index, unl}]}, rippled_version, retired, obsolete, ...}]}`. In-flight amendments have `threshold` populated; enabled amendments have `ledger_index` + `date` instead.
- **UNL-scoped field semantics (critical):** `consensus` is the trusted-validator tally (validators with `unl == "vl.ripple.com"` divided by 35 = the vl.ripple.com UNL size). `voted.count` is total broadcasting nodes INCLUDING non-UNL — do NOT use raw (see `feedback_verify_field_semantics_before_reporting_movement.md`). Parser reads UNL-scoped fields only: filter `voted.validators` for `unl == "vl.ripple.com"`, cross-check against `consensus` percentage.
- **Cadence:** ledger-fresh (VHS aggregates continuously from validator manifests); observed `ledger_index` in validator entries within seconds of current.
- **Cost:** free, no key, no auth. CORS wide-open (`access-control-allow-origin: *`).
- **Failure modes:** DNS/HTTPS outage, schema change (service self-labels `version: "0.0.1-beta.0"` — beta wobble risk), stale data during VHS incident.
- **Terms (verified 2026-08-22):** **No published Terms of Service.** `xrpl.org` publishes only a Privacy Policy (`/about/privacy-policy`); no ToS or license page. `foundation.xrpl.org/terms/` → 301 redirect to `xrpl.org/` root (no landing page). `data.xrpl.org` and `vhs.prod.ripplex.io` publish no robots.txt (root returns the endpoint index JSON). `ripple.com/terms` returns 404. Bare `ripplex.io` doesn't resolve. This is the same **absent-and-ambiguous** silence class as Ripple's public rippled cluster (`s1/s2/s2-clio`) documented in `project_clio_terms_primary_source_2026-08-15.md` — Foundation-aligned public infrastructure operated without published usage terms, which the site already depends on daily. No attribution required by any published document; we attribute anyway (parity with the /amendments interim citation flip 2026-08-22).
- **Data richness vs xrpscan:** VHS additionally exposes per-validator UNL affiliation (which UNL each validator sits on), which xrpscan does not. Same underlying facts, richer presentation.
- **Verdict:** **PRIMARY.** Absence-of-terms on Foundation-aligned public infrastructure = same class as s1/s2 which we already stand on. Ledger-fresh, wide-open CORS, richer payload. Beta wobble risk mitigated by §6.2/§6.3 fallback layers (stale-serve → 6h ceiling → honest unavailable).

### 4.2 bithomp.com (FALLBACK CANDIDATE)
- **Endpoint:** `https://bithomp.com/api/v2/amendments` (public, key optional for higher rate).
- **Response shape:** similar `count/validations` fields; formats vary by version.
- **Verdict:** viable secondary. Add to pack as OPTIONAL fallback if we observe xrpscan flap. Not in initial ship — YAGNI unless xrpscan proves unreliable in 30 days of production.

### 4.3 Direct validator sweep (REJECTED for initial ship)
- Fetch UNL from `https://vl.ripple.com`, poll each validator's `/vl` and `/validators` endpoints, tally votes ourselves.
- **Pros:** zero third-party dependency; strongest freshness; strongest verification story ("we compute it ourselves").
- **Cons:** N validator connections (35 on UNL currently) per refresh, trust-list management, timeouts per validator, harder correctness story if any validator is offline.
- **Verdict:** deferred to Phase 2b as an OPTIONAL cross-check that runs on a slower cadence (e.g., 6h) and alerts if aggregator deviates by >2 vote counts on any amendment. See §7 rollout Step 3.

### 4.4 Our own rippled node (CONSIDERED, REJECTED)
- **Why interesting:** we already run a full-history rippled on Lenovo (LAN 192.168.40.95). It has its own view.
- **Why rejected:** rippled's `feature` RPC returns the same responding-node-OWN-vote structure regardless of which node we ask. That's the whole gap we're solving. Our node doesn't have the network tally either.

## 5. Architecture

### 5.1 New module: `amendments_network_votes.py`

Single module, single public function:

```python
def fetch_network_vote_tallies_cached() -> dict:
    """Return {hash: {count, validations, threshold, as_of_iso, source_url}}
    for every amendment the aggregator knows about. Cached 300s (aligned
    with amendments_state.CACHE_TTL). Returns {} on complete failure —
    consumers must treat missing entries as 'not fetched', not zero votes."""
```

Internals:
- httpx.get to xrpscan endpoint with 10s timeout
- Parse response, keyed by `amendment` hash
- Cache in module-scope dict with `threading.Lock` (same pattern as `amendments_state._cache`)
- SWR-adjacent: if fetch fails AND we have a cached result, return cached with `as_of_iso` reflecting the STALE fetch time (not now). Consumer decides how to display staleness.

### 5.2 Wiring into `amendments_state.fetch_amendments_state`

In the in-flight iteration loop (`amendments_state.py:244-249`), after building each entry:

```python
elif info.get("supported"):
    entry = {"hash": h, "name": name}
    net_vote = network_votes.get(h)  # dict OR None
    if net_vote is not None:
        entry["network_vote"] = net_vote
    # else: entry has no network_vote key — template renders
    # "network vote not fetched" honestly.
    in_flight.append(entry)
```

The `INTERIM_VOTE_NOTES` fallback is REMOVED — no more manual dict.

Because `fetch_network_vote_tallies_cached()` returns `{}` on complete failure, the "not fetched" state is uniform: every in-flight entry gets `network_vote` present (from cache or fresh) OR no key at all.

### 5.3 Envelope shape returned to template

Add two top-level fields to `fetch_amendments_state` return:

```python
"network_votes_source": {
    "url": "https://api.xrpscan.com/api/v1/amendments",
    "as_of_iso": "2026-08-21T22:14:33Z",  # when xrpscan was fetched
    "status": "ok" | "stale" | "unavailable",
    "stale_age_seconds": 1247,  # only present when status="stale"
},
"in_flight_with_votes_count": 5,  # of len(in_flight)
"in_flight_without_votes_count": 1,  # remainder
```

Template uses `network_votes_source.status` to render the top-of-section chip:
- `ok` — green chip: "Network vote data: xrpscan, as of 2026-08-21 22:14 UTC"
- `stale` — amber chip: "Network vote data: xrpscan, last successful fetch 21 min ago (aggregator currently unavailable)"
- `unavailable` — grey chip: "Network vote data currently unavailable — showing responding-node vote only"

### 5.4 Per-entry rendering (template change)

Each in-flight amendment card gets a new line below the name:

- If `network_vote` present:
  > Network vote: **14 / 35** validators (threshold: 28)
  > Source: xrpscan.com, 2026-08-21 22:14 UTC
- If `network_vote` absent:
  > Network vote: **not fetched** (see aggregator status above)

**Deliberately verbose citation** — the primary-source rule (`feedback_ledger_facts_before_narrative.md`) plus the analytics-spike attribution rule filed today (`feedback_analytics_spike_reuse_prior_measurement.md`) both push us toward "show your source" as first-class UI, not a footnote.

## 6. Freshness + cache semantics

### 6.1 Cache TTL alignment

- `amendments_state.CACHE_TTL` = 300s (env-overridable via `AMENDMENTS_CACHE_TTL`)
- `amendments_network_votes.CACHE_TTL` = 300s (env-overridable via `NETWORK_VOTES_CACHE_TTL`, defaults to same)
- Both are fetched during the same `fetch_amendments_state` call, so they age in lockstep and the cache lifetime is the LATER of the two.

### 6.2 Fresh / stale / unavailable state machine

Given `last_fetch_ts` and `last_fetch_result`:

| Age vs TTL | Last result | Status | Template chip | Per-entry data |
|---|---|---|---|---|
| < TTL | success | `ok` | green (fresh) | populated |
| < TTL | fail (never succeeded) | `unavailable` | grey | absent |
| ≥ TTL, refetch succeeds | success | `ok` | green (fresh) | populated |
| ≥ TTL, refetch fails, have prior success | prior success | `stale` | amber + age | populated (stale) |
| ≥ TTL, refetch fails, NO prior success | fail | `unavailable` | grey | absent |

### 6.3 Stale-serving bound

If refetch has been failing for >6 hours (configurable via `NETWORK_VOTES_STALE_CEILING_SECONDS`), status transitions to `unavailable` regardless of prior data — 6h-old vote counts are worse than "not fetched" because amendment activation windows are 14 days and 6h of drift can bracket a state change.

### 6.4 Interaction with the base state

`fetch_amendments_state` itself already returns `{"ok": False}` if either the `feature` or `ledger_entry` RPC fails. The network-votes layer is ADDITIVE and does NOT affect the `ok` flag — a working base state with unavailable network votes still renders the page (as it does today, just with `INTERIM_VOTE_NOTES` gaps).

## 7. Rollout

### 7.1 Step 0 — unit tests (must pass before wiring)

`tests/test_amendments_network_votes.py`:

1. **Happy path.** Mock xrpscan response, assert parse + dict-shape correct.
2. **Response schema drift.** Missing `count` or `validations` field → entry skipped, not crash.
3. **Fetch failure with no prior cache.** httpx raises → returns `{}`, status `unavailable`.
4. **Fetch failure with prior success.** First call succeeds (mock A), second call fails after TTL → returns prior data, status `stale`, age computed.
5. **Stale ceiling.** Advance clock past 6h stale ceiling → status transitions to `unavailable`.
6. **Cache TTL respected.** Two calls within TTL → only one HTTP call.

`tests/test_amendments_state_with_network_votes.py`:

7. **Wiring integration.** Mock both base RPCs + network votes, assert `in_flight` entries have `network_vote` field, envelope fields present.
8. **Partial coverage.** Network votes returns data for 3 of 5 in-flight → 3 with `network_vote`, 2 without. Envelope counts correct.
9. **`INTERIM_VOTE_NOTES` removed cleanly.** Assert the dict no longer influences in-flight entries (dict may exist as a symbol for git-blame history but does not populate anything).

Target: 9 tests, all under 3s combined (all network calls mocked).

### 7.2 Step 1 — new module ships as standalone commit (PR #1)

- `amendments_network_votes.py` + its tests, ZERO wiring.
- No template changes.
- No behavior change on /amendments render.
- Merges independently; rollback = trivial revert.

### 7.3 Step 2 — wiring ships as follow-up commit (PR #2)

- `amendments_state.py` calls the new module.
- Template adds envelope chip + per-entry rendering.
- `INTERIM_VOTE_NOTES` dict CLEARED but not deleted (git-blame preserves the reasoning; docstring updated to point at the live module).
- Env var `NETWORK_VOTES_ENABLED` (default `"true"`) as kill switch — flip to `"false"` in Render dashboard for instant rollback to responding-node-vote-only.

**Watch first 15 min post-deploy:**
- /amendments renders (curl `xrpldashboard.onrender.com/amendments` returns 200)
- Envelope chip shows `ok` (green)
- At least one in-flight amendment shows a network_vote count
- No exceptions in gunicorn logs mentioning `amendments_network_votes`
- xrpscan fetch latency in logs (should be <500ms in normal case)

### 7.4 Step 3 — validator-sweep cross-check (Phase 2b, DEFERRED)

Not in this pack. Deferred to observation-window post-Step 2:

- Add `amendments_validator_sweep.py` that polls UNL validators directly on a 6h cadence.
- Compare aggregator vs sweep results.
- If any amendment differs by >2 votes: log warning; if >5 votes: BetterStack alert.
- Provides the "aggregator is wrong" detection story we don't have today.

Ship candidate: 30 days post-Step 2 GREEN, or immediately after any observed xrpscan-vs-reality mismatch.

## 8. Failure modes + guardrails

### 8.1 xrpscan schema drift
**Mode:** xrpscan renames `count` → `votes` or `validations` → `validators`.
**Behavior with guardrails:** per-entry `.get()` reads return None, entry is skipped (not crashed), envelope shows `unavailable` if all entries skip.
**Recovery:** JSON-shape unit test would fail in CI on next tag pull; manual update to parser.

### 8.2 xrpscan returns 200 with empty body / null response
**Mode:** upstream sends `{}` or `[]` instead of the expected array.
**Behavior:** `fetch_network_vote_tallies_cached` returns `{}`; envelope status `unavailable` if never-successful, else `stale`.

### 8.3 xrpscan returns malformed hashes (upper vs lower case)
**Mode:** hash-case mismatch between xrpscan and our `feature` RPC.
**Behavior:** would silently miss all entries.
**Guardrail:** normalize both sides to uppercase in the join. Explicit test.

### 8.4 xrpscan rate-caps us
**Mode:** free tier throttles at N req/day; we get 429.
**Behavior:** treated as fetch failure → stale-serve until success.
**Guardrail:** 300s TTL = 288 req/day worst case. Unless xrpscan caps below that, we're safe.
**Rollback if hit:** flip `NETWORK_VOTES_CACHE_TTL` to 3600s (24 req/day), bumps freshness bar to 1h but removes any conceivable rate concern.

### 8.5 Aggregator LIES (data is wrong)
**Mode:** xrpscan reports vote count X, actual network tally is Y.
**Behavior in initial ship:** we show X and cite xrpscan. If X is wrong, our page is wrong.
**Guardrail (deferred):** validator-sweep cross-check (§7.4) — detects deviation, alerts.
**Guardrail (initial ship):** explicit source citation on every count. "Source: xrpscan.com" is the truth-in-labeling that says "if this is wrong, xrpscan is wrong." Our attribution posture is intact.

### 8.6 Aggregator briefly serves prior-window data during their own refresh
**Mode:** xrpscan is refreshing their internal cache; we hit them mid-flip and get a 60s-old snapshot.
**Behavior:** we cache that for 300s. Worst-case staleness: 360s (6 min). Amendment vote velocity: hours-to-days. Non-issue.

### 8.7 Our own cache lock contention
**Mode:** simultaneous request storm on cache expiry.
**Behavior:** using the same `threading.Lock` pattern as `amendments_state._cache_lock`. Serializes refetches — one thread fetches, others wait then return the freshly-cached value.
**Note:** this is the SAME single-flight semantics the Phase 2 memory-aware cache exports. If Phase 2 lands first, `amendments_network_votes` should use `MemoryAwareTTLCache` instead of a bespoke lock. If this pack lands first, keep the bespoke lock; migrate later.

### 8.8 Empty in-flight list (all amendments enabled or none supported)
**Mode:** rare but possible in a quiet development window.
**Behavior:** section renders empty; envelope chip still shows aggregator status.
**No special handling needed** — the join is over an empty set, so no attempt to render aggregator data.

## 9. What Phase 2b / future might build

- **§7.4 validator-sweep cross-check** (deferred).
- **Historical vote velocity:** store a small append-only log of `(hash, count, validations, fetched_at)` tuples to Postgres. Enables sparkline of "how many validators added support in last 7d". Design: separate walker on 6h cron, writes to `amendment_vote_history` table.
- **Alerting on activation window entry:** if any amendment crosses 80% and enters Majorities, fire a BetterStack notice + queue a /amendments correction-note (Charlie ruling: this is the exact class the 2026-08-17 CLARITY cloture-motion correction was about — automated escort would have caught it).
- **Public API surface:** expose the network-vote-tallies dict as `/api/v1/amendments/votes` for machine consumers. Free-tier, cached.

## 10. Rulings requested from Charlie

Ordered blocking → deferrable.

### 10.1 [RULED 2026-08-22] Aggregator choice: VHS (data.xrpl.org) sole source
Source scoping 2026-08-22 (this session) found xrpscan's Terms of Service (CC BY-NC-SA 4.0 + written-consent gate for commercial context) do not cover us on face. VHS (data.xrpl.org) is same-silence-class Foundation-aligned public infrastructure — parity with s1/s2 which the site already stands on daily — with richer payload (per-validator UNL affiliation).

**Verdict:** VHS = primary. xrpscan = STANDBY-PENDING-CONSENT (see §4.1); if consent grant arrives, xrpscan becomes cross-check source alongside VHS, not primary. Bithomp remains as §4.2 optional secondary if VHS proves unreliable in observation.

Parser reads UNL-scoped fields only (`consensus` percentage + validators filtered on `unl == "vl.ripple.com"`); never raw `voted.count`. Field-semantics lesson baked into the parser per `feedback_verify_field_semantics_before_reporting_movement.md`.

### 10.2 [BLOCKS PR #2] Stale ceiling: 6 hours
§6.3. Alternatives:
- **6h (JJ default)** — balances "stale-is-better-than-nothing" against "amendment activations are 14 days so 6h is <2% of the window".
- **1h** — more conservative; more time in `unavailable` state during aggregator flaps.
- **24h** — more lenient; would serve up to 1-day-old data.

My recommendation: **6h**. Rationale: any decision an operator would make from this page (bump binary, alert users, publish correction note) has response times measured in hours-to-days; 6h stale data is inside operator reaction time, 24h is not.

### 10.3 [BLOCKS PR #2] Kill switch env var: `NETWORK_VOTES_ENABLED`
§7.3. Alternatives:
- **Keep the kill switch (JJ default)** — enables Render dashboard env-flip rollback to responding-node-only rendering.
- **Skip the kill switch** — one less env var; rollback = git revert.

My recommendation: **keep**. Rationale: matches the Phase 2 rollout pattern (`CACHE_ENABLED` / `SF_GUARD_ENABLED`). Cheap insurance; consistent with our operational discipline.

### 10.4 [DEFERRABLE] Attribution surface density
§5.4 renders the source citation on EVERY entry. Alternatives:
- **Per-entry (JJ default)** — verbose but bulletproof; each count carries its source.
- **Once at top of section** — cleaner; relies on reader carrying the citation down the list.
- **Behind a `<details>` toggle** — clean by default, verifiable on click.

My recommendation: **per-entry, but compressed** — "Source: xrpscan · 22:14 UTC" as a small caption line under each count. Not a bulleted line, not a chip.

### 10.5 [DEFERRABLE] Retain `INTERIM_VOTE_NOTES` dict as historical symbol
§7.3. Alternatives:
- **Clear the dict, keep the symbol** (`INTERIM_VOTE_NOTES = {}`) with docstring pointing at new module (JJ default). Preserves git-blame explanation for future readers.
- **Delete the dict entirely.** Cleaner code; git-blame still finds the old definition.

My recommendation: **clear + keep symbol** for one release, then delete in a followup cleanup. Rationale: whoever reads `amendments_state.py` in 3 months should see the pointer to the new module inline.

### 10.6 [DEFERRABLE] Ship order relative to Phase 2 memory-aware cache
§8.7 notes that the bespoke lock in `amendments_network_votes` could migrate to `MemoryAwareTTLCache` once Phase 2 lands. Alternatives:
- **Ship live-fetch first, migrate later** (JJ default if Phase 2 has any rollout delay) — no coupling.
- **Wait for Phase 2 primitive to land, then ship live-fetch using it** — one less code path to migrate.

My recommendation: **ship independently, migrate later**. Rationale: this pack has its own value and its own timeline; entangling releases makes rollback harder.

---

**End of design pack v1.** Sections 1-10 all fully written. Awaiting Charlie rulings on 10.1-10.3 to unblock code. 10.4-10.6 can be decided at PR review. Post-ruling next step: unit tests (Step 0) → `amendments_network_votes.py` module (PR #1) → wiring (PR #2). Estimated ~2-3 hours of code + test writing after ack lands.

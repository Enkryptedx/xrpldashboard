# TOKEN NAMING — Deep Dive Design
**Status:** Design-only. No builds. Rulings at end.
**Filed:** 2026-08-25
**Queue slot:** Post-cert, after POOLS-COMETS (Phase A+B merged)
**Scope:** What the pipeline does today, what it doesn't, what it should, and the license/trust framework before any new source touches prod.

---

## 1. Current State — What `enrich_token_names.py` Actually Does

### The script (three parts, one file)

`enrich_token_names.py` is a Mac-local enrichment walker. It populates `token_names.json`
(a flat JSON dict on disk) from first-party sources only. App.py and xrpl_stream.py read
this file at startup and hot-reload it on mtime change.

**Part A — MPT metadata** (`source="mpt_metadata"`)
Reads `mpt_snapshot.json` (written daily by the `mpt_snapshot` launchd walker, which calls
the XRPL node directly). Writes every MPT issuance that has a name or ticker. Trust tier:
highest — this is on-ledger XLS-89 metadata, first-party from the protocol.
Current count: **211 entries**.

**Part B — IOU TOML enrichment** (`source="toml"` / `source="domain_fallback"`)
Iterates unlabeled IOU issuers from `token_volume` (top 200 by trade count). For each:
1. Fetches the issuer's on-chain `Domain` field via `account_info` RPC.
2. Fetches `https://{domain}/.well-known/xrp-ledger.toml`.
3. Looks for a `[[CURRENCIES]]` block where `code` + `issuer` match (normalized to 40-char
   hex). Full match → `source="toml"` with the block's name/symbol/desc.
4. Domain/TOML found but no matching currency block → `source="domain_fallback"` with
   org name only (no token name claimed).
Insert-only — never overwrites existing entries. Current counts: **12 toml + 2 domain_fallback**.

**Part C — LP-token derivation** (`source="lp_derived"`)
Computes the protocol-deterministic LP-token currency code for each AMM pool via
`sha512Half(sort(currency_bytes(asset1), currency_bytes(asset2)))`. Only writes the label
when BOTH sides of the pool are already verified (no `TODO_curation_pass` gate). This
transitively endorses both component tokens, so strict-mode is correct.
Current count: **35 entries**.

**Legacy manual entries** (`source="unknown"`, `verified_via="TODO_curation_pass"`)
33 manually-curated entries predating the TOML pipeline; 13 are `TODO_curation_pass`-gated
(Bitstamp/Ripple gateway tokens: USD, EUR, BTC, etc. via `rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq`
and similar). These are shown on /tokens today but their curation provenance is undocumented.

### Why "unscheduled" is the wrong word

The launchd plist is loaded and the job runs weekly (`StartInterval=604800`). `RunAtLoad=false`
means it doesn't fire on login — it fires on the 7-day interval only. Last confirmed run:
**2026-08-04** (3 weeks ago). The job appears to be running on its cadence but may have been
silently skipped during the power/FileVault events in August. Walker health is written on
start/end so the pager catches silent failures. "Unscheduled" was an overstatement; the accurate
framing is: weekly, Mac-local, unmonitored for cadence drift (no "last run > 10 days" alert).

---

## 2. The Real Number — Unnamed Share on /tokens

**Source:** `volumes.db` queried 2026-08-25.

| Bucket | Total pairs | Named | Unnamed | Unnamed % |
|--------|-------------|-------|---------|-----------|
| 3-char currency codes | 2,279 | 14 | 2,265 | 99.4% |
| 40-char hex codes | 12,220 | 35 | 12,185 | 99.7% |
| **All traded pairs** | **14,499** | **49** | **14,450** | **99.7%** |

Of the 49 named pairs: 35 are LP tokens (lp_derived), 12 are IOU TOML, 2 are domain_fallback.
The 211 MPT entries in token_names.json mostly don't appear in token_volume (MPT trading is
thin relative to IOU volume).

**Top 5 unnamed by trade count (from top-100 volume pairs):**

| Display | Issuer prefix | Trades |
|---------|--------------|--------|
| 42495478… (BTC-like hex) | rBitcoiN… | 1,545,916 |
| 43394252… (hex) | rBCf85rm… | 1,339,487 |
| ARK | rf5Jzzy6… | 1,042,015 |
| PLR | rNSYhWLh… | 815,309 |
| STX | rSTAYKxF… | 754,806 |

These are the highest-signal missing labels — any enrichment pass should prioritize them.

---

## 3. The Source Map — Every Way a Token Gets a Name

### Source A: Issuer's own xrp-ledger.toml (already implemented)
**Trust tier:** HIGH — issuer self-asserts; they control the domain and the TOML.
**Coverage estimate:** Low. TOML adoption is sparse outside the largest issuers.
In the last run (2026-08-04): 6 new entries from 200 issuers checked. ~3% hit rate
at the top-200 band; long-tail hit rate will be lower.
**License:** None needed. Issuer publishes their own metadata; we're reading and displaying it.
The TOML spec (XRPL Standards XLS-12d) was designed for exactly this use case.
**Risk:** Domain can be dropped or TOML modified post-indexing. Names can change or be
retracted. The pipeline is insert-only, so stale names persist. Invalidation is not implemented.

### Source B: MPT on-ledger metadata (already implemented)
**Trust tier:** HIGHEST — encoded in ledger, cryptographically final once validated.
**Coverage:** Complete for all MPT issuances. ~211 today; will grow as MPT adoption grows.
**License:** Public ledger data. No third-party terms apply.
**Risk:** Minimal. The data is immutable per-issuance. `mpt_snapshot.py` refreshes it daily.

### Source C: LP-token derivation (already implemented)
**Trust tier:** HIGHEST — protocol-deterministic, reproducible from on-ledger AMM state.
**Coverage:** Any AMM pool where both component tokens are already verified.
**License:** None needed. Mathematical derivation from public data.
**Risk:** Only write when both sides are verified. Already enforced by `_shippable_side()`.

### Source D: Community / aggregator token lists (NOT implemented — license check required)
Potential sources include:

| List | Publisher | Notes | License status |
|------|-----------|-------|----------------|
| XUMM token list | XRPL Labs / Xaman | `xrpl-labs/xumm-dev-portal`, curated labels | **NOT CHECKED** — must read ToS/repo license before use |
| FirstLedger token list | FirstLedger.net | Community-curated | **NOT CHECKED** — website ToS unclear |
| XRPScan labels | XRPScan | Account + token labels | **BLOCKED** — XRPScan ToS prohibits data extraction (xrpscan lesson applies equally to token labels as to whale labels) |
| Sologenic / SOLO DEX | Sologenic | Token registry for their DEX | **NOT CHECKED** |
| Bithomp labels | Bithomp | Account + token info | **NOT CHECKED** — likely same class as XRPScan |

**Standing rule:** The xrpscan lesson applies to name-lists. Read every source's terms
before republishing their labels. License check is a BLOCKING prerequisite; no community
list data lands in token_names.json without a documented license finding.

### Source E: On-chain domain → TOML (extended — not yet fully saturated)
The current Part B checks top-200 issuers only. Coverage improves if the limit is raised
(with a runtime cost: ~0.27s per issuer for RPC + TOML fetch → 200 issuers ≈ 54s,
500 issuers ≈ 135s, 1000 issuers ≈ 4.5 min). Raising the cap is safe and license-clean.

---

## 4. The Honest-Label Design

The site's claims system uses green (verified exact) / yellow (estimated) / unlabeled
to distinguish confidence levels. Token names need the same treatment.

### Proposed trust tiers

| Tier | Display | Badge/color | Sources |
|------|---------|-------------|---------|
| **Verified** | Full name + ticker | Green dot (matches `status-exact`) | `toml`, `mpt_metadata`, `lp_derived` |
| **Issuer-known** | Issuer org name only, no token name claimed | Yellow or neutral | `domain_fallback` |
| **Unverified** | Raw ticker (3-char) or hex prefix | No badge, muted | legacy `unknown` / `TODO_curation_pass` |
| **Unnamed** | Hex prefix or "—" | No badge, muted | Not in token_names.json |

### Rendering rule
An unverified name (tier 3 or 4) NEVER displays identically to a verified name. The tier
must be visible or one tap away. Minimal viable: a `title` attribute with the source on
hover. Better: a small colored dot matching the existing claims palette.

### What changes in app.py
`_load_token_names_dict()` already returns the full entry including `source` and `verified_via`.
`app.py:1602` notes the `verified_via` gate logic. No structural change needed — the tier
can be computed at template-render time from the existing fields.

---

## 5. The Pipeline — Scheduled Enrichment Walker

### Current state
- **Cadence:** Weekly (`StartInterval=604800`). launchd job: `com.charliebruce.xrpldashboard.enrich_token_names`.
- **Trigger:** Interval-only (`RunAtLoad=false`). No manual trigger in the queue.
- **Storage:** `token_names.json` on Mac disk. Render reads a snapshot baked at deploy time
  (Render doesn't have access to the live Mac file). This means **enrichment results only
  reach prod at the next push** — there is a standing deploy-lag for name improvements.
- **Refresh/invalidation:** Insert-only — existing entries are never updated. If an issuer
  changes their TOML (renames a token, updates description), the old entry persists forever.
  No invalidation logic exists.
- **Failure semantics (loud-skip doctrine):** Per-issuer fetch errors are logged and skipped;
  the run continues. `walker_health` start/end written so the pager catches a silent total
  failure. Partial success is acceptable — each issuer is independent.

### Gap: deploy-lag for name improvements
If enrich_token_names adds 50 new names today, they don't reach Render until the next
`git push`. The file lives on Mac only. Options at ruling time:
- A) Accept the lag (current). Names improve weekly + push. Simple.
- B) Write enriched names to Postgres (`token_names` table) and have app.py prefer PG over
  the file. Eliminates deploy-lag. More complex, adds PG dependency.
- C) Move the enrichment walker to run on Render (or a Neon-writing cron). Eliminates
  deploy-lag and Mac-dependency. Requires hosting the XRPL RPC calls from Render, which
  has egress implications.

Recommend: **A** for now (cheapest, zero new infra). Note the lag in methodology copy.

### Gap: cadence alert threshold too coarse — alert exists, fires too late
**Correction (filed 2026-08-25):** A cadence-drift alert DOES exist and fired tonight at
20:23 ET — 21 days after the last successful run. The threshold is
`max(cadence * 3, 24h)` = `max(604800 * 3, 86400)` = 1,814,400 s = **21 days**. That
means 3 missed weekly cycles before anyone hears. The accurate gap is threshold
granularity, not alert absence.

**Why 21 days was crossed tonight:** Two power events (2026-08-13 + 2026-08-20) each
reset launchd's `StartInterval` countdown from zero. The Aug 5 run pushed the next
scheduled run to Aug 12 → reset to Aug 21 → reset to **Aug 27 (tomorrow)**. launchd
considers the job on-schedule; walker_health shows it stale. The mismatch is the
design gap: `StartInterval` resets on reboot but walker_health compares against
wall-clock age, not launchd's internal timer.

**Interim mute:** `tools/l1_pager.py WALKER_STALENESS_MUTES["enrich_token_names"]`
expires **2026-09-08**. Re-page suppressed tonight and through the post-cert build
window. Dated, pointed, references this doc.

---

## 6. Rulings Needed (Recommend-and-Approve)

**R1 — Community list license sweep**
Before any community list can contribute token names: assign the sweep. Options:
(a) Research each list's terms in a single pass (2-3h desk work), file findings, then
    decide which (if any) to add. Unlicensed = hard pass per xrpscan precedent.
(b) Skip community lists entirely for now — rely on TOML + MPT only, which are license-clean.
**Recommend: (a) — do the sweep, file findings, then decide. Don't assume safe.**

**R2 — Display tier for legacy `TODO_curation_pass` entries**
The 13 manually-curated entries (Bitstamp USD/EUR/BTC, Ripple gateways) have no documented
verification source. Treat them as tier 3 (unverified) until a proper TOML or primary-source
check is done, OR promote them to verified after a manual audit of each.
**Recommend: flag as unverified in display pending a 1h audit pass. Don't promote blindly.**

**R3 — Raise Part B issuer limit**
Current: 200. Raising to 500 costs ~2 extra minutes of runtime weekly on the Mac.
The top-volume unnamed tokens (ARK, PLR, STX) may fall within the top-500 by issuer
if their issuers have set a Domain field.
**Recommend: raise to 500. No license risk. Low cost. File as a one-line code change.**

**R4 — Deploy-lag: accept or fix**
See §5 options A/B/C above.
**Recommend: A (accept lag). Revisit when token naming has more coverage. Not urgent.**

**R5 — Cadence alert threshold**
Alert exists (fires at 21d = 3× weekly cadence). The gap is granularity: 3 missed
cycles before anyone hears. Design options: (a) tighten multiplier for weekly walkers
to 1.5× (10d threshold), (b) add per-walker threshold overrides, (c) accept 3× as
policy and document it. The `StartInterval`/walker_health mismatch on power-event
reboots is a separate structural issue — worth a design note in the post-cert rebuild.
**Recommend: (a) or (b) decided in the post-cert rebuild pack. Not pre-cert scope.**

**R6 — Invalidation**
The insert-only policy means stale names persist. For now: acceptable (names rarely
change, and wrong names from TOML are the issuer's own fault). A future invalidation
pass would re-fetch TOML for all existing entries and update changed fields. Not urgent.
**Recommend: park. Not in post-cert scope.**

---

## Build Slot

Post-cert queue, **behind POOLS-COMETS** (Phase A+B merged build).
No code ships from this document until:
1. All rulings above are given by Charlie.
2. Any community list included has a documented license finding on file.
3. The display tier design is approved (green/yellow/unlabeled palette confirmed).

Estimated LOC when rulings land: Part B limit raise (~1 line), cadence alert (~5 lines
to pager config), display tier in templates (~15 lines), methodology copy (~20 words).
The license sweep and manual audit are desk work, not code.

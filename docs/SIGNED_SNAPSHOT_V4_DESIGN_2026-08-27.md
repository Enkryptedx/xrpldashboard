# Signed-snapshot v4 — richer truth in the weekly stamp

**Status:** RULED 2026-08-27 22:22 ET. All defaults accepted EXCEPT §1c (see §7). No code written yet.
**Filed:** 2026-08-27 evening (post-2026-08-27 phantom-#9 symmetry note — v4 was approved conceptually 2026-08-26 but never persisted as a design pack until now).
**Referenced by:** `research/SITE_AUDIT_QUADFECTA_2026-08-26.md` §2 line 41, §12 line 301.

---

## The scope in one paragraph

The current signed snapshot (`schema_version=3`, 9 numeric metrics, per-snapshot leaf, weekly XRPL-Payment anchor via `ONLEDGER_ANCHOR_SPEC.md` v1) commits *what's on the ledger and in a few of our derived-from-ledger caches*. It says nothing about **whether our own machinery was healthy at stamp time**, whether **our claim manifest is intact**, or whether **our editorial layer has drifted**. V4 folds three new evidence areas into every future stamp so a verifier can independently confirm all three from the signed envelope alone. The on-chain anchor spec is untouched (v1 remains LOCKED-FINAL); v4 lives entirely inside the signed-snapshot JSON that the anchor commits by root.

---

## §0. THE FIRST RULING NEEDED — Shape A vs Shape B (the leaf-vs-metric question)

Charlie's directive said "three new leaves join the weekly anchor." Two readings are viable and the choice cascades through everything else. **Pick one before I write a line of code.**

### Shape A — three new **metrics** inside the existing single-leaf-per-snapshot

- `collect_metrics()` returns 12 metric entries instead of 9 (the current xrpl/amm/mpt/rlusd/rwa metrics + `walker_health_summary`, `claims_index_state`, `editorial_state`).
- `_hash_leaf()` still runs over the whole leaf payload (`signing_domain + schema_version + snapshot_date_utc + metrics`), so **one leaf per snapshot as today**.
- Merkle tree is unchanged (one node per weekly stamp, chain grows linearly).
- `schema_version` bumps 3 → 4.
- **Simplest, additive, symmetric with how RLUSD/RWA were added.** Historical audit paths keep working; every verifier already ready to see new metric entries just gets three more.

### Shape B — three actual new **leaves** per snapshot

- Each stamp writes 4 leaves in the same "cycle" (current-metrics leaf + walker-health leaf + claims-index leaf + editorial-state leaf).
- Merkle tree has 4× the width per stamp.
- `chain.json` `leaves` array grows by 4 per cycle instead of 1.
- Each leaf gets its own leaf_hash + audit_path + signature envelope; each is independently verifiable.
- **More granular, more truthful about what's being anchored** — but restructures the tree, breaks the current "one leaf = one snapshot" invariant, and complicates the historical shape: v1-3 stamps have 1 leaf per cycle, v4 stamps have 4, so a verifier walking the chain has to know the schema at each position.

### My read (rulable, not ruled)

**Shape A is the right choice tonight** for three reasons:
1. Preserves the "one leaf = one snapshot" chain shape → verifier logic doesn't fork by schema version at chain-walk time.
2. The three new areas are properly summarized as *metric-shaped* facts (a count, a hash, a set of freshness stamps). We don't lose fidelity by summarizing.
3. Shape B's granularity is only valuable if someone wants to prove *just* walker-health independently of the snapshot — no reader has asked for that. YAGNI.

**If you rule Shape B**, the rest of the pack rewrites — the source reads and canonical shapes stay, but §3 backward-compat and §4 acceptance tests change significantly.

**Assumption for the rest of this pack: Shape A. If B, we redraft §3/§4.**

---

## §1. Source-of-truth reads per leaf

### 1a. `walker_health_summary` metric

**What we want to commit:** a machine-verifiable snapshot of walker fleet health at stamp time — enough that a reader can, next month, prove which walkers were green/stale/dead when this week's numbers were captured. This is the anti-silent-failure evidence: if a walker was dark when its dashboard numbers were captured, this metric surfaces that fact in the signed envelope.

**SoT read (Postgres):**

```sql
SELECT walker_name, last_run_ok, consecutive_failures,
       EXTRACT(EPOCH FROM (NOW() - last_success_at))::bigint AS age_seconds,
       cadence_seconds
  FROM walker_health
  ORDER BY walker_name;
```

(Same shape as `db.read_walker_health_all()` at db.py:2848 — we can call it directly rather than duplicate SQL.)

**Metric value shape (proposal):**

```json
{
  "name": "walker_health_summary",
  "value": {
    "total_walkers": 27,
    "green_count": 25,
    "stale_count": 2,
    "dead_count": 0,
    "walkers_digest_sha256": "3f7c9a…"
  },
  "unit": "walkers",
  "source": "walker_health table via db.read_walker_health_all()"
}
```

Where `walkers_digest_sha256` is the SHA-256 of the **canonical-JSON** serialization of the sorted list:

```python
[
  {
    "walker": "amm_snapshot_walker",
    "state": "green"|"stale"|"dead",   # derived from last_run_ok + age vs cadence*N
    "consecutive_failures": 0,
    "age_multiples_of_cadence": 0.4    # rounded to 1dp
  },
  # ... all walkers, alphabetically ...
]
```

This gives a reader: (a) top-line counts they can eyeball; (b) a digest to compare against if they can recompute the same shape from `/walker_health` (which we already surface). Three counts + one digest = ~120 bytes on-chain-committed evidence about our own machinery.

**Thresholds** (proposal):
- `green` = `last_run_ok=true AND age_seconds ≤ 2 × cadence_seconds`
- `stale` = `last_run_ok=true AND 2 × cadence < age ≤ 8 × cadence` (or `last_run_ok=false AND consecutive_failures < 3`)
- `dead` = `age > 8 × cadence` OR `consecutive_failures ≥ 3`

These match the /walker_health page's severity buckets. If they don't match, that's an integrity break and I need to know before shipping.

**Open question for Charlie:** should `walkers_digest_sha256` be over the *sorted names + states only* (small, stable) or over the *full per-walker detail* (larger, brittle to noise like `age_multiples_of_cadence` fluctuations)? My default is the sorted-detail-with-1dp-rounding above — verifiable but not so tight that a routine cadence blip breaks the digest. **Rulable.**

### 1b. `claims_index_state` metric

**What we want to commit:** proof that our public-claim manifest (`CLAIMS.yaml`, Truth-Audit Layer 4) is intact — that we didn't silently remove or edit a claim between stamps. If it changes, the metric changes with it and the change is committed forever alongside the stamp.

**SoT read (file + git):**

```python
# The file itself
claims_yaml_bytes = open("CLAIMS.yaml", "rb").read()
claims_sha256 = hashlib.sha256(claims_yaml_bytes).hexdigest()

# Structural summary (parsed)
import yaml
doc = yaml.safe_load(claims_yaml_bytes)
pages = doc.get("pages", {})
claim_count = sum(
    len(p.get("claims", []) or []) for p in pages.values()
)
page_count = len(pages)

# Git commit that owns the current CLAIMS.yaml
git_head_short = subprocess.check_output(
    ["git", "log", "-n1", "--format=%h", "--", "CLAIMS.yaml"],
    text=True
).strip()
```

**Metric value shape:**

```json
{
  "name": "claims_index_state",
  "value": {
    "page_count": 4,
    "claim_count": 47,
    "claims_yaml_sha256": "8a2b91…",
    "claims_yaml_git_short": "706bc0a"
  },
  "unit": "claims",
  "source": "CLAIMS.yaml file hash + git log"
}
```

**Why the git short and the file SHA both:** the git short lets a reader `git show <hash>:CLAIMS.yaml` and diff; the file SHA lets a reader without git access still verify. Redundant on purpose — Layer 4 is defense-in-depth.

**Open question for Charlie:** do we commit only the byte-hash (small, fast, always correct), or also `page_count`+`claim_count` (softer, more useful at a glance)? My default is the shape above — both. Pure byte-hash is the ground truth; counts are eyeball convenience. **Rulable.**

### 1c. `editorial_state` metric — RULED FRESHNESS-ONLY FOR v4

**RULING 2026-08-27 22:22 ET:** freshness-only. Correction/wound registries are real new sources-of-truth that deserve their own design sitting — NOT prerequisite files invented at midnight under a stamp deadline. They join in v5, designed properly. `docs/CORRECTION_REGISTRY.md` + `docs/WOUND_REGISTRY.md` are NOT created for this v4 build.

**v4 scope (post-ruling):** `editorial_state` commits only the `LAST_VERIFIED_*` freshness stamps from `app.py`. That's it. Registry-based fields (`correction_*`, `wound_*`) are deferred to v5.

**What we want to commit (v4 scope):** proof that the editorial layer's declared freshness is what we said it was. If we quietly let `LAST_VERIFIED_AGENT_TIER_METHODOLOGY` age past its 90-day guard, the metric changes and the change is committed forever.

**SoT read (v4 — freshness-only):**

Single input, git-tracked:

- **Freshness stamps** — `LAST_VERIFIED_REGULATION` (app.py:241), `LAST_VERIFIED_AGENT_TIER_METHODOLOGY` (app.py:257). Enumeration at stamp time via `re.finditer(r'^LAST_VERIFIED_(\w+)\s*=\s*"(\d{4}-\d{2}-\d{2})"', app_py_source, flags=re.M)` — so new `LAST_VERIFIED_*` constants added later are picked up automatically without touching `signed_snapshot.py`.

**Metric value shape (v4, ruled):**

```json
{
  "name": "editorial_state",
  "value": {
    "last_verified_stamps": {
      "LAST_VERIFIED_REGULATION": "2026-08-17",
      "LAST_VERIFIED_AGENT_TIER_METHODOLOGY": "2026-08-27"
    }
  },
  "unit": "editorial",
  "source": "app.py LAST_VERIFIED_* constants (regex-enumerated at stamp time)"
}
```

Nested `last_verified_stamps` is a dict — `sort_keys=True` orders its entries alphabetically by constant name. Deterministic.

**Deferred to v5** (owned, will get their own design sitting):

- `correction_registry_sha256` + `correction_registry_git_short` + `correction_count` — requires creating `docs/CORRECTION_REGISTRY.md` as a canonical file (first entry: 2026-08-17 /regulation correction; today it lives in narrative + on-chain correction anchor only).
- `wound_registry_sha256` + `wound_registry_git_short` + `wound_count_open` + `wound_count_closed` — requires creating `docs/WOUND_REGISTRY.md` as a distilled canonical file separate from the working `SITE_AUDIT_QUADFECTA_2026-08-26.md` §wounds table.

**Reason for deferral (Charlie's words, 2026-08-27 22:22 ET):** "The correction/wound registries are real new sources-of-truth that deserve their own design sitting — not prerequisite files invented at midnight under a stamp deadline. They join in v5, designed properly."

---

## §2. Canonical shape (serialization + ordering)

**Unchanged from v3.** Every new metric entry follows the existing `{name, value, unit, source}` shape (see signed_snapshot.py:329-334 for the pattern). Serialization goes through `_canonical_json()` (sort_keys=True, no whitespace, no NaN tolerance). Nested objects inside `value` are dicts — `sort_keys=True` sorts them recursively, so `walkers_digest_sha256` will always come after `total_walkers` alphabetically, and a reader recomputes byte-identical bytes.

**One rule I want on record:** if any nested value is a **list**, its ORDER is part of the commitment. `walkers_digest_sha256`'s input list is sorted alphabetically by walker name; we do NOT sort by state or count. Sorted by walker name is the canonical order. Written down here because sort-key choice for lists is not derivable from `sort_keys=True`.

**Metric-list order:** the existing `collect_metrics()` produces metrics in insertion order (xrpl, amm, mpt, named_accounts, rlusd_*, rwa). V4 additions go **at the end** of the list, in the order: `walker_health_summary`, `claims_index_state`, `editorial_state`. Alphabetical would be cleaner, but insertion-order matches what's in the code today and preserves diff readability. **Rulable — alphabetical or insertion-order? My default is insertion-order-with-new-at-end.**

---

## §3. Backward-compatibility statement

### The invariant we protect

**Every historical signed snapshot (v1, v2, v3) verifies against ITS OWN recorded `chain_root`, with ITS OWN recorded `audit_path`, using the SAME public key.** Adding v4 leaves to the chain does not touch any historical file. The current `chain.json` continues to have exactly one leaf entry per snapshot date; v4 stamps append to that list identically to v3 stamps (Shape A guarantees this).

### How a verifier distinguishes v3 from v4

The leaf payload always contains `schema_version` as an explicit integer:

```json
{
  "signing_domain": "xrpldashboard.com/signed_snapshot/v1",
  "schema_version": 3 or 4,
  "snapshot_date_utc": "2026-08-27",
  "metrics": [ ... ]
}
```

A v3-only verifier that fetches a v4 snapshot file will:
1. See `schema_version: 4` in the leaf payload.
2. Recompute the leaf hash byte-for-byte via `_canonical_json` — **succeeds**, because `_canonical_json` is version-agnostic bytes.
3. Verify the audit path against `chain_root` — **succeeds**, because the audit path is a chain of SHA-256s that don't care about the semantic content of the leaf.
4. Verify the Ed25519 signature over the envelope — **succeeds**, because the envelope binds `leaf_hash` (not `leaf_payload_semantics`).
5. Attempt to parse the metrics list — if they only know the 9 v3 metric names, they read those and ignore the 3 v4 additions. **No error.** (This assumes verifiers loop-over-metrics rather than assuming a fixed schema. `docs/methodology#signed-snapshots` reflects this shape.)

**Written rule for verifier authors:** treat `metrics` as an unordered set of `{name, value, unit, source}` entries. Match by `name`. Unknown `name`s are ignored, not errors. This shape has been forward-compat-friendly since v1.

**Written rule for stamp authors (us):** never REMOVE a metric name once shipped in a chain. Never RENAME one. Only ADD. If a metric becomes obsolete, keep emitting it (possibly with `value: null`) until the next major-version bump that's explicitly documented as breaking.

**One place this rule bites us:** `mpt_snapshot_unavailable_or_stale` on 2026-07-28 was recorded as an `error`, not `metric: value: null`. The current v3 shape says "missing metric = absent from list". Changing to "missing metric = present with value: null" would itself be a schema change. **Decision: v4 preserves current v3 behavior (absent-when-missing) so the change is purely additive. Written down so no one changes it accidentally.**

### `schema_version` in `chain.json`

`chain.json` currently has `"schema_version": 3` at its top level (see signed_snapshot.py:492). **Proposal: bump chain.json's schema_version to 4 when the first v4 snapshot lands, and record `schema_version_history` as a new field to remember the transition:**

```json
{
  "schema_version": 4,
  "schema_version_history": [
    {"version": 3, "last_snapshot_date": "2026-08-22"},
    {"version": 4, "first_snapshot_date": "2026-08-29"}
  ],
  "leaves": [ ... ],
  "current_root": "…"
}
```

**Rulable — do you want the history array, or just the top-level number?**

---

## §4. Acceptance-test spec

Four gates, all must pass before the v4 code becomes the daily-ceremony code:

### 4a. Dry-verify v4 stamp against CURRENT chain (offline)

```bash
python3 signed_snapshot.py --dry-run > /tmp/v4_dry.json
python3 signed_snapshot.py --verify /tmp/v4_dry.json  # (existing --verify flag)
```

Expected: signature valid, leaf hash matches, audit path recomputes chain_root, chain_root equals value pre-computed against current `chain.json` + 1 hypothetical v4 leaf.

### 4b. Verify EVERY historical snapshot still passes with v4 code loaded

```bash
for f in signed_snapshots/2026-*.json; do
    python3 signed_snapshot.py --verify "$f" || echo "FAIL: $f"
done
```

Expected: zero FAIL lines. Every historical snapshot (v3 today) verifies with the v4-aware code.

### 4c. Shape-C canary reads v4 correctly

`tools/anchor_canary.py:check_chain_anchors` fetches `/.well-known/snapshots/chain.json` and compares each on-chain anchor's `chain_root_hex` to `lookup_chain_root_for_date(chain, snapshot_date)`. This code is version-agnostic (it compares hex strings). **We manually run:**

```bash
python3 tools/anchor_canary.py --dry-run
```

against a pre-staged local `chain.json` containing a hypothetical v4 leaf. Expected: `latest anchor root matches live chain.json` and the enriched heartbeat message renders the v4 leaf's root_hex prefix without error.

### 4d. The `walker_health_summary` digest is stable across re-runs

Two `--dry-run` executions within one minute must produce identical `walkers_digest_sha256` values (walker cadences don't shift by more than 1 second in that window, and `age_multiples_of_cadence` is rounded to 1dp — should hold). If it doesn't, the digest input needs a tighter rounding or a snapshot-time freeze.

**Cross-cut acceptance:** anchor #4 (this Friday) runs v3, unchanged, because v4 cannot possibly pass all four gates + get a Charlie ruling + get pushed + get a week of soak by Friday morning. **v4 debuts at anchor #5 (next Friday, 2026-09-04) at the earliest.** Consistent with your timing-honesty rule.

---

## §5. Failure modes at stamp time

Charlie's house rule: **never stamp a guess.** When a leaf's SoT read fails:

### 5a. Walker-health source unreadable (PG unavailable, query throws)

**Proposal: refuse the stamp.** `collect_metrics()` today records an entry in `errors` when a source is unavailable and OMITS the metric from the list. For the three new v4 metrics, my proposal is **stricter**: a missing walker-health/claims-index/editorial-state source is a **refuse-to-stamp** signal (raise SystemExit with a specific error), because these are the "our machinery is healthy" evidence. Emitting a signed stamp that silently omits proof-of-our-own-health is the exact anti-pattern the metric was created to prevent.

**Rationale:** for a ledger metric (e.g., `rlusd_xrpl_supply`), a missing source is honest — the ledger is upstream, we can't invent it. For a walker-health metric, a missing source *is* the walker being dead, and stamping without it hides the failure. Anti-Layer-4 in shape.

### 5b. Claims-index unreadable (CLAIMS.yaml missing or unparseable)

**Refuse the stamp.** Same reasoning. CLAIMS.yaml is git-tracked and in the repo — it can't be missing without something breaking upstream. Stamping through a missing CLAIMS.yaml would lie about the manifest being intact when it isn't there at all.

### 5c. Editorial-state partial (freshness stamps read OK, correction registry missing)

**Refuse the stamp.** Same reasoning. If one of the three components silently drops out, the whole `editorial_state` metric drops out, and now the stamp is quieter than it should be about a domain we chose to commit.

### 5d. If the strict-refuse rule breaks the daily ceremony repeatedly

If a walker legitimately goes stale and blocks stamping every day for a week, we have a bigger problem than v4 — but the graceful-degrade escape hatch is a `--force-partial-v4` flag that emits an EMPTY-object metric with a `"stamp_time_unavailable": true` field, and pages a warning. **Never a guess; explicit absent.** This flag would be for exceptional recovery only, not the default path.

**Rulable — do you want strict-refuse (my default) or graceful-degrade with honest-absent markers as the default? I recommend strict-refuse. Charlie decides.**

---

## §6. What lands where and when

**If Shape A ruled + all §1 defaults accepted (10 minutes of your reading time):**

1. Tonight: I create `docs/CORRECTION_REGISTRY.md` + `docs/WOUND_REGISTRY.md` skeletons (both prerequisite files). ~15 min.
2. Tonight: I write the v4 metric-collection functions + bump `SCHEMA_VERSION` to 4 + wire the three new metrics into `collect_metrics()`. ~30-45 min.
3. Tonight: Acceptance tests 4a-4d run locally, results reported. ~10 min.
4. Tonight: Push walkthrough served, you push.
5. Tomorrow morning (Friday): anchor #4 runs v3 code as normal — v4 code is on disk but the ceremony script hasn't cut over yet.
6. Next 7 days: soak. Any daily-cadence signed snapshot (if we do daily; v3 comments suggest daily builds even if anchor is weekly) exercises the v4 code path.
7. **Next Friday (2026-09-04): anchor #5 stamps v4.**

**If Shape B ruled:** I redraft the pack; probably ~45 additional minutes before code, and the acceptance tests grow to include chain-shape-walk tests (verifier must correctly walk 1-leaf-per-cycle historical vs 4-leaves-per-cycle v4).

**If any §1 shape is ruled differently:** ~15-30 min re-scoping per component, then same flow.

**If timing-honesty gate fails (any of the above blows the tonight budget):** v4 debuts at anchor #6 or later. Anchor #4 is safe on v3. Nothing rushes.

---

## §7. Rulings — LANDED 2026-08-27 22:22 ET

Charlie: "ACK all defaults EXCEPT §1c."

- [x] **§0** — Shape A (metric-additions inside the existing single-leaf-per-snapshot). Ruled.
- [x] **§1a** — `walkers_digest_sha256` input = sorted detail with 1dp rounding. Ruled.
- [x] **§1b** — `claims_index_state` shape = SHA + git-short + counts (all four). Ruled.
- [x] **§1c** — editorial-state scope = **FRESHNESS-ONLY for v4**. Correction/wound registries deferred to v5, designed properly. Ruled.
- [x] **§2** — metrics list order for new entries = insertion-order at end. Ruled.
- [x] **§3** — `chain.json` schema_version bump = add `schema_version_history` array. Ruled.
- [x] **§5** — failure mode when a v4 SoT read fails = strict-refuse (never stamp a guess). Ruled.

**Timing confirmed:** anchor #4 tomorrow runs v3 unchanged; v4 debuts anchor #5 (2026-09-04) with a week of testing behind it. Correctness > deadline.

**Build week begins next**: v4 code lands on disk mid-week, acceptance gates 4a-4d run local + soak, push walkthrough served when green. v5 correction/wound registries queued for a separate design sitting after v4 debuts clean.

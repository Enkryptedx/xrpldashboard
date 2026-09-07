# /tokens visual — lanes by verification tier (scope doc, not build)

**Status:** scope only, per Charlie's ruling 2026-09-07. No code lands from this document; it exists to sharpen the decision before we commit to the refactor.

## Today's visual (baseline)

`/tokens` renders a live-trade grid where each pulse's **lane** is decided by taxonomy category-family (fold via `CATEGORY_FAMILY`). The five lanes present in the v5 design:

1. `stablecoin` (regulated + gateway)
2. `wrapped` (wrapped_bridge)
3. `utility` (native_utility_chain, dex_utility, defi_lending, defi_yield, gaming, community)
4. `memecoin`
5. `unlabeled` (fold target for `not_yet_reviewed` and `reviewed_unlabeled` — same visual today; distinguishable via tooltip)

Category-family folding is a fast, correct read for "what class of thing traded" — but it collapses a critical axis of trust. A `memecoin` labeled by a curator with a citation and a `memecoin` inferred mechanically from absence-of-signal look identical in this visual. They shouldn't.

## Proposed alternative — verification-tier lanes

Reorganize lanes by **the strength of the attestation** rather than the category. Each pulse still carries its category (rendered in-pill or on hover), but lane assignment reflects how much evidence the registry has about that token.

Proposed 5-lane tier order (top → bottom):

1. **`verified`** — token has an L3 curator attestation with `verified_via` URL and matching two-way toml proof, OR MPT metadata with `asset_subclass` populated (regulated-entity-issued stablecoins, RWA-attested tokens, named DEX utility tokens). Highest-attestation pulses.
2. **`self_described`** — token has a toml claim (or MPT metadata) but no curator's independent attestation. What the issuer says about themselves, unaudited.
3. **`curator_inferred`** — a curator has assigned a category based on structural evidence (issuer address pattern, community-lead identification) but the row carries `tier=curator-inferred` explicitly. Distinguished from `verified` by the reader's tooltip and by a lighter pulse ring.
4. **`mechanical`** — L1-derivable classes: `lp_token` (AMM pool share by construction), `non_standard_code` (non-printable hex codes). These need no human judgment.
5. **`not_yet_reviewed` + `reviewed_unlabeled`** — the two review-status values from the taxonomy split (2026-09-07). Kept together in one lane visually — both share "no category applies" — but their tooltip differs, and the coverage-gauge counts them separately (per taxonomy_v1.md).

## Why this reorganizes the story

- **Trust is legible.** A pulse in the top lane reads as "the strongest attestation we have"; a pulse in lane 5 reads as "we honestly know nothing about this." Category information is preserved but no longer dominates the reader's first impression.
- **Coverage gap is visible.** Today's `unlabeled` bar is one of the biggest visual features, but it fold-collapses two distinct honesty states. Under the new mapping, the bottom lane's split into "reviewed" vs "not-reviewed" matches the taxonomy's two states directly.
- **Ticker-collision and non_standard_code flags become chromatic overlays, not lane movers.** A ticker-collision USDT pulse would land in `not_yet_reviewed` (its default state) with an amber-ringed pulse — the ring is the collision. That preserves the lane semantic while giving collisions their own visual signal.

## What stays the same

- Pulse spawn rate + physics: unchanged (adaptive fall speed from 7b lands regardless).
- Category color still keys the pulse fill: `stablecoin_regulated` remains blue, `memecoin` pink, etc. Lane assignment changes; color doesn't.
- Live WS feed shape: unchanged. Same `LABELS[key]` map, same `spawnPulse` entry point.
- Regression protection: existing v5 Gate-3 story around the "floor inside the unlabeled bar" survives the refactor — the floor is a subset of the bottom lane's `not_yet_reviewed` state.

## What changes, concretely

- `laneIndex(cat)` in tokens.html becomes `laneIndex(tierOf(cat, sourceInfo))` where `sourceInfo` includes:
  - the row's provenance source (`curator`, `toml`, `mpt-metadata`, `mechanical`, `curator-inferred`, or absence)
  - the `reviewed` flag from `token_category_current`
- `CATEGORY_FAMILY` becomes `TIER_OF_CATEGORY` — a two-argument fold that considers category AND tier.
- The template's hero-rollup bars restructure: instead of "one bar per category family," it becomes "one bar per verification tier," each showing the category breakdown as inline segments.
- `/token` detail badges gain a tier chip alongside the category chip (already 2026-09-07 in progress via the review-status split).

## Data prerequisites (already in place)

- `token_category_history` has `tier`, `source`, `citation_url`, `curator_authority` — everything needed to derive verification tier per row.
- `token_category_current` view exposes the current tier per (currency, issuer). Ready to consume.
- Taxonomy v1 already documents the tier vocabulary: `verified | self-described | curator-inferred | mechanical | bare` — the lanes map 1:1 to these.

## Risks + gates

- **Regression window on the v5 hero rollup.** The current bars are load-bearing for the sovereignty-tier narrative. The refactor rebuilds those bars; a stale-data window during rollout could show incorrect tier counts. Ship behind a feature flag; verify against yesterday's snapshot before flipping.
- **Curator queue impact.** A pulse rendered in `curator_inferred` is a visible mismatch relative to a fully-verified pulse; that visibility may accelerate curator attention (good) or spike disputes (needs a queue). Wait until the L2c submission form is live before flipping the visual — otherwise disputes have nowhere to land.
- **Adaptive fall-speed interaction.** The current 7b logic uses `pulses.length` + `cat === 'unlabeled'` for the quiet-labeled dwell path. Under the new tier scheme, "labeled" needs to become "tier ≥ self_described" (i.e., not in the bottom lane). Small edit; needs a test.

## Attorney touchpoints

None specifically flagged for this doc — the taxonomy vocabulary is already published under Charlie's editorial authority; this is a visual reorganization, not a new class assertion about any specific token.

## Decision needed

Charlie approves the tier-lane concept (yes/no + variant preferences), then this doc becomes the acceptance criteria for the build in a follow-up session. If declined, we keep the current category-family lanes and route the review-status split entirely into the /token detail render (already shipped) + the coverage gauge (deferred).

Owner: Charlie · JJ builds after approval.

# NFT audit + "verified NFT view" — scope doc

**Status:** scope only, per Charlie's ruling 2026-09-07. No code lands from this document. Content policy sub-decisions are attorney-gated and explicitly named as such.

## What `/nfts` does today (baseline audit)

- Reads `nft_activity` table (walker-populated from the XRPL `NFTokenMint`/`NFTokenAcceptOffer`/`NFTokenBurn` stream).
- Renders a paginated list of recent NFT trades with issuer, taxon, transaction hash, price (drops), buyer / seller / broker fields, currency + currency_issuer for the price, and the raw XRPL NFTokenID.
- No image display today. No metadata resolution. No content policy applied — the raw hex-encoded URI (`uri` in NFTokenMint) is not fetched, decoded, or displayed.
- Sort: `close_time DESC`. No filter for a specific issuer, no filter for "verified issuers only," no filter by taxon.

## Threat model + baseline honesty

The XRPL NFT surface has three durable spoofability vectors that a "verified NFT" view must address:

1. **Metadata-issuer ≠ minter.** An attacker can mint an NFT with a metadata URI that points at a JSON file claiming a well-known issuer's identity. The minter address is inescapably on-chain; the metadata is off-chain narrative. A user who sees the metadata-declared "Ripple x SomeArtist" without seeing the minter address may believe the wrong issuer.
2. **Metadata JSON drift.** IPFS content-addressed URIs (`ipfs://<cid>`) are stable — the CID is a hash of the content. HTTP(S) URIs (`https://example.com/nft/123.json`) are not — the server can serve different bytes tomorrow than today. A verified view must handle these two cases differently.
3. **Impersonation-by-similarity.** A collection called "Zerpmon" and one called "Zerpmoms" render identically at a glance; issuer address is the ground truth.

## Proposed "verified NFT view" — layered

### Layer 0 — always shown (mechanical)

- Minter address (`Issuer` field of the NFTokenMint transaction).
- Taxon (numeric collection identifier chosen by the minter).
- Metadata URI raw form (hex-decoded).
- Ledger index + tx hash for permanence.

These four are unspoofable — they're on-chain facts about the mint itself. No image, no metadata resolution needed.

### Layer 1 — content resolution (opt-in per collection)

Fetch the metadata JSON, decode, extract `name` / `description` / `image` fields. This layer is **opt-in per curator-verified collection** — collections that pass the "verified" gate (Layer 2) get their metadata fetched and rendered inline. Others render Layer 0 only.

Rationale: fetching metadata for every NFT trade would (a) pull arbitrary content through our infrastructure, some of which may be malicious (see §"Content policy" below), and (b) balloon our egress cost with third-party gateway traffic.

**IPFS handling — own gateway vs third-party.** Two options:

- **Own IPFS node.** Run a bandwidth-metered IPFS node (Kubo daemon) that resolves `ipfs://<cid>` locally. Advantages: no third-party can log our lookups (privacy for the reader), no dependency on `ipfs.io` or Cloudflare's gateway going down. Disadvantages: operator cost (~5-20 GB storage baseline, bandwidth for the CIDs we pin), Charlie's list of infrastructure grows.
- **Curated whitelist of gateways.** Use `cf-ipfs.com` or `ipfs.io` for reads, with a per-gateway health check. Cheaper to run; leaks lookup patterns to the gateway operator. Rate limits from the gateway can slow user requests.

Recommend: **start with the whitelisted-gateway approach + fall-open to raw CID display + IPFS-explorer link**. Move to own-node only when read volume justifies the operator cost. Track a simple "IPFS gateway latency" metric to know when to switch.

**HTTPS metadata.** Resolve once, cache the resolved JSON with a content-hash. If the URL serves different content on a later fetch, mark the row as "metadata drift observed" — that's a signal a verifier should refuse.

### Layer 2 — verified gate ("verified NFT view")

A collection is **verified** when ALL of the following hold:

1. **Metadata-issuer-must-match-minter spoof check.** The metadata JSON's `issuer` (or `attributes.issuer` or equivalent) field, if present, must match the on-chain minter address. If the metadata claims a different issuer than the actual minter, the collection is REJECTED from the verified view — it lands in Layer 0 with a "metadata-issuer mismatch" chip.

2. **Curator whitelist entry.** The (minter, taxon) tuple appears in a curator-maintained NFT registry file (analogous to `ticker_canonical_issuers.json` for tokens). The registry entry names the collection, its brand identity, and a citation URL. Same L3 evidence bar as for the token registry.

3. **IPFS content-addressed if the metadata was resolved from an ipfs:// URI**, OR content-hash-stable HTTP if from an https:// URI. Metadata drift → auto-demotion to Layer 0 pending curator review.

Only the verified subset renders images, brand names, and full metadata inline. Everything else renders Layer 0 with a discoverable "no attestation" pill.

## Content policy (attorney gate)

Explicitly deferred to legal review:

- What NFT content is allowed to render inline on the site (nudity, hate speech, prohibited-jurisdiction imagery, deepfakes).
- Whether we assume common-carrier or curator status when we cache metadata to our own infra.
- Notice-and-takedown workflow when a curator-verified collection is later found to include prohibited content.
- Jurisdictional applicability (DMCA in the US, DSA in the EU, other regional rules).

**Nothing about content rendering ships until Charlie's attorney has reviewed this section.** The technical infrastructure (Layers 0-2) can be built without content rendering; the visual gates on the attorney's ruling on what's rendered.

## Data prerequisites

- `nft_activity` table exists + is populated.
- Need new table: `nft_collection_verified` — curator registry of (minter, taxon, brand_name, verified_via_url, first_verified_at, notes). Analogous to `named_accounts.json` structure but scoped to NFT collections.
- Need new walker: `nft_metadata_resolver_walker.py` — fetches metadata JSON for verified collections, caches, detects drift. Runs on a polite cadence (1-2 min per fetch, prioritizing recently-active collections).
- Optional (bigger scope): `nft_image_proxy` — serves resized/sanitized images from a caching layer, with the content-policy gate applied. Not required for MVP.

## Rollout gates

Ship in this order; each gate requires the previous to be complete + verified.

1. Layer 0 rendering (already close to today's state). No new tables, just the display refinement (add ledger_index + tx hash if not already visible).
2. Curator registry file `nft_collection_verified.json` scaffold, empty by default. Charlie names 3-5 initial verified collections (e.g., Zerpmon, XRPillars, Ark Institute) once the file exists.
3. Metadata-issuer-must-match-minter spoof check as a walker path. Fires per (minter, taxon) on first observation; result stored in `nft_activity_metadata` cache. Rejects populate a "metadata-issuer mismatch" audit log.
4. IPFS gateway integration for verified collections only. Whitelisted gateway with per-gateway health check.
5. Attorney content-policy review + gated inline image rendering behind a feature flag.
6. Public flip of the verified view.

## What this scope doc IS NOT

- Not a promise of a delivery date.
- Not a design for the walker code — the walker's shape follows once Charlie approves the tier logic.
- Not the attorney's content policy; that lives elsewhere and gates step 5 above.

Owner: Charlie · JJ builds after approval.

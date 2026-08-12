# XRPL Notary — Dark-Build Spec (INTERNAL WORKING NAME)

**Status:** DARK BUILD. Internal working name only. Public brand held
pending Indiana notary-statute counsel review (per
`memory/project_notary_name_legal_catch_2026-08-12.md`). Every artifact
in this build ships the same day-one disclaimer regardless of which
brand word ends up on the landing page.

**Author:** Claude (per Charlie's Track 3 build order 2026-08-12).
**Sibling docs:** `docs/ONLEDGER_ANCHOR_SPEC.md` (this generalizes the
memo shape from that spec into a publisher-agnostic protocol);
`docs/X402_RAILS_DARK_SCOPING.md` (the paid-tier flip is mediated by
the rails-dark middleware — never a code change).

---

## Day-one disclaimer (ships in EVERY artifact)

> **Proves a file existed unchanged at a point in time. Does NOT
> verify identity or replace a notary public.**

This line appears on:

- The spec (this doc, above §Purpose).
- The `/notary/*` HTTP endpoints (in every response body).
- Every receipt (in the receipt JSON as a top-level `disclaimer`
  field, human-readable).
- The verify library README + every `verify()` call's return dict.
- The landing page copy whenever it exists.
- The XRPL Payment memo footer (as much as fits — the memo carries a
  one-word `disclaimer_ref` pointing at a stable URL where the full
  line is published).

The disclaimer is not decorative. It is the primary anti-conflation
signal — this service does what OpenTimestamps does (cryptographic
timestamping), not what a notary public does (identity attestation,
witnessed signing). Advertising-word restrictions in states like
Indiana attach to the word "notary" itself, so even the internal
working name gets swapped before any public label lands.

**Backup names on shelf** (per notary-name legal catch memo):

- XRPL Anchor
- XRPL Timestamp
- XRPL Attest
- XRPL Proof-of-Existence

The first attorney call that clears the notary-word question wins the
label. Until then: `XRPL Notary` internal working name; every public
artifact stays unshipped.

---

## §1 — Purpose

A publisher-agnostic anchoring-as-a-service pipeline built on the same
signed-snapshot + on-ledger memo shape used for
`xrpldashboard.com/.well-known/snapshots/`.

**What it does:**

1. Accepts a SHA-256 digest of an arbitrary file from a third-party
   publisher (identified by a DID or `pubkey.pem` fingerprint).
2. Emits a signed receipt containing the digest, timestamp, and
   publisher identifier, signed with our Ed25519 snapshot signing key.
3. Anchors the receipt hash inside an XRPL Payment memo from a
   dedicated Notary account to our ops wallet, on the same cadence as
   the signed-snapshot chain root (weekly manual initially, walker-
   automated post-signal).
4. Publishes the receipt at a stable URL under
   `xrpldashboard.com/notary/receipt/<id>.json` and appends to a
   per-publisher `chain.json` file.
5. Provides an MIT-licensed verify library
   (`xrpl_notary_verify.py`) that a third party can drop into any
   Python environment to independently verify a receipt against XRPL
   without contacting our servers.

**What it does NOT do:**

- **No identity verification.** We do not verify who the publisher
  is. We do not accept government ID. We do not witness signatures.
  We anchor a hash a caller sent us; that caller's identity claim is
  entirely their DID resolution problem.
- **No custody of the file.** The publisher retains their own file.
  We only ever see the SHA-256 digest. Loss of the original file =
  loss of the ability to verify it against the digest we anchored.
- **No legal advice.** This is a cryptographic timestamp, not a legal
  instrument. What jurisdictions accept a cryptographic timestamp as
  evidence is a question for the receiver's counsel, not ours.

---

## §2 — Memo format (v1, generalizes on-ledger anchor v1)

The existing `xrpldashboard/anchor/v1|<date>|<chain_root>` memo shape
locks in a single-publisher assumption. The notary generalization:

```
xrpldashboard/notary/v1|<publisher_id>|<utc_date>|<sha256_digest>
```

Fields:

- `xrpldashboard/notary/v1` — namespace + protocol version. Bumps on
  any breaking change (never quietly).
- `<publisher_id>` — the identifier the publisher registers with us.
  Accepted forms:
  - DID (`did:web:example.com`, `did:key:z6Mk...`, etc.)
  - Ed25519 public key fingerprint (16 hex chars, SHA-256 of the
    PEM public key, first 64 bits — matches
    `/.well-known/snapshots/pubkey_fingerprint.txt` convention)
  - Reserved literal `self` for `xrpldashboard.com`'s own anchors
    (backwards-compat with the v1 anchor spec).
- `<utc_date>` — `YYYY-MM-DD` in UTC, day the receipt was signed.
- `<sha256_digest>` — hex-encoded SHA-256 of the receipt JSON with
  canonical key ordering (RFC 8785 JCS if possible; otherwise
  `json.dumps(..., sort_keys=True, separators=(",", ":"))` — locked
  by v1).

**Memo carries the receipt digest, not the file digest.** The receipt
is a JSON object that itself contains the file digest, publisher_id,
utc_date, disclaimer, and other fields. Anchoring the receipt hash
means the memo attests to the full receipt structure; the file digest
is verifiable through the receipt.

Rationale: matches OpenTimestamps' aggregation shape (anchor the
Merkle root, not each leaf) so a future edit can move from
one-anchor-per-file to Merkle-batched anchors without a memo format
break.

**MemoData wallet-padding rule** (inherited from
`docs/ONLEDGER_ANCHOR_SPEC.md`): Xaman appends newlines to short
memos. Verifiers MUST `.strip()` the decoded MemoData before parsing.
This rule locks at v1.

**Prohibited memo content** (inherited from on-ledger anchor spec §
Prohibited operations): no other memo fields, no off-anchor content,
no cross-currency memo abuse. Any Notary Payment carrying a memo that
doesn't parse under the v1 rule is invalid.

---

## §3 — Receipt shape (v1)

```json
{
  "protocol": "xrpldashboard/notary/v1",
  "receipt_id": "2026-09-01-a7f3c9d2",
  "utc_date": "2026-09-01",
  "utc_timestamp": "2026-09-01T14:22:31Z",
  "publisher_id": "did:web:example.com",
  "sha256_digest": "6c1b8a4f...",
  "signature_algorithm": "ed25519",
  "signature": "base64-of-ed25519-signature-over-canonical-json-of-fields-above",
  "signing_key_fingerprint": "a5b7c9d1e2f34567",
  "disclaimer": "Proves a file existed unchanged at a point in time. Does NOT verify identity or replace a notary public.",
  "onledger_anchor_tx": "<pending-or-tx-hash>",
  "onledger_anchor_ledger": null
}
```

The `onledger_anchor_tx` is `"pending"` until the weekly anchor
window fires; the receipt is re-published with the tx hash once
anchored. The verify library treats `pending` as "signature valid,
not-yet-anchored" — an honest partial state, never a lie.

**Canonical JSON for signing:** sort keys, no whitespace,
UTF-8-encoded. Fields signed: everything except `signature`,
`onledger_anchor_tx`, `onledger_anchor_ledger` (those are populated
after the sig).

---

## §4 — Endpoints (dark today; return honest 503 with Retry-After)

Every endpoint below ships flagged-off behind `NOTARY_ENABLED=0`
(default). When flagged off, they return HTTP 503 with a body that
names the enable timeline and points at the spec.

### `POST /notary/anchor`

Anchor a digest.

Request:
```json
{
  "publisher_id": "did:web:example.com",
  "sha256_digest": "6c1b8a4f...",
  "utc_date": "2026-09-01"
}
```

Response (200 when enabled):
```json
{
  "receipt": { <receipt shape above> },
  "receipt_url": "https://xrpldashboard.com/notary/receipt/2026-09-01-a7f3c9d2",
  "chain_url": "https://xrpldashboard.com/notary/chain/did%3Aweb%3Aexample.com.json",
  "disclaimer": "Proves a file existed unchanged at a point in time. Does NOT verify identity or replace a notary public.",
  "anchor_pending_until": "2026-09-04T13:00:00Z"
}
```

Response (503 flagged-off):
```json
{
  "error": "notary_disabled",
  "reason": "public flag pending Indiana notary-statute counsel review",
  "spec_url": "https://xrpldashboard.com/notary/spec",
  "disclaimer": "Proves a file existed unchanged at a point in time. Does NOT verify identity or replace a notary public."
}
```

Header: `Retry-After: 604800` (a week — a soft signal, not a promise).

### `GET /notary/receipt/<id>`

Fetch a receipt by id. Returns the receipt JSON described in §3.

Under flag-off: 503 with same shape as anchor endpoint.

### `POST /notary/verify`

Verify a receipt server-side (convenience for callers who don't want
to run the verify lib themselves). The verify lib remains the
authoritative source; this endpoint is a courtesy.

Under flag-off: 503 with same shape.

### `GET /notary/chain/<publisher_id>.json`

Per-publisher chain of receipts (like `chain.json` for the signed-
snapshot chain, but scoped to one publisher). Under flag-off: 503.

### `GET /notary/spec`

Redirects to this document rendered as HTML. Under flag-off: still
serves the spec — the spec IS the public commitment even when the
service is dark.

---

## §5 — Verify library (`xrpl_notary_verify.py`, MIT)

Ships in-repo at the top-level as `xrpl_notary_verify.py`. Design
mirrors `verify_snapshot_signature` from the existing signed-snapshot
chain — small, dependency-light, drop-in.

Public API:

```python
def verify_receipt(
    receipt: dict,
    pubkey_pem: str,
    xrpl_endpoint: str = "https://s1.ripple.com:51234",
) -> dict:
    """Verify a notary receipt against the XRPL.

    Returns a dict:
        {"valid": True/False, "reasons": [...], "anchor": {...}, "disclaimer": ...}

    Never raises on invalid receipts — an invalid receipt is a valid
    return, not an exception. Raises only on infrastructure errors
    (network down, endpoint returns malformed JSON)."""
```

Checks performed:

1. Signature verifies against `pubkey_pem` over the canonical JSON of
   the signed fields (§3 canonicalization rule).
2. `signing_key_fingerprint` matches the first 8 hex chars of
   SHA-256(pubkey_pem).
3. If `onledger_anchor_tx` is not `"pending"`, the tx is fetched from
   `xrpl_endpoint`, verified `validated=True`, source account matches
   the registered Notary account, destination matches ops, memo
   parses under §2 memo format, and the memo's `<sha256_digest>`
   matches SHA-256(canonical_json_of_receipt_fields_without_anchor).
4. `disclaimer` field matches the day-one disclaimer verbatim
   (verifiers of unknown provenance can then use presence-of-
   disclaimer as an integrity signal).

**First fixture** — the genesis anchor from
`docs/ONLEDGER_ANCHOR_SPEC.md`:

- Tx hash: `01D0BB9D230955F43DB35703E2EB7F5DFA43CEB69CCBBF57FBC8F17407E50DF8`
- Ledger: `106140698`
- Publisher: `self` (xrpldashboard.com's own chain root, not a
  third-party file)

The genesis anchor uses the v1 memo shape without a publisher_id
field (it predates the notary generalization). The verify library
handles the shim: memos matching `xrpldashboard/anchor/v1|...` are
treated as `publisher_id="self"` for compatibility.

---

## §6 — Tier design (per SELLABLE_REQUIRES_SOVEREIGN_SOURCE)

**Free forever tier:**

- 1 anchor / week / DID (or publisher_id).
- All receipts are publicly fetchable regardless of tier.
- All chain.json files are publicly fetchable.
- Verify library is MIT — no per-verify cost, no gate.

**Paid tier (rails-dark today; flip mediated by x402 middleware):**

- Any anchor above 1/week/DID.
- Suggested price: ~1 RLUSD per anchor. Actual price locked by
  Charlie post-attorney-gate; the number is not committed in code.
- Non-eligible under Fence #8 until this service qualifies under
  SELLABLE_REQUIRES_SOVEREIGN_SOURCE. Anchoring itself is a first-
  order action we do; sovereignty class is `own_node` from day one.
  (The service produces its own data — a receipt we signed — not
  someone else's data resold.)
- x402 wrapper applies with:
  ```python
  @x402_maybe_require_payment(
      sovereignty_class=SOVEREIGNTY_OWN_NODE,
      price_drops=lambda req: _notary_price_for_request(req),
      scope_note_url="https://xrpldashboard.com/methodology#notary-paid-tier",
  )
  ```
- Sub-tier free-vs-paid decision happens INSIDE the route (rate-
  bucket check) before the middleware fires, so 1/week/DID stays free
  even in enforcement=on. The middleware only sees requests that
  already burned through the free quota.

**Never tier:**

- Any anchor that would violate the day-one disclaimer's spirit —
  e.g., a request that asks us to "certify" or "notarize" (words we
  do not use). Requests that mislabel their intent get a 400 with
  the disclaimer in the body.

---

## §7 — Wallet + account (dark-build design; funds pending)

**Notary account:** SEPARATE from the on-ledger anchor account
(`rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ`) and SEPARATE from ops
(`rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd`). Reason: role separation. The
anchor account attests to xrpldashboard's own snapshot chain
(publisher=`self`). The Notary account attests to third-party
publishers. If one is ever compromised, the other's history is not
polluted.

**Account state:** UNCREATED as of 2026-08-12. Address generation
deferred until the public-brand-name attorney gate clears — we don't
want to burn address bytes on an account whose owner label may
change.

**Funding:** ~12 XRP (same shape as anchor account) whenever the
address exists. MoonPay→ops→Notary hop, matching the pattern
established for anchor account.

**Prohibited operations** (mirror anchor spec §Prohibited): no trust
lines, no token issuance, no cross-currency, no non-ops destinations,
no off-format memos.

---

## §8 — Ship gates

Nothing below moves to production without ALL of these:

- [ ] Public brand name cleared by counsel (Indiana notary-statute
      question resolved; see notary-name legal catch memo).
- [ ] Notary account address generated, format-validated, and
      character-CONFIRMED by Charlie.
- [ ] Notary account funded (~12 XRP).
- [ ] First notary receipt signed with signing key and verified by
      the MIT verify lib.
- [ ] First notary anchor tx on XRPL, verified by the verify lib.
- [ ] `/notary/spec` route renders this doc.
- [ ] `NOTARY_ENABLED=1` flip.
- [ ] Attorney-cleared ToS for third-party publishers using the
      paid tier (per x402 rails-dark attorney line items).
- [ ] Day-one disclaimer appears in EVERY artifact per §Day-one
      disclaimer.

Rails-dark middleware (Track 2, this branch series) is a
pre-requisite — the paid tier flip is an env-var change, not code.

---

## §9 — Change log

- **2026-08-12** — Initial dark-build spec authored. Internal name
  only. Endpoint stubs + verify lib in same commit series. No public
  surface, no XRPL activity, no attorney gate touched. Slots into the
  build queue at `earliest 2026-09-01/15` per
  `memory/project_xrpl_build_possibilities_research_2026-08-11.md`.

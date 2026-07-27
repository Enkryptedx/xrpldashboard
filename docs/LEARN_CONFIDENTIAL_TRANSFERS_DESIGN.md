# /learn/confidential-transfers — design doc (PARKED)

**Status:** internal reference. Not built. Not scheduled.
**Ship trigger:** the `ConfidentialTransfer` amendment enters voting (moves from `Draft` to a state visible in `feature` RPC output). If the amendment moves straight to activation without a public voting window, the trigger fires the moment it appears in the `feature` list.
**Do not build before that trigger.** Same content-page market-demand discipline that parked `/learn/vaults`: research is clean, the ship-signal is external.

Primary source anchor for every claim on the eventual page:
https://xls.xrpl.org/xls/XLS-0096-confidential-mpt.html
Fetched fresh 2026-07-27. Re-fetch and re-verify on every copy edit — the spec is `Draft` and can move under us.

---

## 1. What the page IS

A plain-English explainer of what Confidential Transfers for MPTs
*is and is not*, aimed at a reader who has heard the phrase
"confidential transfers on XRPL" and does not know what changed and
what didn't.

The single most load-bearing correction the page delivers:
**this is not Monero-style privacy.** The issuer keeps a decryptable
mirror of every holder's confidential balance. Transfer amounts are
hidden from *the public* and from *other holders*, not from the
issuer. Any explainer that lets a reader walk away thinking "my
tokens are private" without the qualifier "from other holders, but
not from the issuer" has misrepresented the amendment.

## 2. What the page IS NOT

- **Not a tutorial.** No key-generation walk-through, no wallet
  screenshots, no "how to send your first confidential transfer."
  Same non-tutorial posture as `/learn/vaults`.
- **Not a privacy recommendation.** The page does not tell readers
  they should or should not use CT. It describes what CT is; the
  reader decides.
- **Not a threat model.** A serious threat-model treatment of
  issuer-collusion, view-key leakage, and coordinated de-anonymisation
  belongs somewhere else (e.g., a separate `/learn/xrpl-privacy`
  page that also covers Payment Channels, DID, Credentials). CT
  explainer stays focused on "what does this amendment change."
- **Not a comparative privacy scorecard.** Comparisons to Monero /
  Zcash / Aztec are unavoidable in the trust-model section (the
  correction above requires the contrast), but the page does not
  rank chains.

## 3. Load-bearing facts (all from XLS-0096 directly)

Every claim on the page must be verifiable against the spec URL
above. The set below is the minimum truth-set — everything else on
the page must be consistent with these.

**Amendment identity:**
- Amendment name: `ConfidentialTransfer` (singular, feature flag
  `featureConfidentialTransfer`).
- Status as of 2026-07-27: `Draft`. Not in voting. Not activated.
- Formal `requires` field: `XLS-33` only.

**What's public on-chain:**
- Sender and receiver r-addresses.
- Issuer identity.
- Transaction type and MPT issuance ID.
- Public ledger totals: `OutstandingAmount`,
  `ConfidentialOutstandingAmount`, `MaxAmount`.

**What's encrypted:**
- Transfer amount in `ConfidentialMPTSend`.
- Individual holder confidential balances.

**Issuer visibility (the load-bearing correction):**
- Every holder's confidential balance is stored *twice* on-ledger:
  once encrypted under the holder's key, and once encrypted under
  the issuer's key (`EncryptedBalanceIssuer`).
- The issuer can decrypt its own mirror to see every holder's
  confidential balance at any time. The spec calls this
  "supply consistency checks and issuer-level auditing."
- The view-key auditor model "requires the auditor to trust that
  the issuer is providing the correct and complete set of view keys
  for the scope of the audit." Trust in the issuer is the primary
  security assumption, not an edge case.

**Cryptographic primitives (name them, don't explain them):**
- **EC-ElGamal** for encryption (additive-homomorphic — supports
  supply-check math without decryption).
- **Bulletproofs** for range proofs (aggregated 754 bytes in `Send`,
  single 688 bytes in `ConvertBack`) — proves amounts are non-negative
  without revealing them.
- **Pedersen commitments** (33 bytes each) — bind sender to a
  claimed amount without revealing it.
- **Schnorr proofs** (64 bytes, used in `ConvertMPT` when
  registering a new holder key).
- **AND-composed compact sigma proofs** — bundle ciphertext-consistency
  / amount-linkage / balance-linkage into single fixed-size proofs.

Name-checking the primitives on the page gives readers a search
handle for external verification without the page attempting a
cryptography lesson. The page is not a cryptography course.

**DynamicMPT relationship (the recurring misconception this page kills):**
The spec's own verbatim note:
> "Note that `sfMutableFlags` is introduced in the amendment
> `DynamicMPT`. To use this field, the `DynamicMPT` amendment must
> be enabled."

DynamicMPT is required *only* for the optional `sfMutableFlags`
field (which stores `lsmfMPTCannotEnableCanHoldConfidentialBalance`
and related mutable flags). CT itself formally requires XLS-33 and
that alone. The page must state clearly: "CT can activate without
DynamicMPT; only the mutable-flag path needs DynamicMPT."

This is the correction that caught me on 2026-07-27 (see
`feedback_reviewer_correction_is_also_a_claim.md`-adjacent case).
It is the single most likely thing for a reader — or a reviewer — to
get wrong from second-hand sources. State it once, plainly, near the
top.

## 4. Section shape (draft outline)

1. **Lede** — one paragraph. "Amendment status: Draft. What it does:
   hides transfer amounts from the public. What it does not do: hide
   your balance from the issuer."
2. **The single most important correction** — the issuer-visibility
   note in one paragraph, quoted spec passage inline.
3. **What changes on-chain when a CT tx lands** — three-column table
   (public / encrypted / issuer-visible).
4. **What the primitives are** — name and one-line purpose. Not a
   lecture.
5. **DynamicMPT: what it actually requires** — the spec's own note
   quoted, plus the "CT can activate without DynamicMPT" call-out.
6. **Trust model** — view-key auditor model, one paragraph. Cross-link
   to `/verify` for the general attestation-vs-endorsement framing.
7. **What this amendment does not do** — not zero-knowledge from
   the issuer, not anonymity (r-addresses stay public), not private
   messaging, not a replacement for DIDs or Credentials.
8. **Freshness chip** — `LAST_VERIFIED_CT` constant. 30-day
   staleness threshold (same as `/learn/vaults` — the spec landscape
   moves slower than legislative status). During the pre-activation
   Draft window, drop to 7-day (spec text can change).

## 5. Truth-audit surface (CLAIMS.yaml block, when built)

Claims to catalogue at build time. Sketching now so the future me
doesn't have to re-derive them.

- `ct_page_purpose`: non-advisory, non-tutorial, describes what CT
  IS. Same risk_note as vault-page purpose claim.
- `ct_amendment_status_and_requires`: `ConfidentialTransfer` amendment,
  `Draft` status, `requires: XLS-33`. Recheck cadence: 7 days during
  Draft window, 30 days once activated.
- `ct_issuer_visibility_disclosure`: the load-bearing correction is
  present, prominent, and verbatim-cites the spec. If the page ever
  ships without this claim visibly on-page, the page is misleading
  and must be pulled.
- `ct_public_vs_encrypted_table`: three-column matches spec.
- `ct_primitives_named`: EC-ElGamal, Bulletproofs, Pedersen,
  Schnorr, AND-composed sigma. Names only; page does not attempt
  primitive explanations.
- `ct_dynamic_mpt_relationship`: CT does not require DynamicMPT
  formally; only `sfMutableFlags` optional field does. Verbatim spec
  note included on page.
- `ct_trust_model_view_key`: view-key auditor model requires trusting
  the issuer's key-set completeness.
- `ct_freshness_chip`: `LAST_VERIFIED_CT` constant + staleness
  threshold. 7-day during Draft window, 30-day post-activation.
  Constant must move on every copy edit.

## 6. Non-goals (deliberately not on the page)

- Cryptography tutorial. Bulletproofs work how they work; the page
  names them, does not derive them.
- Comparative anonymity ranking against Zcash / Monero / Aztec.
  The trust-model section makes the *contrast* to justify the
  issuer-visibility correction; that's the ceiling.
- Adoption speculation. Amendment is `Draft`; no one has committed
  to using it. The page describes the primitive, not the ecosystem.
- Regulatory framing. CT + KYC + travel rule + FinCEN is a whole
  separate content surface; keeping it out of this page keeps
  scope defensible.
- Any wallet-side "how to enable" language. This page does not
  produce enablement clicks.

## 7. Ship criterion (repeat)

Amendment moves from `Draft` to visible-in-`feature`-RPC (i.e.,
enters voting or activates). At that moment:

1. Re-fetch XLS-0096. Re-verify every load-bearing fact above
   against the spec text. Diff against this doc; any drift is
   a build-blocker until reconciled.
2. Write the page against the load-bearing facts.
3. Catalogue the 8 claims in CLAIMS.yaml with data_paths,
   risk_notes, primary_sources.
4. Add `LAST_VERIFIED_CT` constant in `app.py` (same discipline as
   `LAST_VERIFIED_REGULATION` / `LAST_VERIFIED_VAULTS`).
5. Run `scripts/claims_check.sh` — should surface the new page's
   claims.
6. Ship in a single commit like `5b3e987` (regulation page).

Before the trigger fires: park. Reviewer polish, additional
research, or new spec-reading passes do NOT unpark. Only the
amendment-status trigger unparks.

## 8. Adjacent surfaces this doc does NOT design

- `/amendments` already renders in-flight amendments. When CT
  enters voting, it appears there automatically via the standard
  `feature` RPC surface — no doc change needed on `/amendments`.
- `/learn/xrpl-privacy` (hypothetical) — a broader XRPL-privacy
  survey (Credentials, DID, Payment Channels, CT) is a separate
  content-page proposal that would need its own market-demand
  check. Not covered here.
- `/methodology` — if CT ships and xrpldashboard ever renders
  CT-related data (e.g., `ConfidentialOutstandingAmount`), the
  methodology cadence table gets a new row. Doc handles at that
  point, not now.

# On-Ledger Anchor — Design Spec

**Status:** SPEC — v1 LOCKED (2026-08-06 after correction-anchor pre-lock amendment; 2026-08-07 pre-first-anchor amendments #2 and #3: tx shape changed from Payment-to-self → Payment-to-ops after Xaman blocked self-Payment at UI layer; memo shape flattened to single-field MemoData because Xaman UI supports one memo string only). **Stage 2 COMPLETE 2026-08-07 21:49:32 UTC** — first anchor tx `01D0BB9D230955F43DB35703E2EB7F5DFA43CEB69CCBBF57FBC8F17407E50DF8` at ledger `106140698`, tesSUCCESS, on-chain-verified via s1.ripple.com. Spec is fully locked at this line; any future memo-shape change becomes v2.

**Anchor account address:** `rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ` — CONFIRMED by Charlie 2026-08-06 (Xaman-generated, seed on paper per discipline, unactivated pending funding — see §Funding status)

**Purpose:** Anchor each daily signed snapshot's `chain_root` hash on the XRPL itself, so the audit trail can be verified independently of `xrpldashboard.com`.

**Decision record:** `memory/project_onledger_anchor_decision_2026-08-06.md`

---

## Design principles (from cross-exam verdict)

1. **Moat-not-feature.** This deepens the existing signed-snapshot chain — it does not add a new product surface.
2. **Load-bearing at 70/30.** Institutional buyers verify that verification is *possible*. Individual hash-fetching is rare. The mechanism must exist and be architecturally sound.
3. **Manual-first, automate-on-signal.** Manual anchor keeps the calendar clock running. Walker + verify tool are HELD pending named triggers (see decision record §Stage 4).

---

## Anchor account

### Designated address

**`rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ`** — labeled "XRPLD Anchor" in Xaman. Format-validated 2026-08-06 via `xrpl-py.core.addresscodec.is_valid_classic_address` (True). Character-for-character CONFIRMED by Charlie 2026-08-06 (msg 10992). Unactivated on-ledger; expected until funding lands (see §Funding status).

**Cashier/notary separation:** the anchor account is separate from Charlie's "XRPLD Ops" account (`rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd` — format-validated via `xrpl-py.core.addresscodec.is_valid_classic_address = True`; character-for-character CONFIRMED by Charlie 2026-08-07 msg 11129). Ops holds the RLUSD trust line and receives MoonPay funding; ops then forwards XRP to the anchor account. Anchor account never holds a trust line, never receives directly from a fiat on-ramp (see §Deviation history for the one exception), never touches any other value flow. This is the cashier (ops) / notary (anchor) split — different roles, different keys, different access surfaces.

### Role

Single-purpose XRPL account. Its ONLY function is submitting `Payment` transactions to the designated ops address (`rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd`) carrying `chain_root` hashes in the Memo field (Type A) or correction records (Type B).

### Prohibited operations

- **No trust lines.** Ever.
- **No token issuance.** Ever.
- **No cross-currency payments.** Ever.
- **No payments to any destination other than the designated ops address** (`rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd`). Every anchor Payment carries a v1 memo.
- **No off-anchor Memo content.** Every Memo follows the format below or the anchor is invalid.

**Rationale:** any anomalous transaction from this account is treated as a compromise signal. Simplicity of the account's on-chain history IS the verification surface.

### Funding target

- **Reserve:** 10 XRP (current base reserve as of 2026-08 — verify against `server_state.validated_ledger.reserve_base_xrp` before funding)
- **Fees buffer:** 2 XRP (years of daily anchors at ~10 drops/tx)
- **Total funding:** ~12 XRP to the anchor address

### Funding status (2026-08-07 — FUNDED)

**Anchor account funded 2026-08-07.** Bank whitelisted MoonPay after Charlie's call; purchase cleared and 49.000008 XRP landed directly in the anchor account (`rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ`). Account activated on-ledger, 1 XRP reserved per current base reserve.

Original intended flow (still applies to future top-ups):

1. MoonPay → ops wallet (fiat on-ramp)
2. Ops → XRP hop → anchor account — activates / tops up
3. Ops → RLUSD trust line (on ops wallet only, NEVER on anchor)
4. First anchor lands same day funding does

Actual flow deviated from step 1-2 on the initial funding — see §Deviation history.

### Deviation history

- **2026-08-07 — MoonPay purchase landed directly in anchor account** (intended target: ops). Ruled *live-with-it*: verification logic checks outbound anchor memos + sequence continuity, not inbound funding history; one funding tx breaks nothing. Future top-ups route ops→anchor per original design.
- **2026-08-07 — Bootstrap-hop anchor→ops** (tx `E94ADB8CF438EB94DCC00725572CBCC03ACC3084F12DE706AEB4D418B6A7438B`, 25 XRP, 2026-08-07 21:18 UTC). Consequence of deviation #1: to activate ops without a second MoonPay round-trip, anchor sent 25 XRP outbound to ops. Under the original "Payment-to-self only" rule this was a one-time exception. Under the amended rule (see §Pre-lock amendment 2026-08-07 below), it's the first tx in the anchor→ops pattern — it just doesn't carry a v1 memo. Verification tools: this tx hash is allowlisted as "bootstrap, no memo"; all subsequent anchor→ops txs MUST carry a v1 memo.

### First anchor — Stage 2 COMPLETE 2026-08-07

- **Tx hash:** `01D0BB9D230955F43DB35703E2EB7F5DFA43CEB69CCBBF57FBC8F17407E50DF8`
- **Ledger index:** `106140698` (validated)
- **Close time:** 2026-08-07 21:49:32 UTC (17:49:32 EDT)
- **Account (source):** `rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ` (anchor)
- **Destination:** `rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd` (ops)
- **Amount:** 1000000 drops (1 XRP)
- **Fee:** 12 drops
- **Sequence:** 106138931
- **TransactionResult:** `tesSUCCESS`
- **MemoData (decoded):** `xrpldashboard/anchor/v1|2026-08-07|c73d65ae5927243b86ee9ddbfd02b967451dc75a6b4678a5a05dadc9dbfdf86a\n\n\n\n\n\n`
- **Verification:** fetched via `https://s1.ripple.com:51234/` (Clio node) 2026-08-07 21:50 UTC, all fields match expected, `validated=true`
- **chain_root anchored:** `c73d65ae5927243b86ee9ddbfd02b967451dc75a6b4678a5a05dadc9dbfdf86a` (from `https://xrpldashboard.com/.well-known/snapshots/2026-08-07.json`)

**Xaman UI artifacts observed (permanent for the Xaman-standard-send path):**
- Xaman auto-populates `MemoType = "Description"` and `MemoFormat = "text/plain"` when using the standard send flow's public-memo field. These are not part of our v1 memo contract but they are harmless — verify tools identify anchor txs by the leading `xrpldashboard/anchor/v1` token in MemoData (per amendment #3), not by MemoType.
- Xaman appends six trailing newlines (`\n\n\n\n\n\n`, hex `0A0A0A0A0A0A`) to the MemoData bytes. Verify tools MUST `.strip()` trailing whitespace on the hash field before comparing to a published snapshot's `chain_root`. This is a Xaman artifact, likely permanent, and does not corrupt the payload.

### Pre-lock amendment 2026-08-07 — transaction shape

**Original design:** anchor Payments went to anchor itself (Payment-to-self), amount 1 drop, memo attached. Rationale: minimal side-effect, no risk of moving XRP anywhere else.

**Blocker discovered while executing first anchor:** Xaman blocks Payment-to-self at its UI layer ("Source and Destination address cannot be the same!") as user-error protection. XRPL protocol allows it; Xaman does not.

**Amendment:** anchor Payments now go from anchor to ops (`rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd`), amount 1 drop or minimum wallet-allowed, memo attached. Every future anchor is anchor → ops with the v1 memo.

**Why acceptable:** amendment lands BEFORE the first anchor tx — no existing on-chain history to invalidate. Same discipline as the 2026-08-06 correction-memo pre-lock amendment. The verification properties the original design guaranteed are all preserved:

- **Account identity** — still enforced (source must be anchor account)
- **Sequence continuity** — still enforced (anchor's sequence number monotonic)
- **Memo shape** — unchanged, v1 LOCKED-FINAL
- **Date + chain_root correspondence** — unchanged
- **No trust lines / no other tokens / no third-party payments** — still enforced; ops is a *designated* destination, not an arbitrary one

Ops's inbound history from anchor becomes a natural receipt log for the anchor stream — a byproduct of the amendment that is mildly useful, not designed-for.

### Pre-lock amendment #3 2026-08-07 — memo shape flattened

**Blocker discovered while executing first anchor:** Xaman's send UI exposes only one memo field ("Enter public memo") — no separate MemoType / MemoFormat inputs. The XRPL protocol supports all three, but the Xaman standard-send flow packs whatever text the user types into MemoData only, leaving MemoType and MemoFormat unset.

**Amendment:** the type namespace previously carried in MemoType (`xrpldashboard/anchor/v1` or `xrpldashboard/anchor/correction/v1`) moves into MemoData as the first pipe-delimited field. MemoType and MemoFormat are not set. Verify tools identify the type by the leading token in MemoData.

**Why acceptable:** amendment lands BEFORE the first anchor — no on-chain history to invalidate. Cost is minor: same information, one container instead of two. Bonus: block explorers that only display MemoData (many do) now show the type namespace without needing to inspect MemoType separately.

**Alternatives considered and rejected:** xApp-based structured signing (unknown ecosystem, exploration cost, no known xApp that does this cleanly), external tx-crafting tools like xrpl.services (adds external website to custody chain per anchor).

### Key custody

- **Generation:** offline / cold-adjacent. NOT generated on the web server, NOT on Render, NOT in a repo commit, NOT in a cloud-synced note.
- **Storage:** paper backup (seed phrase / secret key written by hand, stored physically) — matches Xaman discipline for the personal wallet.
- **Access surface:** signing happens on a machine that has never held any other production credential. For the manual weekly cadence, this is fine (local sign, submit via `wss://s1.ripple.com:443` or the Lenovo local rippled).
- **Backup redundancy:** at least two physical copies, geographically separated.

### Rotation path (if compromised)

1. Generate new anchor account, offline, per the discipline above.
2. Post a public note on `xrpldashboard.com` (likely on `/covenant` or the signed-snapshot docs page) declaring:
   - Compromise date
   - Old account address (marked HISTORICAL; anchors before compromise date remain valid)
   - New account address (marked CURRENT)
3. Old anchors remain valid *history* — the on-chain memos before the compromise date are still cryptographically bound to snapshots we published on those dates. What the compromised key can no longer do is create *new* trusted anchors.
4. Update the verify tool's account allowlist to include the new account for dates >= rotation date.

### Compromise model (stated plainly)

**A stolen key writes FAKE anchors — this is worse than no anchor at all if the verify path is naive.** A verify tool that only checks "hash exists on-chain" would be *positively misleading* under a stolen-key scenario, because an attacker could write our chain_root hashes into the on-chain history to forge continuity.

**Therefore the verify path MUST check:**

1. **Account identity.** Memo tx must originate from the current-anchor account (or a documented historical account for dates < rotation).
2. **Sequence continuity.** Anchor txs form a monotonic sequence per account; gaps or forks are surfaced as warnings, not silently accepted.
3. **Date-in-memo matches ledger close time** (with reasonable tolerance — anchor may be submitted hours after snapshot signing).
4. **Chain_root matches the published snapshot for that date.** The on-chain memo is only meaningful if it agrees with the published signed snapshot; disagreement = one of the two is compromised.

Mere "hash present on-chain" is a broken check. Any verify tool we ship must implement all four.

---

## Memo format (v1 LOCKED-FINAL — both types below are permanent contract)

Two MemoTypes ship in v1. Both were decided BEFORE the first anchor lands, because retrofitting a memo format after anchors exist is the one impossible edit. Any future shape becomes v2.

**Note (2026-08-07 amendment #3):** the memo shape below packs the type namespace into MemoData itself because Xaman's send UI supports only a single memo text field (no MemoType / MemoFormat inputs). See §Pre-lock amendment #3 2026-08-07 in the account section.

### Type A — Standard anchor

Every routine anchor transaction carries **exactly one Memo** with:

- **MemoData** (hex-encoded UTF-8, single field): `"xrpldashboard/anchor/v1|<snapshot_date>|<chain_root_hex>"`
- **MemoType / MemoFormat:** not set (Xaman UI doesn't expose them; verify tools rely on the leading namespace token in MemoData)

Where:

- `<snapshot_date>` = ISO 8601 date, UTC, form `YYYY-MM-DD`, referring to the snapshot date (not the anchor submission date, which may lag by hours)
- `<chain_root_hex>` = the snapshot's `chain_root` hash as lowercase hex, no `0x` prefix
- Separators are single ASCII pipes `|`

**Example MemoData (pre-hex):** `xrpldashboard/anchor/v1|2026-08-06|a3f2c1e8b4...9d`

### Type B — Correction anchor

Issued **ONLY when a published-and-anchored snapshot is later corrected** — the on-chain extension of the published-bug-history discipline. References the original anchor's tx hash explicitly so the on-ledger story reads *wrong → caught → corrected*, each step timestamped, none editable.

- **MemoData** (hex-encoded UTF-8, single field): `"xrpldashboard/anchor/correction/v1|<original-date>|<original-tx-hash>|<corrected-chain_root_hex>"`
- **MemoType / MemoFormat:** not set (same rationale as Type A)

Where:

- `<original-date>` = the `<snapshot_date>` from the original (Type A) anchor being corrected
- `<original-tx-hash>` = the transaction hash of the original anchor, uppercase hex (per XRPL convention)
- `<corrected-chain_root_hex>` = the new `chain_root` hash as lowercase hex, no `0x` prefix
- Separators are single ASCII pipes `|`

**Example MemoData (pre-hex):** `xrpldashboard/anchor/correction/v1|2026-08-06|A3F2C1E8B4...9D|b7e4d2f1c9...8a`

**Semantics:**
- A Type B anchor NEVER replaces or invalidates the original Type A anchor on-chain. Both remain in the ledger permanently.
- The correction is a *new fact* — "on date X, we published Y; on date X+N, we discovered Y was wrong and the correct value is Z." The chain preserves both statements.
- Manual at this stage, like all anchors. The eventual verify tool learns both types: Type A queries return the current best-known root (last non-superseded Type A or the corrected root from the most recent Type B referencing that date); disagreement between Type A and any downstream Type B is surfaced as a *documented correction*, not a warning.

### Verifier requirements (v1, normative)

Any tool verifying v1 anchors MUST:

1. **Strip trailing whitespace** from the decoded MemoData string before splitting on `|` or comparing any field. Xaman's standard-send flow appends six trailing newlines (`\n\n\n\n\n\n`) to every anchor's MemoData; other wallets or automation paths may append different whitespace. The rule is universal: `memo_data.decode('utf-8').rstrip()` before parsing, and `.strip()` on each pipe-delimited field before comparing.
2. **Identify anchor type by leading token in MemoData**, not by MemoType. MemoType is not part of the v1 contract; Xaman auto-populates it with `"Description"` and other wallets may leave it unset or populate it differently. Only the leading `xrpldashboard/anchor/v1` or `xrpldashboard/anchor/correction/v1` token in MemoData is authoritative.
3. **Enforce source-account identity** — tx must originate from the current-anchor account (or documented historical account for dates before rotation).
4. **Enforce destination-account identity** — tx must go to the designated ops address (`rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd`), except for the one allowlisted bootstrap-hop tx (see §Deviation history).
5. **Enforce sequence continuity** — anchor txs form a monotonic sequence per account; gaps or forks surface as warnings.
6. **Compare `<chain_root_hex>` field to published snapshot** for the same `<snapshot_date>`. Disagreement = one of the two is compromised.

Failure of any of items 3-6 = compromise signal. Item 1-2 failures = contract violation, not compromise, but still cause for anchor to be rejected.

### Versioning

**v1 LOCKED-FINAL** with both types above and the verifier requirements above. If we later need a different memo shape (e.g., anchor batching, root-of-roots, revocation), it becomes v2 — v1 anchors of either type remain valid for their date range indefinitely.

### Why this format

- **Human-scannable in xrpl.org / bithomp explorer** — a curious reader can see the date + hash without decoding tooling
- **Machine-parseable** — `split('|')` gives ordered fields
- **Namespace-tagged** — MemoType prevents collision with other Memo consumers and cleanly distinguishes standard vs correction
- **Fixed-shape** — well under XRPL Memo size limits, no truncation risk
- **Corrections are on-chain first-class** — the vocabulary for being wrong is permanent BEFORE the permanence starts, which is the order a truth-first site does it in

---

## Transaction shape

Each anchor is a `Payment` from the anchor account (`rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ`) to the designated ops account (`rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd`), amount = 1 drop (or minimum wallet-allowed), Fee = network standard, with one Memo per the format above.

**Why anchor→ops (not Payment-to-self):** original design specified Payment-to-self for minimal side-effect. During execution of first anchor 2026-08-07, Xaman was found to block Payment-to-self at its UI layer. Pre-first-anchor amendment moved the destination to ops. See §Pre-lock amendment 2026-08-07 in the account section for full rationale.

**Alternatives considered and rejected:**
- **`AccountSet` with Memo** — rejected because AccountSet has parameters that could accidentally modify account state.
- **Payment-to-self via external tx-crafting tools** (e.g., xrpl.services) — rejected because it adds an external website into the chain of custody for every anchor.
- **Importing anchor seed into a different wallet** — rejected because it violates the Xaman-only custody discipline.

---

## Cadence

- **Stage 2 (this week):** first manual anchor — one-off, done by hand
- **Stage 3 (weeks 1–4 after first anchor):** manual weekly, either Charlie or the assistant staging the tx for Charlie to sign
- **Stage 4 (on named trigger):** `snapshot_anchor_walker` runs daily, submitting one anchor per new signed snapshot. Named triggers: RippleX conversation scheduled, institutional 2nd round, or 30d of manual proving annoying enough to justify build.

---

## Publication requirements

Once the first anchor lands:

- Update the signed-snapshot docs page with: *"Chain additionally anchored on the XRPL itself since YYYY-MM-DD. First anchor: tx `<txhash>` at ledger index `<idx>`. Anchor account address: `rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ`. Correction anchors (MemoType `xrpldashboard/anchor/correction/v1`) are issued whenever a prior anchor is superseded — the on-chain audit trail preserves both statements. See /methodology or /audits for verification instructions."*
- Update `/covenant` (when it ships) to include the anchor account address + both memo formats as numbered public commitments
- Update `_LLMS_TXT` and `_AGENTS_JSON` to expose the anchor account + both memo formats machine-readably

---

## Explicitly NOT in this spec

- The `snapshot_anchor_walker` implementation (HELD)
- The `verify_snapshot_on_ledger` MCP tool implementation (HELD)
- The `/anchor` public page (deferred until /covenant + /audits ship)
- Multi-signature custody (single-key is acceptable for a $5-value account; revisit only if we ever hold meaningful XRP here)
- Any tokenomics, oracle publishing, or on-chain governance role (permanently rejected — see decision record §"Explicitly rejected")

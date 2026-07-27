# /learn/vaults — design notes

**Page:** `templates/learn_vaults.html`
**Route:** `app.py:learn_vaults()` at `/learn/vaults`
**Catalogued:** `CLAIMS.yaml` block `/learn/vaults` (8 claims)
**Freshness constant:** `LAST_VERIFIED_VAULTS` (30-day staleness)
**Drafted:** 2026-07-26, Charlie's GO after Sunday-evening research arc.

---

## The page's spine (Charlie's phrasing, verbatim into the design)

> XRPL has all the vault ingredients live on mainnet today — escrow, multisign, key rotation — but no consumer wallet wires them together. Real protection exists; it takes real manual setup. The gap is the story, and this page owns the explanation layer nobody else will write. Bitcoin needs protocol changes still unshipped after years; XRPL's gap is purely assembly and interface. That contrast is the page's quiet authority move.

## Why the page stops where it stops

The page **describes** vault-like protections; it **does not teach** anyone to build one. This is deliberate and load-bearing:

1. **Bad setup is worse than no setup.** Disabling the master key with an invalid signer list leaves the account permanently unrecoverable. A half-taught reader following an incomplete tutorial can lose everything. A page that half-teaches is a page that hurts.
2. **The "four-zeros" reasoning.** A setup tutorial narrows the population it serves; the miss rate on a public tutorial for a high-stakes irreversible action is exactly the population that most needs professional help. Non-tutorial framing routes those readers to official docs + someone experienced, which is the right outcome for them.
3. **Non-advisory is a house rule.** Same discipline as `/check` and `/regulation`. If any future edit drifts toward "you should," the CLAIMS.yaml `vaults_page_purpose` entry is violated and the diff must be re-scoped before push.

The page names apps (Xaman, Bithomp Tools, Liana, Zengo, Ledger) as **examples of what things are**. Naming is not endorsement. Same rule that lets `/check` name Chainabuse/OFAC without implying it's a service recommendation.

## Why the scam-inversion warning is FIRST

"Vault" is a live scammer keyword (six named patterns in the warning box, each primary-sourced). A page titled "vaults" published without a scam-inversion warning up top would be net-negative — it would boost the SEO of a keyword adversaries are actively exploiting. The warning is the reason the page is publishable at all.

Rule of thumb from the box: *if the "vault" requires you to reveal a secret to activate it, it isn't a vault. It's a drain.* That line is the single most useful sentence on the page for a first-time reader.

## Corrections vs Charlie's initial brief (all folded in)

| Charlie's brief | Corrected in the draft |
|---|---|
| BIP-345 = the OP_VAULT proposal | Named as "no longer pursued as standalone, effectively superseded by BIP-443." Reader flagged if they encounter a page still calling it the current proposal. |
| Unchained example: 3-of-5 → 2-of-5 decaying multisig | Corrected to actual 3-of-3 → 2-of-3 per the real Unchained post (unchained.com/blog/examining-the-tradeoffs-of-miniscript-timelock-wallets, updated 2026-04-17). |
| Zengo Vault as consumer self-custody option | Called out as MPC-based, not native-Bitcoin self-custody. Durability caveat named. |
| DepositAuth grouped with the other three as protection | Explicitly reclassified as spam/compliance filter with **zero theft protection**. Included in the feature table so readers can see WHY it does not belong in the vault list. |
| Escrow-to-self ≈ vault | Escrow reframed as "time-locked delivery" not "key-theft-resistant vault." EscrowFinish is permissionless — this is the load-bearing honesty. |

## Additions beyond the initial brief

Four items Charlie's brief did not include, folded in during draft:

1. **Compromised-issuer angle** (issued tokens on XRPL — Freeze/Clawback on trustlines). Footnoted in section 7.
2. **Mempool-alarm architecture gap** — Bitcoin's vault design assumes mempool-visible unauthorized withdrawals; XRPL doesn't have a mempool in the same sense. This is why the Bitcoin vault design does not port cleanly, independent of tooling. Section 7.
3. **Ledger-in-Xaman as honest baseline** — the "cold master + everyday signing" combination most XRP holders can actually reach today. Section 5.
4. **Covenant scoping** — BIP-119 (CTV) and BIP-118 (ANYPREVOUT) named and explicitly scoped OUT of the page, per Charlie's "name-and-decline beats silence" note.

## The elevation-rule call (from Sunday triage)

Charlie's standing rule: if the scam-sweep found an active pattern targeting XRPL users specifically, elevate immediately. **The sweep found zero XRPL-network-specific vault-lure patterns.** Closest is the Ledger physical-mail scam, chain-agnostic but reaches XRP holders on Ledger devices — named on the page as "reaches XRP holders in practice" but did NOT trip the elevate rule. This is documented here so future-Charlie can trace why the page shipped as content rather than as an incident response.

If a new XRPL-network-specific vault-lure pattern is observed post-ship, the `vaults_scam_inversion_warning` CLAIMS entry names the elevation obligation.

## Freshness discipline

- Threshold: 30 days (vs 7 for `/regulation`). Vault landscape moves slower than legislative status.
- Any copy edit MUST bump `LAST_VERIFIED_VAULTS` in `app.py`.
- Any diff touching `templates/learn_vaults.html` should re-verify:
  - Xaman UI status (Xaman-App issue #452 open? nixer.escrow help page still under construction?)
  - XLS-65 SingleAssetVault activation status
  - BIP-345 / covenant-family status
  - The six scam-pattern source links (any 404s or retractions)

## Review gate

**Not shipped.** Charlie's gate before publish, same discipline as `/regulation`'s three-audit arc. Deliverable is the draft artifact for his read. Ship after his edits + approval + a claims_check.sh pass.

## Queue position at draft time

- Behind: `bridge_signer_walker` diagnosis, `nft_activity_backfill` diagnosis, amendment-watcher build.
- Rationale: those are production reliability; this is content. Research does not preempt fire.

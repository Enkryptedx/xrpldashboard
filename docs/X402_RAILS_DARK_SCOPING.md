# xrpldashboard — x402 Rails-Dark Scoping Memo

**Date:** 2026-08-09 (Sunday afternoon, prompted by Charlie's ruling — see message log)
**Status:** SCOPING MEMO — six-section research package, NOT a build order. Charlie reads, approves the queue slot, memo becomes the build spec when Wave 1 clears and pg_backup pin ships.
**Author:** Claude (per Charlie's scoping order)

---

## The ruling (verbatim summary of Charlie's order)

The October decision date conflated two questions:

1. **Deciding to charge** — waits for evidence (demand telemetry, attorney answers, market-shape signals). October remains.
2. **Building the rails** — gains nothing from waiting. Sites that win the machine-payment wave are the ones already wired when demand arrives.

**Move:** x402 build shifts from October to the near-term list, structured as **build-dark**. Rails installed, price = zero, paywall OFF. Zero money touched until attorneys + entity are sorted. Retail free tier untouched forever — x402 is machine/bulk endpoints only.

Standing priorities preserved:
- pg_backup pin fix (top, next fresh session)
- Wave 1 walker #1 retry (Lenovo sudo password in hand)
- Remaining Wave 1 walkers
- Monday: L2 first weekly heartbeat 09-10 ET, Taft/attorney calls, crawler read
- Thursday: anchor #2 + canary

x402 rails-dark slots **after Wave 1 completes, before L2-v2**.

---

## Verifying Charlie's stated facts (before the memo builds on them)

Per verifiability doctrine, six facts cross-checked before load-bearing on them:

| Charlie stated | Verified | Source |
|---|---|---|
| x402 moved to Linux Foundation | ✅ operational launch 2026-07-14 | Linux Foundation press release |
| ~40 members | ✅ 40-member coalition | Multiple 2026-07 press |
| Incl. Visa, Mastercard, Amex, Stripe, Google, AWS, Cloudflare, Circle, Solana Foundation | ✅ all confirmed + **Coinbase, Ripple, Adyen, Fiserv, Shopify, Stellar Dev Foundation, MoonPay, Monad Foundation** | Linux Foundation launch page |
| 100M+ transactions on Base | ✅ ~165M by mid-2026 per Coinbase | Coinbase cite in awesome-x402 |
| ~$600M annualized | ⚠️ **stated as $50M last-30-days ≈ $600M annualized in earlier paid-tier memo (2026-08-03); recent third-party reporting cites "75M payments moving $24M in a month" (~$288M annualized). Both point at $200-600M range; state as "hundreds of millions annualized" not a precise figure** | Multiple |
| Cloudflare ships a monetization gateway | ✅ [developers.cloudflare.com/agents/tools/payments/x402/](https://developers.cloudflare.com/agents/tools/payments/x402/) | Direct fetch |

**One material update Charlie's ruling didn't have** (per verifiability, disclose immediately):

**XRPL has a native x402 facilitator, live, since June 2026.** t54.ai operates `xrpl-facilitator-mainnet.t54.ai` and the PyPI package `x402-xrpl` provides Python `require_payment` middleware. Ripple shipped the "XRPL AI Starter Kit" 2026-06-10 with XRP + RLUSD support. As of 2026-07-22, XRPL has recorded **1.4M+ autonomous x402 transactions**; XRP settlement volume via x402 is **up 289%** since the Starter Kit launched.

**This dissolves Charlie's stated "moat on XRPL, rails on Base" irony.** We can settle x402 in RLUSD on our existing ops wallet (`rwrcJL…TXfd`, RLUSD trust line set 2026-08-07). Same chain as our anchor. Same chain as our signed snapshot chain. The whole stack stays single-chain.

Sources for this footnote:
- [XRPL x402 Facilitator | Presigned Payments](https://xrpl-x402.t54.ai/)
- [Agentic Payments with X402 on the XRP Ledger (xrpl.org)](https://xrpl.org/es-es/docs/agents/agentic-payments-x402)
- [x402-xrpl (PyPI)](https://pypi.org/project/x402-xrpl/)
- [Ripple joins card giants backing x402 (CoinDesk, 2026-07-15)](https://www.coindesk.com/tech/2026/07/15/visa-mastercard-and-ripple-join-the-standard-letting-ai-agents-pay-in-stablecoins)

---

## § 1. Architecture on Flask / Render / Cloudflare

### The x402 flow (one page)

```
Agent                     Origin (us, Flask on Render)                Facilitator
  │                                 │                                       │
  │  GET /paid/endpoint             │                                       │
  ├────────────────────────────────>│                                       │
  │                                 │                                       │
  │  HTTP 402 Payment Required      │                                       │
  │  {network, asset, amount, pay_to, facilitator_url}                      │
  │<────────────────────────────────┤                                       │
  │                                 │                                       │
  │  [agent signs XRPL Payment tx offline via xrpl-py]                      │
  │                                 │                                       │
  │  GET /paid/endpoint             │                                       │
  │  X-PAYMENT: <presigned tx>      │                                       │
  ├────────────────────────────────>│                                       │
  │                                 │  verify + submit (presigned)          │
  │                                 ├──────────────────────────────────────>│
  │                                 │                                       │
  │                                 │  signed receipt (tx hash + memo)      │
  │                                 │<──────────────────────────────────────┤
  │                                 │                                       │
  │  200 OK + response data         │                                       │
  │  X-PAYMENT-RECEIPT: <hash>      │                                       │
  │<────────────────────────────────┤                                       │
```

**No custody, ever.** We never hold private keys. The facilitator submits the presigned tx to the XRPL and returns the receipt. The agent's wallet moves RLUSD directly into our ops wallet on-chain. This is the shape the sovereignty rule + four-zeros posture both require.

### Facilitator options

| Option | Chain(s) | Assets | Custody | Python/Flask? | Cost | Recommendation |
|---|---|---|:---:|:---:|---|---|
| **t54 XRPL facilitator** (`xrpl-x402.t54.ai`) | XRPL mainnet + testnet | XRP, **RLUSD**, USDC-on-XRPL, IOUs | None (verify + submit) | ✅ `x402-xrpl` on PyPI, `require_payment` middleware | No visible pricing on landing page; treat as "free public tier, verify at attorney gate" | ✅ **BEST FIT** |
| **Coinbase facilitator** (`x402.org/facilitator`) | Base, Ethereum, Polygon, Optimism, Arbitrum, Avalanche, Solana, Aptos, Stellar, Sui | USDC | None (verify + submit) | ✅ `x402` on PyPI | Free public facilitator | Fallback only |
| **Cloudflare Agents SDK** | Same as Coinbase (uses their facilitator) | USDC | None | ❌ Node-only (`x402-hono`, `@x402/fetch`) | Cloudflare Workers pricing | Not our path (wrong stack) |
| **Self-verify (build our own)** | XRPL (via our local rippled) | Any | None | Would need to write | Dev cost only | Overkill — t54 already does this |

**Verdict: `x402-xrpl` (t54's XRPL facilitator, Python middleware) is the right fit.** Three reasons:

1. **Same chain as our anchor + signed snapshots.** Single-chain trust stack is a story feature, not a bug.
2. **Python-native Flask middleware ready.** The `require_payment(path=..., price=..., pay_to_address=..., facilitator_url=..., network="xrpl:0", asset="RLUSD")` decorator is exactly the shape we want.
3. **Ops wallet already has the RLUSD trust line** (set 2026-08-07 by tx `8F4C2A…31FF`). No new wallet, no new chain, no new custody posture.

### Does Render / Cloudflare complicate anything?

**Render (our origin):** No. 402 responses are plain HTTP. Flask returns them, Render passes them through. The `require_payment` middleware is a `@app.before_request` hook or route decorator — identical infrastructure shape to our existing rate limiter.

**Cloudflare front:** No. Our CF front terminates TLS and proxies to Render. It doesn't strip custom status codes or headers. The one gotcha to watch:

- **CF may cache 402 responses.** Confirm cache-control on 402 responses is `no-store` (standard for payment-required flows) and add a Page Rule if CF's default TTL misbehaves. Test in build.
- **CF Tunnel for the MCP endpoint on Lenovo:** already lands on `mcp.xrpldashboard.com` per prior work. If x402-gated MCP tools ship, the Tunnel flow is identical — 402 status transits fine.

**No infra migration needed.** All x402 wiring lives inside the Flask app.

---

## § 2. Endpoint selection (free/paid boundary, for Charlie's sign-off)

### Free forever — non-negotiable trust surfaces

**Every one of these stays free by design. Charging for any of them would gut the "verifiable = commitment" thesis.**

| Surface | Why free forever |
|---|---|
| All human dashboard pages (`/`, `/whales`, `/tokens`, `/amms`, `/rlusd`, `/rwa`, `/methodology`, `/snapshots`, `/coverage`, `/health`, `/audits`*, `/incidents`*, `/corrections`*) | Retail promise, unchangeable |
| `/claims`, `/agents.json`, `/llms.txt`, `/.well-known/*` | Machine discovery layer. Trust surfaces MUST be free — charging for proof-of-work-in-progress collapses credibility. |
| MCP basic tools (all 20 agent-tier tools) | Free tier promised on Anthropic + Smithery directories; four-zeros posture depends on it |
| Signed snapshot verification (`verify_snapshot_signature`) | Stateless cryptography; no per-call cost |
| `/.well-known/snapshots/*.json` + `chain.json` + `pubkey.pem` | Signed snapshot chain — the moat itself |
| On-ledger anchor tx (viewable on XRPScan, Bithomp, our page) | Public commitment; not ours to gate |
| `CLAIMS.yaml` (raw file) | Our own claim manifest — must be publicly re-derivable |

*(surfaces marked with `*` are backlog from the trust-surface-widening conversation earlier tonight, not yet shipped, but pre-committed free)*

**Rule:** any surface where the value proposition is "here is our commitment / here is how you can catch us wrong" stays free. Metering these would undermine the audit trail.

### Paid candidates — endpoints wired for x402 (rails-dark until flip-ON)

**Sourced from `docs/PAID_MACHINE_TIER_DESIGN.md` § 1 candidates A-D + the trust-surface-widening `/datasets/` proposal from tonight's conversation.**

| Endpoint | What it serves | Sovereignty class (from paid-tier § 0) | Rails-ready today? | Notes |
|---|---|---|:---:|---|
| **`/api/history/signed`** (Candidate A) | Bulk signed historical time-series pulls with per-response Merkle signature | RLUSD-XRPL: SOVEREIGN ✅ · Escrow/oracle/cold-storage: SOVEREIGN ✅ · Whales/tokens/AMM: **PUBLIC-INFRA-DEPENDENT until Batch B completes** ⚠️ | Wire middleware now, gate content to sovereign series | This is the lead paid candidate |
| **`/api/webhooks/*`** (Candidate B) | Signed-envelope event push (whale threshold, R-alarms, amendment changes) | Same as above per event class | Wire when webhook infra exists | Behind Candidate A in queue |
| **`/api/verify/bulk`** (Candidate C) | Signature verification throughput above the free 100/day/IP threshold | SOVEREIGN by construction | Wire middleware now, keep free tier below N req/day/IP | Cleanest sovereignty-wise |
| **`/datasets/YYYY-MM-DD/*.parquet`** (from tonight's moat conversation) | Bulk daily dumps of every aggregation table backing a public number | Per-table (mixed) | Free daily-download tier + metered above threshold, per attorneys | Overlaps Candidate A |

**Sovereignty gating (from `docs/PAID_MACHINE_TIER_DESIGN.md` § 0, tightened 2026-08-10 to the named invariant SELLABLE_REQUIRES_SOVEREIGN_SOURCE):**

> "We only sell data we source ourselves and can prove ourselves."

Signature over PUBLIC-INFRA-DEPENDENT or THIRD-PARTY-DERIVED data = selling someone else's reliability under our name. Rails can be wired to any endpoint; **content that isn't SOVEREIGN cannot be metered even after flip-ON.** Batch B completion is a hard gate on most of the interesting series. Rails-dark shipping does NOT change this — it means the rails are installed but flipping ON is still gated on sovereignty per-series.

### Paid-candidate filter under SELLABLE_REQUIRES_SOVEREIGN_SOURCE (2026-08-10 amendment)

Applying the named invariant to Candidates A–D and the /datasets/ proposal:

| Endpoint / dataset | Underlying source (today) | Under invariant | Notes |
|---|---|---|---|
| Bulk signed history — sovereign series (RLUSD-XRPL, escrow, oracle, cold-storage) | own rippled forward-walk | **PAID-ELIGIBLE** at flip-ON | Untouched |
| Bulk signed history — whales/tokens/AMM live | own rippled forward-walk (post-Batch B) | **PAID-ELIGIBLE** after Batch B completes | Untouched |
| Bulk signed history — NFT live activity | Batch B Wave 2 migrates `nft_activity` activity-mode to own rippled (walker uses standard `Ledger` RPC, clean cutover — see Track 2 findings 2026-08-10) | **PAID-ELIGIBLE post-migration** | New under invariant |
| Bulk signed history — NFT historical backfill (2026-04-01 floor → head) | Public s2-clio.ripple.com (backfill mode requires full history our rippled doesn't hold; ~95.6% coverage, ~129K residual holes disclosed) | **FREE-ONLY UNTIL RE-DERIVED** from our own full-history node | Filtered off paid tier by invariant. Path to sellable: buy full-history node → re-import backfill under own-node sourcing → flip endpoint sovereignty. Not before. |
| Webhooks (whale threshold, R-alarms, amendment changes) | Own rippled forward-walk | **PAID-ELIGIBLE** at flip-ON | Untouched |
| `/api/verify/bulk` | Stateless cryptography over our published pubkey | **PAID-ELIGIBLE** by construction (verification IS our work product) | Untouched |
| `/datasets/YYYY-MM-DD/*.parquet` — per-table | Mixed. Sovereign tables paid-eligible; Clio-fed tables (NFT historical, any archival-only) free-only until re-derived | **PER-TABLE** filter | Sovereignty stamp per-table required in the dataset manifest |

**The invariant restricts what can be paid; it does not restrict what can be free.** Everything currently free stays free. The rule is additive.

**Re-derivation is a knob, not a fantasy.** For any endpoint currently disqualified (i.e., Clio-fed backfill), the fix is one purchase (full-history rippled node) + re-import — not a rewrite. Trigger: "when revenue justifies re-derivation." Charlie's "I will upgrade as needed" is on record.

### Rate-tier proposal (for Charlie's sign-off)

Free tier (unchanged, unmetered):
- All human pages: unlimited (subject to existing anti-scraper fleet-block)
- MCP tools: 600 calls / hour / session (existing)
- Anonymous API: 60 req/min/IP (existing)
- AI crawler API: 300 req/min/IP (existing UA-allowlisted)
- Bulk-verification: 100 req/day/IP (proposed free ceiling for `/api/verify/bulk`)
- Daily dataset dumps: 1 file / day / IP (proposed free ceiling for `/datasets/`)

Paid tier (dark today, per-endpoint pricing decision at October + attorney gate):
- Historical bulk pulls: metered per query result-row count or per-payload byte
- Webhooks: metered per event delivered
- Bulk-verify above 100/day/IP: metered per verification
- Dataset dumps above 1/day/IP: metered per file

**Every paid endpoint has an "honest 402" mode**: on hitting the free-tier ceiling, the response is a 402 with a scope note pointing at `/methodology#paid-tier`, so the friction is machine-readable and disclosed — never a silent 429.

---

## § 3. Wallet + chain question (findings, flagged for attorney)

**Charlie's stated framing:**
> "x402 settles predominantly in USDC on Base/Solana — NOT XRPL. Does receiving USDC on Base require a new wallet/custody posture?"

**Finding: the framing is out of date by ~2 months.** Since 2026-06-10, XRPL has native x402 support with XRP + RLUSD settlement via t54's facilitator. As of 2026-07-22, **1.4M+ XRPL x402 txs**; **XRP settlement +289%**. Ripple joined x402 Foundation as premier member in July 2026.

### Two paths, one recommendation

**Path A — XRPL-native settlement in RLUSD (RECOMMENDED):**
- Facilitator: `xrpl-x402.t54.ai` (t54)
- Chain: XRPL mainnet
- Asset: RLUSD
- Destination: existing ops wallet `rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd` (RLUSD trust line set 2026-08-07)
- Custody: **none** (presigned txs, agent → our wallet directly, we never hold keys we don't already hold)
- New wallet infra: **none** (ops wallet exists)
- New chain surface: **none** (single-chain trust stack)
- Tax reporting: RLUSD stablecoin receipts book like USD (attorney confirms treatment)

**Path B — Base-USDC settlement (FALLBACK):**
- Facilitator: Coinbase's public facilitator (`x402.org/facilitator`)
- Chain: Base
- Asset: USDC
- Destination: **new Base wallet required** (we don't have one)
- Custody: none (presigned), but new hot wallet on a new chain = new operational surface
- Tax reporting: USDC receipts + new-chain ledger for accounting
- Only justification: if attorneys land on an XRPL-side money-transmission or sovereignty concern that Base-USDC uniquely avoids

**Recommendation: Path A is the shortest, cleanest, most on-thesis path.** The moat is on XRPL. The signed snapshot chain is on XRPL. The on-ledger anchor is on XRPL. Making the machine-payment rail also XRPL turns "single-chain trust stack" into a story feature we can put on `/methodology`. Ripple is a premier x402 Foundation member — the XRPL-x402 story is not niche.

**The irony resolved:** Charlie's "moat on XRPL, rails elsewhere" tension only existed in the world where Ripple hadn't shipped the Starter Kit. That world ended 2026-06-10.

### Second XRPL-specific consideration (attorney-flag)

RLUSD is a Ripple-issued stablecoin. Receiving RLUSD for API services means we're accepting a Ripple-issued token as payment. Attorney question: does that create any counterparty-risk disclosure obligation, or any special treatment vs. a Circle-issued USDC (also on XRPL)? Likely no — both are non-custodial stablecoin receipts — but flag.

**Alternative asset on the same chain:** USDC-on-XRPL is also live via Circle's XRPL native issuance. If RLUSD counterparty concern surfaces, USDC-on-XRPL keeps everything else in Path A intact.

---

## § 4. Build-dark mechanics

### Enforcement modes (env-var driven)

```
X402_ENFORCEMENT=off          (default — middleware bypassed, endpoint free-tier)
X402_ENFORCEMENT=dry_run      (middleware active, testnet facilitator, price=1 drop, receipts logged for verification)
X402_ENFORCEMENT=on           (middleware active, mainnet facilitator, real price, real payments)
```

**In practice for build-dark:**
- `off` on production → endpoint serves the free surface as if x402 didn't exist. External behavior unchanged.
- `dry_run` on staging or a hidden endpoint → we can exercise the full presigned flow against testnet, capture receipts, prove the plumbing works, delete state.
- `on` never enabled in main branch until attorneys clear.

### Middleware wiring (illustrative — not code, not committed)

```python
# candidate route, illustrative
from x402_xrpl.server import require_payment
from config import X402_ENFORCEMENT, X402_FACILITATOR_URL, X402_PAY_TO, X402_NETWORK

@app.route("/api/history/signed", methods=["GET"])
@rate_limit("60 per minute")
@x402_maybe_require_payment(  # thin wrapper that no-ops when X402_ENFORCEMENT=off
    price_drops=lambda req: 0,           # zero until flip-ON — build-dark
    pay_to_address=X402_PAY_TO,          # ops wallet
    facilitator_url=X402_FACILITATOR_URL,
    network=X402_NETWORK,                # "xrpl:0" mainnet, "xrpl:1" testnet
    asset="RLUSD",
    source_tag=SOURCE_TAG_HISTORY_API,   # separate tag per endpoint for accounting
)
def api_history_signed():
    return _serve_signed_history_payload(request)
```

The `x402_maybe_require_payment` wrapper is the whole build-dark trick: when `X402_ENFORCEMENT=off`, it's a pass-through. When `dry_run`, it forces testnet-facilitator flow. When `on`, it enables mainnet. Env-var swap flips the whole surface.

### Testnet dry-run proof

1. Set `X402_ENFORCEMENT=dry_run`, `X402_FACILITATOR_URL=https://xrpl-facilitator-testnet.t54.ai`, `X402_NETWORK=xrpl:1`.
2. Fund a testnet wallet (agent side) with testnet XRP + RLUSD faucet.
3. Curl a candidate endpoint → 402 response with testnet payment requirements.
4. Sign a testnet Payment tx with `xrpl-py`, submit via the flow → get receipt.
5. Re-curl with `X-PAYMENT: <receipt>` → 200 + response body.
6. Verify: testnet ledger shows the tx; our ops-testnet wallet received the payment; receipt validates against pubkey.
7. Delete testnet state, commit only the code (env vars in `.env.example` only).

**Ship gate for rails-dark:** dry-run round-trip proof captured in `docs/x402_dark_ship_evidence/` (screenshots + tx hashes + response bodies). Same evidence-preservation discipline as every other ship. No flip-ON without this.

### Goal state

Flipping ON is: (1) update three env vars in Render dashboard, (2) attorney green-light in hand, (3) `/methodology#for-machine-payments` copy already published disclosing the tier. **Config change, not engineering sprint.**

---

## § 5. Effort estimate + queue slot

### Honest hours

| Task | Hours (solo) |
|---|---|
| Middleware wiring on 3-5 candidate endpoints (routes stay OFF) | 6-8 |
| Env-var enforcement toggle + `x402_maybe_require_payment` wrapper + tests | 3-4 |
| Testnet dry-run end-to-end capture + evidence commit | 6-8 |
| Ops-wallet source_tag / destination_tag design for per-endpoint accounting | 2-3 |
| `/methodology#for-machine-payments` section (disclosed, disabled, per verifiability doctrine) | 2-3 |
| Test suite (unit + integration) — enforcement-off pass-through, enforcement-dry_run 402 shape, enforcement-on rejection when no receipt | 4-6 |
| **Total** | **23-32 hours ≈ one focused sprint (3-4 days)** |

### Queue slot

**Charlie's standing priorities stay ahead:**

1. `pg_backup pin fix` — top priority next fresh session (from `project_pg_backup_pin_regression_2026-08-08.md`)
2. Wave 1 walker #1 retry — Lenovo sudo password now in hand
3. Remaining Wave 1 walkers (#2–#6, per `project_batch_b_wave_1_runbook_v2_2026-08-08.md`)
4. L2 first weekly heartbeat verification — Monday 2026-08-10 09-10 ET
5. Thursday anchor #2 — 2026-08-14

**Proposed slot for x402 rails-dark:**
- **Earliest: 2026-08-15** (Friday, after anchor #2 cadence proves)
- **Latest before L2-v2 build: 2026-08-22** (Sat, when L2-v2 auto-quarantine build becomes eligible per `feedback_auto_remediation_doctrine.md`)
- **Best fit: 2026-08-15 through 2026-08-19** as a focused 3-4 day sprint

**Charlie: approve, adjust, or reject the slot.** If Wave 1 slips (walker #1 retry hits credential issues again or unsafe-sinks fix delays), rails-dark shifts week-for-week. Do not preempt any standing priority.

**Non-preemption invariants:**
- Rails-dark work does NOT touch any human page.
- Rails-dark work does NOT change any existing free tier.
- Rails-dark work does NOT ship a paid product — only the plumbing for a future paid product.
- If Charlie says "hold" during Wave 1 for any reason, this slots later, no argument.

---

## § 6. Attorney brief — line items for Monday's Taft call

**Charlie: transpose these into whatever brief-format you use for the attorney call.** They ride alongside existing questions from `docs/AGENT_TIER_DESIGN.md` § Revisit triggers and `docs/PAID_MACHINE_TIER_DESIGN.md` § Hard gates.

### Line items (add to attorney brief verbatim)

**1. Entity for receiving crypto revenue**
- Should x402 receipts (RLUSD or XRP) book to a US LLC, sole prop with Schedule C, or wait-until-formed?
- Indiana LLC filing implications (SoS + registered agent + BOI report). Timing considerations vs. rails-dark ship date.

**2. Money-transmission classification — receipt of RLUSD for API access**
- Federal (FinCEN): is receiving RLUSD as payment for API services a "money transmission" activity? Prevailing read is no — payment for services, not custody or transmission — but confirm.
- Indiana state (money-transmitter statute IC 28-8-4): does state law match federal treatment?
- Distinction from custody: we never hold user keys, never accept funds for onward transmission. The x402 flow is agent → our wallet directly on-chain. Custody exposure = zero by construction.

**3. Tax treatment**
- Federal income treatment of RLUSD receipts: stablecoin-as-property (2014 IRS Notice) vs. stablecoin-as-cash-equivalent (2024 revenue procedure trend)? Impact on gain/loss recognition per transaction.
- Sales/use tax on digital services delivered via API: Indiana treatment of SaaS/API access. Interstate delivery (all customers are machine agents, likely global).
- Bookkeeping: per-endpoint source_tag segregation is designed for this — attorney to confirm the granularity attorneys/CPA want.

**4. Terms of Service for machine consumers**
- Refund policy: x402 payments are on-chain irreversible. What's the ToS shape for "no refunds, but here's the honest-partial receipt if content was degraded"?
- Dispute-resolution: who is a "machine consumer"? Can an LLC represented by an agent bring a dispute? Governing law + venue clause?
- Source_tag identification: machine consumer identifies itself by source_tag on payment; is this sufficient for ToS acceptance (deemed-consent per usage)?
- No-KYC posture at micro-payment scale: FinCEN thresholds for KYC-triggering activity; per-customer cumulative reporting obligations if any.

**5. Facilitator ToS flow-down (t54.ai)**
- We use t54's facilitator to verify + submit presigned payments. Do we need any pass-through language in our ToS acknowledging t54's role?
- Receipts and signed-payload preservation: what's our retention obligation for x402 receipts (evidence trail vs. privacy)?

**6. Data licensing (relevant to paid-tier Candidate E; adjacent to x402)**
- Signed historical dataset licensing to eval labs / model builders — is this classified differently from per-call API access?
- Same-chain settlement (RLUSD on XRPL) — does data licensing invoke any additional securities-law consideration (a distinct question from money-transmission)?

**7. Signed-snapshot verification tool as a public utility**
- Already free. Attorney: is a public cryptographic verification endpoint any different, legally, from publishing the public key itself (which we already do)? Believed: no. Confirming.

### Attorney gate — GO/NO-GO checklist

Flip-ON of x402 rails is gated on **all** of the following:

- [ ] Entity decision made (LLC formed, sole prop confirmed, or "wait" is the answer)
- [ ] Money-transmission classification: cleared as non-transmitter for our specific flow
- [ ] Tax treatment: known and books/CPA workflow designed
- [ ] ToS for machine consumers: drafted, reviewed, published
- [ ] Facilitator ToS flow-down: reviewed, any required language included
- [ ] Refund + dispute policy: written and disclosed on `/methodology#for-machine-payments`
- [ ] Sales/use tax: registered where required (or exemption confirmed)
- [ ] `LAST_VERIFIED_ATTORNEY_REVIEW` constant bumped in the code, visible on the machine-payments methodology section

**Rails-dark shipping does NOT require any of this.** Rails-dark is code-only, no money touched, no ToS obligation. The above list gates flip-ON, not shipping the middleware.

---

## Fences (load-bearing, restate on any future edit)

1. **Free tier untouched.** All human pages, all trust surfaces, all existing MCP tools stay free. No exceptions. If a future edit proposes gating any free-forever surface, reject.
2. **SELLABLE_REQUIRES_SOVEREIGN_SOURCE (named invariant, ratified 2026-08-10).** An endpoint may only enter the paid tier if the underlying data was read from **our own infrastructure** (own rippled / own full-history node / own Clio deployment). Any endpoint whose data is derived from a third-party pipe (Ripple's public Clio archive, s1/s2 public cluster, any external data provider) is **free forever, honestly labeled**, until it is re-derived from our own node. Sovereignty tier and revenue tier are **the same line**. `sovereignty=own_node` ⇒ eligible for paid; anything else ⇒ free with source disclosure. See `project_data_licensing_and_scraper_train_research_2026-08-10.md` for the legal footing (raw blockchain facts are unownable, but data pulled through someone else's pipe carries their terms; re-derivation from our own node gives clean chain of title).
3. **Sovereignty rule binds even after rails-dark ships.** Wiring middleware to a route doesn't authorize serving non-sovereign data behind that middleware. Per-endpoint sovereignty class must be SOVEREIGN before flip-ON.
4. **No custody, ever.** Every path in this memo relies on non-custodial facilitator flow. If a future edit proposes holding user keys or funds, reject and refer to four-zeros posture.
5. **`X402_ENFORCEMENT=off` is the default.** In every environment except the deliberate testnet dry-run staging. A committed change that flips the default to `on` is a bug, not a feature.
6. **Attorney gate is a hard gate, not a soft one.** Flip-ON without every attorney-gate checkbox ticked is unshippable.
7. **XRPL-native single-chain trust stack is the story.** Any future proposal to add Base-USDC as the primary rail (rather than fallback) needs to defeat the "the moat, the anchor, the signed chain, and the rail are all on XRPL" narrative first. Very high bar.
8. **Code-level enforcement of the sovereign-source invariant.** An endpoint's `paid_eligible` flag returns false unless its underlying source claims `own_node`. Belt-and-suspenders: a future dev cannot accidentally flip a Clio-fed endpoint to paid.

---

## Change log

- **2026-08-09**: Memo drafted per Charlie's scoping order (Sunday 17:44 EDT ruling). Six sections + attorney line items + fences. Awaiting Charlie's queue-slot approval.
- **2026-08-10 (evening)**: Amended per Charlie's tonight-build-order ruling. Added SELLABLE_REQUIRES_SOVEREIGN_SOURCE as named invariant (Fence #2, new Fence #8 code-level enforcement); filtered paid-candidate table (§ 2) under it — NFT historical backfill goes free-only until re-derived from our own full-history node, all other candidates unaffected; NFT live activity added to paid-eligible list post-Batch-B Wave 2 (walker code-read Track 2 confirmed clean migration — uses standard `Ledger` RPC, no Clio-specific path). Backing research + Cloudflare/AWS/AI-scraper-train context filed in `project_data_licensing_and_scraper_train_research_2026-08-10.md`.

---

**End of scoping memo. Not a build order — build slots into queue after Charlie approves.**

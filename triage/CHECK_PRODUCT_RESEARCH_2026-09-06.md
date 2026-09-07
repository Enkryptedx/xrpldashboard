# /check product research — build on the engine to actually stop scams

**Authored:** 2026-09-06 05:20 UTC · JJ · independent view formed from code + landscape before consulting yesterday's proposals
**Charlie's framing:** "An actual verifier in this space would be a big deal especially for scams. People are losing money everyday from clever but shady scammers. We have to be more clever than the scammers."
**LEGAL FENCE (carried through every proposal below):** the sanctions screen and any scam-related signal are RESEARCH LEADS surfaced with sources — never compliance conclusions, never "safe" or "unsafe" verdicts. Any wording a machine could parse as a guarantee needs attorney review before we let machines pay for it. Fence marker: 🚨 in the doc.

---

## Part 1 — What /check is today, in product terms

For each input kind, the human's question vs what /check actually answers:

### Address (r-address)

| the human is asking | /check answers | it does NOT answer |
|---|---|---|
| "Should I send money to this address?" | "Here are the identity claims on file: named-account label + two-way toml attestation (if any) + Domain field + ledger flags." | Whether the address is a fresh scam wallet, whether it's ever been reported for fraud, whether the pattern of funding/activity fits a scam typology |
| "Is this exchange's deposit address real?" | Yes/no if it's in `named_accounts.json` as a verified exchange | Whether the address you PASTED is the address the exchange actually gave YOU — homoglyph / clipboard-hijack detection isn't there |
| "Was this address flagged by anyone?" | OFAC SDN yes/no (US Treasury list, snapshot ≤ 24h old) | Non-OFAC scam databases: Chainabuse, ScamSniffer, xrpl.to scam-tracker, Xaman warnings, community-reported |

**Where /check stops one step short of "should I send money?":** it answers **identity confidence** (is this address who they claim to be?) but not **risk** (is this a scam pattern?). A scammer who registers a domain, puts an address in the toml, and passes two-way verification would render as `verified` in the current tier vocabulary. Legitimate — the address IS provably controlled by the domain — but they still steal. Identity is table-stakes; the risk story is missing.

### Token (currency + issuer)

| the human is asking | /check answers | it does NOT answer |
|---|---|---|
| "Is this token real?" | Whether the issuer has a first-party attestation for THIS specific token; if the issuer has a name but not for this token, the Bitstamp-trap override kicks in and explicitly says so | Whether the token's activity pattern matches "issuer creating N cent-value tokens per week to spam trust lines" |
| "Is USDT USDT?" | Yes — for the `rHjUEu…yMvK` case tested yesterday, /check correctly discloses the issuer is `usdxrp.net` (attested), not Tether. Ticker-collision handled by the source narrative | The user still has to READ the source URL to notice "usdxrp.net" ≠ "tether.to" — no red-flag chip that says "warning: ticker matches a well-known non-XRPL asset" |
| "Should I trust-line this?" | Issuer identity signals | Whether trust-lining opens the user to a freeze the issuer could exploit (Freeze capability IS surfaced, but the "so what?" for a user is left implicit) |

### URL / domain

| the human is asking | /check answers | it does NOT answer |
|---|---|---|
| "Is this the real Xaman site?" | Domain age (RDAP), TLS cert age (crt.sh), whether the site publishes a valid `xrp-ledger.toml` two-way-tied to any XRPL addresses | **Look-alike / homoglyph detection** — a domain that visually mimics a legit one isn't flagged; the user has to compare character-by-character |
| "Did this site get registered yesterday to phish me?" | Yes — RDAP age is surfaced | The recency isn't paired with a risk narrative ("registered 2 days ago + no toml + no long CT history = phish typology") |

### Message (pasted text)

| the human is asking | /check answers | it does NOT answer |
|---|---|---|
| "I got this message asking me to send XRP — is it a scam?" | Extracts r-addresses + URLs from the message and runs the address/URL check on each | Whether the message TEXT itself matches known scam scripts (seed-phrase harvest patterns, "verify your wallet" phishing, "send X get 2X" templates, deepfake CEO scripts) |
| "Someone sent me this on Telegram claiming to be Ripple support" | Runs address/URL check on any extracted entities | Impersonation-pattern detection — no lexical / message-template classifier |

**The gap in one line:** /check answers "who is this?" beautifully. Human paste-boxes ask "should I trust this?" — which needs an additional RISK dimension /check doesn't yet compute.

---

## Part 2 — The scam landscape (web research, cited)

External source citations below; all quotes/paraphrases marked. Recent = 2025-2026 window. Numbers are what public sources report; treat as reported, not audited.

### 2a. What's actually costing people money right now

**Reported losses and typologies (all from external sources — untrusted; cite when repeating):**

- **XRP giveaway / "send X get 2X" scams: reported $150M stolen 2020-2025** per Bitrue's 2026 write-up. Escalated in May 2026 — Ripple CTO David Schwartz issued a public scam alert on 2026-05-14 confirming a "sharp escalation" across Telegram/Instagram/YouTube. Source: `https://www.bitrue.com/blog/xrp-giveaway-scams-explained-why-investors-should-be-careful` (external, untrusted).
- **Xaman impersonation + fake desktop wallet campaign, May 2026** — Xaman founder Wietse Wind reported 20+ fake X accounts and 10+ lookalike domains pushing a fake "Xaman desktop wallet" download + fake XRP airdrop. Source: `https://safebrowz.com/blog/xrp-airdrop-xaman-wallet-drainer-scam-2026` (external, untrusted).
- **South Korea Flare Network staking site: $19M drained from 71 investors** (2025-10-16 through 2025-10-23), per Seoul Metropolitan Police Agency; fake staking site drained 3.4M XRP. Source: `https://www.sahmcapital.com/news/content/xrp-scam-in-south-korea-defrauds-investors-of-19-million-2026-07-30` (external, untrusted).
- **"Safe XRPL verify message" NFT phishing-drain campaign: 2,609,788 XRP-equivalent drained across 35,987 flagged scam NFTs from 1,831 scammer wallets** — targeting Xaman, FuzzyBear, PHNIX, RLUSD holders. Live tracker with **free API** at `https://xrpl.to/insights/xrpl-nft-scam-tracker`. Source: `https://xrpl.to/insights/xrpl-nft-scam-tracker` (external, untrusted). **THIS IS A DIRECTLY-INGESTABLE SIGNAL FEED FOR US.**
- **Wietse Wind Feb 2026 phishing briefing:** 6 specific phishing vectors identified. Source: `https://u.today/protect-your-xrp-6-new-phishing-tactics-identified-by-xrpl-contributor-wietse-wind` (external, untrusted).
- **XRP-ledger-verification-alert token scam:** fraudulent verification alerts drained 14,646 tokens per Crypto Economy report. Source: `https://crypto-economy.com/scammers-target-xrp-holders-with-fraudulent-xrp-ledger-verification-alerts/` (external, untrusted).

### 2b. Scam categories → signals that catch them BEFORE the send

| scam typology | on-chain / on-web signal that catches it PRE-SEND |
|---|---|
| **Fake airdrop "send X get 2X"** | Destination account age < 30 days · funding source = single upstream address · zero prior outbound history · Domain field absent or fresh · no two-way toml · community reports (Chainabuse / Xaman / xrpl.to tracker) |
| **Fake Ripple/Xaman/exchange support** | The URL flow: domain age (already have) + homoglyph distance to xaman.app / ripple.com / xrpscan.com (**don't have**) + TLS cert age (already have) + toml doesn't publish and never has |
| **Dusting + fake token drops** | Trust-line offer pattern: same issuer sending trivial TrustSet offers to many accounts within short window · issuer account age · issuer has never received XRP · issuer's other tokens (are they issuing a swarm?) |
| **Malicious trust-line offers** | Issuer's toml missing or issuer_verification=self · issuer created many tokens rapidly · token name mimics well-known ticker (USDT / USDC / RLUSD / XRP) with different issuer |
| **Phishing sites cloning XRPScan/Xaman** | Homoglyph distance to known-legit domain list · TLS SAN mismatch · CT log recency · absence from known-legit registered-domain list · toml presence + validity |
| **Seed-phrase harvesting** | Message classifier: any text asking for "seed" / "mnemonic" / "12 words" / "24 words" / "recovery phrase" / "private key" = 100% scam signal (no legitimate service asks). URL: /verify /connect-wallet /import paths on any non-legit-listed domain |
| **Pig-butchering with XRPL payouts** | Destination address funding pattern: fresh account, funded via exchange, receiving from many "victim" address patterns · website age < 90d · WHOIS anonymous · no independent Trustpilot / Google presence |
| **Address homoglyph / clipboard-hijack** | The address the user pasted vs their intended address (need both, out of scope for /check as one-arg). BUT: return a set of near-matches ("this address is 1 char off from `rBoNzS1cZ…`, which is Bitfinex Hot") — actionable when the near-match is a known-legit account |

### 2c. Which signals /check already computes / could / would need 3p

| signal | in /check today? | achievable from own-node + own-DB? | third-party required? |
|---|---|---|---|
| Account age (first-seen ledger) | ❌ | ✅ own-node: `account_tx` walk to earliest tx | none |
| Funding source (who first funded this account) | ❌ | ✅ own-node: `account_tx` earliest tx | none |
| Recent activity pattern (payment count, ratio in/out, distinct counterparties) | ❌ | ✅ own-node + events.db | none |
| On-chain Domain field | ✅ | ✅ already reading | — |
| Two-way toml attestation | ✅ | ✅ walker-cached | — |
| Trust-line spam pattern (issuer TrustSet fan-out) | ❌ | ✅ own-node: issuer's outgoing TrustSet count per 24h | none |
| Ticker-collision warning ("USDT that isn't Tether") | ❌ (surfaced via source narrative, not chipped) | ✅ own-file: known-ticker→canonical-issuer map, curator-maintained | none |
| Look-alike / homoglyph address distance | ❌ | ✅ own-DB: precompute distance-1 neighbors for every named_account address | none |
| Look-alike / homoglyph domain distance | ❌ | ✅ own-file: known-legit-domain list + Levenshtein / IDN normalization | none |
| URL registration age (RDAP) | ✅ (live at request time) | partial (RDAP is public) | rdap.org (already using) |
| TLS cert history | ✅ (crt.sh) | partial | crt.sh (already using) |
| OFAC SDN | ✅ (walker-cached daily) | ✅ | OFAC public XML (already ingesting) |
| Community scam-address list (xrpl.to NFT scam tracker) | ❌ | ⚠ needs their API | `api.xrpl.to` — 3p but XRPL-community-run, terms need review before we integrate |
| Chainabuse address reports | ❌ | ⚠ needs their API + terms | chainabuse.com — 3p |
| Xaman scam warnings | ❌ | ⚠ private to Xaman | xaman.app — 3p (talk to Wietse — Ripple-community-aligned) |
| Message-template classifier (seed-phrase harvest patterns) | ❌ | ✅ own-implementation — regex + curated pattern list | none |
| Deepfake / impersonation detection on media | ❌ | ⚠ out of scope for us; not a core competency | — |

### 2d. What existing tools do (pattern reference)

Sourcing here is what each tool publicly documents; every entry is external, untrusted:

- **XRPScan account labels:** curator-maintained address labels; strong on well-known exchanges/protocols; misses fresh scam clusters and doesn't publish a scam-verdict axis. Similar model to our `named_accounts.json`.
- **Bithomp warnings:** address-level flags for known scam accounts; user-reported + curator-reviewed; strong on retrospective flagging, weaker on real-time.
- **Xaman xApp scam alerts:** blocks known-bad addresses at wallet-submit time inside the Xaman UX; effective because it's PRE-SEND at the payment interface. Not available outside Xaman.
- **Chainabuse:** community-reported scam address database with API; broad multi-chain coverage; strong recall, variable precision (community moderation).
- **ScamSniffer:** URL-level phishing detection browser extension; strong on Ethereum, growing XRPL coverage; API available.
- **Etherscan phishing flags:** address-level annotations; strong pattern to copy — visible on every page that shows the address.
- **xrpl.to NFT scam tracker:** XRPL-specific live tracker (2.6M XRP drained across 35k NFTs from 1.8k wallets) with **free API**; XRPL-community-run, terms clean-looking (needs read).

**Where they miss + where /check could be the trusted one:**

- **They're multi-tool.** A user checking Bithomp, then Chainabuse, then Xaman is doing the work we should do in one query.
- **They mix identity and risk.** Someone reading "flagged" on Bithomp doesn't know if it's OFAC, community-reported, dusting-source, or old spam.
- **They don't disclose sourcing per fact.** /check's `source_label` + `checked_at_utc` per signal is already ahead of every listed tool on transparency.
- **They don't sign responses.** No signed receipt = no cryptographic audit trail = machines can't rely on the answer without re-querying.
- **They're not machine-first.** A wallet, exchange, or LLM tool integrating pre-transaction verification needs a batch endpoint + deterministic verdict codes + latency SLA. None of the above publishes that shape well.

**Our differentiators to lean into:**
1. **Sovereignty of the data source** (own-node, own-DB) — verifiable and censorship-resistant
2. **Per-fact `source_label` + `checked_at_utc`** — already better than the tools on transparency
3. **Signed receipts** (proposal, doesn't exist yet — would be unique)
4. **First-class machine surface** (already committed in the pricing_transition: HTTP-first, x402-billable)
5. **Aggregation across the entire scam-signal landscape into ONE explainable output** — collapses the 4-tab workflow to one query

---

## Part 3 — Tech improvements to the engine (concrete, cited to code)

### 3a. Accuracy holes and their fixes

| current failure mode | fix |
|---|---|
| **Ticker collision** — /check surfaces the source URL but doesn't chip a warning. A first-time visitor might miss that `USDT` on XRPL ≠ Tether. | Add a `ticker_collision_warning` signal: for any token or account labeled with a well-known ticker (USDT, USDC, DAI, BTC, ETH, WBTC…), if the issuer doesn't match the canonical off-chain issuer, emit an amber signal "Warning: this token uses the ticker 'USDT' but is issued by rHjUEu…yMvK (usdxrp.net), which is not the same as Tether Ltd.'s USDT on other chains." Add `ticker_canonical_issuers.json` — curator file listing canonical issuers per ticker. Zero-3p, own-file. |
| **Domain set but toml missing** — Current tier degrades to `self` correctly, but the reader doesn't know the toml was CHECKED and MISSING vs never-checked. | Change the signal wording to explicitly say "Domain field claims 'usdxrp.net' — attempted `xrp-ledger.toml` fetch at [timestamp], **fetch failed with 404** [OR: toml served but does not list this address]." The check happened, disclose the result. |
| **Deleted accounts** — `actNotFound` reported but not tied to a signal narrative | Frame: "This address once existed but has been deleted from the ledger (verified against server_state ledger [index])." AccountRoot deletion is an on-chain event; expose it. |
| **Look-alike addresses** — no check | Precompute distance-1 (single-char) neighbors for every address in `named_accounts.json`. If user pastes a distance-1 address, return an amber signal: "This address is 1 character different from `rBoNzS1cZ…` (Bitfinex Hot Wallet). If someone told you to send to that address, double-check the last 4 characters." |
| **Tokens with the same name from different issuers** | Reverse index in `token_names.json`: `by_ticker: {"USDT": [issuer1, issuer2, …]}`. If a check turns up any token with a ticker that has ≥2 issuers, disclose the list with each issuer's tier. |
| **URLs that redirect** | Follow HTTP redirects (max 3, timeout-bounded); disclose the redirect chain as its own signal. Terminal domain gets the RDAP + crt.sh + toml checks. |

### 3b. Speed and freshness

**What a "check" costs today (rough measure needed; not measured directly yet):**
- 1 AccountInfo (own-node, ~10-30ms LAN)
- 1 named_accounts.json lookup (in-process cache, <1ms)
- 1 account_labels PG lookup (~30-100ms Neon)
- 1 OFAC SDN lookup (in-process cache after first load, <1ms)
- Optional: 1 RDAP fetch (up to 3s), 1 crt.sh fetch (up to 4s), 1 toml fetch (up to 2s)

**Address check with no URL work: ~50-150ms.** URL check: up to ~9s (dominated by 3p fetches).

**Proposal — precomputed risk index for known-active addresses:**
- New walker `address_risk_walker.py` — runs hourly, iterates every address in `named_accounts.json` (~thousands) plus any address seen in the last 30d of events.db (~tens of thousands)
- For each: precompute the on-chain signals from 2b (account age, funding source, activity pattern, trust-line fan-out, ticker-collision applicability)
- Store in `address_risk_index` PG table with `computed_at` timestamp
- /check reads from the precomputed index first; on cache-miss OR staleness > 24h, computes live + writes to index
- **Result: ~5-20ms for cache-hit addresses (the majority for popular queries), ~150ms cache-miss**

### 3c. Explainability — plain-language verdict + evidence list

**Proposed output shape** (in addition to the existing signals/capabilities):

```json
{
  "verdict_line": "This address has NO on-chain identity claim, is 3 days old, was funded by an exchange, and has received 47 payments but sent 0 — that pattern matches 'clone address' scams. If someone asked you to send here, stop and verify through another channel.",
  "verdict_confidence": "high",   // low | medium | high — how many signals agree
  "why": [
    { "signal": "account_age", "value": "3 days", "weight": "concern" },
    { "signal": "funding_source", "value": "single exchange deposit (Bitstamp Hot)", "weight": "neutral" },
    { "signal": "in_out_ratio", "value": "47 in, 0 out", "weight": "concern" },
    { "signal": "identity_tier", "value": "bare", "weight": "concern" },
    { "signal": "ofac_sdn", "value": "not on list", "weight": "clear" }
  ]
}
```

🚨 **LEGAL FENCE:** the `verdict_line` field must be **descriptive not prescriptive** — describes the observed pattern + a suggestion to "verify through another channel" — never asserts "this is a scam" or "this is safe." Attorney read before shipping the verdict-line copy.

**Fixed vocabulary — extends the 6-tier from yesterday:**

- **Identity tier** (unchanged from yesterday): verified / self-described / labeled / bare / sanctioned / unknown
- **Risk signals** (new, discrete + machine-parseable):
  - `account_age_new` (< 30d), `account_age_established` (≥ 90d)
  - `funding_pattern_normal`, `funding_pattern_single_source`
  - `activity_pattern_normal`, `activity_pattern_receiving_only`, `activity_pattern_fan_out`
  - `ticker_collision_warning`
  - `homoglyph_warning`
  - `trust_line_spam_pattern`
  - `community_reported_scam` (if we integrate xrpl.to / Chainabuse)
- **Verdict confidence:** low / medium / high (based on N-of-M signals concurring)

### 3d. Signed receipts — killer feature

**Proposal:** every /check response includes a signed receipt block:

```json
{
  "receipt": {
    "signing_pubkey_fingerprint": "7F:D4:F2:F4:D2:57:7C:BE",
    "signature_ed25519": "<128-hex-chars>",
    "signed_over": "sha256(canonical(response.data + response.signals + response.capabilities + response.verdict + response.checked_at_utc))",
    "spec_url": "https://xrpldashboard.com/methodology#check-receipt-v1",
    "chain": {
      "chain_id": "check-daily-YYYY-MM-DD",
      "leaf_index": N,
      "chain_root_at_close": "<hex>"
    }
  }
}
```

**How it works:**
- Sign each response with the same Ed25519 key we use for signed snapshots (`7F:D4:F2:F4:D2:57:7C:BE`) — key reuse is intentional; one trust root
- At UTC day close, roll up all the day's response signatures into a Merkle tree; publish the root at `/.well-known/checks/YYYY-MM-DD.json` (same shape as the snapshot chain)
- Publish anchors to XRPL alongside the snapshot chain anchors — one anchor covenant for both

**Value:**
- Human users can screenshot + save the receipt as forensic evidence ("I checked address X at 03:11 UTC and it was tier=verified, ofac=clear")
- Machines can verify without re-querying (crypto anchor)
- Exchanges/wallets integrating /check as a pre-transaction gate have an audit trail that survives us going down
- If someone screenshots a forged /check result, our published key won't verify their fake signature — the receipt is un-fakeable

### 3e. Machine surface — what's missing for pre-transaction gate use

Current `/check.json` returns rich per-query results, but for a wallet / exchange / LLM integrating "should I pay this address?" the missing pieces:

- **Latency budget disclosed:** need `p50_ms` / `p95_ms` published in `agents.json`; SLO commitment (< 500ms for cached, < 2s for cache-miss on address; URL check separate SLO)
- **Batch endpoint:** POST `/check.batch.json` with `{queries: [q1, q2, …]}` returns one response with array of results; single Ed25519 signature over the whole batch. Cuts round-trip cost for wallets checking a payment routing table
- **Deterministic verdict codes:** in addition to `verdict_line`, emit `verdict_code` (short-string identifier: `pass_verified`, `pass_bare`, `warn_ticker_collision`, `warn_new_account_receiving`, `flag_ofac_sdn`, `flag_homoglyph`, …) — machines route on the code, humans read the line
- **Error shapes:** documented error codes with schema in openapi.json (`400 bad_input`, `404 address_not_found_on_ledger`, `429 rate_limit`, `502 upstream_check_failed`, `503 partial_result`) — standard JSON Problem Details (RFC 7807)
- **Idempotency key:** clients pass an `Idempotency-Key` header; we return cached response for 60s if the same key comes in with the same query — prevents replay races on pre-transaction checks

### 3f. Watch + notify

**Proposal:** a subscription table `check_watches` (Postgres) with rows `(subscriber_id, query, notify_channel, threshold_change)`. Walker `check_watch_walker.py` runs every 15 min; for each active watch:
- Re-runs the /check
- Compares against last stored state
- If threshold_change met (e.g. identity_tier changed, new risk signal appeared, OFAC status flipped, community_reported first-seen), notifies

**Notify channels:**
- Webhook (POST to a URL with the new /check result + signed receipt) — machine-first
- Email — human
- MCP push (proto: `check.watch.notify` tool call) — for MCP clients
- Telegram (optional) — for you specifically

**Rate limits + auth:**
- Anonymous: no watches (need contact channel)
- Verified email: 10 watches, 1/day check frequency
- Paid HTTP tier: 1000 watches, 15-min check frequency, webhook

---

## Part 4 — Ranked product directions (my independent ranking)

Ranked by **(scam-victim impact) × (feasibility on own-node) ÷ (effort)**. Free-forever vs paid-machine noted per row. Numbering is priority, not sequence.

| # | proposal | victim impact | own-node feasibility | effort | free/paid | 🚨 legal? |
|---|---|---|---|---|---|---|
| 1 | **Signed receipts on every /check response** (Part 3d) | Medium immediate, HIGH systemic (any tool built on us gets un-fakeable evidence) | ✅ full — reuse snapshot signing key + chain infra | S-M (~200 lines, methodology page update) | free for anyone | none — cryptographic transparency, not verdict |
| 2 | **Message-template classifier for seed-phrase / phishing text** | HIGH direct (catches victim BEFORE they paste seed anywhere) | ✅ full — regex + curated pattern list | S (~100 lines) | free | ⚠ 🚨 low — pattern-match on message text is factual detection ("this message contains a request for your seed phrase") not a scam verdict, but wording matters |
| 3 | **Ticker-collision warning** (Part 3a) | HIGH direct (the "USDT that isn't Tether" case) | ✅ full — one curator file | S (~50 lines + curator file) | free | none — descriptive fact |
| 4 | **Homoglyph / distance-1 address warning** | HIGH direct (clipboard-hijack is a top loss vector) | ✅ full — precompute in-process | S-M (~150 lines, walker + lookup) | free | none — factual distance measurement |
| 5 | **Homoglyph / IDN domain-similarity check on URLs** | HIGH direct (phishing site detection) | ✅ full — Levenshtein + IDN normalization against a curated known-legit list | S-M (~150 lines + curator list) | free | none — factual distance |
| 6 | **Address risk-index walker + plain-language verdict-line** (Part 3b + 3c) | HIGH systemic (this is the "actual verifier" Charlie described) | ✅ full — own-node + own-DB | M (~500 lines, walker + refactor + editorial pass) | free basic verdict; paid tier for verdict_confidence + full signal decomposition | 🚨 **high — attorney read on the verdict_line copy is mandatory before shipping.** Wording must stay descriptive not prescriptive |
| 7 | **Community scam-address feed integration** (start with xrpl.to's free API) | HIGH direct (catches actively-live scam addresses) | ⚠ partial — 3p feed, cached locally | S-M (~200 lines + cache walker + terms review) | free (with attribution) | 🚨 medium — need to make clear this is 3p-sourced, not our verdict, and their terms need reading before ingest |
| 8 | **Batch endpoint + verdict codes + latency SLO** (Part 3e) | Medium direct (unlocks wallet/exchange integrations) | ✅ full | M (~300 lines, openapi update, SLO commitment) | paid HTTP tier | none — API shape |
| 9 | **Watch + notify** (Part 3f) | Medium direct (long-tail: an address flips OFAC or gets community-reported after you first checked) | ✅ full — walker + PG table | M-L (~600 lines, walker + notify plumbing + auth) | tiered: free = email 1/day; paid = webhook 15-min + MCP push | 🚨 medium — notification wording same fence as verdict_line |
| 10 | **Follow-URL redirect chain + terminal-domain checks** | Medium direct (catches URL-shortener phishing) | ✅ full — HTTP client with redirect | S | free | none |
| 11 | **Wallet-detail-page "was this in Chainabuse?" chip** | Medium direct (surfaces community reports at browse time, not just /check paste) | ⚠ partial — needs Chainabuse API | M (~250 lines + terms/legal on scraping vs API) | free | 🚨 medium — Chainabuse ToS + attribution |
| 12 | **Precomputed risk index available as a downloadable dataset** (daily signed snapshot of the entire risk index) | LOW direct, HIGH systemic (other tools can build on us) | ✅ full — same anchor infra | M | free (public good) or paid (bulk API) | 🚨 medium — attorney on the dataset license + disclaimer |
| 13 | **Deepfake / media impersonation detection** | HIGH direct but LOW feasibility (not our competency) | ❌ out of scope | XL | n/a | 🚨 HIGH |
| 14 | **On-page "Check it" chip everywhere an address appears** (whales / tokens / wallet-detail / pools / claims / MCP tool descriptions) | Medium direct (surfaces the tool by proximity) | ✅ full — template plumbing | S | free — this is the site's own dogfooding | none |

**The free/paid line — my recommendation:**

- **Free forever:** items 1, 2, 3, 4, 5, 7, 10, 11, 14 — anything that catches a scam BEFORE a human sends money must be free. This is the mission. Paying to know if you're being scammed is grotesque.
- **Free basic, paid deeper:** item 6 — anonymous user gets identity tier + top 3 risk signals; paid tier gets full signal decomposition + verdict_confidence + programmatic verdict_code
- **Paid HTTP tier:** items 8, 9, 12 — batch API, watch/notify, bulk dataset are producer-tier features that fund the free consumer tier. `future_billed_surface = "http"` from yesterday's ruling applies.

**The "one big product to build" answer** (if you can only pick ONE): items 6 + 7 (address risk-index + community scam-address feed) combined. That's the shift from "identity confidence" to "actionable verdict." Everything else is scaffolding around it. Estimated combined effort: ~1000 lines across walker + engine + editorial. Attorney sign-off gates the shipping date, not the coding date.

---

## Part 5 — Compare to Claude's yesterday list

Claude yesterday proposed: unify as the site's one verifier · signed receipts · "check it" link everywhere · watch/notify on status change · history of past results · lean into scam triage for URLs/messages.

**Agree with (verbatim overlap):**
- **Signed receipts** — my #1 too. We converge here independently. Killer feature, uniquely ours.
- **"Check it" link everywhere** — my #14. Small effort, high dogfooding value.
- **Watch/notify** — my #9. Same shape.
- **Scam triage for URLs/messages** — my #2 (messages), #5 (URLs), #10 (redirects). Same direction, I broke it into 3 concrete features vs 1 category.
- **Unify /whales and /check via shared module** — yesterday's Part 3, still the right architecture.

**Where I go further than Claude:**

- **Claude didn't rank by scam-victim-impact.** My ranked list prioritizes the message-template classifier (#2) and homoglyph warnings (#4, #5) above signed receipts, because those catch victims *before* the send while receipts help *after*.
- **Ticker-collision warning (#3)** — Claude didn't call this out as a top-tier fix. But this is the ORIGINAL "USDT that isn't Tether" complaint from your screenshot last night. Small effort, direct fix.
- **Address risk-index walker + plain-language verdict-line (#6)** — Claude implied it under "unify as the site's one verifier" but didn't push the plain-language verdict-line as the killer output. This is what turns /check from "answer" to "actionable verdict." Attorney gate.
- **Community scam-address feed integration (#7)** — Claude didn't propose ingesting xrpl.to's free scam-tracker API. That's the direct path to catching live active scams (2.6M XRP already tracked; 1,831 scammer wallets on their list).
- **Batch endpoint + verdict codes (#8)** — Claude proposed the machine surface generally but didn't specify batch or verdict codes. Wallets integrating pre-transaction verification need the batch shape.

**Where I disagree with Claude:**

- **"History of past results"** — Claude proposed this; I think it's low-value UNLESS bundled with signed receipts. A history without receipts is just a database that could have been forged after the fact. With receipts, history is meaningful (each row is an un-fakeable snapshot of state at a point in time) — but at that point the receipt IS the history mechanism. I'd fold this into #1, not build it as a separate feature.

**Where Claude missed something I flagged:**

- **The verdict-line editorial fence** — Claude noted "attorney's eyes before machines pay for it" in the caveat but didn't build the fence into every proposal. My #6 explicitly gates on attorney read of the verdict-line copy, because that's THE feature attorneys will care about. Every proposal that emits a verdict-adjacent output needs the fence in scope.
- **Free-vs-paid line explicitly at the "does it catch a victim?" boundary** — Claude didn't articulate a principle. Mine: anything catching a scam pre-send is free forever; anything reducing operator or paid-agent workflow cost is paid. That's the honest line and it also protects the mission from being "priced-out safety."

**What Claude proposed that I don't have (and think matters):**

- Nothing standing out that isn't captured under my broader items. Claude's list is a subset of mine, ranked differently.

---

## Final synthesis

**The mission-critical shift:** /check today answers "who?" — it should also answer "should?" A person about to be scammed doesn't need better identity metadata; they need a plain-language verdict-line that says "the pattern of this address matches 'clone address' scams — stop and verify through another channel" WITH the evidence list so they can override us if they know better.

**The two proposals that would move the needle most (my picks):**
1. **Signed receipts on every response** (#1) — un-fakeable evidence, builds the entire trust stack for machines and humans
2. **Address risk-index walker + plain-language verdict-line** (#6, gated on attorney) — turns /check into the "actual verifier" Charlie asked for

**The three cheap-and-immediate wins (this week):**
- Ticker-collision warning (#3) — closes yesterday's USDT complaint
- Homoglyph address distance-1 warning (#4)
- Message-template classifier for seed-phrase requests (#2)

**None of this ships until the tx-type badge (yesterday's approved 3c) lands.** That first fix teaches the pattern; everything else builds on the same shape.

**🚨 Attorney gate summary:** items 6, 7, 9, 11, 12, 13 all need legal review before shipping any verdict-adjacent copy. The attorney meeting Charlie has queued (per yesterday's `pricing_transition` fields being held for that meeting) should cover the verdict-line copy at the same time.

## Appendix — sources

All external sources are UNTRUSTED per web_search / web_fetch wrapping; cite when repeating any quantitative claim.

- Bitrue.com (2026): XRP giveaway scams: `https://www.bitrue.com/blog/xrp-giveaway-scams-explained-why-investors-should-be-careful`
- SafeBrowz (2026-05): Xaman drainer campaign: `https://safebrowz.com/blog/xrp-airdrop-xaman-wallet-drainer-scam-2026`
- Sahmcapital (2026-07-30): Korean Flare Network staking scam: `https://www.sahmcapital.com/news/content/xrp-scam-in-south-korea-defrauds-investors-of-19-million-2026-07-30`
- xrpl.to NFT scam tracker (2026, live): `https://xrpl.to/insights/xrpl-nft-scam-tracker` — free API at `https://api.xrpl.to/api/docs`, catalog at `https://xrpl.to/.well-known/api-catalog`
- u.today (2026-02): Wietse Wind's 6 phishing vectors briefing: `https://u.today/protect-your-xrp-6-new-phishing-tactics-identified-by-xrpl-contributor-wietse-wind`
- Crypto Economy (2026): XRPL verification-alert scam: `https://crypto-economy.com/scammers-target-xrp-holders-with-fraudulent-xrp-ledger-verification-alerts/`
- ItchOL / airdropalert / zipmex / coinsbit / allaboutxrp: 2026 XRP scam typology overviews — cited only for typology framing, not quantitative claims
- Ledger + 99Bitcoins: dusting attack overviews

Existing tools referenced (pattern-only, no ingestion assumed): XRPScan, Bithomp, Xaman, Chainabuse, ScamSniffer, Etherscan phishing flags.

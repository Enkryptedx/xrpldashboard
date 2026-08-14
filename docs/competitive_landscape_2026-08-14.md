# Competitive landscape — 2026-08-14

Two independent research runs: JJ blind research + Charlie's Claude session. Merged where they agree; deltas noted explicitly. Filed as reference, not action — no builds triggered.

---

## The XRPL analytics map

| Tool | Monthly traffic | Lane | Verdict |
|------|----------------|------|---------|
| XRPScan | ~320-343K (SimilarWeb; both runs agree on order of magnitude) | Explorer/lookup | Dominant incumbent; breadth without depth. Tells you what happened, not what it means. No signed data, no methodology commitment, no machine layer. Different job than us. |
| Bithomp | ~57K (-31% MoM; both runs exact match) | Explorer + dev API | Est. 2015, aging. €30/mo API entry. Good historical data. Shrinking. |
| livenet.xrpl.org | Unknown | XRPL Foundation official | Free, open source. No analytics layer, no monetization. Legitimacy without insight. |
| XPMarket | Unknown | Trading platform | DEX + NFT + AMM trading. Different lane — they want you to trade. |
| Rich-List.info | ~7.6K (Charlie session); JJ found +35% MoM trend (older data) | Wealth distribution only | Narrow. No signed data. Useful for one question only. |
| xrp.cafe | Unknown | NFT marketplace | Different lane. |
| OnTheDex | Unknown | Token price/volume API | Feeds CoinGecko/CoinMarketCap. Infrastructure, not consumer analytics. Potential distribution partner (not competitor). |
| XRPL Services | Unknown | Tx execution + stats | API-first. Not consumer-facing. |
| **xrplanalytics.com** | Unknown | **Free analytics, live-from-ledger** | **JJ-found, not in Charlie session.** Free, no signup, covers ETF flows/escrow/whales/AMM/DEX. Claims "no approximations, no delays." Most direct free-lane competitor. Missing: no signed data, no sovereignty story, no methodology commitment, no MCP. Not a trust-stack threat; worth watching. |

**The picture:** The pie is small. XRPScan and Bithomp dominate but serve a lookup job, not an analytics job. The "understand + trust" layer of XRPL analytics is wide open. The incumbents are aging lookup tools. xrplanalytics.com is the only new entrant in our exact lane, and they lack the trust architecture.

Our differentiation is not data coverage — it's the stack: signed snapshots + sovereignty + on-chain anchor + methodology commitment + MCP. No other player on XRPL has that combination.

---

## The Nansen line (named standard)

**Named for filing purposes: the Fact/Opinion Standard**

> *Factual/identity labels (provably true, primary-source verifiable) = publishable free, no standard violated.*
> *Judgment labels (skill assessments, performance predictions, trading implications) = violates claims-need-receipts standard + potential regulatory exposure.*
> *The line is the truth-standard itself.*

**Context:** Nansen (nansen.ai) sells 500M+ wallet labels across 28+ chains at $49-$1,899/mo. Their "Smart Money" metric is a proprietary PnL-based skill judgment — they label wallets as "good traders" based on performance history. XRPL is NOT explicitly covered in their top chain list.

**What we can do (free, evidence-backed, backlog candidate — not fired):**
Entity identification — exchange cold wallets, known issuers, Ripple escrow accounts, foundation wallets, market makers. These are provable from on-ledger behavior + public knowledge. We already partially do this on /whales. Expanding the structured label set is a legitimate backlog candidate; it would make us the first credible entity-labeler on XRPL with a methodology commitment — a lane Nansen hasn't entered.

**What we cannot do:**
"Smart Money" / trading-skill judgments = unfalsifiable opinion. "Wallet X trades well" is a claim we can't prove and implies trading advice. Same principle that killed the AI-wallet-narration concept. This is permanent, not a timing decision.

---

## xrpdashboard.com — brand collision + competitive note

**The product:** $9.99/mo Pro / $29.99/mo Power. Whale alerts (own validator), portfolio tracking, DCA bots, yield optimization, live price. Claims 5K users.

**Revenue estimate:** ~$1-3K/mo realistic (both runs converge; Charlie session said $1-2.5K). Paid whale alerts = our free /whales since June.

**How they survive:** Selling what our standards forbid — trading signals, sentiment indicators, portfolio tracking, DCA advice. Their moat against us is our ethics. Our moat against them is our receipts.

**Brand collision — FILED FOR TAFT:**
One letter apart: `xrpdashboard.com` vs `xrpldashboard.com`. As we get cited by AI systems and directories under our name, confused attribution becomes a real problem. If Anthropic's directory or Glama cites "xrpldashboard" and a user searches "xrpdashboard," they hit a paid trading-advice app with a near-identical name.

Cross-reference: `project_attorney_lens_research_2026-08-13.md` — add to Taft agenda. USPTO trademark search on "xrpldashboard" / "xrpl dashboard" is the first step. No action now; filed for attorney review.

---

## Where the two runs agreed

- Traffic order of magnitude: XRPScan ~320-343K (minor measurement-period difference). Bithomp ~57K exact.
- Nansen line: Complete agreement. Factual labels = doable. Judgment labels = violates standard.
- xrpdashboard.com revenue estimate: converge on $1-3K/mo.
- Free /whales already beats their paid feature: confirmed.
- Brand collision worth filing for Taft: agreed.
- Anchored record = uncopyable moat. Time-in-cadence IS strategy: agreed.
- "1-of-1" framing: structurally correct on the signed-snapshots + sovereignty + MCP + on-chain anchor stack.

## Where the runs diverged

| Topic | Charlie session | JJ research | Resolution |
|-------|----------------|-------------|------------|
| xrplanalytics.com | Not mentioned | Most direct free-lane competitor | Goes on watch list |
| Nansen pricing | $99-$1,899/mo | $49-$1,899/mo (Pro annual $49, monthly $69) | Minor; pricing may have changed; not material to strategy |
| Machine vs. human track | Blended in Phase 3 | Should be separate tracks | Execution note: MCP/signed API already moving autonomously; ask-box is a separate human-facing build on its own timeline |
| OnTheDex | Not mentioned | Potential distribution partner | Low priority backlog; feeds CoinGecko/CMC |

## Push-back on "I'm not competing"

Charlie's framing is spiritually right — we're building a reference layer, not chasing traffic. But the execution still requires winning search results, AI citations, and directory slots. The cleaner frame: **"We're building the reference layer, not the traffic layer."** Same thing, without the competitive anxiety, and it maps cleanly to what we're actually doing.

---

## Backlog candidates filed (not fired)

1. **Entity-label expansion** — exchange/issuer/escrow/foundation identification, evidence-backed, free. First mover on XRPL entity labels with methodology commitment. Trigger: when walker capacity allows post-Batch-B.
2. **xrplanalytics.com watch** — track quarterly. If they add methodology/sovereignty layer, they become the closest competitor we have.
3. **Brand-collision trademark hygiene** — xrpdashboard.com — add to Taft agenda. USPTO search first step.
4. **OnTheDex distribution angle** — assess as token-data distribution partner (low priority, post-rails-dark).

---

*Research date: 2026-08-14. Two independent runs: JJ blind research + Charlie's Claude strategy session. Deltas noted explicitly.*

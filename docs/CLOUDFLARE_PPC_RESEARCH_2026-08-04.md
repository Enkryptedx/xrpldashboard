# Cloudflare Pay Per Crawl — Feasibility Research

**Date:** 2026-08-04
**Status:** STAGED FOR CHARLIE'S WORD — research, not a build. Decision pending.
**Trigger:** Fresh-Claude external evaluation flagged Pay Per Crawl as a candidate F (see `docs/PAID_MACHINE_TIER_DESIGN.md § 1.F`). Charlie's directive (msg 10622): "OPT-IN-NOW / OPT-IN-LATER-AT-TRAFFIC-X / CONFLICTS-WITH-FLYWHEEL-SKIP. Stage for Charlie's word."

---

## Headline finding

**Cloudflare bifurcated the product in the year between fresh-Claude's search training data and today.** There are now TWO distinct offerings, and the honest answer to Charlie's four questions is different for each.

| Product | Status (2026-08) | Verdict for xrpldashboard |
|---|---|---|
| **Pay Per Crawl** (402-per-request, launched July 2025) | Private beta, paid-plan-tier-gated | **CONFLICTS-WITH-FLYWHEEL-SKIP** |
| **Pay Per Citation** (announced July 1, 2026) | Cloudflare-described "experiment," broad availability "later in 2026" | **OPT-IN-LATER-AT-BROAD-AVAILABILITY** |

Cloudflare itself, in the July 1, 2026 announcement, called the original Pay Per Crawl "a first step" and argued that "**crawling is a poor proxy for value.**" They pivoted to paying publishers when content appears in AI answers (partners: Ceramic.ai, You.com). That is the model that aligns with our current strategy; the 402-per-crawl model does not.

---

## Charlie's four questions, answered

### (1) Are we on Cloudflare's eligible tier?

**No, and it's worse than a tier question.**

- Pay Per Crawl's 402 feature is documented as **"Paid Plan Users"** only via AI Crawl Control. We are on free.
- Even for Paid Plan users, the full Pay Per Crawl monetization surface remains in **private beta requiring application** at `cloudflare.com/paypercrawl-signup/` (Enterprise customers can go through their Account Executive; there is no self-serve path).
- Broad availability is described as "later in 2026, without a specific date."
- Pay Per Citation eligibility for the July 2026 partnerships (Ceramic.ai, You.com) is "publishers who opt in" — no plan-tier restriction stated, but no self-serve enrollment surface is documented yet.

**Answer:** We would need (a) upgrade to a paid Cloudflare plan AND (b) get accepted into the private beta to enable Pay Per Crawl at all. Neither is a "zero build cost" scenario. Pay Per Citation is voluntary opt-in and plan-agnostic in principle, but the enrollment mechanism is not yet public.

### (2) What's the actual opt-in mechanism?

- **Pay Per Crawl:** application form at `cloudflare.com/paypercrawl-signup/` OR Account Executive contact for existing Enterprise customers. Not a dashboard toggle.
- **AI Crawl Control (the GA'd 402-blocking cousin):** dashboard-configurable, paid-plan-only.
- **Pay Per Citation:** publisher opt-in via arrangement with Ceramic.ai / You.com; broad Cloudflare-native availability not yet shipped.

**Answer:** Nothing that fits the "zero build cost, flip a toggle" claim from the fresh-Claude review.

### (3) Does it conflict with the AI-crawler allowlist? (THE FLYWHEEL QUESTION)

**This is where the honest read matters.** Our Day-6 identified-crawler tier already implements a citation-vs-training distinction Cloudflare doesn't:

- `agent_tier_rate_limit.py:73-99` — positive allowlist of 15 UA substrings, explicitly annotated for retrieval/citation intent (line 74: "OpenAI (retrieval/citation, distinct from GPTBot-training-only)"; line 90: "distinct from meta-externalagent which is training-only and blocked").
- Citation crawlers (oai-searchbot, chatgpt-user, claude-searchbot, perplexity-user, google-extended, applebot-extended, meta-searchbot) get **300 req/min + audit-URL header pointing at `/coverage`** — this is the flywheel we're building.
- Training-only bots (meta-externalagent) are already blocked in `app.py:_BLOCKED_UA_FRAGMENTS`.

Cloudflare's Pay Per Crawl model is **crawler-level Allow/Charge/Block**, not purpose-level. Cloudflare's verified-bot inventory does not distinguish OAI-SearchBot from GPTBot with the granularity our allowlist does — so enabling Pay Per Crawl would either:

- **Charge citation crawlers** (undermines the flywheel — the point of Day 6 was to make it *free and audited* for GPTBot / ClaudeBot / OAI-SearchBot to cite us), OR
- **Only charge unverified/anonymous crawlers** (which mostly don't identify themselves as bots at all — they show up as browser-UA-spoofing fleet traffic, which we already handle with the /whales fleet-block + agent-tier fleet-block extension per `AGENT_TIER_DESIGN.md`).

**The conflict is real for Pay Per Crawl (402 model).** The whole point of the Day 6 allowlist + audit-URL header is to convert crawler traffic into structured citations. 402-ing that traffic asks Cloudflare to charge for the exact resource we're deliberately handing away, at the exact moment we haven't yet read whether the giveaway is producing citations (first weekly crawler-harvest read = Friday).

**The conflict does NOT apply to Pay Per Citation.** The July 2026 model pays us WHEN content appears in AI answers. The more citation crawlers succeed, the more we earn. That is the flywheel with a monetization primitive on top, not a monetization primitive that throttles the flywheel.

**Answer:** Pay Per Crawl (402) directly conflicts. Pay Per Citation is the aligned version — and it's not yet broadly available.

### (4) What would we honestly expect at our traffic?

Fresh Claude's estimate: "low hundreds/year" in USD revenue.

Our current identified AI-crawler traffic (per `agent_tier_rate_limit.py` telemetry, first read Friday) is unmeasured but bounded by our overall site scale. Anonymous crawler traffic is already partially handled by fleet-block (blocking) — the residual layer Pay Per Crawl could monetize is small.

Realistic bands:
- **Pay Per Crawl** (if we somehow got into the beta and Cloudflare's cut is favorable): **low tens to low hundreds of dollars per year.** Below the threshold that justifies upgrading the Cloudflare plan tier for the 402 feature.
- **Pay Per Citation** (once broadly available): **unmeasurable today** — depends on how often our facts get incorporated into Ceramic / You.com / participating answer engines' output. If our citability discipline (envelope + honest_partial + published bug history) is a citation multiplier, this could materially outpace Pay Per Crawl. But the pricing structure is undisclosed and there's no reporting infrastructure yet.

**Answer:** Pay Per Crawl revenue expectation does not clear the "worth upgrading plan tier + applying to beta" threshold. Pay Per Citation is the plausible upside if broad availability ships.

---

## Verdict (per Charlie's shape)

**Pay Per Crawl (402 model): CONFLICTS-WITH-FLYWHEEL-SKIP.**

Reasoning stack:
1. **Eligibility gate:** paid plan + private-beta application. Not zero-cost to enter.
2. **Flywheel conflict:** the Cloudflare Allow/Charge/Block granularity is coarser than our Day-6 citation/training distinction. Enabling PPC would either 402 our citation crawlers (killing the flywheel we haven't even read yet) or apply only to already-blocked anonymous fleet traffic (near-zero incremental revenue).
3. **Revenue floor:** low tens to low hundreds of USD/year. Below the Cloudflare-plan-upgrade break-even.
4. **Cloudflare's own signal:** they pivoted away from the 402 model on July 1, 2026, calling crawling "a poor proxy for value." Following a product the vendor is de-emphasizing is a poor bet.

**Pay Per Citation (July 2026 model): OPT-IN-LATER-AT-BROAD-AVAILABILITY.**

Reasoning stack:
1. **Aligned with strategy:** pays on citation appearance = same revenue axis the flywheel builds toward.
2. **Respects all five sovereignty rules:** the data stays free at the source, Cloudflare / Ceramic / You.com pay from their answer-monetization side; we get compensated for citability, not for gating.
3. **No enrollment surface public yet** — not actionable today.
4. **Correct posture:** watch for broad availability (Cloudflare says "later in 2026, without a specific date"). Revisit when the enrollment mechanism ships. Kill trigger: 90 days past 2026-12-31 with no publisher enrollment path published.

**Watch trigger (concrete):**
- Set a reminder on `docs/PAID_MACHINE_TIER_DESIGN.md` § 1.F next-review date to **2026-11-01** (matches the memo's 60-90d demand-window decision date).
- If Pay Per Citation has a public publisher opt-in surface by then, evaluate against the five sovereignty rules and the citability discipline claims. If it's aligned, opt in.
- If Cloudflare abandons or narrows Pay Per Citation before 2026-11-01, close the watch and mark § 1.F CLOSED.

---

## Implication for `docs/PAID_MACHINE_TIER_DESIGN.md § 1.F`

The § 1.F entry I added in commit `539d4a9` treats Cloudflare Pay Per Crawl as the one-thing candidate. That entry is now **partially wrong on facts**:

- The "zero build cost" claim requires a Cloudflare plan upgrade + beta acceptance — not zero.
- The "Cloudflare owns payments" attorney-light claim holds for both PPC and Pay Per Citation, but the flywheel-conflict analysis was underweighted in the original entry.
- The correct decomposition is two entries — F.1 (PPC = SKIP with named reasons) and F.2 (Pay Per Citation = WATCH with named trigger).

**Recommended memo revision, pending Charlie's word:**

Rewrite § 1.F as two sub-candidates. F.1 documents the PPC decline with reasoning. F.2 documents the Pay Per Citation watch with a specific 2026-11-01 revisit trigger. Both entries reference this research document.

Do not enact the revision without Charlie's explicit go — the current § 1.F is on the record from `539d4a9` and represents a valid audit trail of the initial fresh-Claude read; overwriting it should be a deliberate decision.

---

## Sources

- [Introducing pay per crawl — Cloudflare Blog (July 1, 2025)](https://blog.cloudflare.com/introducing-pay-per-crawl/) — original 402-per-crawl announcement, private beta.
- [The next step for content creators in working with AI bots: Introducing AI Crawl Control — Cloudflare Blog](https://blog.cloudflare.com/introducing-ai-crawl-control/) — GA'd Aug 2025; 402 for paid-plan users, dashboard-configurable.
- [Cloudflare stops charging AI per crawl and starts paying per answer — ppc.land (2026-07-01)](https://ppc.land/cloudflare-stops-charging-ai-per-crawl-and-starts-paying-per-answer/) — coverage of the pivot to Pay Per Citation, Ceramic.ai + You.com partnerships.
- [Cloudflare docs: Pay Per Crawl (What is Pay Per Crawl)](https://developers.cloudflare.com/ai-audit/features/pay-per-crawl/what-is-pay-per-crawl/) — official docs; confirms closed beta.
- Local: `/Users/charliebruce/xrpl_test/agent_tier_rate_limit.py:73-99` — our positive allowlist for identified citation crawlers, the flywheel this decision protects.

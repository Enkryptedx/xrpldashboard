# Multi-AI Audit Plan — 2026-09-04

Author: JJ (Opus 4-7). Research conducted 2026-09-04 EDT afternoon after anchor #5 stamped. Everything current-model reflects public info retrieved this afternoon via live web search; sources dated and linked at the bottom. My own training data cuts off at January 2026, so nothing about Fable 5.1, Mythos 5.1, GPT-6 Astra, Gemini 3.8 Flash, Grok 4.6, or Llama 5 comes from my parametric knowledge — it's all fresh web research.

---

## Charlie's brief (verbatim)

> "What's the next step in automating the dashboard? I want to get everything fixed and working correctly first. Every single you or JJ or ChatGPT or Gemini or grok or perplexity does an audit, everyone always finds something. I want to use all of the major AI's accordingly. Use one AI specifically (based on their strong suits) to prompt the other AI specifically (based on their strong suits) to prompt the other AI specifically so on and so forth. Eventually we will get to a general consensus. That way, we can make an awesome site for human viewers and a very well functioning site for machines to access the data. We can use the best abilities of all the models after we research where they score on everything. This website is important. We have to build it accordingly."

Follow-up in his own words: research every major model's strengths and weaknesses, figure out which one to START with, use what that round produces to improve the site for humans and machines, then use the data to pick the NEXT model, and rotate through all of them. *"If humans need data and don't know where to get it, we have it. If machines need data for whatever reason, we have it."*

---

## PART 1 — The model map (as of 2026-09-04)

### OpenAI — **GPT-6 Astra** (released 2026-09-03)

**Best at (with source):**
- **Computer use, coding, cybersecurity, science** — OpenAI positions Astra as "state-of-the-art capabilities across computer use, coding, cybersecurity, and science" (OpenAI product page, Sep 3 2026). First OpenAI model rated **"Critical"** on their internal cyber preparedness scale.
- **Agentic coding & long-context computer-use workflows** — targets these workflows rather than everyday chat, per HokAI (2026-09-03). GPQA reported near 96%.
- **Access with computer-use tool** — the model can natively drive a browser and take real actions on a virtual desktop (successor to their computer-use preview line).

**Weaknesses / failure modes:**
- **Access is gated** — participants in OpenAI's application-based cybersecurity program get first crack (CNBC, 2026-09-03). Standard API access is rolling out; not everything is immediately available.
- **Hallucination baseline** — no independent benchmark yet (5 days old at time of writing); vendor's own claims dominate the public data.
- **Cyber capabilities disabled by default outside gated programs** — some pentest/security research prompts get refused unless you're in the cyber program.
- **First-two-days telemetry unreliable** — Forbes flagged that OpenAI's own launch page briefly went down mid-announcement (Sep 3), and the AGI framing from Brockman is being contested (BetaNews). Take vendor claims with more skepticism than usual.

**Access:**
- ChatGPT Plus / Pro / Business / Enterprise (rolling out)
- API: `gpt-6-astra` — $10 / M input, $1 / M cached input, $50 / M output (Coursiv, llm-stats.com, both 2026-09-03/04)
- Context window: **1.05M tokens** input, 128K output
- Knowledge cutoff: April 30, 2026
- Live URL reading: yes (via built-in browsing tool + computer-use)
- Code execution: yes (agentic + code interpreter)

**Fit against our six audit tasks:**
| Task | Rank | Reason |
|---|---|---|
| (i) External-fact verification | 2/6 | Browsing tool is good, but Perplexity is purpose-built for cited-answers |
| (ii) Code-path tracing against repo | 2/6 | Fable is the coding leader; Astra is competitive but not clearly ahead |
| (iii) Truth-copy review of rendered pages | 3/6 | Solid; loses to Fable on writing-quality nuance and Gemini on multimodal seeing |
| (iv) Spec/doctrine review | 3/6 | Broad reasoning ok; Fable's careful-writer voice wins here |
| **(v) Machine-surface review** | **1/6** | **Computer-use tool navigates the actual machine surfaces the way a real agent would — this is Astra's home field** |
| (vi) Human-reader UX review | 3/6 | Solid; Fable and Gemini beat it on the "does this READ well?" question |

---

### Anthropic — **Claude Fable 5.1** (released 2026-09-01)

**Best at (with source):**
- **Coding + agentic coding** — Anthropic's own claim: "world's most advanced models for coding and knowledge work" (MacRumors, 2026-09-01). **81.2% on SWE-bench Pro** (HokAI, 2026-09-01). **55.8% Terminal-Bench** (explainx.ai). Long code refactors, front-end / visual code generation, finance and analysis tasks per OpenRouter's model card.
- **Long-running agentic workflows** — Anthropic markets it for "long-running, high-stakes work that runs for hours and spans many applications" (AWS launch post, 2026-09-01).
- **Careful writing / honesty framing** — the entire Claude lineage prioritizes calibrated honesty; on our own work (this session) has been the model driving JJ's outputs against `verify_before_verdict`-style rules.
- **75% cheaper cache reads** vs Fable 5 (unchanged base pricing) — makes repeated audits over the same repo cheap (Marktechpost, 2026-09-01).
- **Loosened safeguards on benign requests** — bio-safety intervenes 85% less often on benign requests than at Fable 5 launch (Anthropic, 2026-09-01). Fable 5.1 can now identify software vulnerabilities in source code, previously refused.

**Weaknesses / failure modes:**
- **Not natively web-connected** at the model level — needs tool wrapping (via MCP or claude.ai's search tool) to fetch URLs. Slower first-hop for pure external-fact verification.
- **Refusal patterns still catch legitimate security research** in some edge cases — improved but not zero.
- **Same-family bias** — since Charlie's already running JJ on Opus 4-7, having Fable 5.1 do the primary audit means both flag the same anchoring biases. Value comes from OTHER models.
- **Mythos 5.1** is same model with full cyber+bio capabilities but restricted (Project Glasswing invite only) — not available to Charlie without contacting Anthropic/AWS/Google Cloud account team.

**Access:**
- claude.ai (Pro/Team/Enterprise) + API
- API: `claude-fable-5-1` — $10 / M input, $50 / M output (unchanged from Fable 5)
- Cache reads: 75% cheaper than Fable 5
- Context window: **1M tokens** input, 128K max output
- Live URL reading: no native (tool-required)
- Code execution: yes (via `code_execution` tool)
- Also on AWS Bedrock, Google Cloud Vertex AI

**Fit against our six audit tasks:**
| Task | Rank | Reason |
|---|---|---|
| (i) External-fact verification | 4/6 | Not web-native; needs tool scaffolding |
| **(ii) Code-path tracing against repo** | **1/6** | **Highest SWE-bench + Terminal-Bench scores + the "identify software vulnerabilities in source code" specific unlock. Purpose-built for exactly this** |
| (iii) Truth-copy review of rendered pages | 2/6 | Beats Astra on writing nuance; loses to Gemini on multimodal (screenshots) |
| **(iv) Spec/doctrine review** | **1/6** | **Careful-writer voice, honesty framing, best at "does this doctrine hold together end-to-end?" reasoning** |
| (v) Machine-surface review | 2/6 | Great at reading + writing the machine surfaces (JSON schemas, llms.txt) but doesn't natively drive a browser |
| (vi) Human-reader UX review | 2/6 | Writing quality is state-of-the-art; loses to Gemini on multimodal screenshot review |

---

### Google — **Gemini 3.1 Pro** (Feb 2026 — current Pro-tier flagship)

**Best at (with source):**
- **Very long context** — 1M tokens, Deep Think reasoning mode, agentic capabilities (llm-stats, theairankings 2026-08-13). First model to break the 1500 Elo barrier on LMArena.
- **Multimodal** — best-in-class image + video understanding; can review actual rendered screenshots and site UI.
- **Reasoning benchmarks** — 91.9% GPQA Diamond, 45.1% ARC-AGI-2 (Deep Think mode) per progressive-robot/ucstrategies.
- **Search integration** — plugged into Google Search's AI Mode and agentic developer tooling.
- **Gemini 3.8 Flash** as the fast/cheap tier (Aug 2026 per felloai) — good for cheap wide-coverage scans.

**Weaknesses / failure modes:**
- **Pro line stalled at 3.1 since February** — vendor's own iteration cadence moved to Flash. The current top Pro model is ~7 months old at this point. Losing benchmark leads to Astra and Fable 5.1.
- **Multimodal input can slip past text-only review** — models "see" images sometimes hallucinate details that aren't there.
- **Google's search citations are stronger than most, but not as tight as Perplexity's** for citation-first workflows.
- **Vendor-reported benchmarks** — many of the "dominates 19/20" claims are Google's own methodology; take with usual caution.

**Access:**
- Gemini app (free tier + Advanced), API via AI Studio and Vertex AI
- Pricing: standard API tier (I'll pin exact rates when we brief the audit); 1M context substantially cheaper than Astra/Fable
- Live URL reading: yes (Google Search integrated)
- Code execution: yes
- Also on Vertex AI, AI Studio

**Fit against our six audit tasks:**
| Task | Rank | Reason |
|---|---|---|
| (i) External-fact verification | 3/6 | Search-integrated + citations; loses to Perplexity's purpose-built shape |
| (ii) Code-path tracing against repo | 4/6 | 1M context lets it hold whole repo; loses to Fable on code-specific benchmarks |
| **(iii) Truth-copy review of rendered pages** | **2/6** | **Multimodal — can literally SEE the rendered pages via screenshots and catch what text-only models miss** |
| (iv) Spec/doctrine review | 4/6 | Long context + reasoning is strong; specifics of writing-quality lag Fable |
| (v) Machine-surface review | 4/6 | Fine; no computer-use tool at Astra's level |
| **(vi) Human-reader UX review** | **1/6** | **Multimodal + long context means it can review WHAT USERS SEE — not just what the code says — better than anyone else on this list** |

---

### xAI — **Grok 4.6** (released 2026-08-12)

**Best at (with source):**
- **Agent-first design + Cursor integration** — xAI positions 4.6 as "agent-first" (geotoolbox.ai, 2026-08). Direct integration with Cursor, Bedrock.
- **Cheapest agent-capable frontier tier** — $2 / M input, $0.5 / M cached, $6 / M output (winzheng, 2026-08-12). Matches GPT-5.6 Soul on several benchmarks per xAI's own claims.
- **Real-time X/Twitter access** — unique among all frontier models. Whatever's on X right now, Grok can see. This is genuinely useful for adversarial reputation checks ("what is Crypto Twitter saying about our site RIGHT NOW?").
- **500K context** — large, though below Astra/Fable/Gemini.

**Weaknesses / failure modes:**
- **Vendor iteration is chaotic** — went from Grok 4 to 4.1 to 4.20 Beta to 4.20 Beta 2 to 4.3 to 4.5 to 4.6 within months. Reliability/consistency across model versions is lower than Anthropic's Fable line.
- **"Approaching GPT-5.6" is a marketing claim**, not an independent verification. Vendor benchmarks dominate.
- **Grok 5 delayed indefinitely** — the 6T-parameter model everyone was expecting is not out.
- **X/Twitter bias in training** may lean toward attention-grabbing / adversarial framing. That's a feature for adversarial audits, a bug for careful truth-copy review.
- **Cyber-safety posture is looser than Anthropic/OpenAI** — worth noting but not immediately relevant to a site audit.

**Access:**
- x.com / Grok app (Premium+), API, Cursor, AWS Bedrock
- API: $2 / M in, $6 / M out — **cheapest frontier here**
- Context: 500K tokens
- Live URL reading: yes (including X real-time)
- Code execution: yes (agent-capable)

**Fit against our six audit tasks:**
| Task | Rank | Reason |
|---|---|---|
| (i) External-fact verification | 5/6 | Real-time X access is unique but noisier than Perplexity's grounded citations |
| (ii) Code-path tracing against repo | 5/6 | Agent-first is good; SWE benchmarks below Fable and Astra |
| (iii) Truth-copy review of rendered pages | 4/6 | Fine; adversarial bias flavors output more than truth-copy work wants |
| **Adversarial-audit / red-team framing** | 1/6 (of a task not in the original six) | **Best at "what would a hostile actor say about this site?" — a natural fit for one specialized round** |
| (v) Machine-surface review | 5/6 | Cursor integration is nice for code but not a full machine-surface driver |
| (vi) Human-reader UX review | 5/6 | Best on "does this survive the CT roast test?"; not on careful readability |

---

### Perplexity — **Sonar Pro / Sonar** (evolving; latest updates 2026-09)

**Best at (with source):**
- **External-fact verification with citations** — Perplexity's Sonar model won the Search Arena evaluation (Perplexity API resources page, benchmarked Mar-Apr 2025 with 10K human preference votes across 11 models). Purpose-built for real-time web search + grounded citations, unlike every other model on this list where web is a tool wrapper.
- **Cheapest per-query real-time-web** — Sonar is $1 / M tokens; Sonar Pro is $3 / M in / $15 / M out. Citation tokens no longer billed as of 2026.
- **"Deep Research" variant** for heavier questions.
- **Model Council + Comet Browser** — the meta-tools have been strong throughout 2026.

**Weaknesses / failure modes:**
- **Not a leader on raw reasoning** — Sonar/Sonar Pro benchmarks below the frontier on GPQA, SWE-bench, etc. It's a search-first model, not a reasoning-first model.
- **Sonar Chat Completions is being renamed to Agent API** — support for the old Sonar endpoint ends **2026-09-27** (Perplexity docs). Migration required if any of our tooling calls the old API.
- **Context windows are shorter** than the frontier — not a fit for "hand it the whole repo" audits.
- **Its findings are as good as its search results** — sometimes surfaces low-quality secondary sources.

**Access:**
- Perplexity Pro (consumer), Sonar API (developer)
- API: Sonar $1 / M; Sonar Pro $3 / M in / $15 / M out
- Live URL reading: **YES, native, with citations** (this is its whole shape)
- Code execution: limited via tools

**Fit against our six audit tasks:**
| Task | Rank | Reason |
|---|---|---|
| **(i) External-fact verification** | **1/6** | **Native web search + citation output. This is Perplexity's entire product** |
| (ii) Code-path tracing against repo | 6/6 | Not what it's built for |
| **(iii) Truth-copy review of rendered pages** | **1/6** | **Can fetch every page, cite what the current source says vs what the site claims. No other model does this without heavy scaffolding** |
| (iv) Spec/doctrine review | 5/6 | Fine; other models do deeper synthesis |
| (v) Machine-surface review | 3/6 | Can fetch and cite `/check.json` etc, but doesn't AGENT through machine surfaces |
| (vi) Human-reader UX review | 4/6 | Can compare our copy to the "typical XRPL user's" mental model based on other sites; not as strong as multimodal Gemini |

---

### Meta — **Llama 5** (released 2026-04-08) + **Muse Spark** (closed sibling)

**Best at (with source):**
- **Open-weight frontier** — 600B+ parameter MoE, 5M-token context window, native video and audio (plainai/aguidetocloud, bitsminds 2026-04-08). Community license is friendlier for commercial use than Llama 4.
- **Near-parity with GPT-5 on key benchmarks** while staying open-weight (agentmarketcap, June 2026). "Recursive Self-Improvement" capability for synthetic data generation.
- **Self-hostable** — the only model on this list that can run on Charlie's own infrastructure (though 600B is a lot of GPU). Everything else sends your prompts to a closed-source cloud.
- **Cheapest sustained load** — for repeated audits over months, self-hosted Llama 5 is dramatically cheaper per query than $10/$50 API calls.

**Weaknesses / failure modes:**
- **6-month-old model at frontier pace** — Fable 5.1 and Astra both leapfrogged it since April. Llama 5's benchmark leadership vs closed models is contested.
- **Open-weight ≠ transparent training** — the weights are public, the training data isn't fully. Auditors care about this.
- **Muse Spark** is the closed sibling that gets Meta's new features first — dual-track means Llama-only workflows may lag Muse Spark features.
- **Deployment complexity** — actually running 600B locally means ~8× H100 or comparable. Not a "sign up and use" model.

**Access:**
- Open weights (Hugging Face, official Meta channels)
- Self-hosted infrastructure OR AWS Bedrock / Together AI / Groq
- API pricing varies by host; roughly $0.30-$1 / M via inference services
- Context: **5M tokens** (largest on this list)
- Live URL reading: no native (tool-required)
- Code execution: yes via tool wrapping

**Fit against our six audit tasks:**
| Task | Rank | Reason |
|---|---|---|
| (i) External-fact verification | 6/6 | No native web |
| (ii) Code-path tracing against repo | 3/6 | 5M context lets it hold the whole repo AND all docs at once, uniquely |
| (iii) Truth-copy review of rendered pages | 5/6 | Fine; no multimodal advantage |
| (iv) Spec/doctrine review | 3/6 | 5M context is a real advantage here — can review the ENTIRE spec + all history at once |
| (v) Machine-surface review | 6/6 | Not agent-native |
| **Sovereignty-audit (self-hostable, no closed-vendor data leak)** | **1/6** | **Unique: the only model that can review the site WITHOUT sending anything to a closed-source cloud. Matters for the "auditors verifying our sovereignty covenant" persona** |

---

## PART 2 — The recommendation

### 2a) Start with **Perplexity Sonar Pro**. Why:

The immediate risk right now is **truth-copy drift** — this week we shipped:
- Anchor #5 (first 7-metric, first sovereign-validation-lookup)
- 8 truth-audit copy fixes across index/pools/methodology/sidechain/mpts/about
- The chain-link defect fix + `docs/CHAIN_LINK_DEFECT_HISTORICAL_2026-09-04.md`
- New copy on /wallet blurb + /pools sourcing

Every one of those is a public claim. **The highest-yield first audit is "does the current site actually say what it should say, and does what it says match what the ledger + code + world actually shows?"** That's Task (i) External-fact verification + Task (iii) Truth-copy review — Perplexity's exact wheelhouse, and it's the only model that natively fetches URLs with citations.

Bonus: Sonar Pro is the cheapest ($3/$15/M) among the frontier-tier reasoning models with citations. We can hit **every route** of the site (~77 of them) in one round without spending real money.

### 2b) Rotation order (with reasoning for each hop):

| Round | Model | Why this hop | Owed output |
|---|---|---|---|
| **1** | **Perplexity Sonar Pro** | External-fact + truth-copy on the current live site. Cheap, cite-first, no code needed. | List of every claim on every page vs cited external source. Everything either verified, contradicted, or "no external source found." |
| **2** | **Claude Fable 5.1** | Code-path trace of every claim Perplexity flagged. Best-in-class SWE-bench. Cache reads 75% cheaper = repeat audit is cheap. Also does spec/doctrine review of the /methodology + covenant docs. | Claim → file:line producer. Flag any claim not backed by code. |
| **3** | **GPT-6 Astra (with computer-use)** | Machine-surface review via actual agentic navigation. Simulate: an AI agent trying to USE our /check.json, .well-known/*, llms.txt, agents.json. Reports where machines get stuck. | Machine-surface UX report. What broke, what was ambiguous, what the LLM-agent had to guess at. |
| **4** | **Gemini 3.1 Pro (multimodal)** | Human-reader UX pass via screenshots + long-context review of entire templates/ + docs/ at once. Finds cross-page inconsistencies invisible to route-by-route audits. Reviews rendered visuals, not just source. | Cross-cutting inconsistency list + human-UX findings. |
| **5** | **Grok 4.6** | Adversarial pass. "Roast this site as if you were a hostile Crypto Twitter thread." Uses real-time X access to check: is anyone actually saying anything about xrpldashboard? Are those claims accurate? Cheapest adversarial round. | Adversarial findings + reputation-signal report. |
| **6** | **Llama 5 (self-hosted or via Bedrock)** | Sovereignty audit. The one model that doesn't require sending our repo to a closed vendor. Reviews the site from the persona of "a researcher who won't trust closed AI vendors' privacy claims." Also serves as the sustained-load model for CI auditing after the initial 6-round burst. | Sovereignty-persona findings + baseline "can this be audited without closed vendors?" report. |

### 2c) Shape — specialization in sequence (Charlie's ruling, 2026-09-04 evening)

Rejecting the "same fixed brief to all six" framing. **Each model gets a DIFFERENT job, matched to its distinct strengths, with a prompt written specifically for that job.** Each round's verified findings shape the NEXT model's brief. Specialization in sequence, not six parallel auditors.

Why this shape:
- The per-model role table in 2b already assigns distinct jobs (outside-view, code-trace, machine-surface, human-UX, adversarial, sovereignty). Wasting a model on someone else's job is expected-yield-negative.
- Findings compound: Round 2 (Fable code-trace) is more valuable when it's targeted at claims Round 1 (Perplexity) flagged as externally-suspect. Same for later rounds.
- Relay drift risk (Claude's original concern) is real but mitigated by the evidence rule: findings must carry a reproducible `evidence_anchor`. Interpretation doesn't propagate; only verifiable facts do.

**The relay protocol between rounds:**
- Round N runs. Findings arrive in the YAML shape.
- JJ + Charlie reconcile Round N's findings: verified, single-source-pending-verify, dropped-no-anchor.
- Round N+1's prompt is written FRESH, bespoke to that model's strengths, and includes:
  - The `verified` findings from Round N as CONTEXT ("these are the confirmed issues; here's what changed since Round N ran"),
  - The `single-source-pending-verify` findings as OPEN QUESTIONS ("verify or refute these"),
  - The distinct job Round N+1 is doing (code-trace / machine-surface / etc.),
  - The evidence rule (unchanged across rounds).
- Round N+1 does NOT see prior models' outputs verbatim — only the reconciled verified list. Prevents laundering one model's interpretation into another's premise.

**Round-1 prompt is bespoke to Perplexity's outside-view strength.** Not a general brief. Written per round, per model, per what that model is best positioned to find.

### 2d) Evidence rule — normalization to a single format

**A finding only counts if it has a reproducible evidence anchor.** All six model outputs get normalized to this shape:

```yaml
finding_id: <auto-generated>
model: <perplexity|fable-5-1|gpt-6-astra|gemini-3-1-pro|grok-4-6|llama-5>
category: truth-violation | missing-disclosure | stale-copy | machine-parity-gap | ux-issue | adversarial-concern | sovereignty-gap
evidence_anchor:
  type: file-line | url | ledger-tx-hash
  value: <exact reference — templates/index.html:846, https://xrpldashboard.com/methodology, tx-hash-A3F2...>
claim: <what the page or code says, verbatim>
reality: <what's actually true, with source>
reproduce_command: <exact bash/curl/grep that shows the discrepancy>
fix_category: copy | code | doctrine | infrastructure | n/a
severity: blocker | major | minor | cosmetic
```

**Reconciliation logic:**
- Two or more models finding the SAME `evidence_anchor` + `claim` = auto-promoted to verified.
- One model finding it = tagged "single-source", subject to JJ verification before addressing.
- If a finding has no valid `evidence_anchor` OR `reproduce_command` fails → dropped, not counted. **Automatic filter, no human reconciliation needed for bad findings.**

Each model gets the same brief that includes this exact schema. Non-conforming output gets rejected before it reaches the consensus merge.

### 2e) Freeze windows / cadence — target "zero verified findings" by mid-September

Given today is 2026-09-04 (Fri), the specialization-in-sequence shape means each round runs against a frozen baseline, findings reconcile between rounds, and the next round's prompt is written against the reconciled findings + a fresh baseline commit.

| Window | Date(s) | Action |
|---|---|---|
| **R1 freeze** | Sep 5-6 (Sat-Sun) | Baseline = commit `292737c` (anchor #5). Round 1 runs Perplexity outside-view. Reconcile Sun PM. |
| **R1 fix** | Sep 7 (Mon) | Apply verified R1 findings. New baseline commit. |
| **R2 freeze** | Sep 8-9 (Tue-Wed) | Round 2 runs Fable code-trace against R1's flagged claims + new baseline. Reconcile Wed PM. |
| **R2 fix** | Sep 10 (Thu) | Apply verified R2 findings. New baseline. |
| **R3 freeze** | Sep 11-12 (Fri-Sat) | Round 3 runs Astra computer-use machine-surface audit against new baseline. Reconcile Sat PM. |
| **R3 fix** | Sep 13 (Sun) | Apply verified R3 findings. New baseline. |
| **R4 freeze** | Sep 14-15 (Mon-Tue) | Round 4 runs Gemini multimodal / cross-cutting audit. Reconcile Tue PM. |
| **R4 fix + convergence check** | Sep 15+ (Wed onward) | Apply verified R4 findings. If yield still worth the round, run R5 (Grok adversarial). If R4 already at zero-new-blockers, park R5/R6 for post-launch. |

Round 5 (Grok adversarial) and Round 6 (Llama sovereignty) are opt-in tail rounds — run them if convergence is stalling OR if you specifically want the adversarial / sovereignty-persona perspectives. Not required to declare victory.

**Convergence definition:** A round has converged when: (a) every reported finding is either verified-and-fixed OR verified-and-explicitly-parked (Charlie decision, documented) OR verified-as-not-a-finding-after-code-review, AND (b) subsequent rounds' finding count is decreasing.

**Don't rush the target — the target is honest zero, not a Sep 15 checkbox.** If any round introduces net-new blocker-severity findings, we extend cleanly rather than declaring convergence artificially.

---

## PART 3 — Two audiences (humans + machines)

For Charlie's stated goal:
> "If humans need data and don't know where to get it, we have it. If machines need data for whatever reason, we have it."

**Best-for-humans reviewers:**
- **Gemini 3.1 Pro (multimodal)** — sees the rendered page as a human does. Only model that can review actual screenshots + video walkthroughs. Best for "is this site pleasant to use?"
- **Claude Fable 5.1** — best writing-quality reviewer. Best for "does this copy read well? Does it treat me like a first-time visitor?"

**Best-for-machines reviewers:**
- **GPT-6 Astra (computer-use)** — agentically drives the site the way a real AI agent would. Best for "does this actually work when a machine tries to use it?"
- **Perplexity Sonar Pro** — cites the machine-readable surfaces (`/check.json`, `.well-known/*`, `llms.txt`, `agents.json`) and checks them for parity + freshness. Best for "does the machine surface match the human surface?"
- **Claude Fable 5.1 (via MCP tool wrapping)** — best at auditing our JSON schemas + envelope shapes end-to-end.

**Day-one question for each model:**

- **Perplexity Sonar Pro:** *"For xrpldashboard.com, list every numerical claim on the homepage, /methodology, and /rlusd, and for each cite whether the value matches your independent web check. Cite sources. Report only claims where you can produce an authoritative external cite."*

- **Claude Fable 5.1:** *"Given repo commit 292737c, trace every 'anchored metric' claim in /methodology to its producing code path. Report file:line for each. Flag any claim not backed by the code, and any code that produces something not claimed on the page."*

- **GPT-6 Astra (computer-use):** *"Use computer-use to navigate xrpldashboard.com as a first-time AI agent. Complete these tasks: (1) look up the balance of `rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ`, (2) find the tx hash of the most recent anchor, (3) find the chain_root for 2026-09-04. Report friction, dead-ends, machine-surface parity gaps, ambiguous copy."*

- **Gemini 3.1 Pro:** *"Load `templates/`, `docs/`, `app.py`, and the .well-known files into your 1M context. Find every place where two facts about the same thing disagree — a cadence stated as '15 min' on one page and '5 min' on another, a source described as 'own node' in one spot and 'public s1' in another. Report file:line pairs. Also: for a first-time human visitor, what will they miss?"*

- **Grok 4.6:** *"Adversarial audit of xrpldashboard.com. Two parts: (1) real-time X/Twitter search — is anyone talking about xrpldashboard.com right now? What are they saying? Are those claims accurate against the site? (2) You are a hostile Crypto Twitter thread trying to discredit this project. What are the strongest angles? What does the site fail to preempt?"*

- **Llama 5 (self-hosted):** *"Sovereignty audit. Persona: a privacy-conscious XRPL researcher who won't trust closed AI vendors. Answer: (1) What data on xrpldashboard.com is verifiable end-to-end without sending anything to a closed-source cloud service? (2) What data is NOT? (3) For each 'NOT' item, what would need to change for a researcher like you to trust it?"*

---

## Uncertainties and caveats

- **Vendor benchmarks dominate the public data on all six models.** Independent evaluations are 1-4 weeks behind releases. GPT-6 Astra is 24 hours old at the time of this writing; treat its "AGI-era" framing with skepticism.
- **Perplexity's Sonar-endpoint sunset (2026-09-27)** happens mid-audit window. Rounds using Sonar should migrate to the new Agent API before Sep 27 or use Sonar Pro throughout.
- **Grok 5 is not out.** All Grok recommendations assume Grok 4.6.
- **Access is not universal.** Charlie will need existing accounts + API keys for OpenAI, Anthropic, Google, xAI, Perplexity, and either Meta AI Studio access or a self-hosting stack for Llama 5. This is a real up-front cost.
- **My knowledge cutoff is January 2026.** Everything current-model in this doc came from web searches this afternoon, cited below. If I'm wrong about a spec, the source URL is a rebuttal target.
- **The parallel-with-staged-consensus approach is my recommendation; Claude has previously recommended pure parallel to Charlie**. The gap is small; if he prefers pure parallel, that's fine — the risk is diminishing returns on Round N when it can't build on Round N-1's verified findings.

---

## Sources

Retrieved 2026-09-04 EDT via DuckDuckGo:

**OpenAI GPT-6 Astra:**
- OpenAI product page — https://openai.com/index/gpt-6-astra/ (2026-09-03)
- CNBC — https://www.cnbc.com/2026/09/03/open-ai-astra-gpt-6-cyber.html (2026-09-03)
- Coursiv — https://coursiv.io/blog/openai-astra (2026-09-03; pricing + rollout)
- llm-stats — https://llm-stats.com/models/gpt-6-astra (2026-09; specs)
- HokAI — https://hokai.io/hub/models/gpt-6-astra (2026-09-03; 96% GPQA)
- BetaNews — https://betanews.com/article/openai-gpt-6-astra-agi-era/ (2026-09-03; AGI framing)
- Forbes — https://www.forbes.com/sites/ronschmelzer/2026/09/03/openai-announces-gpt-6-astra-or-does-it/ (2026-09-03; contested framing)
- alphacorp — https://alphacorp.ai/blog/gpt-6-astra-launch-benchmarks-pricing-and-everything-you-need-to-know (Critical cyber, knowledge cutoff)

**Anthropic Claude Fable 5.1 / Mythos 5.1:**
- Anthropic launch page — https://www.anthropic.com/claude-fable-and-mythos-5-1 (2026-09-01)
- Anthropic Mythos page — https://www.anthropic.com/claude/mythos (85% fewer bio refusals on benign)
- Marktechpost — https://www.marktechpost.com/2026/09/01/anthropic-releases-claude-fable-5-1-and-claude-mythos-5-1-52-6-on-terminal-bench-science-and-75-cheaper-cache-reads/ (2026-09-01)
- MacRumors — https://www.macrumors.com/2026/09/01/anthropic-claude-fable-5-1/ (2026-09-01)
- AWS Bedrock launch — https://aws.amazon.com/about-aws/whats-new/2026/09/claude-fable-5-1-aws/ (2026-09-01)
- Anthropic docs — https://platform.claude.com/docs/en/models/fable-5-1/overview (specs)
- HokAI — https://hokai.io/hub/models/claude-fable-5.1 (81.2% SWE-bench Pro, 1M context)
- DataCamp — https://www.datacamp.com/blog/claude-fable-5-1 (Terminal-Bench-Science 2x jump)
- OpenRouter — https://openrouter.ai/anthropic/claude-fable-5.1 (agentic workflow strengths)

**Google Gemini 3.1 Pro:**
- Vellum — https://www.vellum.ai/blog/google-gemini-3-benchmarks (benchmark analysis)
- llm-stats — https://llm-stats.com/blog/research/gemini-3-pro-launch (1M context, Deep Think)
- theairankings — https://theairankings.com/google/gemini-3-pro/ (1501 Elo)
- ProgressiveRobot — https://www.progressiverobot.com/2026/08/13/gemini-3-1-pro/ (2026-08-13; current top Pro)
- felloai — https://felloai.com/ultimate-gemini-model-comparison/ (Flash raced to 3.8, Pro stalled at 3.1)
- ucstrategies — https://ucstrategies.com/news/gemini-3-pro-guide-benchmarks-api-pricing-deep-think-mode-2026/ (91.9% GPQA)

**xAI Grok 4.6:**
- benchlm — https://benchlm.ai/models/grok-4-6 (top xAI model per benchmarks Sep 2026)
- codersera — https://codersera.com/blog/grok-4-6-launch-guide-2026/ (2026-08-12 launch, 500K context)
- geotoolbox — https://geotoolbox.ai/blog/grok-4-6 (agent-first framing)
- winzheng — https://www.winzheng.com/en/article/grok-46-xai-release-pricing-competition ($2/$6 pricing)

**Perplexity Sonar:**
- Perplexity Search Arena results — https://www.perplexity.ai/api-platform/resources/perplexity-sonar-dominates-new-search-arena-evaluation
- Perplexity docs — https://docs.perplexity.ai/docs/sonar/models/sonar (Sep 27 2026 sunset)
- Perplexity model card — https://docs.perplexity.ai/docs/sonar/models
- Sonar Pro pricing — https://www.leadscalc.com/calculators/ai/models/sonar-pro ($3/$15/M)

**Meta Llama 5:**
- Plain AI — https://plainai.aguidetocloud.com/changes/meta-llama-5/ (2026-04-08 launch, 600B MoE, 5M context)
- bitsminds — https://www.bitsminds.com/news/meta-llama-5-open-source-600b-2026 (2026-04-08 announcement)
- swfte — https://www.swfte.com/blog/llama-5-open-weight-deep-dive-2026 ("return to frontier")
- agentmarketcap — https://agentmarketcap.ai/blog/2026/06/06/llama-5-open-source-agent-economics-meta-superintelligence (near-parity claim)
- startuphub — https://www.startuphub.ai/ai-news/ai-figures/2026/figure-mark-zuckerberg-dual-track-open-closed-2026-06-05 (Muse Spark closed sibling)

---

## Round 1 — Perplexity's job: THE OUTSIDE VIEW

**Prompt (draft for Charlie's approval before execution):**

```
You are auditing xrpldashboard.com from the outside — as the world sees it. Your job is not to inspect our code or infer how our data is produced; you cannot see either. Your job is to answer, with citations, whether this site serves its two intended audiences well, and where it does not.

## Context (what the site is)

xrpldashboard.com is a free educational data dashboard for the XRP Ledger (XRPL), built to serve two audiences:

- HUMANS who need XRPL data and don't know where else to get it — first-time visitors, researchers, XRPL developers, casual observers wanting to understand XRPL activity. The site is free for humans and will remain free.
- MACHINES who need XRPL data — AI agents, LLM tools, scrapers, downstream services. Machine access is currently free but will become paid at some point in the near future. Machine-readable surfaces exist today at /check.json, /.well-known/snapshots/*, /.well-known/security.txt, llms.txt, agents.json, and route-specific JSON endpoints.

The site makes a sovereignty claim: data on anchored pages originates from the operator's own rippled node (an XRPL full node the operator runs), not from third-party public infrastructure. This claim is backed by weekly on-ledger anchor transactions that sign the day's data snapshot to XRPL. Anchor #5 landed today (2026-09-04) at ledger 106,764,148, tx BB72E91012DA3B05C060FAD64DD405DC09A8BAB3EC1A079AFD82367D65A3B44B, committing chain_root 8e259732deab4050bab0c2af4a6e184949627670c3109fccbe049007d162abbb. This is the first 7-metric anchor (previously 12; two Ethereum-side metrics were removed because they can't meet the "originated from our own node" covenant).

## Your job (six parts)

For each: cite sources with URLs. Use direct fetches of xrpldashboard.com pages and any external XRPL sites you can reach. Distinguish what you can verify from what you cannot.

(a) FIRST-VISIT WALKTHROUGH — HUMAN. Look at xrpldashboard.com as a first-time human visitor. Walk the homepage, /about, /methodology, /rlusd, /pools, /amendments, /whales, /nfts, /lending, /wallet, /check, and any other surface a first-time visitor would encounter. Describe what a first-time visitor experiences and where they get stuck: what's unclear, what's missing, what's ambiguous, what's harder to find than it should be. Where possible, cite comparable pages on competing sites showing what worked better there.

(b) FIRST-VISIT WALKTHROUGH — MACHINE. Look at the machine-readable surfaces:
- /llms.txt
- /agents.json
- /.well-known/security.txt
- /.well-known/snapshots/2026-09-04.json (and chain.json)
- /check.json (try with q=<any XRPL account>)
- Any other .well-known or route.json you can find
Describe what a first-time AI agent / researcher / scraper trying to programmatically consume XRPL data from us would experience. Where are the machine surfaces discoverable? Where are they missing? Where is documentation ambiguous, missing, or contradictory between the human page and the machine surface? Where would an autonomous agent get stuck or make a wrong guess?

(c) COMPETITIVE COMPARISON. Compare xrpldashboard.com against every other public XRPL data source you can find that people actually use: XRPScan, Bithomp, XRPL Meta, xrpl.org tooling, xrplf.org's amendment tracker, Sologenic, XRPCharts, and anything else that shows up in search. For each competitor site, note what humans and machines can get there that they cannot get from xrpldashboard.com, and vice versa. Cite each competitor's URL.

(d) HUMAN SEARCH INTENT. Identify what humans are actually searching for around XRPL data — recent Reddit/Xrpchat/xrpforum questions, Stack Exchange XRPL questions, X (Twitter) common questions, YouTube comment questions, comments on XRPScan/Bithomp. For each search intent that shows up multiple times, note whether xrpldashboard.com surfaces an answer, and where. Cite the sources where those questions live.

(e) MACHINE / AGENT NEEDS. What do AI agents, researchers, downstream services need from an XRPL data source? Consider:
  - Discoverability (can an agent FIND the machine surfaces without a human pointing at them?)
  - Machine-readable manifests (schema, versioning, freshness contracts)
  - Signed / provenance-verifiable data (cryptographic signatures, ledger anchors)
  - Rate-limit clarity (what limits, communicated where?)
  - Pricing clarity (currently free, becoming paid — is that stated? where? in a machine-readable form?)
  - Access mechanics (API keys, authentication, error responses)
For each need, note whether xrpldashboard.com currently exposes it in a machine-consumable way.

(f) EXTERNAL-CLAIM CROSS-CHECK. Read every claim on the site that is externally verifiable (XRPL account balances, transaction hashes, amendment states, anchor tx hashes, ledger indices, cross-chain references). For each claim, either verify it against an independent source (XRPScan / Bithomp / xrpl.org / etc.) or flag that it can't be verified externally. Cite the independent source in every case. Flag any contradiction between what our site claims and what an independent XRPL source shows.

## What you are NOT qualified to judge from outside

You cannot see our repository, our node's actual routing, our data-pipeline internals, or how any specific value is produced end-to-end. Do NOT make claims like "this value is sourced from public RPC" or "this walker runs on X cadence" — those are internal facts you can only infer. Label ANY internal-inference statement as `outside_inference: unverified`. The next audit round handles inward-facing questions.

## Access failures are findings

Report every page or endpoint you attempted and could NOT access — blocked, timed out, error, or bot-challenge — with the URL and what you saw. An unreachable page is a finding, not a skip.

## Date-stamp every observation

Every observation must include the date you accessed the page (e.g. "as seen 2026-09-05"), so findings can be re-checked after fixes.

## Output format

Return findings in this exact YAML shape. One YAML document per finding. Do not summarize — enumerate every finding, however small.

---
finding_id: <auto-numbered, R1-001, R1-002, …>
category: human-ux-gap | machine-discoverability-gap | contradictory-claim | missing-competitor-parity | search-intent-unmet | machine-surface-gap | external-claim-fail | outside-inference
audience: human | machine | both
evidence_anchor:
  type: url | search-result-url | competitor-url | xrpl-ledger-tx
  value: <exact URL>
  fetched_at: <ISO timestamp when you retrieved it>
claim_or_state: <what our page says or does, verbatim if possible, quoted>
observation: <what's lacking, contradictory, missing, ambiguous, or verifiable/unverifiable>
external_source_cite: <URL of the independent source proving your observation, if applicable>
proposed_improvement: <one-sentence improvement in plain English>
impact_for_humans: blocker | major | minor | cosmetic | n/a
impact_for_machines: blocker | major | minor | cosmetic | n/a
outside_inference: false | true  # true if you're guessing at internals
---

After listing all findings, provide TWO ranked lists:
1. Top 10 findings by human impact (blocker+major first).
2. Top 10 findings by machine impact (blocker+major first).

Then a closing summary in three lines: what xrpldashboard does well relative to competitors, what it lacks most, what would move the needle most for each audience.
```

Charlie approval status: **awaiting**. Once approved, execute against Sonar Pro (via API if `PERPLEXITY_API_KEY` set, otherwise via Perplexity Pro web app with the prompt copy-pasted).

### Access checklist for Round 1

| Prerequisite | Status | Action if missing |
|---|---|---|
| Perplexity Pro account or Sonar Pro API key | **unknown as of 2026-09-04 EDT evening — no env var set on this Mac** | Sign up at perplexity.ai (Pro $20/mo) OR at perplexity.ai/settings/api (API keys, $3/$15/M Sonar Pro) |
| API key (if programmatic) | Would live at `~/.config/xrpldashboard/env` as `PERPLEXITY_API_KEY=...` | Rotate on same discipline as other secrets — never printed in chat |

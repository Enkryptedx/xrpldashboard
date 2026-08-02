# Glow Application — DRAFT

**Status:** DRAFT. Charlie gates before any submit. No form fields
have been filled on the Glow platform itself; this file exists to
line up the answers so the platform submission is a paste-and-review
rather than a compose-from-scratch.

**Program:** [Glow](https://glow-docs.xrpl-commons.org/), retroactive
funding for XRPL public-good contributions, run by XRPL Commons.

**Wave / window:** Wave #5, application window **open through
2026-08-31** (29 days from today). This is the current live window
per the Glow docs site at time of drafting (2026-08-02).

**Retroactive scope:** Work performed in the last six months
(2026-02-02 → 2026-08-02). xrpldashboard has **535 commits** in this
window per `git log --since='2026-02-02'`.

**Nomination path:** Glow accepts both Scout-nominated and
self-nominated contributors. Charlie's decision: self-nominate now or
wait for a Scout — either lands in the same platform application form.

---

## Field-by-field draft

Per Glow's live docs, contributors fill:

1. Existing-project selection or create-new
2. Detailed contribution description
3. Project category selection
4. Ecosystem value explanation
5. Employment independence disclosure
6. Wallet connection + KYC (platform-side, not draftable here)

Answers below map to fields 1–5. Fields marked **[CHARLIE INPUT]**
need his word before final.

---

### Field 1 — Project

**Project name:** xrpldashboard

**Project URL:** https://xrpldashboard.com

**Repository:** [CHARLIE INPUT — public GitHub URL, if Charlie wants
to name it; the repo is currently local + Render, not clear if the
GH URL is the intended public front door]

**Existing on Glow?** [CHARLIE INPUT — check the platform. If a
Scout has already created an entry, use it; otherwise create new]

---

### Field 2 — Detailed contribution description

The 2026-02-02 → 2026-08-02 window landed two shipped systems on
xrpldashboard, both free and MIT-licensed:

**1. Four-layer self-audit system.** Instead of asserting "the numbers
are right," the site publishes the mechanism by which they prove
themselves. Every number on the site can be traced through:
- **Layer 1** (source): the walker that computed it, with commit hash
- **Layer 2** (freshness): a walker that watches the walker, so a
  53-day stall doesn't repeat (the founding case: RLUSD supply
  reported as flat for 53 straight days before this layer existed)
- **Layer 3** (cross-check): independent recomputation from a second
  source (e.g. on-chain snapshot vs. postgres cache)
- **Layer 4** (claims manifest): `CLAIMS.yaml` — every human-facing
  fact on the site is a named claim with a source, a cross-check
  policy, and a change-safety rule. Modifying the fact without
  updating the manifest is a build error.

Layer 2's first live catch (2026-07-22) was a 24-hour bug in
`rlusd_xrpl_net_change_24h` reported to a canary within hours, root-
caused and closed the same day. The prior class of "silently wrong
for weeks" bug is now bounded to ≤24h + one walker cycle.

**2. Agent tier — the first MCP server whose data proves itself.**
Shipped over seven build days (2026-07-29 → 2026-08-02), the agent
tier makes xrpldashboard directly consumable by AI agents (Claude,
ChatGPT, etc.) via the Model Context Protocol. Fifteen tools, each
wrapped in a machine-readable envelope with:
- `proof.source` — where the number came from
- `proof.as_of` — when it was true
- `proof.freshness_contract` — the promise ("≤5min", "daily", etc.)
- `proof.methodology_url` — a same-commit link to how it's computed
- `proof.claims_ref` — a link to the CLAIMS.yaml entry
- `proof.honest_partial` + `scope_note` — flag when the response
  isn't the full picture
- For any tool naming a third party (e.g., Ripple as RLUSD issuer),
  a `dispute_contact_url` inside the payload

Flagship tool pair (#18 + #19, shipped 2026-08-02):
`get_signed_snapshot` returns the day's data with an Ed25519 signature
+ Merkle audit path; `verify_snapshot_signature` verifies any envelope
statelessly. The writer and the verifier share no state. A caller can
screenshot today and prove six months from now that the number wasn't
silently changed. The pubkey is published on **two independent
external channels** (HTTPS `.well-known/snapshots/pubkey.pem` +
DNS TXT `_xrpld-snapshot-key.xrpldashboard.com`).

**Ship-gate demo receipt.** On 2026-08-02 14:24–14:50 EDT, four
prompts ran through Claude Desktop as an off-the-shelf MCP client:
envelope surfacing, third-party dispute URL, round-trip verification
(`verify_result: true`), single-bit tamper case (`verify_result:
false`, specific reason). Textual log with `[FROM-TRANSCRIPT]` /
`[FROM-INVENTORY]` / `[FROM-USER-REPORT]` / `[RECONSTRUCTED]`
provenance marks: `docs/agent_tier_ship_evidence/RUN_LOG.md`.

**Commits (representative, not exhaustive):**
- `2acda81` — agent tier design doc (2026-07-28 kickoff)
- Day 1: `0f712a4` `/llms.txt`, `e2b85d1` `/.well-known/agents.json`,
  `13bd728` methodology "For AI agents" section, `14ce55a`
  single-source freshness constant
- Day 5: `8bf4bf0` — OpenAPI 3.0.3 spec with
  `AGENT_TIER_MCP_INVENTORY` as source of truth
- Day 6: `d6b2b25` — rate limits + fleet-block extension +
  AI-crawler audit header (the free-for-agents, cost-for-abuse
  posture)
- Day 7: `8a5f53a` — signed-snapshot MCP tools #18 + #19
- Day 7: `60f3f1c` — ship-gate demo packet
- Day 7 tail: `acfabd1` — RUN_LOG.md
- Sibling in-window work worth naming: `db38ecc` `d0e4e50` (RLUSD
  false-flat closure), `12f2c94` (reconciliation-surface advance
  trigger), `7ea0b22` (bot_hashes advance-trigger, third sibling in
  the class), `c7a84b0` (is_bot column flip: 400ms → 19ms cold path,
  a 21× improvement on the analytics page)

---

### Field 3 — Project category

Best-fit primary category per Glow's seven-category taxonomy:

**Primary: Community Tools** — xrpldashboard is a free XRPL
explorer/analytical utility that anyone can consult without an
account, a key, or a wallet connect.

**Secondary: Infrastructure** — the agent tier MCP server is
infrastructure for AI-agent access to XRPL data. The signed-snapshot
subsystem is a verification primitive other projects can lift
(pubkey + verifier code are both public).

**Tertiary: Documentation & Examples** — `/methodology`,
`/regulation`, `/coverage`, `/llms.txt`, `/.well-known/agents.json`,
and the CLAIMS.yaml manifest itself are all live documentation
surfaces.

---

### Field 4 — Ecosystem value

Three concrete asks XRPL developers or AI agents can already make
against xrpldashboard, that they couldn't easily make against
alternatives:

**1. "Prove the number, don't just assert it."** Every stat on the
site is traceable to a source, a cross-check, and a CLAIMS.yaml
entry. If a stat drifts silently, the site says so (`honest_partial:
true`, `scope_note`). This is the model the rest of the ecosystem
can copy — `CLAIMS.yaml` is a portable pattern, not a proprietary
gate.

**2. "Give me the data as a machine, not a screen."** The 15 MCP
tools turn xrpldashboard into a first-class data source for AI
agents. No competitor in the XRPL analytics space currently ships an
MCP server as of 2026-08-02. A Claude, ChatGPT, or bespoke agent can
plug in via stdio or streamable-http and consume XRPL data through a
machine-legible envelope, with rate limits designed to be
free-for-good-actors and cost-for-abuse.

**3. "Prove my archived answer wasn't tampered with."** The
signed-snapshot moat means an agent (or a human) can save today's
answer and, arbitrarily later, cryptographically prove no one
silently changed the underlying number. This is the receipt an AI
citation depends on: "you told me X on 2026-08-01" isn't a claim
that ages well without a signature. Now it does.

**Adjacent public goods shipped in the same window** (all free, all
MIT):
- `/regulation` — plain-English CLARITY Act tracker with calendar
  triggers (a public-good briefing page most XRPL users have no
  other source for)
- `/check` — address-signal aggregator with OFAC SDN, domain age,
  earliest SSL cert, and FCRA disclaimers (Phase 1; Phase 2 gated
  on legal consult)
- `/whales` — real-time large-transfer surfacer with cache-headers
  discipline for AI-citation accuracy

**Test discipline.** 74/74 agent-tier tests green; 6/6 ship-gate bar
walk PASS as of 2026-08-02.

---

### Field 5 — Employment independence

**[CHARLIE INPUT REQUIRED — this is the honesty gate.]**

Draft language, subject to Charlie's rewrite based on his actual
situation:

> This work was performed independently, outside any employment
> relationship. xrpldashboard is a personal open-source project
> (repository under Charlie Bruce, MIT licensed, no employer
> assignment). No salary, contract, or grant has funded the work
> covered by this application to date.
>
> Roadmap disclosure: a paid agent-tier is under consideration as
> a future revenue stream to sustain infrastructure costs (rippled
> node, Neon Postgres, Render). **Nothing shipped in the retroactive
> window is paywalled, and no paywalling of shipped work is planned.**
> The Glow eligibility rule that "freely available to the community"
> is required is honored in writing: the tier structure, if it ever
> exists, will add capacity for paying agents on top of an unchanged
> free surface, not remove capacity from the free surface.

Charlie: replace the first paragraph with your own words about your
current employment / income situation. The roadmap-disclosure
paragraph is the §1 verdict's condition, honored in writing per the
prior standing decision.

---

## Receipts appendix — for the reviewer

Everything below is publicly verifiable at the URLs / commits named,
no login required.

**Verify the code:**
- Repository: [CHARLIE INPUT — public GitHub URL]
- License: MIT (see `LICENSE`)
- Full test suite: `pytest tests/` — 74/74 pass in ~17s

**Verify the site is running the code:**
- Homepage: https://xrpldashboard.com
- Methodology: https://xrpldashboard.com/methodology
- Agent-tier discovery: https://xrpldashboard.com/llms.txt
- Agents.json:
  https://xrpldashboard.com/.well-known/agents.json
- OpenAPI spec: https://xrpldashboard.com/api/openapi.json
- CLAIMS.yaml manifest: `CLAIMS.yaml` in repo root

**Verify the moat:**
- Signed-snapshot pubkey (HTTPS):
  https://xrpldashboard.com/.well-known/snapshots/pubkey.pem
- Signed-snapshot pubkey (DNS):
  `dig _xrpld-snapshot-key.xrpldashboard.com TXT`
- Fingerprint (both channels): `7F:D4:F2:F4:D2:57:7C:BE`
- Live demo transcript:
  `docs/agent_tier_ship_evidence/RUN_LOG.md`
- Demo packet (prompts + config):
  `docs/agent_tier_ship_evidence/DEMO_PACKET.md`

**Verify the transparency infrastructure:**
- Four-layer audit design: `docs/TRUTH_AUDIT_DESIGN.md`
- Working-tree discipline: `docs/WORKING_TREE_DISCIPLINE.md`
- Monitor audit (Q3 2026): `docs/MONITOR_AUDIT_2026-07.md`
- Layer-2 first-catch writeup:
  `docs/LAYER2_FIRST_CATCH_RLUSD_2026-07-22.md` (approx path;
  Charlie confirms exact filename)

---

## Notes for Charlie (before submitting)

**Known unknowns that need your input:**
1. Public GitHub URL (Field 1 + Receipts appendix)
2. Wallet address you want to attach for KYC / disbursement
3. Employment-independence exact wording (Field 5)
4. Whether to self-nominate now or wait for a Scout
5. Whether you want any commit hashes above swapped in/out (I picked
   the most representative ~15; the window has 535 to choose from)

**Suggested timing.** The application window closes 2026-08-31.
Suggest submitting no earlier than after the six PNG screenshots
land in `docs/agent_tier_ship_evidence/` — the RUN_LOG is textual
evidence, the PNGs are visual receipts, and the submission is
strongest with both. If the PNGs don't land, this draft stands on
its own — but it's a weaker version of itself.

**Do NOT submit yet.** This is a paste-and-review draft. Read
end-to-end, mark up, then either edit here or drop your rewrite in
and I'll re-pass.

---

## Sources (draft evidence provenance)

- [Glow docs — main site](https://glow-docs.xrpl-commons.org/)
- [Glow docs — project eligibility](https://glow-docs.xrpl-commons.org/project-eligibility)
- [XRPL Commons — Why Glow](https://www.xrpl-commons.org/newsroom/why-glow-celebrating-xrpl-contributors-2)
- [Glow application entry point](https://glow.xrpl-commons.org/)

---

*Draft prepared 2026-08-02 as part of the Day 7 ship-gate close.
Charlie owns the submission decision, the timing, and the final
wording of Field 5.*

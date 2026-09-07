# Round 3 prompt — ChatGPT / GPT-6 Astra (BE THE MACHINE)

Not yet run. Charlie approves the prompt shape, then runs it himself on ChatGPT (Path A, web app).
Round 2 (Fable code-trace) parked — Round 3 goes first per Charlie's 2026-09-04 ruling.

---

## Access check

**Does Charlie's ChatGPT plan have GPT-6 Astra?** Rolling out to Plus/Pro/Business/Enterprise starting **Sep 3, 2026** (source: coursiv.io/blog/openai-astra + OpenAI product page). Free-tier accounts will see the older model line for weeks/months.

- If **Plus / Pro / Business / Enterprise**: Astra should be in the model picker. Pick it. Enable the "**Agent**" / "**Browsing**" / "**Computer-use**" mode (label varies by rollout wave).
- If **Free**: highest available is likely GPT-5.6 Sol/Terra/Luna. The prompt below still works — Astra's edge is computer-use fidelity, but any browsing-capable model can do a read-the-docs / make-the-calls / report exercise. Note in Charlie's reply which model was actually used.

---

## Paste-ready prompt (single plain-text block for Charlie's phone copy)

Copy the entire block below between the `═══` lines. No markdown headers inside, no nested lists — flowing paragraphs that survive a phone paste.

═══════════════════════════════════════════════════════════════

You are auditing xrpldashboard.com as a machine. Not as a code reviewer, not as a UX reviewer — as an autonomous AI agent that needs reliable XRP Ledger data with cryptographic provenance and has never heard of this site before. Your job is to become its customer unaided and report what breaks.

Environment note. You are running as ChatGPT free-tier — no agent-mode / computer-use tool, no code interpreter. Do parts 1, 2, 5, 7, 8 via web browsing alone (if the browse tool is available; if not, say so and stop). For part 3 fetch what you can via URL navigation, one endpoint at a time. For part 4 (signature verification) you cannot actually run crypto code — reason about it textually and mark that finding "needs-live-verification: requires code execution" instead of claiming to have verified. For part 6, send only a small number of malformed requests (three or four), not a rate-limit stress test. Be honest about the ceiling of what you can prove versus what you can only observe.

Context you need. xrpldashboard.com is a free educational XRPL data dashboard for two audiences. Humans get plain-language explainers plus live dashboards; that is free forever. Machines get discovery documents, machine-readable manifests, signed daily snapshots, and route-specific JSON endpoints; this is currently free but will become paid at some point in the near future. The site makes a sovereignty claim: anchored data originates from the operator's own rippled node, not third-party public infrastructure. Anchor #5 was stamped to the XRP Ledger on 2026-09-04 at ledger 106,764,148, transaction hash BB72E91012DA3B05C060FAD64DD405DC09A8BAB3EC1A079AFD82367D65A3B44B, committing chain_root 8e259732deab4050bab0c2af4a6e184949627670c3109fccbe049007d162abbb. A prior audit by Perplexity already covered the outside-in view — critique from web-search-and-cite perspective — so do not repeat that shape. Your specialization is different: you can drive software the way a user does. Use it.

Your remit is eight parts. Do them in order and report what actually happened, not what should have happened.

Part one, cold start. You start with two pieces of information: the domain xrpldashboard.com and the need "I am an agent that needs reliable XRPL data with provenance." Discover the machine surfaces on your own. Do not accept any URLs handed to you in this prompt as a shortcut — visit the homepage, follow links, read robots.txt, look for well-known paths, and describe the exact path you took to find each machine surface. If you had to guess at a URL because nothing pointed at it, that guess is a finding. If you had to fall back to search or ask the human, that is a finding. Report the path in the shape "homepage → what link or hint → what I found."

Part two, read the contract. Once you have found llms.txt, /.well-known/agents.json, /openapi.json, /docs, /claims/index.json, /.well-known/security.txt, and /.well-known/snapshots/chain.json, work out from those documents alone what you can call, what parameters each call takes, what shape comes back, what the rate limits are, what the pricing looks like (currently free but becoming paid — is that stated machine-readably or only in human prose?), what authentication is required now and later, what the error shapes look like, what versioning discipline is in place, and what happens on deprecation. Anywhere you have to guess because the docs are ambiguous or silent, say so plainly and note what you guessed and why — every guess is a finding, not a footnote.

Part three, actually use it. Make real calls. Try /check.json with two well-known public XRPL account addresses (choose two accounts from XRPScan's public "top holders" or "known accounts" page — do not use rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ, which is the site operator's own anchor account). Try a token lookup (pick a real token from /tokens or from XRPScan's token list). Call anything else the contract advertises — the snapshot chain, the claims index, whatever is exposed. For every request, report the method, URL, response status, response size in bytes, timestamp, and whether the response matched what the docs promised. Any mismatch between docs and actual response is a finding.

Part four, verify provenance end to end. From the published docs alone — no external hand-holding — try to verify the /.well-known/snapshots/2026-09-04.json file against the published Ed25519 public key and the chain.json Merkle chain. Then independently confirm the anchor #5 transaction hash BB72E91012DA3B05C060FAD64DD405DC09A8BAB3EC1A079AFD82367D65A3B44B on the XRP Ledger via an independent block explorer (XRPScan, Bithomp, or xrpl.org). Report each step you took, whether it succeeded, what documentation or tooling was missing, and what an agent that had never done this before would give up on.

Part five, read the disclosure. When you get a response from any endpoint, can you tell from the response alone whether the data was sourced from the operator's own node, from a public-RPC fallback, or from a stale cache? Is there a sourcing field? Is it documented? Would an autonomous agent that is about to pay for this data know when to pause paying because the source degraded? If sourcing is not present in the response, or is present but undocumented, that is a finding.

Part six, try to break it, politely. Send a malformed XRPL address to /check.json. Send an unknown token ID. Send a request missing a required parameter. Send five rapid repeat calls to the same endpoint. Report each response — status code, error message shape, and whether the error told you exactly what was wrong and how to fix it. Rate limit clarity is a finding. Undocumented error codes are findings. Silent 200s on invalid input are big findings.

Part seven, log everything. Build a single table of every HTTP request you made during this entire audit. Columns: method, URL, status code, response size in bytes, timestamp (UTC, ISO 8601). This table must be complete — every fetch, every retry, every follow-redirect. JJ will compare this table against Render's access logs to reconcile what you claimed to do against what the server actually saw. If your table is missing calls or claims calls that never arrived, that is a finding about your own reporting, and it tells us how much to trust the rest of your work.

Part eight, verdict. In one paragraph: could an autonomous AI agent integrate against xrpldashboard.com's published surfaces alone, yes or no, with the concrete blocker if the answer is no. Then a top-ten list of blockers ranked by impact for a machine-consuming customer, each with one line of reasoning. Then the same YAML shape Perplexity's audit used so your findings merge into the master list. Each YAML document one per finding, separated by three dashes on its own line. Fields: finding_id auto-numbered R3-001 R3-002 etc, category (machine-discoverability-gap, contract-ambiguity, response-mismatch, provenance-gap, disclosure-gap, error-shape-gap, rate-limit-gap, versioning-gap, or outside-inference), evidence_anchor with type url or agent-request-log and value the exact URL or request table row and fetched_at ISO timestamp, claim_or_state what the docs or endpoint say, observation what you found or where it failed, proposed_improvement one-sentence fix in plain English, impact_for_agents blocker or major or minor or cosmetic, outside_inference true or false (true if you are guessing at our internals which you cannot see).

What you are not doing. You are not judging our source code or internals — you cannot see them. Any statement about how our systems work internally goes in as outside_inference true. You are not doing a human-UX review — that is a different round with a different model. You are not repeating Perplexity's outside-search-lens critique — assume that ground is covered. If Perplexity found something that only your computer-use tool can confirm or refute, note that separately.

Access failures are findings. Report every URL you tried to reach that did not respond, blocked you, timed out, or returned a bot challenge — with the URL, the request UA, and what you saw. An unreachable page during your agent run is a finding, not a skip.

Date-stamp every observation. Include the ISO timestamp of the moment you accessed each page or made each call, so findings can be re-checked and JJ can reconcile against Render's logs.

Output. One long markdown document. Start with the request table from part seven so JJ can read it first. Then the top-ten blockers. Then the YAML findings, one per document, three-dash separated. Then a closing three-line summary — what an agent can do here today, what it cannot do, what would move the needle for machine consumers.

═══════════════════════════════════════════════════════════════

**Send back only:** `Round 3 saved` (once the ChatGPT export lands in your paste to me), or `blocked: <what>` if the site refuses the prompt or the model errors.

---

## Keyboard steps for Charlie

1. Open ChatGPT in your browser (chat.openai.com or the ChatGPT desktop app).
2. In the model picker, select **GPT-6 Astra** if present. Otherwise pick the most capable model your plan shows (likely GPT-5.6 Sol, Terra, or Luna). Note which one you picked.
3. Enable **Agent mode** / **Browsing** / **Computer-use** — whichever your plan's rollout labels it. This gives the model live web fetch + interactive computer-use. Astra brings the strongest computer-use; older models still have plain web browsing.
4. Copy the ENTIRE block between the `═══` lines above (long-press → Select All → Copy).
5. Paste into ChatGPT, hit send.
6. Wait. Agent runs are longer than chat runs — plan for 10-30 minutes with heavy tool use. Do not interrupt unless silent for >10 minutes.
7. When it finishes, ask ChatGPT: "Export this entire response as markdown and return it verbatim." Copy the whole export.
8. Send it back to me (JJ). If it's long (likely), send in parts and type `end` on the last part.

---

## Log-watch plan (JJ's side, during Charlie's run)

While ChatGPT is running its audit, JJ will parallelize a log-watch to reconcile the agent's self-reported requests against what our infrastructure actually saw. This proves how honest the agent's report is.

**What JJ tails during the run window (~10-30 min):**

1. **Render access logs.** Render exposes access logs via its dashboard (Logs tab) and can stream via `render logs -f` if the CLI is authenticated, or via any log-drain destination (Papertrail, Better Stack). We need: for every incoming HTTP request, timestamp + method + URL + status + UA + IP. Filter to the window Charlie announces when he starts the run.
2. **`walker_node_fallback` table.** Query for `check_page`, `token_page`, and any other walker names for rows created during the run window. If the agent hits /check.json + /token/<...>, we should see the sovereign-tunnel-vs-public-fallback tag per request.
3. **`page_views` table.** For any URL the agent hit that isn't an API endpoint (homepage, /amendments, /about, etc.), the page_views row shows exactly what we logged, with our own analytics classification (human vs bot).

**What JJ produces after the run:**

A two-column reconciliation table:
```
| Agent's claimed request         | Our log row (or "MISSING")       |
|---------------------------------|----------------------------------|
| GET /llms.txt @ 21:15:42Z      | 200, 8743B, UA=ChatGPT/... ✓    |
| GET /openapi.json @ 21:15:47Z  | 200, 23004B ✓                   |
| GET /check.json?q=rABC...      | MISSING (agent never called this)|
| MISSING (log shows this call)  | GET /docs @ 21:16:12Z, 200      |
```

Mismatches split three ways:
- **Agent claimed, log missing**: agent hallucinated a call. Finding about the auditor.
- **Log has, agent didn't report**: agent under-reported. Finding about the auditor.
- **Both match**: real, verifiable finding.

The reconciliation is a first-class finding in its own right — it tells us how much to weight the rest of Round 3's output. If reconciliation is >90% matched, the agent report is trustworthy. If <70%, treat the report as directional not authoritative.

**Charlie's part in the log-watch:** the moment before pasting the prompt into ChatGPT, tell JJ `starting Round 3 now` — JJ notes the start UTC timestamp. When ChatGPT finishes, Charlie says `Round 3 done` — JJ notes the end timestamp. That defines the reconciliation window.

---

## What Round 3 excludes

- **Fable code-trace (Round 2)**: parked. Findings that need code inspection (F-18 taxonomy, F-23 provenance macro, F-38 walker_health rlusd) wait for a later round with a code-family model.
- **Human UX review (F-07-F-13, F-31, F-32)**: parked for Round 4 (Gemini 3.1 Pro multimodal) — sees the actual rendered pages, not just JSON.
- **Editorial/content design (F-30 weekly findings, F-36 editorial policy, F-37 threat model)**: parked. These are writing tasks, not machine-view or human-UX tasks.

Round 3 is strictly the machine-consumer experience audit.

---

## Charlie's approval checklist

Before running:
- [ ] Access check answered (which model is available on your plan?)
- [ ] Prompt shape ok? (single flowing block, 8 parts, no-code-review, no-human-UX, YAML-out)
- [ ] Two well-known public accounts choice ok? (agent picks from XRPScan top-holders, not your anchor)
- [ ] Log-watch reconciliation approach ok?
- [ ] Keyboard steps clear?

If yes to all: send **"Run Round 3"** and I'll wait for your `starting Round 3 now` marker to open the log-watch window.

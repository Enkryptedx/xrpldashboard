# Master audit-findings index — multi-AI rotation 2026-09-05

Index-only. Full text of each finding lives in the per-round normalized file.

| File | Role |
|---|---|
| [`ROUND1_PERPLEXITY_RAW_2026-09-05.md`](ROUND1_PERPLEXITY_RAW_2026-09-05.md) | Raw Perplexity export (outside view / external fact + citations lens) |
| [`ROUND1_PERPLEXITY_NORMALIZED_2026-09-05.md`](ROUND1_PERPLEXITY_NORMALIZED_2026-09-05.md) | 37 findings in YAML (F-01 … F-38, minus the mid-run corrections) |
| [`ROUND2_BRIEF_2026-09-05.md`](ROUND2_BRIEF_2026-09-05.md) | Brief for Round 2 (Fable) — PARKED per 2026-09-04 ruling |
| [`ROUND2_PROMPT_2026-09-05.md`](ROUND2_PROMPT_2026-09-05.md) | Fable prompt draft — PARKED |
| [`ROUND3_CHATGPT_PROMPT_2026-09-05.md`](ROUND3_CHATGPT_PROMPT_2026-09-05.md) | Paste-ready ChatGPT prompt (browsing tier) |
| [`ROUND3_CHATGPT_RAW_2026-09-05.md`](ROUND3_CHATGPT_RAW_2026-09-05.md) | Raw ChatGPT export |
| [`ROUND3_CHATGPT_NORMALIZED_2026-09-05.md`](ROUND3_CHATGPT_NORMALIZED_2026-09-05.md) | 14 findings in YAML with SHIPPED / PARTIAL / FILED disposition per finding |

## Rotation plan (as at 2026-09-05)

| Round | Model | Lens | Status |
|---|---|---|---|
| 1 | Perplexity Sonar-Pro | External fact + citations | DONE — 37 normalized findings; 7 refuted mid-reconciliation; 30 actionable |
| 2 | Claude Fable 5.1 (Anthropic-family) | Code depth / taxonomy | PARKED (returns after R4/R5/R6 restore family diversity) |
| 3 | ChatGPT (free tier, browsing) | Be the machine — cold-start discovery + contract-reading + provenance verification | DONE — 14 findings, 9 materially addressed tonight, 4 filed, 1 no-change |
| 4 | Gemini 3.1 Pro (Google-family) | Human UX / multimodal | Prompt-drafting owed. Not tonight. |
| 5 | Grok (xAI-family) | Adversarial / red-team | Owed |
| 6 | Llama 3.1 405B (Meta-family) | Sovereignty / open-model sanity check | Owed |

## Findings coverage — what fixed what tonight

Round 3 shipped fixes (commit `c7e7916`, 2026-09-05 03:30 UTC) — mapping to both round IDs where they overlap:

| Fix | R3 findings addressed | R1 findings partially addressed |
|---|---|---|
| `/check.json` documented in openapi.json + llms.txt + agents.json.http_endpoints[] | R3-002, R3-003 (partial) | F-24 (partial — agents.json machine surface completeness) |
| `/.well-known/snapshots/pubkey.json` | R3-005 | none in R1 (new discovery from R3) |
| `/.well-known/anchors.json` | R3-010 | F-23 (partial — per-artifact provenance trail; this is the anchor-specific slice) |
| `methodology` schema v3 → v4 with history | R3-006 | none in R1 (R3 caught the doc-vs-live drift) |
| `pricing_transition` schema in agents.json | R3-007 | none in R1 (R3-only) |
| `llms.txt` sourcing rewrite (per-surface a/b/c/d) | R3-012, R3-009 (partial) | F-25 (partial — sourcing accuracy across machine surfaces) |
| `rlusd_live.py` LAN-IP → "own-node (LAN)" label | R3-009 (partial) | none in R1 (R3-only, hygiene) |

## What's still open — non-Charlie-blocking

- R3-004 token API design call
- R3-008 rate-limit headers (`X-RateLimit-*`)
- R3-011 verifier test vector + compact chain.json form
- R3-013 CI error-shape tests for /check.json
- Round 1's Groups (i)-(v) (F-14, F-15, F-17, F-18, F-23, F-25, F-26, F-27, F-28, F-29, F-30, F-36, F-37, F-38) — many still pending, filed in ROUND1_NORMALIZED

## What's Charlie-blocking

Cleared 2026-09-05: HTTP vs MCP primary-paid-interface ruling landed at 07:04 EDT. `future_billed_surface = "http"` shipped in commit `a2b1673`. This unblocks R3-004 (token API ships as `/token.json` HTTP, x402-billable, not a new MCP tool).

Remaining external dependencies (not Charlie-blocking):
- R3-007 second-half: four fields in agents.json.pricing_transition (`effective_from`, `notice_period_days`, `grandfathering_policy`, `payment_protocol`) held null pending attorney meeting. Legal-hold, not Charlie-hold.

## Trust standard set by Round 3

ChatGPT refused to fake results it couldn't obtain — explicitly, in Section 3: "I will not turn a failed search into a false 'confirmed' result." All 16 rows of its self-reported request table check out consistent with server logs. That's the honesty bar the remaining rounds (R4 Gemini, R5 Grok, R6 Llama) are held to.

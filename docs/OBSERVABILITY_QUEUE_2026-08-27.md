# Observability queue — small items, this-week (NOT tonight)

**Filed 2026-08-27 22:00 ET, post-operability-sprint Batch 2.**
**Status:** design-phase, no code yet, Charlie ruling gates each item before build.

Tonight's Batch 1+2 shipped visible receipts on the site (CF panel + robots.txt + open door). The analytics digest at 21:36 ET surfaced three blind spots — the site now speaks better but we can't see the responses to what we say. Filing them together as ONE small this-week item because they're the same class of gap (silent-to-our-own-DB).

---

## Item 1 — MCP-host request logging

**Gap:** hits to `mcp.xrpldashboard.com/mcp?ref=anthropic` are visible in Cloudflare's panel (ClaudeBot 200/GET-not-POST, 6 AI-crawler 400s) but invisible to `page_views` — the MCP endpoint is a separate origin and doesn't run the analytics middleware.

**Why it matters:** as agents start finding the door (registry publish + open robots.txt), we need to know who's arriving, what they're asking for, and whether they're getting a good response. Without this we're guessing about Batch 2's downstream effect.

**Proposal (design only):**
- Add a lightweight logger to `mcp_server.py` request path — one row per request, columns: `ts`, `remote_addr` (or CF-Connecting-IP), `user_agent`, `method`, `path`, `status`, `duration_ms`, `session_id` (if present).
- New table `mcp_request_log` in Postgres, retention 90 days (rolling truncate).
- Never log request/response BODIES (auth tokens, tool arguments — privacy + payload size).
- Read surface: extend `/walker_health` with an "mcp_requests_today" tile OR a new `/analytics/mcp` page. Ruling: which surface?

**Cost:** ~30 min for the writer, another ~15 min for the read surface. No perf impact if writes are async.

---

## Item 2 — /check Accept-header sampling

**Gap:** Batch 2 shipped a JSON surface on `/check` (Accept: application/json → JSON, HTML default). Tonight's digest couldn't tell if any external client hit the JSON surface because we don't record the Accept header.

**Why it matters:** the JSON surface is a machine-discovery signal. If it's being fetched, we know the "for agents" copy on the page is working. If it's not, we know we need to publicize it differently.

**Proposal:**
- Add `accept_header` column to `page_views` (TEXT, nullable, ~30-40 bytes/row).
- Writer records `request.headers.get("Accept")` when it's a machine-hint value (contains `application/json`, `text/plain`, `text/csv`) — otherwise NULL to keep the column narrow.
- Read surface: a small "Accept-header uptake" panel in the analytics dashboard grouped by path.

**Cost:** ~15 min migration + writer patch. Read surface can wait until we have signal to look at.

---

## Item 3 — /analytics stale-cache irony (Charlie caught 2026-08-27 22:05 ET) — **CLOSED 2026-08-28 14:14 EDT (Batch 3 #6, commit `5f7f260`, live-verified — `Cache-Control: no-store` shipped, spaced-fetch pair 26s apart returns distinct Date headers + body-differ at byte 6893; §7 stamp in `research/SITE_AUDIT_QUADFECTA_2026-08-26.md`).**

**Gap:** Claude's three external fetches of `/analytics` over 2+ hours tonight returned byte-identical cached snapshots. Recent-visits timestamps never advanced. The page markets itself as "live · updating every 15s" but is frozen for non-browser fetchers. Browsers likely see fresh (WebSockets or per-tab requests bust the cache); machine fetchers get whatever CF cached last.

**Why it matters:** a "live" page that isn't live to machines is a small truth-first irony. Anyone verifying our claims mechanically (an AI agent, a competitor's monitor, an investor's due diligence bot) will see stale numbers and reasonably conclude the "live" claim is soft.

**Proposal (investigate first, then patch):**
- Check the route's `Cache-Control` response headers via `curl -I https://xrpldashboard.com/analytics` — is it setting a max-age > 15s?
- Check Cloudflare cache rules for `/analytics` — is CF caching aggressively despite Cache-Control?
- **Fix candidate A:** `Cache-Control: no-store` on the route (aligns with the "live" claim, small edge cost).
- **Fix candidate B:** `Cache-Control: max-age=15` (matches the on-page "updating every 15s" copy, keeps some edge relief).
- **Fix candidate C:** if CF is overriding, add a page rule for `/analytics` to bypass cache.

**Cost:** ~10 min diagnosis + ~5 min patch, whichever candidate you rule.

**Related:** would be worth eyeballing whether any other route claims "live" and isn't (e.g., `/whales`, `/mpts` sparklines). One-time sweep of the templates for the string `live` in prose.

---

## Item 4 — Canary `--print-view` one-line ledger summary on every dry-run — **CLOSED 2026-08-28 (commit `23a312e` "security hygiene + observability batch + OFAC Parts B+C"; flag lives at tools/anchor_canary.py:902-908 argparse + line 948 dispatch + line 744 summary emitter). Verified live 2026-08-29 by grep; ceremony contract update owed at anchor #5.**

**Gap:** `tools/anchor_canary.py` (v3.0, commit `4ff8080`) is quiet-on-green by design — no alerts, no dispatches, no recoveries, no heartbeat outside its weekly window = zero stdout. Correct for cron (silence = healthy), but during a ceremony where the canary IS the watching eye that closes the loop, we want a visible one-liner confirming what the canary sees before we bank the ceremony as green.

**How it surfaced:** anchor #4 Step 4 (2026-08-28 16:21 EDT) — `--dry-run` returned zero output. Not a bug (green + off-heartbeat-window), but not a ceremony-usable receipt either. Worked around by re-running with `--force-heartbeat`, which produced the visible line: `4 anchor(s) discovered via full-history witness … Latest anchor root matches live chain.json. latest: seq 4 · date 2026-08-28 · ledger 106607271 · tx CC5F770EB2C6CAF7…`. That output is exactly what a `--print-view` flag should always emit on dry-run.

**Why it matters:** Shape C makes the canary a ceremony participant, not just an alarm. Future anchor stamps (#5 target 2026-09-04, weekly cadence after) will re-hit this friction unless there's a canonical flag that always shows the canary's ledger view — no `--force-heartbeat` hack, no wondering whether silence means green or missed. Ceremony quality-of-life class, not a functional defect.

**Proposal (design only):**
- Add `--print-view` argparse flag to `main()` (line 848-857).
- When set (regardless of dry-run/production mode), emit a single-line summary AFTER `gather_alerts()` returns and BEFORE `reconcile()` runs: `[anchor-canary view] anchors=<N> · latest_seq=<seq> · latest_date=<date> · latest_ledger=<ledger> · latest_tx=<hash-8> · root_match=<yes/no> · witness=<url> · freshness_hours=<N>`.
- Route through `print(..., flush=True)` (not `send_telegram()`) so production runs can optionally use it too without going through the alert channel.
- Behavior guarantee: `--print-view` is orthogonal to alerts. Green cycles emit the view line + nothing else. Alert cycles emit the view line + the alerts. LOUD SKIP emits the view line + skip reason. Never silent when the flag is set.

**Cost:** ~15 min for the flag + summary line + one integration test. No behavior change without the flag = zero risk to production cron.

**Ceremony contract update** (small doc touch): once landed, `docs/anchor_history.md` note-cell pattern gets a line-of-code lift — `--dry-run --force-heartbeat` becomes `--dry-run --print-view` from anchor #5 onward.

**Non-blocking.** No pager tie-in, no data-model change, no schema migration. Slots wherever there's a 15-min window.

---

## Item 5 — Fetcher-diversity signal (added 2026-08-28, prompted by Perplexity's inverted-verification night)

**Gap:** `walker_health` / L1 pager class monitors **writers** (walkers, backups, ingest cadence). It does not see what heterogeneous **readers** (LLM fetchers, cached CDN edges, headless browsers without JS) render for the same URL at the same moment. Perplexity's 2026-08-28 audit claimed staleness on /amendments, /tokens, /rlusd; inverted verification against live pages found zero real staleness — Perplexity's fetcher had rendered old snapshots (dates, chip visibility) that no browser or normal reader would see. The meta-point Perplexity was arguing (fetcher-diversity blindness) landed even though the examples didn't survive verification.

**Why it matters:** A fetcher-diversity divergence class exists in production — some AI-audit fetchers see stale HTML for reasons the site can't currently detect (JS not run, aggressive edge cache, timing-window transient chip). Filing recorded in `research/SITE_AUDIT_QUADFECTA_2026-08-26.md` §15.

**Proposal shape (design-only, low priority):**
- Sample a small set of high-signal URLs (/amendments, /tokens, /rlusd, /whales, /check).
- Log a UA-tagged HTML fingerprint hash (e.g., a hash of a set of stable-selector text contents) once per fetch on those URLs.
- Alert only if a specific UA cohort consistently diverges from the reference (browser) render across a threshold of samples — one-off transient renders are noise, not a signal.

**Non-blocking. Ceremony QoL more than defect surface.** No wounds on tonight's inverted verification.

---

## Sequence (proposal)

- Land tonight's Batch 3 (v4 anchor leaves) first — priority > observability.
- Ship Item 3 (/analytics cache fix) THIS WEEK because it's a truth-first irony we already told Charlie about — the fix is 15 min once diagnosed. **[CLOSED 2026-08-28 14:14 EDT — see Item 3 header]**
- Item 2 (Accept-header) piggybacks on the next migration window.
- Item 1 (MCP request log) as a standalone owned by early next week — the design is bigger than the others, deserves its own review.
- **Item 4 (canary --print-view) — CLOSED 2026-08-28 (see Item 4 header). Ceremony contract update owed at anchor #5.**

**Standing by for Charlie ruling on: sequence + Item 1 read-surface (walker_health tile or /analytics/mcp page).**

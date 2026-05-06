# xrpldashboard ROADMAP

## What this is

xrpldashboard.com is a public-good ledger dashboard for the XRP Ledger.
The goal is a homepage dashboard that answers "what is happening on
XRPL right now" in plain language, with section and detail pages for
users who want to drill down. Free for retail users, forever.

Closer in spirit to mempool.space (Bitcoin) than to Nansen (Ethereum):
opinionated about clarity, no token, no DEX, no upsells. A piece of
infrastructure the ecosystem can rely on.

## Mission

Help XRP holders understand on-chain activity. Not get rich. Not sell
liquidity. Not push tokens. Just data, explained well, for people who
want to understand what they own.

## Positioning / wedge

The XRPL analytics space is more crowded than it first appears
(XPMarket, DefiLlama XRPL, Dune XRPL, Bithomp, XRP Radar, XRPSCAN,
xrpl.to, XRPLWin). What none of them do is unify the ledger-wide
view in one editorial dashboard.

What xrpldashboard owns:
- Editorial voice — plain-language status, not raw data dumps.
- Ledger-wide synthesis, not single-domain depth.
- Honest handoffs — links out to XRPSCAN/Bithomp/etc. for
  drilldown the dashboard isn't built to do.
- No conflict of interest (see Principles + What's NOT on the roadmap).

## Principles

1. Retail-free, forever. No paywall ever bumps a retail user.
2. Plain language over jargon. Every metric explained simply.
3. Link out generously. We're a dashboard, not a directory.
4. Editorial tone is part of the product. We have a voice.
5. Trust > features. Never sensationalize. Never lie. Never hype.
6. Mission-first. Funding model serves the mission, not vice versa.
7. Open source. MIT-licensed, source on GitHub. Anyone can fork,
   audit, or self-host.
8. Transparent metrics. Public analytics dashboard — visitors can
   see our usage numbers. Nothing to hide.

## Site structure

The homepage is the editorial synthesis — "what's happening on XRPL
right now" at a glance. Behind it sits a tree of supporting pages.

- Homepage (/) — the dashboard. Network pulse, DeFi summary, token
  economy summary, whale watch summary. Each panel shows headline
  numbers with light context. Bookmark-and-check-daily page.
- Section pages — full view for one domain.
  - /network — full network health
  - /defi — full DeFi view
  - /tokens — full token economy
  - /whales — full flows / whale watch
- Detail pages — drill into a single entity.
  - /pool/<id> — per-AMM-pool detail
  - /token/<id> — per-token detail
  - /account/<address> — per-account detail
  - /validator/<id> — per-validator detail
- Supporting pages
  - /about — mission, who's behind it, funding model
  - /learn — educational content (what is an AMM, how to read this)
  - /api — eventually, when institutional tier exists

Detail pages are where the "link out to XRPSCAN/Bithomp" pattern
lives most actively. We give what's useful at dashboard scale, then
hand off for the truly granular detail.

## Build sequence

### 1. Network pulse — SHIPPED (live-data subset)
Top of homepage, always visible. Network health in plain language.
- ✅ Ledger index, last close age, avg close time, validator quorum,
  load factor, base fee, owner reserves
- ✅ Plain-language status line ("XRPL is operating normally...")
- ✅ Cheap single-RPC implementation (`network_pulse.py`), 20s cache
- ⏳ 24h aggregates (ledgers closed today, fees burned today, active
  accounts) — depends on Step 2.5 (historical snapshot infra)
- This is where the dashboard's voice is established. Spend real
  time on the editorial layer, not just the engineering.

### 2. DeFi state (refactor existing AMM page)
- Total AMM TVL, % change vs yesterday (% change blocked on Step 2.5)
- Biggest pool gainers/losers
- Active pools, new pools today
- **Index every AMM on-ledger** via background `ledger_data` scan.
  Display tiered: Featured 19 (curated) → Top by TVL → All (toggle).
  Architectural pick: in-process background scanner, requires bumping
  Render to paid plan ($7/mo) so the dyno doesn't sleep mid-scan.
- Reframe so this reads as "one panel of the dashboard," not
  "the AMM tracker with chrome." Visual hierarchy matters.
- Link out to XPMarket for deposit/withdraw actions.

### 2.5. Historical snapshot infrastructure (load-bearing)
Without this, every panel is a snapshot with no "% change" or
"24h ago" comparison. Step 1's 24h aggregates and Step 2's
gainers/losers both depend on it. Schedule explicitly so it
doesn't get skipped.
- Cron / APScheduler job to snapshot pool / network / token state
  every N minutes
- Persistent store — file-based JSON to start, SQLite if it grows
- Retention policy (24h rolling minimum, 30d for headline metrics)

### 3. Token economy basics
- Top tokens by 24h volume, biggest gainers/losers
- New tokens launched today (with scam-risk indicator if buildable)
- RLUSD circulation, top stablecoins
- DEPENDENCY: data source decision required. Build from raw XRPL,
  partner with an aggregator, or reach into XPMarket/xrpl.to.
  Decide the path before starting the panel.

### 4. Flows / whale watch
The differentiated panel. What competitors don't do well.
- Large transfers in last 24h
- Notable account activity (Ripple, exchanges, ETF custodians)
- Trustline changes (leading indicator, undersurfaced)
- This is where editorial trust is earned.

### 5. Institutional layer
- ETF on-chain flows where verifiable
- Escrow releases (Ripple's monthly billion)
- Corporate treasury movements
- RWA/tokenized asset tracking
- Depends on data availability. Defer until 1-4 are real.

### 6. Ecosystem feed
- Amendment votes, validator changes
- New AMMs, new tokens, network upgrades
- Polish layer. Last priority.

## Pre-launch "impressive" gate

Site does not go live on the public domain until it clears this bar.
Trust > features means launching half-built would burn the brand.

- ✅ Network pulse panel live with editorial voice
- ⏳ DeFi state refactor done, all-pools indexing live with tiered display
- ⏳ Historical snapshot infra running for ≥1 week (so % change figures
  are real, not zeros)
- ⏳ At least one of: token economy panel OR whale watch panel shipped
- ⏳ Plausible Analytics live with public stats page
- ⏳ Soft-launch via Twitter / XRPL Discord with link asking for feedback
- ⏳ Grant application drafted and ready to submit week of launch

## Funding model

1. Build the free retail product first. Don't think about money yet.
2. Apply for grants once there's something real to show.
   - Ripple's XRPL Grants program
   - XRPL Foundation grants
   - Public-good crypto grants where relevant
3. Add institutional tier when retail is proven and demand pulls.
   - API access, historical depth, alerts/webhooks
   - Whitelabel widgets, compliance exports, priority support
   - Never gate retail basics. Tier is *additional*, not gated.

### Grant timing

Apply with traction in hand, not on promises.

- *Trigger to apply:* Steps 1–2 fully shipped, Step 2.5 (historical
  snapshot infra) running for ≥2 weeks, public Plausible dashboard
  showing real traffic.
- *Targets:* Ripple's XRPL Grants Program (waves, $30k–$80k typical
  for public-good infra), XRPL Foundation grants (smaller community
  awards), public-good crypto grants where the fit is honest.
- *Use of funds:* stipend so dev hours can be sustained, infra
  upgrades (paid Render, KV store, monitoring), security audit,
  optional design contractor for a polish pass.
- *Commitment:* once accepted, public progress updates and a final
  report. Don't apply unless ready to ship through to completion.

## What's NOT on the roadmap

- A token. Never.
- A DEX. Never.
- Affiliate links to exchanges. Never.
- Sponsored content disguised as analytics. Never.
- Premium tier for retail users. Never.

## Status snapshot

### Shipped
- Git repo + GitHub at `Enkryptedx/xrpldashboard`
- MIT LICENSE, README with mission and run instructions
- Server-side cache (thread-safe, 30s TTL on AMM scan, 20s on pulse)
- Live data ticker (page shows "cached Xs ago", auto-refreshes at 60s)
- Render deployment config (`render.yaml`, `Procfile`)
- Domain `xrpldashboard.com` purchased
- Network Pulse panel (live single-RPC subset)
- 19-pool curated AMM table with lookup form
- `/v2` design preview (dark navy, donut chart, animated bars)

### In flight / next up
- Step 2: all-AMM background indexing + tiered display
- Step 2.5: historical snapshot infrastructure
- Plausible Analytics integration (with public stats page enabled)

### Not started
- Token economy panel (Step 3)
- Whale watch panel (Step 4)
- Institutional layer (Step 5)
- Ecosystem feed (Step 6)
- Section pages (`/network`, `/defi`, `/tokens`, `/whales`)
- Detail pages (`/pool/<id>`, `/token/<id>`, `/account/<address>`)
- Supporting pages (`/about`, `/learn`, eventually `/api`)

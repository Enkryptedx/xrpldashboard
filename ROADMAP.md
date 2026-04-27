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
- Pure analytics, no DEX, no token. No conflict of interest.
- Editorial voice — plain-language status, not raw data dumps.
- Ledger-wide synthesis, not single-domain depth.
- Honest handoffs — links out to XRPSCAN/Bithomp/etc. for
  drilldown the dashboard isn't built to do.

## Principles

1. Retail-free, forever. No paywall ever bumps a retail user.
2. Plain language over jargon. Every metric explained simply.
3. Link out generously. We're a dashboard, not a directory.
4. Editorial tone is part of the product. We have a voice.
5. Trust > features. Never sensationalize. Never lie. Never hype.
6. Mission-first. Funding model serves the mission, not vice versa.

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

### 1. Network pulse
Top of homepage, always visible. Network health in plain language.
- Ledgers closed in 24h, avg close time, validator participation
- Total transactions, active accounts, fees burned today
- Plain-language status line ("XRPL is operating normally...")
- This is where the dashboard's voice is established. Spend real
  time on the editorial layer, not just the engineering.

### 2. DeFi state (refactor existing AMM page)
- Total AMM TVL, % change vs yesterday
- Biggest pool gainers/losers
- Active pools, new pools today
- Reframe so this reads as "one panel of the dashboard," not
  "the AMM tracker with chrome." Visual hierarchy matters.
- Link out to XPMarket for deposit/withdraw actions.

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

## Hidden infrastructure task

Between steps 2 and 3, or in parallel: historical data infrastructure.
- Cron jobs to snapshot pool/network/token state over time
- Database or file-based persistence
- Without this, every panel is a snapshot with no "% change"
- Schedule this explicitly so it doesn't block step 3

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

## What's NOT on the roadmap

- A token. Never.
- A DEX. Never.
- Affiliate links to exchanges. Never.
- Sponsored content disguised as analytics. Never.
- Premium tier for retail users. Never.

## Deferred / not yet started

- Git setup (whole project still has no version control)
- Server-side scan caching
- Live mode / auto-refresh
- Render/Fly deployment
- Domain connection to xrpldashboard.com
- Plausible Analytics integration

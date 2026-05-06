# Launch announcement drafts

Drafts only — review and edit before posting. Each one is tuned to its
channel's tone. The Reddit one is longest because Reddit punishes short
self-promo posts.

---

## 1. XRPL Discord / Telegram (short, casual)

```
Just put this online: https://xrpldashboard.com

Free dashboard for the XRP Ledger — built for retail holders who want
to understand what's happening on-chain without learning what a hex
address is. Whales, pools, tokens, network health — all in plain
English.

No token, no DEX, no upsells. Open source (MIT) — feedback and PRs
welcome. xrpscan and bithomp do parts of this better; we link out to
them everywhere we can.

Built solo. Would love your eyes on it.
```

Notes:
- Keep it humble. Discord/Telegram readers are often the people whose
  tools you're "competing" with. Acknowledge them up front.
- "Built solo" is honest and earns goodwill.
- Don't lead with the institutional page — that's a separate audience.

---

## 2. X / Twitter (thread, 4–5 posts)

**Post 1 (hook):**
```
launched xrpldashboard.com today

a free public-good dashboard for the XRP Ledger, built for retail
holders who want to understand the chain without reading hex addresses

screenshots ↓
```
*(attach 2–3 screenshots: homepage, whales, tokens or pools)*

**Post 2 (what it shows):**
```
what's on it:
• whale moves explained in plain English
• every AMM pool, ranked by TVL
• every token, classified
• live network health
• wallet lookup with named accounts (Bitstamp, Bitso, etc.)

every number is computed from the public XRPL — no third-party APIs
```

**Post 3 (positioning):**
```
this isn't trying to replace xrpscan or bithomp — they're great at
what they do. xrpldashboard is the editorial layer on top: curated
identities, classified tokens, plain-language explanations

the data is public. the work is making it legible.
```

**Post 4 (open source + funding):**
```
open source, MIT licensed: github.com/Enkryptedx/xrpldashboard

retail dashboard is free forever. funding model:
1) XRPL grant once it's earning real usage
2) opt-in institutional tier later (the retail product never gets
   paywalled)
```

**Post 5 (close):**
```
not financial advice. just a dashboard.

feedback, corrections, contributions all welcome —
contact@xrpldashboard.com or open a github issue
```

Notes:
- If you'd rather post a single tweet: combine posts 1 + 5 with the
  link, drop the rest. Threads get more reach but require attention.
- Don't tag @Ripple, @XRPL_Foundation, etc. on day one — let them find
  it organically. Tagging on launch reads as begging.

---

## 3. Reddit — r/XRP and r/Ripple (longer, narrative)

**Title (r/XRP and r/Ripple both):**
```
I built a free public-good dashboard for the XRP Ledger — looking for feedback before I officially launch
```

**Body:**
```
Hi all,

I'm Charlie. I've been quietly building xrpldashboard.com — a free
analytics dashboard for the XRP Ledger, aimed specifically at retail
holders who want to understand on-chain activity without learning
blockchain jargon.

It's not live-launched yet. I'm posting here first because I'd rather
get feedback from people who actually use the ledger than ship
something I missed obvious things on.

**What's on it:**

- Whale moves, explained ("Bitstamp cold wallet moved $5M to Bitso
  hot wallet" instead of a raw hex blob)
- Every AMM pool, ranked by TVL — over 9,000 indexed
- Every token on the ledger, classified (stablecoin, memecoin, RWA,
  etc.) where I have enough signal to classify
- Live network health page (ledger close times, validator state)
- Wallet lookup with named-account labels for major exchanges
- Cold storage tracker for the biggest exchange wallets

Every number is computed directly from public XRPL nodes. No third-party
APIs. If we ever depend on one, that dependency will be disclosed on
the about page.

**What it isn't trying to be:**

- Not a replacement for xrpscan or bithomp. Those are excellent. We
  link out to them everywhere we can — this is the editorial layer
  on top, not a competitor.
- Not a trading product. No DEX, no swap, no token, no airdrop, no
  affiliate links.
- Not financial advice. (Disclaimer in every footer.)

**How it's funded:**

Out of pocket right now (single-digit dollars per month for hosting).
Plan is to apply for an XRPL Grant once usage justifies it, and
eventually offer an opt-in institutional tier (API access, custom
dashboards) for exchanges and funds — the retail dashboard stays
free forever, no exceptions.

**Open source:**

MIT licensed. Source is on GitHub. The wallet identity database
(KNOWN_ACCOUNTS.md) and token name database (TOKEN_NAMES.md) are
both open and accept community PRs — same model mempool.space uses.

Repo: https://github.com/Enkryptedx/xrpldashboard

**Asks:**

1. Click around and tell me what's confusing, broken, or wrong.
2. If you spot a wallet label that's incorrect or missing, the
   KNOWN_ACCOUNTS.md PR is one click away.
3. If you have an exchange / fund / compliance contact who looks at
   XRPL data professionally, the institutional page is at
   /institutional and I'd love an intro.

Thanks for reading. Roast politely.

— Charlie
```

Notes:
- Reddit punishes self-promo without context. The "looking for feedback
  before I officially launch" framing turns it from promo → request,
  which both subs allow.
- Posting to r/XRP and r/Ripple within a few hours of each other is
  fine. Posting verbatim to r/CryptoCurrency or r/Cryptocurrency
  without modification is risky — they auto-mod self-promotion.
- Don't crosspost, just create two separate posts. Crossposts get
  less engagement.

---

## Sequencing suggestion

Day 0 (launch):
- Discord / Telegram first (lowest stakes, fastest signal)
- X thread ~2 hours later (after you've fixed anything Discord caught)
- Reddit posts evening of same day, US time (best Reddit traffic)

Day 1-2:
- Respond to every comment personally
- Fix anything substantive that came up
- Update KNOWN_ACCOUNTS.md / TOKEN_NAMES.md from any community
  corrections

Day 3+:
- One follow-up X post acknowledging the top piece of feedback received
- Quietly send the institutional page link to any 1:1 conversations
  that came in

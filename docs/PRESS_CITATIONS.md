# Press citations of xrpldashboard.com

Running record of first-party press citations that resolve to specific
articles, with verification of the cited numbers where recoverable. Kept
in verify-then-record order — no entry lands here without an accuracy
column marked y / partial / n / unverifiable.

Column key:
- Cited: the exact figures/claims attributed to /amendments (or another page)
- Verified: y = independently reconstructed from live data + our code path.
  partial = some figures confirmed, others cannot be reconstructed from
  our archived state. n = wrong. unverifiable = we don't archive the
  underlying data and no third-party archive was available.
- Sovereignty: whether our page served from the sovereign path (Lenovo
  rpc tunnel) or fell back to public RPC at the cited fetch time —
  read from walker_node_fallback.

---

## 2026-09-18 · CryptoSlate · Oluwapelumi Adejumo (Editor)

- **URL**: https://cryptoslate.com/xrpls-new-lending-tool-could-lock-up-your-xrp-from-minutes-to-decades/
- **What was cited**: "A live dashboard snapshot fetched Sept. 17 did not
  surface LendingProtocolV1_1 in the responding node's feature feed…
  placed the base LendingProtocol amendment at 13 of 35 trusted-validator
  votes and SingleAssetVault at 16 of 35, below the displayed threshold
  of 28."
- **Cited landing page**: /amendments
- **Fetch date claimed**: 2026-09-17 (hour unspecified)

### Verification (2026-09-20)

| Claim | Verdict | Basis |
|---|---|---|
| Threshold 28 of 35 | **confirmed as rippled's threshold number; CORRECTED 2026-09-25 on what it means** | VHS reports `threshold: "28/35"`; rippled's `AmendmentSet` computes `threshold = max(1, trusted*80/100)` = 28 and requires yes votes **strictly greater** than it — so a majority needs **29 of 35**, and 28 loses it (rippled `AmendmentTable.cpp`, read 2026-09-25). Our own record confirms: PermissionDelegationV1_1 held 28/35 at the 2026-09-23 roll call and lost its majority at flag ledger 107181569. The earlier "ceil(35 × 0.80) = 28 is the number needed" reading in this row was wrong by one; the page now shows "needs 29 of 35" and carries a dated correction note. Two further facts for anyone quoting the mechanism: (1) rippled carries each trusted validator's **last seen vote forward for 24 hours** (`TrustedVotes`), so a validator that is merely quiet at one roll call does not drop the tally — the vote must be absent or changed in its latest validation; (2) on the 2026-09-23 loss, a trusted validator's yes vote was absent at that roll call; the cause is not confirmed. |
| LendingProtocolV1_1 not in responding node's feature feed | **confirmed** | Live check 2026-09-20: Lenovo rippled `feature` RPC returns 104 known features. Only 2 lending/vault names present: `LendingProtocol` (hash 565B90CA…) and `SingleAssetVault` (hash 81BD2619…). VHS lists `LendingProtocolV1_1` at hash A360E2BF…, absent from Lenovo's list — matches the documented "in Majorities but the responding node does not recognize the hash" scenario noted in the amendments_state.py header comment. |
| LendingProtocol at 13/35 votes on Sept 17 | **unverifiable** | Our stack does not archive VHS vote tallies. VHS's `date=` query parameter returns current-shape tally with count=null for our targets, not a Sept-17-as-of snapshot. Lenovo's rolling window starts at ledger 107,068,362 (~2026-09-17 21:00 UTC per close-time inspection) so most of Sept 17 is below range. |
| SingleAssetVault at 16/35 votes on Sept 17 | **unverifiable** | Same as above. |
| Numbers were "below threshold" | **directionally confirmed** | Ledger 107,070,000 (Sept 18 13:14 UTC, one day after fetch): 94 enabled amendments, only 1 in Majorities (hash 9F287AED…, NOT LendingProtocol or SingleAssetVault). Both cited amendments were NOT in Majorities — consistent with the article's "below threshold" claim. |

### Sovereignty
- `walker_node_fallback` for `walker_name='amendments_state'` during Sep 14–20: **0 rows**. Zero fallbacks to public RPC across the entire outage window.
- Same window, other walkers DID record fallbacks (`check_page`, `network_pulse`, `wallet_data` on Sep 15, `network_pulse` on Sep 17). Only amendments_state stayed sovereign throughout.
- **/amendments served from Lenovo rpc tunnel on Sept 17. Not a public-RPC fallback.**

### Referrer trail
- 4 `https://cryptoslate.com/` referrers on Fri 2026-09-18 (06:02, 11:34, 15:45, 19:06 UTC) — all bare-origin per Chrome's `strict-origin-when-cross-origin` default. Now identified as originating from this article by the author himself.

---

## DRAFT batch — Sep 18–24 news referrers (fetched 2026-09-24, verify-then-record pending)

Status: **DRAFT**. Articles fetched read-only (no outlet contacted, nothing on
the site). Each read-in-full entry carries the verbatim sentence that mentions
xrpldashboard or links to us. Number verification (the `Verified` column) is NOT
yet done for this batch — do not promote to a citation section until each cited
figure is reconstructed from our data + code path.

Mention key: **linked+quoted** = a link to us AND a sentence naming us in body
copy; **link only** = href to us but no naming sentence in prose; **none** = no
link and no mention found.

Verified key (this batch): **y** = figure attributed to us reconstructed from
`amendment_tally_reconstructions` + our code path; **unverifiable** = date below
our reconstruction range (table starts 2026-09-21) or no archived tally exists;
blank = no us-attributed figure to verify (link/mention only).

Amendment-cited key: which upgrade the article is about — **Batch v1.1** /
**PermissionDelegationV1_1** / **both** / **other**.

| # | Date | Outlet | URL | Read | Amendment cited | Links to us | Verbatim mention |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-24 | Yahoo Finance (TheStreet syndication) | https://finance.yahoo.com/markets/crypto/articles/xrp-ledger-eyes-october-upgrade-205658238.html | full | PermissionDelegationV1_1 | `xrpldashboard.com/amendments` | **sourced attribution (link-as-name)**: "The upgrade in question, PermissionDelegationV1_1, entered a 14-day activation countdown on Sep. 21 after 29 of the network's 35 trusted validators backed it, according to the XRPL amendments dashboard." — Verified **y** (see below) |
| 2 | 2026-09-20 | Blockonomi | https://blockonomi.com/xrp-ledger-batch-v1-1-nears-activation-as-asset-managers-prepare | full | Batch v1.1 | `xrpldashboard.com/amendments` | **linked+quoted**: "The XRPL Dashboard lists support from 30 of 35 tracked validators." — Verified **y** (fetch 2026-09-20 06:34 UTC; Batch 30/28 + Sep-15 14:06:41 UTC majority start both match ledger; see below) |
| 3 | 2026-09-23 | Blockto | https://blockto.io/news/xrp-ledger-sets-new-date-for-feature-letting-banks-split-account-permissions | full | PermissionDelegationV1_1 | `xrpldashboard.com/amendments` | "The update, called [Batch V1.1], …" — link is inline in the sentence naming the update; our name not spelled in prose (link only) |
| 4 | 2026-09-21 | Coin Insider | https://coininsider.org/news/ripple-says-asset-managers-are-eyeing-batch-v1-1-upgrade | full | Batch v1.1 | `xrpldashboard.com/amendments` | link only, no mention in text |
| 5 | 2026-09-19 | Coinwelt (DE) | https://coinwelt.de/news/xrp-ledger-batch-v11-atomare-zahlungen | full | Batch v1.1 | `xrpldashboard.com/amendments` | link only, no mention in text |
| 6 | (undated) | Crinance | https://crinance.com/xrpl-fixes-critical-pre-mainnet-flaw-but-client-apps-remain-at-risk-123890.html | full | other (pre-mainnet flaw fix) | `xrpldashboard.com/amendments` | **linked+quoted**: "…22, xrpldashboard showed 30 of 35 trusted validators supporting the amendment, above its displayed 28-vote threshold." — Verified **y** (see below) |
| 7 | 2026-09-23 | Cryptomaan (NL) | https://cryptomaan.nl/nieuws/xrp-ledger-permission-delegation-upgrade | full | PermissionDelegationV1_1 | `xrpldashboard.com/amendments` | link only, no mention in text |
| 8 | 2026-08-28 | AllAboutXRP | https://allaboutxrp.com/news/xrpl-fixcleanup3-3-0-majority-activation-window | full | other (FixCleanup3_3_0) | `xrpldashboard.com/amendments` | **linked+quoted**: "XRPLDashboard independently displays the same count and a conditional September 11 projection." — Verified **unverifiable** (see below). ⚠ OLD article (Aug 28); Sep-11 projection is now a past date. |
| 9 | 2026-09-21 | UseTheBitcoin | https://usethebitcoin.com/news/xrp-ledger-batch-v1-1-gains-institutional-interest-ahead-of-activation | full | Batch v1.1 | `xrpldashboard.com/amendments` | link only, no mention in text |
| 10 | (undated) | WordUp News | https://wordupnews.com/cryptocurrency/xrp-ledger-delegation-upgrade-could-go-live-oct-5-will-xrp-benefit | full | PermissionDelegationV1_1 | `xrpldashboard.com/amendments` | link only, no mention in text |
| 11 | (undated) | WordUp News | https://wordupnews.com/cryptocurrency/revolut-faces-multiple-ransom-demands-with-no-direct-contact | full | other (off-topic Revolut) | none | none — no link, no mention (off-topic Revolut piece; referrer likely site-nav bleed) |
| 12 | 2026-09-19 | CoinDesk | https://www.coindesk.com/tech/2026/09/19/ripple-says-asset-managers-are-preparing-for-xrp-ledger-s-next-payments-upgrade | full | Batch v1.1 | `xrpldashboard.com/amendments` | link only, no name in prose — but **link-citation as the source of the validator count**: the `/amendments` href sits inside the "The amendment has support from [link]" sentence, i.e. CoinDesk sources the vote tally to our page via the link rather than naming us. |
| 13 | 2026-09-18 | amznusa.com (aggregator of CryptoSlate/Akiba Wright) | https://amznusa.com/xrpls-new-lending-tool-could-lock-up-your-xrp-from-minutes-to-decades-liam-akiba-wright-amznusa-com/ | full | other (lending/vault) | `xrpldashboard.com/amendments` | link only, no mention in text (scraped repost of the Sep-18 CryptoSlate lending article) |
| 14 | 2026-09-2x | New Economy (JP) | https://www.neweconomy.jp/posts/610604 | **DRAFT — not read** | (unread) | (referrer, 2 hits since Sep 24) | **DRAFT link-only** — not fetched/read; referrer bleed from analytics 2026-09-25. No naming sentence recorded. Verify + classify on next read pass. |
| 15 | 2026-09-23 | CoinDesk (2nd article) | https://www.coindesk.com/tech/2026/09/23/xrp-ledger-retries-upgrade-that-lets-banks-split-payment-and-compliance-duties | **DRAFT — not read** | (unread) | (referrer, 1 hit since Sep 24) | **DRAFT link-only** — distinct newer CoinDesk piece (domain already filed at #12 for the Sep-19 article; this is a different Sep-23 URL). Not fetched/read; no naming sentence recorded. Verify on next read pass. |

### MCP / agent-directory listings (not press citations)

| Date seen | Directory | Listing URL | Status |
|---|---|---|---|
| 2026-09-25 | mcpindex.ai | https://mcpindex.ai/server/com-xrpldashboard-xrpldashboard-mcp | **DRAFT link-only** — our MCP server appeared as a referrer 2026-09-25 (1 hit), indicating an indexed listing. Not verified beyond the referrer; confirm the listing content on next pass. |

### Headline-only (blocked, not read-in-full)

| Date | Outlet | URL | Status | Wayback |
|---|---|---|---|---|
| 2026-09-24 | TheStreet | https://www.thestreet.com/crypto/markets/xrp-ledger-eyes-an-october-upgrade-that-banks-have-been-waiting-for | HTTP 403 (anti-bot wall) | Wayback lookup itself 429'd 2026-09-24; retry. Body readable via the Yahoo syndication (entry #1). |

### Verification pass (run 2026-09-24, source: amendment_tally_reconstructions)
- **#1 Yahoo/TheStreet — VERIFIED y.** Article: "PermissionDelegationV1_1 …
  entered a 14-day activation countdown on Sep. 21 after 29 of the network's 35
  trusted validators backed it, according to the XRPL amendments dashboard."
  `amendment_tally_reconstructions` for `as_of_date='2026-09-21'` holds an
  amendment (hash `0F48FF56…`) at **29 votes** vs **unl_threshold=28**
  (source `vhs_current_at_date_2026-09-21`) — matches "29 of 35" exactly. This
  is a **sourced attribution**: the outlet credits the count to our dashboard
  via the `/amendments` link (link-as-name), the strongest citation form in
  this batch.
- **#6 Crinance — VERIFIED y.** `amendment_tally_reconstructions` for
  `as_of_date='2026-09-22'` holds exactly one amendment at **30 votes** against
  **unl_threshold=28** (source `vhs_current_at_date_2026-09-22`). Matches the
  article's "30 of 35 … above its displayed 28-vote threshold" exactly. The
  "of 35" is the UNL size; our displayed threshold ceil(35×0.80)=28 is unchanged.
- **#8 AllAboutXRP — UNVERIFIABLE.** The reconstruction table's earliest row is
  **2026-09-21**; the article is dated **2026-08-28**, below range. No
  `FixCleanup3_3_0` rows exist in the table at any date, so the "same count /
  September 11 projection" claim cannot be reconstructed from first-party
  archived data. Do not promote. (Referrer hit on Sep 18 is a re-visit of an
  old article, not fresh coverage.)
- **#2 Blockonomi — VERIFIED y (fetch-date caveat).** Fetch date pinned:
  article `datePublished` = **2026-09-20 06:34 UTC** (Brenda Mary). The 30/35
  claim is about **BatchV1_1**, not PermissionDelegation — the article states
  the majority "began September 15 at 14:06:41 UTC" and "activation could
  follow September 29," both an EXACT match to our ledger read (Batch majority
  CloseTime 2026-09-15 14:06:41 UTC → activation 2026-09-29 14:06:41 UTC, from
  amendment_majority_history). The "30 of 35" figure: `amendment_tally_recon`
  has Batch (hash 9F287AED…) at **30/28** on Sep 21, 22, 24 (dipped to 29 only
  Sep 23), and amendment_majority_history records Batch's majority at
  `vote_count_at_first = 30/28`. CAVEAT: the fetch date (Sep 20) is one day
  before the reconstruction table's earliest row (Sep 21), so the tally is
  not first-party archived FOR Sep 20 specifically — but the value is stable
  30/28 on every date we hold on both sides of that boundary, and the
  majority-start timestamp Blockonomi cites is an exact ledger match.
  Verdict: promote to y; the amendment, figure, and timestamp all reconcile.

### Corrections to the initial pass
- **#2 Blockonomi was mis-tagged "link only" in the first draft — it DOES name
  us in prose**: "The XRPL Dashboard lists support from 30 of 35 tracked
  validators." Re-grep confirmed. Reclassified linked+quoted. (Its 30/35 figure
  is the same tally verified for #6; not separately marked y pending a per-date
  check of Blockonomi's own fetch date, but consistent with our Sep-22 archive.)
- Several remaining "link only" entries embed our `/amendments` href inside a
  support-count sentence (#3 Blockto, #12 CoinDesk) without naming us — a
  **link-citation** (the link IS the source attribution) rather than a
  named-source citation. Recorded as link only per the mention key, with #12
  annotated as a link-citation source of the validator count.


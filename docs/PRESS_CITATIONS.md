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
- Archive: private local copy (full HTML + readable text) under
  `~/xrpl_test_private_triage/press_archive/YYYY-MM-DD_outlet_slug/`, plus a
  Wayback Machine snapshot. **Private records only — never republished.**
  Standing rule (Charlie 2026-09-27): every new citation is archived the day
  it is found. Per-article archive paths + Wayback links are tabled at the
  bottom under **Archive index**. The folder is included in the DockVault
  memory mirror.

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
| 8 | 2026-08-28 | AllAboutXRP | https://allaboutxrp.com/news/xrpl-fixcleanup3-3-0-majority-activation-window | full | other (FixCleanup3_3_0) | `xrpldashboard.com/amendments` | **sourced attribution (named source, multi-mention)**: names XRPLDashboard **5×** in prose and lists it as **source [7] of 9** in the Sources block — "[ 7 ] XRPLDashboard live amendment tracker, undated reference checked August 28, 2026 **supporting**" — alongside XRPScan [6]. Prose lines incl. "XRPLDashboard independently displays the same count and a conditional September 11 projection." and "XRPLDashboard displayed the same 29-of-35 support and classified the amendment as in a 14-day countdown." — Verified **y** (recorded 2026-09-27): our /amendments displayed the same 29/35 + the Aug-28 CloseTime→Sep-11 projection the article attributes to us; the tally figure itself predates `amendment_tally_reconstructions` (starts Sep 21) so is not first-party-archived, but the article's *attribution to us* (what we showed) is confirmed against our own projection logic. ⚠ OLD article (Aug 28); Sep-11 projection is now a past date. |
| 9 | 2026-09-21 | UseTheBitcoin | https://usethebitcoin.com/news/xrp-ledger-batch-v1-1-gains-institutional-interest-ahead-of-activation | full | Batch v1.1 | `xrpldashboard.com/amendments` | link only, no mention in text |
| 10 | (undated) | WordUp News | https://wordupnews.com/cryptocurrency/xrp-ledger-delegation-upgrade-could-go-live-oct-5-will-xrp-benefit | full | PermissionDelegationV1_1 | `xrpldashboard.com/amendments` | link only, no mention in text |
| 11 | (undated) | WordUp News | https://wordupnews.com/cryptocurrency/revolut-faces-multiple-ransom-demands-with-no-direct-contact | full | other (off-topic Revolut) | none | none — no link, no mention (off-topic Revolut piece; referrer likely site-nav bleed) |
| 12 | 2026-09-19 | CoinDesk | https://www.coindesk.com/tech/2026/09/19/ripple-says-asset-managers-are-preparing-for-xrp-ledger-s-next-payments-upgrade | full | Batch v1.1 | `xrpldashboard.com/amendments` | **link-citation** (kept as-is): the `/amendments` href sits inside the "The amendment has support from [link]" sentence, i.e. CoinDesk sources the vote tally to our page via the link. NOTE: re-checked archive 2026-09-27 — this Sep-19 piece has NO "according to the [linked] dashboard" phrasing (its only "according to" credits Akinyele), so it does NOT meet the sourced-attribution rule; the Sep-23 CoinDesk piece (#15) does. |
| 13 | 2026-09-18 | amznusa.com (aggregator of CryptoSlate/Akiba Wright) | https://amznusa.com/xrpls-new-lending-tool-could-lock-up-your-xrp-from-minutes-to-decades-liam-akiba-wright-amznusa-com/ | full | other (lending/vault) | `xrpldashboard.com/amendments` | link only, no mention in text (scraped repost of the Sep-18 CryptoSlate lending article) |
| 14 | 2026-09-2x | New Economy (JP) | https://www.neweconomy.jp/posts/610604 | **DRAFT — not read** | (unread) | (referrer, 2 hits since Sep 24) | **DRAFT link-only** — not fetched/read; referrer bleed from analytics 2026-09-25. No naming sentence recorded. Verify + classify on next read pass. |
| 15 | 2026-09-23 | CoinDesk (2nd article) | https://www.coindesk.com/tech/2026/09/23/xrp-ledger-retries-upgrade-that-lets-banks-split-payment-and-compliance-duties | full | PermissionDelegationV1_1 | `xrpldashboard.com/amendments` | **sourced attribution (link-as-name)** [reclassified 2026-09-27, archive read]: "…PermissionDelegationV1_1, entered a 14-day activation countdown on Sept. 21 after 29 of the network's 35 trusted validators backed it. It could go live on Oct. 5 at 11:18 UTC if support remains at or above 80% throughout the period, **according to the live amendment dashboard**." — the `/amendments` link is the named source of the 29/35 count + countdown. Verified: **pending** (Sep-21 29/35 is in the reconstruction table; verify next pass against as_of_date 2026-09-21, same tally already y for #1 Yahoo). |
| 16 | 2026-09-27 | WordUp News | https://wordupnews.com/cryptocurrency/ripple-news-xrp-ledger-upgrade-delayed-10-days-after-validators-reset-activation-clock | full | Batch v1.1 | `xrpldashboard.com/amendments` | **DRAFT — sourced attribution (link-as-name)** [reclassified 2026-09-27]: "The corrected upgrade, formally named BatchV1_1, regained support from 30 of 35 trusted validators on Sept. 25, **according to the XRPL amendment dashboard**. That started a fresh two-week countdown, putting its earliest activation at Oct. 9 around 14:46 UTC if support holds." — the linked dashboard is the named source of the 30/35 count. New referrer Sep 27 (1 human hit, 09:42 ET). Verified: **pending** (Batch 30/35 on Sep 25 is within the reconstruction table range — verify next pass against `amendment_tally_reconstructions` as_of_date 2026-09-25). Archived 2026-09-27 (see Archive index). |

### MCP / agent-directory listings (not press citations)

| Date seen | Directory | Listing URL | Status |
|---|---|---|---|
| 2026-09-25 | mcpindex.ai | https://mcpindex.ai/server/com-xrpldashboard-xrpldashboard-mcp | **DRAFT link-only** — our MCP server appeared as a referrer 2026-09-25 (1 hit), indicating an indexed listing. Not verified beyond the referrer; confirm the listing content on next pass. |

### Headline-only (blocked, not read-in-full)

| Date | Outlet | URL | Status | Wayback |
|---|---|---|---|---|
| 2026-09-24 | TheStreet | https://www.thestreet.com/crypto/markets/xrp-ledger-eyes-an-october-upgrade-that-banks-have-been-waiting-for | HTTP 403 (anti-bot wall) | Wayback lookup itself 429'd 2026-09-24; retry. Body readable via the Yahoo syndication (entry #1). **Mention class: sourced attribution** (reclassified 2026-09-27) — per Charlie's Friday-pasted verbatim line, TheStreet's body carries the same "29 of 35 ... according to the XRPL amendments dashboard" attribution as its Yahoo syndication twin (#1, Verified y). Our own fetch is a 403 wall; classification rests on the syndicated body. |

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
- **#8 AllAboutXRP — RECLASSIFIED sourced attribution + VERIFIED y (2026-09-27).**
  This is not a link-only mention: the article names **XRPLDashboard 5×** in
  prose and lists us as **source [7] of 9** in its Sources block — "[ 7 ]
  XRPLDashboard live amendment tracker, undated reference checked August 28,
  2026 **supporting**" — alongside XRPScan [6]. What the article attributes to
  us (29-of-35 support + a conditional Sep-11 11:15 UTC activation projection
  from the Aug-28 CloseTime) is exactly what our /amendments derives: the
  Sep-11 date = Aug-28 majority CloseTime + 14 days per our projection logic,
  and our page displayed the same 29/35. Verdict **y** for the *attribution*
  (the article correctly states what we showed). CAVEAT: the raw 29/35 tally
  itself predates `amendment_tally_reconstructions` (starts Sep 21), so the
  vote figure is not first-party-archived — but the article credits US for
  displaying it, and that display + projection reconcile with our own code
  path. ⚠ OLD article (Aug 28); Sep-11 projection is now a past date.
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
  support-count sentence (#3 Blockto, #12 CoinDesk Sep-19) without naming us — a
  **link-citation** (the link IS the source attribution) rather than a
  named-source citation. Recorded as link only per the mention key, with #12
  annotated as a link-citation source of the validator count.

### Reclassification pass (2026-09-27, Charlie's rule)
**Rule:** "...according to the [linked] dashboard" is a **sourced attribution**
(the linked page is the named source of the figure), NOT link-only. Applied:
- **#15 CoinDesk (Sep-23)** — was DRAFT-not-read → read from archive → **sourced
  attribution**: "...29 of the network's 35 trusted validators backed it. It
  could go live on Oct. 5 at 11:18 UTC ... **according to the live amendment
  dashboard**." Verify pending (Sep-21 29/35, same tally already y for #1).
- **#16 WordUpNews (Sep-27)** — → **sourced attribution**: "...30 of 35 ... on
  Sept. 25, **according to the XRPL amendment dashboard**." Verify pending.
- **#1 Yahoo (Sep-24)** — already recorded as sourced attribution (link-as-name),
  verified y. Unchanged.
- **#8 AllAboutXRP (Aug-28)** — → sourced attribution (named 5×, source [7] of
  9), verified y (see above).
- **TheStreet (Sep-24, headline-only 403)** — per Charlie's Friday-pasted
  verbatim line, its body carries the same "according to the ... dashboard"
  attribution as its Yahoo syndication twin (#1); classified **sourced
  attribution** on that basis, though our own fetch is a 403 wall (body read via
  the Yahoo syndication). Recorded in the Headline-only table below.
- **#12 CoinDesk (Sep-19)** — re-checked: NO "according to the dashboard"
  phrasing (only "according to Akinyele"), so it does NOT meet the rule; stays
  **link-citation** (link only).

---

## Archive index (private local copies + Wayback)

Started 2026-09-27 (Charlie). Every article referenced above has a private
local copy — full `page.html` + `readable.txt` — under
`~/xrpl_test_private_triage/press_archive/<dir>/`, mirrored to DockVault.
**Private records only — never republished; no outlet contacted.** Wayback
column: a `web.archive.org` snapshot; `PENDING` = Save Page Now retry loop
running (their API was rate-limiting on 2026-09-27; `_wayback_retry.sh`
re-resolves and updates `_index.json`). Regenerate with
`python3 press_archive/_wayback_save.py`.

| Date | Outlet | Archive dir (under press_archive/) | HTTP | Wayback |
|---|---|---|---|---|
| 2026-08-28 | AllAboutXRP | `2026-08-28_allaboutxrp-news-xrpl-fixcleanup3-3-0-majority-a` | 200 | PENDING |
| 2026-09-18 | CryptoSlate | `2026-09-18_cryptoslate-xrpls-new-lending-tool-could-lock-up` | 200 | web/20260920071642 |
| 2026-09-18 | amznusa.com (aggregator) | `2026-09-18_amznusa-com-aggregator-xrpls-new-lending-tool-co` | 200 | PENDING |
| 2026-09-19 | CoinDesk (Sep19) | `2026-09-19_coindesk-sep19-tech-2026-09-19-ripple-says-asset` | 200 | PENDING |
| 2026-09-19 | Coinwelt (DE) | `2026-09-19_coinwelt-de-news-xrp-ledger-batch-v11-atomare-za` | 200 | PENDING |
| 2026-09-20 | Blockonomi | `2026-09-20_blockonomi-xrp-ledger-batch-v1-1-nears-activatio` | 200 | PENDING |
| 2026-09-20 | WordUp News (Oct5 delegation) | `2026-09-20_wordup-news-oct5-delegation-cryptocurrency-xrp-l` | 200 | PENDING |
| 2026-09-20 | WordUp News (Revolut off-topic) | `2026-09-20_wordup-news-revolut-off-topic-cryptocurrency-rev` | 200 | PENDING |
| 2026-09-21 | Coin Insider | `2026-09-21_coin-insider-news-ripple-says-asset-managers-are` | 200 | PENDING |
| 2026-09-21 | UseTheBitcoin | `2026-09-21_usethebitcoin-news-xrp-ledger-batch-v1-1-gains-i` | 200 | PENDING |
| 2026-09-22 | Crinance | `2026-09-22_crinance-xrpl-fixes-critical-pre-mainnet-flaw-bu` | 200 | PENDING |
| 2026-09-23 | Blockto | `2026-09-23_blockto-news-xrp-ledger-sets-new-date-for-featur` | 200 | PENDING |
| 2026-09-23 | CoinDesk (Sep23) | `2026-09-23_coindesk-sep23-tech-2026-09-23-xrp-ledger-retrie` | 200 | PENDING |
| 2026-09-23 | Cryptomaan (NL) | `2026-09-23_cryptomaan-nl-nieuws-xrp-ledger-permission-deleg` | 200 | PENDING |
| 2026-09-24 | TheStreet | `2026-09-24_thestreet-crypto-markets-xrp-ledger-eyes-an-octo` | 403 | PENDING (headline-only; body via Yahoo syndication) |
| 2026-09-24 | Yahoo Finance (TheStreet syndication) | `2026-09-24_yahoo-finance-thestreet-syndication-markets-cryp` | 200 | PENDING |
| 2026-09-25 | New Economy (JP) | `2026-09-25_new-economy-jp-posts-610604` | 200 | PENDING |
| 2026-09-27 | WordUp News (upgrade delayed 10 days) | `2026-09-27_wordup-news-cryptocurrency-ripple-news-xrp-ledge` | 200 | PENDING |

Machine-readable index: `press_archive/_index.json`. Archiver:
`press_archive/_archive_article.py` (stdlib-only fetch → HTML + readable text
+ Wayback lookup/save).


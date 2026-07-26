# /check — General Scam-Signal Aggregator
## Design Document

**Date:** 2026-07-25
**Status:** DESIGN ONLY — no build until Charlie reviews
**Founding framing:** *We do not detect scams. We aggregate facts from named sources with timestamps, and let the reader decide. Every phrase, every field, every fence in this document exists to keep that sentence true as the tool grows.*

---

## Motivation

The `/check` page today verifies XRPL claims — paste an address, token, or URL, get sourced signals about who has publicly claimed it. It works. Users pass through cleanly and it has already caught real impersonation attempts.

The mission expansion is straightforward: **the world is being scammed by phone calls, SMS, email, mail, social media, and AI-generated pitches, and the pattern is stable.** Every scam, pre-AI and post-AI, converges on the same skeleton: **urgency + payment demand + unverifiable contact + brand-new infrastructure.** A tool that surfaces those structural markers — without ever claiming "this is a scam" — helps ordinary people evaluate what's in front of them.

This doc is the constitution for that expansion. It exists so nobody (human or AI) six months from now suggests a "trust score" or a "risk badge" without first reading why the answer is no. The fence lines below are load-bearing legally, editorially, and operationally.

---

## The one thesis

**Detect structure, not intent.**

Intent detection is a treadmill — scammers use AI, we use AI to detect their AI, they use better AI, we use better AI. Nobody wins and every hop introduces false positives against innocent people.

Structure detection is stable. A three-day-old domain sending "your account is suspended" was a scam in 2015 and it's a scam in 2026. The linguistic signature (urgency + authority + payment) has been the same for a century of confidence scams. The infrastructure signature (young domain, brand-new phone number, throwaway email) has been the same for thirty years of internet fraud.

Surface the structure verbatim. Cite the source. Let the reader decide.

---

## The three phases

### Phase 1 — foundations (free, small)

Ships the pattern for everything that follows.

- **Domain age** via RDAP (`rdap.org` / registrar RDAP endpoints) + earliest SSL certificate via Certificate Transparency (`crt.sh`). Signal: *"Domain registered N days ago; earliest SSL cert issued YYYY-MM-DD."* No key, no cost.
- **OFAC SDN cross-check** for any wallet address in any chain. Signal: *"Not present in the U.S. Treasury OFAC SDN list, checked YYYY-MM-DD"* or *"Present in the OFAC SDN list, entry dated YYYY-MM-DD."* Free, official, US Treasury action = zero defamation risk.
- **FCRA disclaimer live from day one.** Bold, unmissable, on the results page and the page footer: *"Not a consumer report. Not for use in decisions covered by the Fair Credit Reporting Act (employment, credit, housing, insurance)."* This is the single most important legal fence in the tool.
- **Mozilla PublicSuffix List** integrated for correct eTLD+1 extraction in URL checks (avoids typosquatting parse bugs).

**Data touched:** none that qualifies as personal data under any jurisdiction reviewed. Domain names, wallet addresses, sanctions list. Ship without legal consult.

### Phase 2 — the phone flagship (free, medium)

Where personal data enters the product. Legal-consult gate sits between Phase 1 and Phase 2 (see [Legal considerations](#international-legal-considerations)).

- **`phonenumbers` library** (Google `libphonenumber`, Apache-2.0, PyPI package `phonenumbers`) running locally. Signals: validity, country, region, timezone, line type (where derivable — US returns `FIXED_LINE_OR_MOBILE` for most NANP numbers; international coverage is better). No API call, no third-party data transfer.
- **FTC DNC Reported Calls API** (`api.ftc.gov/v0/dnc-complaints`) — locally ingested and indexed. FTC API does NOT support per-number filter; ingest is required. Signals: *"FTC received N Do-Not-Call complaints about this number between YYYY-MM-DD and YYYY-MM-DD. Most recent subject: '…'. Marked robocall: Y/N."*
- **FCC Unwanted Calls dataset** (`opendata.fcc.gov/Consumer/Consumer-Complaints-Data-Unwanted-Calls/vakf-fz8e`) — also locally mirrored. Signals: *"FCC received M unwanted-call complaints about this number, most recent YYYY-MM-DD, issue: 'Robocalls'."*
- **Rotating-numbers caveat** shown on every phone result, front and center: *"A clean lookup does not mean the number is safe. Scammers rotate phone numbers in days. Absence of complaints ≠ absence of risk."*
- **International honesty line** shown on every phone result: *"US complaint records only apply to US numbers. For international numbers we provide structural information from public numbering plans; no comparable open per-number complaint data exists for other countries."*

**Data touched:** phone numbers. Classified as personal data under GDPR, PIPEDA, LGPD, APPI, Australia Privacy Act. See [Legal considerations](#international-legal-considerations).

### Phase 3 — extended coverage (free, medium)

- **URL blocklist mirrors**: URLhaus (abuse.ch), MetaMask `eth-phishing-detect`, ScamSniffer scam-database. Each mirrored locally with daily refresh, each quoted verbatim with source + fetched-at timestamp. URLhaus terms of use permit non-commercial republish; if the site ever monetizes materially, buy the commercial license.
- **Chainabuse API** for cross-chain wallet reports (ETH / BTC / SOL / TRON / etc). Free tier. Republish verbatim: *"N Chainabuse reports categorized as [X], most recent [date]."* Same house-style guardrails as any crowdsourced source — never aggregate into a score.
- **Hosting-infrastructure fingerprint (URL side only)** — for URL checks, resolve the domain and report structural facts about where it is hosted: ASN + hoster name (e.g., "hosted at DigitalOcean, ASN 14061"), country of the hosting IP, and whether origin is masked by a public CDN (Cloudflare / Fastly / Akamai). Sources: MaxMind GeoLite2 ASN (free tier), CAIDA ASN database, DNS + IP-to-ASN lookup. **NOT a "VPN detector"** — that framing fails the base-rate test (privacy-conscious users, journalists, iCloud Private Relay, and everyone in a censored country all use VPNs; a signal that fires on ~half the legitimate internet is noise, not signal). Report infrastructure as evidence-not-verdict: "hosted at Provider X in Country Y, origin masked by Cloudflare" lets the reader draw their own inference from *specific facts* (bulletproof host vs Fortune-500 datacenter, jurisdiction with takedown mechanism vs without). Do **not** ship a "suspicious hosting" pill or aggregate score. Base-rate caveat renders on-page alongside the signal itself: *"Hosting infrastructure alone is not a scam signal — legitimate sites also use CDNs, hide origins, and host in low-cost providers. This describes where the site lives, not what it is doing."*
- **Sourcify** (optional, deferred) — open-source verified smart contract registry, if we ever check ETH contract addresses.

**Data touched:** URLs, cross-chain wallet addresses, and URL-derived hosting infrastructure (ASN, hoster, country, CDN). Not personal data under standard interpretations — hosting facts describe infrastructure, not individuals.

**Hosting-fingerprint legal note:** Labeling infrastructure edges closer to defamation-shaped territory than sanctions lookup (a sanctions match is a government-published fact about a specific address; a hosting note is our characterization of a business relationship). The Phase 3 legal review — which happens whenever URL blocklists ship — expands to cover the exact wording of hosting-fingerprint copy. Neutral factual language only ("hosted at X, ASN Y"), never inference language ("cheap VPS", "bulletproof host", "commonly used by scammers"). If the attorney has any concern, ship blocklists without the hosting fingerprint and revisit.

---

## Data architecture

### The mirror-both pattern

**Both FTC and FCC are locally mirrored.** FTC forces this (no per-number filter). FCC could be queried live via Socrata, but we mirror it anyway for three reasons:

1. **One local query path.** Uniform code path, uniform failure modes, uniform freshness stamps.
2. **No runtime dependency on third-party uptime.** The external-CDN discipline (avoid third-party runtime paths) transfers cleanly to data sources.
3. **Query performance.** Indexed Postgres beats a Socrata roundtrip every time.

### The ingest walker

The FTC + FCC ingest is a walker in the same shape as every other walker in this codebase:

- **Scope declaration** row in `walker_scope_declarations` (declared scope, filter notes, honest partials).
- **Health row** in `walker_health` (last success, cadence, severity).
- **Freshness stamp** exposed on `/check` results so users see when the underlying data was last synced.
- **CLAIMS.yaml entries** for the phone-related claims on `/check`, so any future diff touching the phone flow trips the checker.
- **Politeness discipline** for backfill: chunked, slept, heartbeat-logged. Mirror the NFT backfill's pattern.

Nothing novel to build. Every discipline this shop has been perfecting all summer applies unchanged.

### Storage projection

- FTC: ~2.6M complaints/year × ~300 bytes/row ≈ 800 MB/year raw, ~400 MB indexed.
- FCC: ~200k/year at similar row size ≈ 60 MB/year.
- Full 10-year backfill: ~5 GB in Postgres including indexes. Trivial for Neon.

---

## Safety fences (with reasoning)

Every fence below has a *why*. If you propose editing one, read the why first.

### Fence 1 — every signal has (label, value, source_label, checked_at_utc)

**Why:** This is the legal shield. Section 230 protects republishing third-party facts; it does not protect content you author. The moment `/check` synthesizes ("we think this is…"), we become the speaker and defamation exposure opens. Every signal must be traceable to a named external source with a date.

### Fence 2 — no verdicts, no scores, no risk badges

**Why:** A "scam risk: high" verdict is the tool's speech, not a republished fact. Drops §230, converts every reported number into a defamation lawsuit vector. Also: crowdsourced scores mask absent evidence, which breaks the honest-limits discipline.

### Fence 3 — no reverse-phone owner lookup, no CNAM

**Why:** "Who owns this number" is FCRA-adjacent territory. Twilio's caller-name (CNAM) service is functionally the same thing wearing a carrier-vendor wrapper. Don't buy it. Don't build it. Even if it's technically available, it's the wrong shape for this tool.

### Fence 4 — no user-submitted reports

**Why:** The moment we accept user-generated scam accusations, we become a data controller for user-contributed defamatory content globally. GDPR obligations multiply, UK Online Safety Act scope potentially engages, defamation republication risk becomes a live vector in every jurisdiction. The tool works by aggregating already-published official data, not by originating claims.

### Fence 5 — no query retention tied to visitor identity

**Why:** The existing POST-triage privacy contract (pasted messages never stored) is a first-order privacy discipline. Extending it: no query is logged with visitor IP, session ID, or any identifier that would allow reconstructing "who asked about whom." This keeps the tool outside "processing personal data" territory in most jurisdictions and eliminates a subpoena target.

### Fence 6 — never use the word "scam" as a verdict

**Why:** Legal (verdict = speech = §230 gone) and honest (we don't have the standing to make that call). The tool aggregates evidence. Users make the call.

### Fence 7 — rotating-numbers caveat, front and center, always

**Why:** A "clean" lookup can create a false-negative verdict by omission. Scammers rotate phone numbers in days. If the tool shows zero complaints without saying "this may mean not-yet-reported, not safe," the absence becomes an implied endorsement. Explicit caveat prevents that inversion.

### Fence 8 — verbatim republish only, never aggregate

**Why:** The moment we combine "N FTC complaints + M FCC complaints + Chainabuse reports" into a composite number or ordinal, we're authoring a claim. Verbatim republish keeps the tool a librarian, not a judge.

### Fence 9 — international scope is honest-by-default

**Why:** For US numbers we have FTC and FCC. For non-US numbers we have neither, and no free source publishes comparable data. Rather than fake coverage, we declare the limit: *"For international numbers we provide structural information from public numbering plans; no comparable open per-number complaint data exists for other countries."* The declared limit doubles as a credibility signal.

### Fence 10 — FCRA disclaimer live from day one

**Why:** If the tool's outputs are used or held out for use in employment, credit, housing, insurance, or tenant screening decisions, we may be classified as a Consumer Reporting Agency — an existential compliance load. The disclaimer is cheap; the exposure without it is not.

---

## Don't-build landmines (with reasoning)

Ideas that sound good and are not. If you find yourself considering any of these, re-read this section first.

- **Aggregate "trust score" / "risk badge" / colored gauge.** See Fence 2. This is the single most tempting mistake because it "just presents the data more clearly." It also drops §230 immunity and creates the strongest defamation vector this tool could invent.
- **Reverse-phone "who owns this number" lookup.** FCRA landmine. Every vendor's TOS forbids the consumer-scam use case anyway.
- **Twilio caller-name (CNAM).** Same as above wearing vendor clothing. Line-type is fine (structural fact). Caller-name is not (identifies a person).
- **Any user-submitted scam-report input.** See Fence 4. Turns the tool into a data controller for defamatory user content globally.
- **Twitter / X handle lookup at scale.** Post-Feb 2026 pay-per-use pricing makes this cost-prohibitive. Bluesky and Farcaster (both free, both open) are the alternatives if we ever add social-handle checks.
- **AI-generated text detection verdict.** All current detectors flag legitimate human writing (Shakespeare, the US Constitution) as AI-generated. There is no reliable source to cite. Surface structural markers (urgency phrases, payment-rail keywords) verbatim instead.
- **Arbitrary-email HaveIBeenPwned lookup.** Their license and our framing collide. Their tool is the tool for that job.
- **VirusTotal Public API integration on a monetized public site.** Permanent-ban risk per their TOS.
- **Community sources (800notes, tellows, TrueCaller-scraped).** Wrong shape — user-opinion content is not "facts from a named source with a timestamp." Adding them dilutes the frame that keeps the tool legally sound.
- **Google Safe Browsing v4** (deprecated) or **Web Risk** (paid, GCP-billed) unless traffic economics change materially.
- **Screenshot / image scanning for scam UI patterns.** Demo-cool, accuracy-poor, no authoritative source to cite.

---

## International legal considerations

This section is deliberately more detailed than the rest of the doc. It is the section a lawyer will read first.

### GDPR (EU / EEA) and UK GDPR

- **Phone numbers are personal data.** Standard classification, uncontested.
- **Legal basis for processing:** legitimate interest under Article 6(1)(f), grounded in **Recital 47** which states in plain text: *"The processing of personal data strictly necessary for the purposes of preventing fraud also constitutes a legitimate interest of the data controller concerned."* Fraud prevention is named by the regulation itself as a legitimate interest. This is the strongest starting position possible for a legitimate-interest analysis.
- **Balancing test** (required under legitimate interest): the tool aggregates already-public official data (FTC and FCC public records) for the purpose of consumer fraud awareness. Data subjects have limited privacy interest in complaints that are already published by their own government. The balance favors processing.
- **Right to erasure (Article 17):** individuals can request removal of specific numbers from `/check` results even if they appear in FTC/FCC data. **A contact path for erasure requests must be published on `/check`.** We honor requests by adding a per-number suppression list checked before results render.
- **Data minimization:** we do not log pasted queries tied to visitor identity. No data-subject-to-querier linkage is created.

### UK Online Safety Act

The OSA regulates "user-to-user services" (users share content with each other) and "search services." `/check` is neither: no user-generated content, no query retention, no user-submitted reports (see Fence 4). **A lookup tool without UGC is plausibly outside the OSA's scope entirely.** This is a designed-out risk, not an open uncertainty. The design's existing rules are what keep it there — do not add any user-content feature without re-evaluating OSA scope.

### DSA (EU Digital Services Act)

- The DSA's hosting-liability framework applies to any intermediary; it is workable.
- The DSA's **trader-verification (KYBC) obligations apply to online marketplaces**, not informational lookup tools. `/check` is not a marketplace.
- Standard transparency obligations apply at very-large-platform scale, which is far above the tool's current or expected reach.

### Defamation — international variance

- **US:** actual-malice standard, high bar, `/check`'s verbatim-republish + named-source design is a well-established defense.
- **UK (Defamation Act 2013):** serious-harm threshold helps, but republication doctrine can create liability even for accurately quoted third-party accusations. The house-style discipline (source + date + verbatim + no synthesis) is the primary defense.
- **Germany / France:** strong personality rights, criminal libel exists. Same house-style discipline applies.
- **Australia (2021 defamation reforms):** added a serious-harm threshold, still plaintiff-friendlier than US. Same discipline applies.

### PIPEDA (Canada), LGPD (Brazil), APPI (Japan), Privacy Act (Australia)

Similar frameworks to GDPR with local variations. Fraud-prevention purpose is generally accepted; the "publicly available exception" is narrower than in the US. The same processing minimization (no query logging, per-request scoping, honored erasure requests) applies.

### The section 230 gap

There is no clean international equivalent to Section 230. In most jurisdictions, republishing a third-party accusation can itself create publisher liability. `/check`'s protection outside the US comes from:

1. **Source authority:** we republish government data (FTC, FCC, OFAC) — not private accusations. Government complaint records are inherently more legally-robust to republish.
2. **Verbatim quotation:** no synthesis, no verdict.
3. **Fence 5** (no query retention) minimizes the data-subject-to-tool relationship.
4. **Fence 4** (no user-submitted content) keeps us from becoming the publisher-of-first-instance for anyone's accusations.

### Legal-consult gate between Phase 1 and Phase 2

Phase 1 touches no personal data (domains, sanctions list, FCRA disclaimer). **No legal review needed to ship Phase 1.**

Phase 2 introduces phone-number processing — personal data in every jurisdiction reviewed. **Before Phase 2 ships:** book a 1-2 hour internet-law attorney consult ($300-800 typical) to sanity-check this design doc and the Phase 2 plan. Note-on-file for how to handle a GDPR erasure request.

---

## Free-forever policy

**The tool is free for retail use, forever, without conditions.**

Reasoning:
- Trust anchor: a free scam-check tool cited by others = adoption + backlinks + citation moat.
- Mission alignment: xrpldashboard is public-good analytics; gating this tool would break voice.
- Retail utility stands on its own — no dependency on a hypothetical future paid tier.

If commercial value is ever added on top (SMS alerts, bulk lookup exports, per-org dashboards, programmatic API access at volume), those become paid features living alongside the free retail tool, not gates on it. Institutional monetization decisions live in a separate plan (API v1 track), and are decoupled from the retail tool's shape.

---

## Future API access

**This section documents the door. It does not open it.**

`/check`'s signal set is a natural candidate endpoint family for API v1 if and when API v1 unparks. The API itself remains gated behind the standing legal checklist (see `project_xrpldashboard_api_v1_anchors` memory + the working-tree parking rule); nothing in this section changes that gate.

### The contract, when it ships

If `/check` signals are ever exposed programmatically, the response contract mirrors the page's editorial contract:

- **Same evidence-not-verdicts format.** Every field carries a `label`, `value`, `source`, and `checked_at_utc`. No composite scores, no risk badges, no verdicts — the same rules that govern the HTML govern the JSON.
- **FCRA disclaimer machine-readable in every response body.** Not a footnote, not a header — a top-level field in every response envelope. Agents consuming this data must carry the disclaimer downstream to their own users. Stripping it is a TOS violation. This propagates Fence 10 across the API boundary.
- **Rate-limited per key.** Standard API v1 tiering applies. Free tier for developer exploration, paid tiers for higher volume, contract-priced tier for bulk (see below).

### Bulk access is its own legal gate

One-human-one-lookup (the page) and automated bulk screening (an API pipeline) are **different legal animals**. FCRA exposure (someone running our data through a tenant-screening or employment-screening pipeline) scales with automation. GDPR exposure (per-request lawful basis argumentation vs. systematic profiling) also scales with automation. Retail lookups are within the fence lines documented above; bulk automated screening is not automatically covered by the same analysis.

**The pre-Phase-2 legal consult's scope expands to cover the API question.** The lawyer reviews the retail Phase 2 design *and* the API exposure question in the same sitting — one $300-800 consult, two answers instead of one. Bulk-access tier stays off the menu until that consult is done and the API v1 gate itself has cleared.

### Why the door stays documented

AI agents evaluating suspicious content for their end users are the natural consumers of structured facts without verdicts. That is agent-native format — no synthesis to unwind, no proprietary score to trust, just cited evidence with timestamps. This is the citation moat with a machine-readable interface. Documenting the door now prevents future contributors from bolting on a paid-verdict endpoint later that would break the same fences the retail tool is engineered around.

---

## Naming constraints

- **Never** use "scam detector," "fraud detector," "risk score," "trust score," "reputation score," or any equivalent verdict-shaped label in copy, marketing, meta tags, headers, or button text.
- **Do** use "check," "signals," "evidence," "public records," "verified against," "facts about," "what we found."
- Page and feature names are conservative and technical. The `/check` route stays `/check`.

---

## What ships in a Phase 1 commit

For clarity when Phase 1 is executed:

1. `check_data.py` gains three new signal producers: `_signal_domain_age`, `_signal_earliest_ssl_cert`, `_signal_ofac_sdn_match`.
2. `check_url` integrates the two domain signals into its output.
3. `check_address` and `check_token` integrate the OFAC signal.
4. `templates/check.html` gains the FCRA disclaimer (bold, unmissable, results page + footer).
5. `CLAIMS.yaml` gains entries for the new signals so any future diff to `check_data.py` trips the checker.
6. `docs/CHECK_IT_DESIGN.md` (this file) is committed alongside so the fence set enters the tree at the same commit as the first feature.
7. Nothing personal-data touching. No phone code, no ingest walker, no lawyer needed for Phase 1.

---

## Sources

**Phase 1:**
- RDAP (registration data access protocol): https://rdap.org/
- Certificate Transparency search: https://crt.sh/
- OFAC SDN list: https://www.treasury.gov/ofac/downloads/sdn.xml
- FCRA text: https://www.ftc.gov/legal-library/browse/statutes/fair-credit-reporting-act
- Mozilla PublicSuffix List: https://publicsuffix.org/

**Phase 2:**
- Google libphonenumber: https://github.com/google/libphonenumber
- Python `phonenumbers` package: https://pypi.org/project/phonenumbers/
- FTC DNC Reported Calls API: https://www.ftc.gov/developer/api/v0/endpoints/do-not-call-dnc-reported-calls-data-api
- FCC Unwanted Calls dataset: https://opendata.fcc.gov/Consumer/Consumer-Complaints-Data-Unwanted-Calls/vakf-fz8e
- GDPR Recital 47 (fraud prevention): https://gdpr-info.eu/recitals/no-47/

**Phase 3:**
- URLhaus (abuse.ch): https://urlhaus.abuse.ch/
- MetaMask eth-phishing-detect: https://github.com/MetaMask/eth-phishing-detect
- Scam Sniffer scam-database: https://github.com/scamsniffer/scam-database
- Chainabuse API: https://docs.chainabuse.com/
- MaxMind GeoLite2 ASN database: https://dev.maxmind.com/geoip/geolite2-free-geolocation-data
- CAIDA AS Rank / ASN metadata: https://asrank.caida.org/
- Sourcify: https://sourcify.dev/

**Legal:**
- GDPR Recital 47: https://gdpr-info.eu/recitals/no-47/
- UK Online Safety Act 2023 scope: https://www.legislation.gov.uk/ukpga/2023/50
- EU DSA: https://digital-strategy.ec.europa.eu/en/policies/digital-services-act-package
- FTC guidance on Fair Credit Reporting Act: https://www.ftc.gov/legal-library/browse/statutes/fair-credit-reporting-act

# DNS Archaeology — xrpldashboard.com apex ghost (2026-08-26)

**Investigator:** claude/openclaw (Charlie's assistant)
**Started:** 2026-08-26 18:56 EDT · **Filed:** 2026-08-26 20:15 EDT
**Class:** Cloudflare-internal authoritative-serving-layer corruption, external-only impact

---

## Executive summary

The Cloudflare-hosted zone for `xrpldashboard.com` shows a correct apex A record (`216.24.57.1` Proxied, Render's shared static IP), but Cloudflare's authoritative name servers (`koa.ns.cloudflare.com`, `zoe.ns.cloudflare.com`) serve a stale AWS EC2 address `18.204.152.241` over UDP:53 with TTL `4294967295` (max int32, ~136 years, Cloudflare's internal "cache forever" sentinel value). DoH (HTTPS-transport) queries against the *same* authoritative NS return the correct proxied edge IPs. The stale record survived: proxy toggle off/save, proxy toggle on/save, and a full delete + fresh re-add. This is Cloudflare-internal ghost data — the archaeology grep proves the IP has no origin in Charlie's infrastructure or git history.

Business impact: automated/API clients (bots, LLM fetchers, monitoring, our own machine-facing surfaces) that use UDP resolvers time out. Browser humans hit the Cloudflare edge normally and get served (traffic log shows 113 unique visitors + 2.86k requests in the poisoning window).

---

## 1 — Multi-vantage survey (18:56 EDT, before any surgery)

| Vantage | Transport | Answer | TTL | Verdict |
|---|---|---|---|---|
| `koa.ns.cloudflare.com` (auth) | UDP:53 | `18.204.152.241` (×2 duplicated) | 4294967295 | 🔴 STALE |
| `zoe.ns.cloudflare.com` (auth) | UDP:53 | `18.204.152.241` (×2 duplicated) | 4294967295 | 🔴 STALE |
| `1.1.1.1` Cloudflare public | UDP:53 | `18.204.152.241` | cached | 🔴 STALE |
| `1.0.0.1` Cloudflare public | UDP:53 | `18.204.152.241` | cached | 🔴 STALE |
| `8.8.8.8` Google | UDP:53 | `18.204.152.241` | cached | 🔴 STALE |
| `8.8.4.4` Google | UDP:53 | `18.204.152.241` | cached | 🔴 STALE |
| `9.9.9.9` Quad9 | UDP:53 | `18.204.152.241` | cached | 🔴 STALE |
| `149.112.112.112` Quad9 | UDP:53 | `18.204.152.241` | cached | 🔴 STALE |
| `208.67.222.222` OpenDNS | UDP:53 | `18.204.152.241` | cached | 🔴 STALE |
| `208.67.220.220` OpenDNS | UDP:53 | `18.204.152.241` | cached | 🔴 STALE |
| Cloudflare DoH `1.1.1.1/dns-query` | HTTPS | `172.67.197.76` + `104.21.41.254` | normal | 🟢 CLEAN |
| Google DoH `dns.google/dns-query` | HTTPS | `172.67.197.76` + `104.21.41.254` | normal | 🟢 CLEAN |
| Quad9 DoH `9.9.9.9/dns-query` | HTTPS | `172.67.197.76` + `104.21.41.254` | normal | 🟢 CLEAN |
| `www.xrpldashboard.com` on same auth NS | UDP:53 | `172.67.197.76` + `104.21.41.254` | 300 | 🟢 CLEAN |

**Pattern:** apex over UDP is poisoned; apex over DoH is clean; `www` on the same nameserver is clean. The bug is scoped to the apex `A` record on the UDP-serving path of the authoritative shard. This is a *dual-answering* condition — the same authoritative NS returns different answers depending on transport, which is diagnostic of a stuck cache/serialization tier in front of the actual zone content.

## 2 — Falsification trail (surgery log)

19:16 – 20:00 EDT, guided edits in Cloudflare dashboard (`newmediaconceptz@gmail.com` account, Zone ID `48768bf37f02e65117d3...`, Account ID `11ba82b8ff2dfb06abea6261547836c8`):

1. **Toggle Proxy OFF, Save.** Result: auth NS deduplicated (2 records → 1 record), but content still `18.204.152.241`.
2. **Toggle Proxy back ON, Save.** Result: auth NS re-serialized back to duplicated `18.204.152.241` × 2.
3. **Delete apex A record entirely** (13 of 200 records remaining, confirmed by table crop).
4. **Add fresh apex A: `@ 216.24.57.1 Proxied Auto`.** Form banner confirmed "xrpldashboard.com points to 216.24.57.1 and has its traffic proxied through Cloudflare." Save success.
5. **Verify auth NS.** koa/zoe both still return `18.204.152.241 × 2 TTL 4294967295`.

**Conclusion:** Cloudflare accepted every zone edit (dashboard, API, and re-read of DNS Records page all show `216.24.57.1 Proxied` — the record we saved is present), but the authoritative UDP-serving layer holds an independent cached apex answer that survives all zone mutations available from the customer dashboard. This is deeper than a normal propagation delay: TTL 4294967295 in outgoing DNS answers is not a legitimate customer-facing value; it is an internal cache sentinel that has escaped.

## 3 — Singular-delegation proof (no forgotten zone)

Working hypothesis at 18:58 EDT: this could be a *forgotten first zone* on a second Cloudflare account holding the registry delegation, with our edits going to the wrong copy.

**Falsified.** Every layer of the delegation chain points to the same pair:

| Vantage | koa.ns.cloudflare.com | zoe.ns.cloudflare.com | Source |
|---|---|---|---|
| Registry (.com gTLD, `a.gtld-servers.net`) | ✅ | ✅ | dig |
| WHOIS registrar record (Squarespace Domains LLC) | ✅ | ✅ | whois |
| Cloudflare zone → DNS Settings → "Cloudflare Nameservers" | ✅ | ✅ | dashboard screenshot |

There is exactly one Cloudflare zone for `xrpldashboard.com`, in the Newmediaconceptz account. The edits are landing in the correct zone.

WHOIS facts:
- Registrar: **Squarespace Domains LLC** (absorbed Google Domains — if the domain was originally registered at Namecheap it was transferred; if via Google Domains it migrated)
- Creation: **2026-04-16**
- Last WHOIS update: **2026-07-17**
- Domain status: `clientDeleteProhibited` + `clientTransferProhibited` (registrar-side transfer lock, normal state)

## 4 — Archaeology grep (the IP has no origin here)

Full-history + working-tree + config sweep for `18.204.152.241` and the `18.204.*` prefix on the Mac:

| Location | `18.204.152.241` | `18.204.*` |
|---|---|---|
| `~/xrpl_test/` git log `-S` across all branches (blob-level) | **0 hits** | **0 hits** |
| `~/xrpl_test/` working tree incl. untracked | 1 hit — this-week wound diagnosis in `docs/SATURDAY_QUEUE_2026-08-22.md:68` | same |
| `~/.openclaw/workspace/` | 12 hits — today's diagnostic notes in `memory/2026-08-26.md` written *about* the ghost | same |
| `~/.config/xrpldashboard/env`, snapshot key | 0 | 0 |
| `~/Library/LaunchAgents/*.plist` | 0 | 0 |
| `~/Documents` (DNS/deploy/render/cloudflare files) | none exist | — |

**Reverse WHOIS on 18.204.152.241:**
- OrgName: Amazon Technologies Inc.
- NetRange: 18.32.0.0 – 18.255.255.255 (US-East-1 shared EC2 space)
- RegDate: 2011-12-08
- Not routable to a specific Charlie-owned resource; classic AWS shared Elastic IP pool.

**Verdict line for the ticket:** *The IP `18.204.152.241` has zero origin in our infrastructure. It appears in zero commits (any branch, any blob) across the entire git history of the project repo, zero active configs, zero launchd plists, zero deployment notes. The only on-disk references anywhere are diagnostic notes written about the ghost since 2026-08-22. This IP was never legitimately part of our zone — it is ghost data internal to Cloudflare's serving layer.*

## 5 — Timeline

- **2026-04-16** — Domain registered at Squarespace, zone created at Cloudflare (Newmediaconceptz account). Delegation to koa/zoe.
- **2026-05-09** — Site launched publicly on Render.
- **2026-08-17** ~16:00 EDT — Custom-domain reachability failure first observed. Multi-hour LAN-blindness episode. Initial diagnosis (LAN cache) later refined; the underlying UDP-poisoning was already present. See standing rule `feedback_external_vantage_before_infra_surgery`.
- **2026-08-22** — Wound diagnosis filed in `docs/SATURDAY_QUEUE_2026-08-22.md:68` explicitly identifying "Cloudflare authoritative UDP NS still serving stale flattened `18.204.152.241` with TTL 4294967295."
- **2026-08-26 18:34 EDT** — Grok and ChatGPT external audit tools return timeouts + malformed-content reports for xrpldashboard.com surfaces, confirming external (non-LAN) impact still active.
- **2026-08-26 18:56 EDT** — Multi-vantage survey table above captured. Dual-answering (DoH clean, UDP poisoned) confirmed.
- **2026-08-26 19:00-20:00 EDT** — Toggle/save/delete/re-add surgery. Ghost survived all.
- **2026-08-26 20:00 EDT** — Singular delegation confirmed. Archaeology grep filed. Ticket drafted (this file).

Symptom duration in evidence: **at least 9 days** (2026-08-17 → 2026-08-26). Possibly longer — this was the first date external vantage caught it.

## 6 — Business impact

- **Human browsers on major networks:** succeed. Their resolvers or their OS DNS have paths to the Cloudflare edge that don't rely on the poisoned UDP answer, OR they proxy through CDN paths. Traffic log during the poisoning window: 113 uniq visitors, 2.86k requests, 24h.
- **Automated / API / LLM-agent traffic:** fails. UDP resolvers (which are what most bots, cURL, LLM crawlers, and monitoring hit) receive `18.204.152.241 TTL 4294967295` and hang or time out attempting to reach that AWS IP (which does not run our service). Grok and ChatGPT external audit tools both confirmed this today.
- **Product identity impact:** xrpldashboard.com is a machine-facing analytics surface. The machine-facing half of our audience is silently blind.

## 7 — Bug fingerprint

The TTL `4294967295` (2³² − 1) is the tell. That value is Cloudflare's internal cache-forever sentinel — it is not a legitimate value to serve in outgoing DNS answers to external resolvers. Its appearance in `dig +noall +answer` output from koa.ns and zoe.ns is evidence that an internal cache/staging layer's data has escaped into external responses. Combined with the dual-answering (DoH ≠ UDP on the same authoritative NS), this is a Cloudflare-side stuck serialization state on the UDP path for this specific zone's apex A record.

## 8 — The ask

Cloudflare Support: please purge and re-serialize the authoritative apex `A` answer for `xrpldashboard.com` on the affected shard. The customer-side zone data is correct (`216.24.57.1` Proxied); the authoritative UDP-serving layer is holding a cached answer that is out of date and carrying an impossible TTL value. Please confirm whether the scheduled zone-database maintenance on **Saturday 2026-08-29 09:00–10:00 UTC** ("Scheduled maintenance to upgrade core databases will take place... Zone-related configuration changes may fail briefly during this window") is expected to clear this state; if so we can wait, if not please escalate.

## 9 — Ticket draft

Subject: **Authoritative NS serving stale apex A with impossible TTL 4294967295 — survives delete + re-add**

Body:

> Zone: `xrpldashboard.com` (Zone ID `48768bf37f02e65117d3...`)
> Account: Newmediaconceptz (Account ID `11ba82b8ff2dfb06abea6261547836c8`)
> Plan: Free
>
> **Problem.** Cloudflare's authoritative name servers for this zone (`koa.ns.cloudflare.com`, `zoe.ns.cloudflare.com`) serve an incorrect apex A answer over UDP with an impossible TTL value:
>
> ```
> $ dig @koa.ns.cloudflare.com xrpldashboard.com A +noall +answer
> xrpldashboard.com.    4294967295 IN A 18.204.152.241
> xrpldashboard.com.    4294967295 IN A 18.204.152.241
> ```
>
> The correct configured content is `216.24.57.1` (Proxied) — Render's shared static IP. TTL `4294967295` (max int32, ~136 years) is not a valid outgoing DNS TTL and appears to be an internal cache-sentinel value that has leaked to external answers.
>
> **Dual-answering pattern.** DoH queries against the same authoritative infrastructure return the correct proxied edge IPs, and the `www` subdomain on the same zone returns correctly over UDP. Only the apex A record over UDP is affected.
>
> **Steps tried from customer side.**
> 1. Toggled Proxy off, saved. Auth NS deduplicated from 2×`18.204.152.241` to 1×`18.204.152.241`. Content unchanged.
> 2. Toggled Proxy back on, saved. Reverted to 2× duplicated stale.
> 3. Deleted apex A record entirely. Verified 13/200 records remaining.
> 4. Added fresh apex A: `@ 216.24.57.1 Proxied Auto`. Save confirmed. Dashboard DNS Records page shows the correct record.
> 5. Waited 30+ minutes. Ghost persists on both koa and zoe.
>
> **Registry / zone consistency confirmed.** .com gTLD delegation, Squarespace WHOIS, and this zone's DNS Settings all show the same pair (koa/zoe). There is no forgotten second zone.
>
> **Origin of stale IP.** `18.204.152.241` (AWS Amazon Technologies Inc., us-east-1 EC2 shared space) does not exist anywhere in our project's git history, working tree, configs, or deployment notes. It was never a legitimate origin for this zone.
>
> **Impact.** Automated clients (bots, LLMs, monitoring, our machine-facing product's downstream API consumers) time out. Browser humans reach the Cloudflare edge normally. This is silently degrading a machine-facing product surface.
>
> **Monitoring casualty.** As of 2026-08-26 23:29 ET the stale answer began breaking our external uptime monitoring — BetterStack probes resolved to the ghost IP and timed out, firing an incident against `xrpldashboard.com/api/heartbeat-age` while the origin `xrpldashboard.onrender.com/api/heartbeat-age` returned HTTP 200 in <1s. We have had to repoint monitors at the origin hostname as a temporary workaround. Your bug is blinding our watchdogs.
>
> **Duration.** In evidence for at least 9 days (first captured externally 2026-08-17; first named as this diagnosis 2026-08-22; still active 2026-08-26 20:00 EDT).
>
> **Ask.** Please purge / re-serialize the authoritative apex A answer for this zone on the affected shard. Please also confirm whether the scheduled zone-database maintenance on 2026-08-29 09:00-10:00 UTC is expected to clear this state.

## 10 — Submission route

Free-plan customers on Cloudflare can open a support ticket via:

1. **Dashboard route (recommended, ties to account automatically):**
   `dash.cloudflare.com` → click the "Support" pill in the top-right of any page → "Contact Cloudflare Support" → open a new case → category: **DNS** → subcategory: **DNS resolution issue** → paste the ticket body above.

2. **Community first (Cloudflare frequently asks Free users to post to Community, which is fine — it accelerates triage):**
   `community.cloudflare.com` → New Topic in DNS → same body.

3. **Twitter/X escalation if Support doesn't respond within 24-48h:** `@CloudflareHelp` — quote the Zone ID and Account ID.

## 11 — Standing-rule updates queued (do not apply until Charlie acks)

The 2026-08-17 custom-domain LAN-blindness mea culpa (memory index `project_custom_domain_lan_blindness_2026-08-17.md`) needs refinement:

- **Not overturned:** the ≥3-external-vantages standing rule still stands; that day's LAN-blindness reading was too narrow but not wrong.
- **New evidence, same event:** Sunday's LAN-only verdict undersold the fault. The dual-answering (DoH clean, UDP poisoned) has been present since at least 2026-08-17. Recent AI-agent audit tools (Grok, ChatGPT external fetcher) hit the same UDP-poisoned answer that our seats do, so "LAN-only" is now known to be false; the fault is *transport-scoped* (UDP-affected, DoH-unaffected), not *network-scoped*.

Follow-up file to update after ticket lands, or when Cloudflare responds — whichever comes first.

## 12 — Watchdog (proposal, do not build yet)

Cadence-hourly probe that dig-compares `koa.ns.cloudflare.com` UDP:53 vs Cloudflare DoH answers for `xrpldashboard.com` A. If UDP ≠ DoH for the apex, or TTL > 604800 (1 week — no legitimate reason to exceed), alert. This closes the window where a stuck serving layer degrades the machine-facing product without paging. Design pack owed post-Taft, matches memory-aware-cache design pack pattern.

## 13 — Submission log / escalation ladder (filed 2026-08-27)

### Filed
- **Community post** — Cloudflare Community DNS & Network sub-category, feature=Nameservers, submitted 2026-08-27 ~08:52 EDT. **APPROVED + LIVE 2026-08-27 10:20 EDT** (account off-hold 10:11 → post approved 10:20 → total queue time ~90 min). Thread URL: https://community.cloudflare.com/t/authoritative-ns-serving-stale-apex-a-record-with-ttl-4294967295-record-doesnt-e/952972
  - Post title: `Authoritative NS serving stale apex A record with TTL 4294967295 — record doesn't exist in zone, survives delete+re-add`
  - Adapted from §9 into the category's structured form (Domain / Error message / Issue / Steps taken / Feature). Monitoring casualty + Aug 29 maintenance ask both present.
- **X escalation** — DRAFTED but NOT POSTED. Overtaken by sjr's morning cellular test before send. No public accusation exists. Corrected in §14.
- **Support case (dashboard route)** — NOT filed. Free plan cannot open a DNS case (only billing/account/registrar); Cloudflare Community IS the intended DNS submission route for Free tier. Route (2) from §10 is the one that applies.
- **BetterStack repoint** — monitor URL swapped `xrpldashboard.com/api/heartbeat-age` → `xrpldashboard.onrender.com/api/heartbeat-age` at 05:40 ET 2026-08-27. Uptime carried over. Revert criteria filed in `memory/2026-08-27.md`.

### Escalation ladder (on record)
- **Silence through Monday 2026-09-01** (staff off weekend, gives community + X 4 business days to bite) → escalate to Pro $20/mo as queue-jumper for direct case submission.
- **OR Aug 29 09:00-10:00 UTC maintenance completes and ghost SURVIVES** (dig `@koa.ns.cloudflare.com` still returns 18.204.152.241 TTL 4294967295 on 2026-08-29 12:00 UTC probe) → same trigger, escalate to Pro.
- Either trigger fires → re-evaluate whether a paid tier is worth the queue-jump vs continued waiting.

### Watching mode
DNS saga active-work COMPLETE. All channels filed. Ghost tracked separately from cert clock (LAN-blindness / machine-facing class; not reset-class per stability doctrine). Revert monitor URL to apex once 24h of clean external vantages establish shard purge stuck.

---

## 14 — MECHANISM CORRECTED (2026-08-27 13:00 EDT)

**The Cloudflare-authoritative-bug diagnosis in §1-§12 is FALSIFIED.** The ghost lives in Charlie's home network path — an ISP-router "Online defense" DNS-filter security feature transparently intercepting UDP:53 traffic and serving a stale block-list IP with the sentinel TTL. Full credit to Cloudflare Community respondent `sjr`, whose first reply named the shape ("this looks like DNS interception by a router security feature") before I did.

### What actually happened

- **Decisive test (2026-08-27 ~12:46 EDT):** cellular hotspot on Mac → `dig @koa.ns.cloudflare.com xrpldashboard.com A +noall +answer` returned Cloudflare-proxy IPs (`104.21.41.254` / `172.67.197.76`) with TTL 300. Same query from home Wi-Fi seconds later returned `18.204.152.241` TTL `4294967295`. Two back-to-back home-Wi-Fi queries returned different answers (clean, then ghost) — inconsistent because the intercepting resolver has multiple upstream/cache shards, only some poisoned.
- **Fix path:** router admin app → Website access → "Approve" `xrpldashboard.com` at Whole Network scope. Post-fix: 5/5 consecutive `dig` runs from home Wi-Fi returned clean Cloudflare-proxy IPs with round-robin ordering swap = normal healthy behavior.
- **Why §1's vantage table looked global.** Every `dig @<public-resolver>` from Charlie's Mac went out UDP:53 → was transparently redirected to the router's Online-defense resolver before it ever left the LAN → received the forged answer. The public resolvers were never actually queried. DoH (HTTPS:443) queries could not be transparently intercepted, so DoH returned clean — that's what the §1 dual-answering pattern actually measured.
- **Why §1-§9's diagnosis felt so solid.** All observations were internally consistent with a Cloudflare-authoritative bug because every UDP vantage came back the same way. The one datapoint that would have falsified it — a truly out-of-LAN external probe — was not run until 12:46 EDT today. Charlie's cellular loading the site cleanly on 08-17 was that datapoint; I discounted it then and again now until sjr forced the cellular `dig` test.

### What §1-§12 got right

- The record `216.24.57.1` was never actually stale in Cloudflare — the zone was correct throughout. All the surgery in §2 (proxy toggle, delete, re-add) was correct on customer-side and had no effect only because there was nothing wrong customer-side.
- Archaeology grep §4 stands: `18.204.152.241` had zero origin in project infrastructure. It was always ghost data — just ghost data in the ISP router's block-list database, not in Cloudflare's serving layer.
- WHOIS / delegation §3 stands. Cert-clock accounting stands (this class remained non-outage / non-reset).
- Standing rule `feedback_external_vantage_before_infra_surgery` stands and just proved its worth a second time — the correction only happened because sjr provided the third external vantage (cellular tether via my phone hotspot ask) that we hadn't secured before. **Two Macs on the same LAN = one vantage** applies just as strongly to `dig` as to browser hits.

### Escalation ladder retracted

- **Cloudflare Community post** (thread 952972) — should be marked SOLVED with the corrected mechanism and full credit to `sjr`. Community reply drafted and posted 2026-08-27 by Charlie ~13:10 EDT. Ticket premise (CF-side authoritative bug) is retracted; final resolution is home-network router allowlist.
- **X/CloudflareHelp post** — **NEVER POSTED.** The morning pivoted to sjr's cellular test before the X send happened. No public accusation against Cloudflare exists on X. No closer needed; X track is dead, DNS closes without it.
- **Pro-tier queue-jump** at 2026-09-01 / 2026-08-29 maintenance — CANCELLED. No CF-side issue to escalate.
- **BetterStack monitor URL** — kept on the `.onrender.com` origin hostname for now, because the "external monitors saw the ghost too" observation (23:29 ET 2026-08-26 fire) is not yet explained by the home-network mechanism alone. See §15.

### Sunday 2026-08-29 maintenance ask

The scheduled maintenance is coincidental, not causal. No action needed from Cloudflare Support. No re-probe scheduled against that window as a diagnostic; watch-list only.

## 15 — Open puzzle folded into a single watch-item

**Observation that does NOT yet fit the home-network-only mechanism:**

1. BetterStack global probe fleet fired an incident 2026-08-26 23:29 ET against `xrpldashboard.com/api/heartbeat-age` while the .onrender.com origin returned HTTP 200 in <1s — probes are hosted, not on Charlie's LAN.
2. Six named AI crawlers went to absolute zero in today's digest (anthropic-ai 590/84.3 → 0, ClaudeBot 143/20.4 → 0, GPTBot 76/10.9 → 0, PerplexityBot 85/12.1 → 0, Google-Extended 82/11.7 → 0, MCP-Cloud-AboutBot 45/6.4 → 0) — these fleets do not use Charlie's home network.
3. Grok and ChatGPT external audit tools returned timeout/malformed content 2026-08-26 18:34 EDT — hosted machines, not Charlie's LAN.

**Candidate explanations (unranked):**

- **(a) Their-side coincidence.** Six vendors, one day, all at zero simultaneously is statistically implausible — this candidate is weakest.
- **(b) Earlier real Cloudflare-side event, now cleared.** The ghost may have briefly been served from CF's authoritative UDP path earlier this week, poisoned public resolvers, then cleared. External resolvers with long negative caches (which some crawler-fleet resolvers keep for hours-to-days) would continue serving the stale value. Home-network intercept masked the recovery on Charlie's seat. Consistent with (1)(2)(3) if the CF-side event was ~08-22 (Saturday queue's original diagnosis) and cleared ~08-26 evening.
- **(c) `.onrender.com`-side quirk.** BetterStack was pointed at the `xrpldashboard.com` hostname, not the origin, so its probes went through CF proxy → could hit CF-edge conditions unrelated to the router-intercept. Weakest for the crawler-fleet observation but plausible for BetterStack alone.

**Watch-item:** re-pull today's crawler fleet numbers 2026-08-28 and 2026-08-29 mornings. If the six named agents rebound to baseline without any external intervention, (b) explains everything — a real CF-side event happened, it cleared without our ticket, and residue continues to drain. If they stay at zero, we still have an external problem separate from the router intercept, and (a)+(c) are the remaining candidates.

**BetterStack monitor URL:** stays on `.onrender.com` origin until 48h of clean external vantages (not just Charlie's seat) establish that the wider issue has resolved.

**Do not open new investigation threads on (a)(b)(c) yet — recheck first.**

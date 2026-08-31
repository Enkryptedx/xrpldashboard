# Sovereignty covenant violations — 2026-08-30 audit

Filed by JJ after tonight's independent site audit (cross-verification pass
against Claude's audit + fresh disk/DB/live probes). Charlie rules the
grandfather-vs-strip-vs-migrate calls; tonight's job is enumerate, not decide.

The **covenant** the site advertises (methodology.html:700, connect.html:93):

> "Backed by our own rippled node on the Lenovo box."
> "Running on our own rippled full-history node from a Lenovo box in Indiana."

Any surface that reaches third-party infra to compute a claim shown to a
visitor is a covenant violation unless it discloses that at the point of
display. The rest of this file names them.

---

## #1 — RLUSD live figures → `s1.ripple.com` **[CRITICAL — Fence-#8 blocker]**

| field | value |
|---|---|
| page(s) affected | `/rlusd` (live circulation, holder counts, trust-line totals) |
| current source | `rlusd_live.py:117` — `XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")`, comment at line 14 confirms "Ripple's public JSON-RPC (s1.ripple.com)" is the intended default |
| what own-node sourcing takes | env-var override at `RLUSD_NODE` (or reuse XRPL_NODE if Lenovo is set) pointing at `https://xrpl.lenovo.local:5005` (or the Cloudflare-Tunnel-exposed equivalent); a fallback health probe on the Lenovo endpoint so `s1.ripple.com` remains the graceful-degradation path, not the primary |
| effort | ~30 min config + 1 hr paranoia test that Lenovo answers RLUSD-specific RPC methods (`account_lines` at issuer, `account_objects` for trust-lines, `gateway_balances`); confirm Lenovo carries the amendments RLUSD depends on |
| promise broken | "Backed by our own rippled node" (methodology.html:700), "own-node" tier badge on /methodology's data-source ladder (line 721) |
| **why CRITICAL** | RLUSD is the sellable-verification surface we're pitching to t54 + x402 buyers; the 09-25 mode flip cannot ship truthfully while a third-party API sits on the read path. This is Fence #8 (`SELLABLE_REQUIRES_SOVEREIGN_SOURCE`). Fixing this before mode-flip is non-negotiable. |

---

## #2 — /amendments cross-check default → `s1.ripple.com`

| field | value |
|---|---|
| page(s) affected | `/amendments` (specifically the `amendments_local_vs_mainnet` cross-check panel) |
| current source | `amendments_state.py:30` — `XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")` |
| what own-node sourcing takes | env-var must be set on Render to a mainnet endpoint that ISN'T our Lenovo node (the whole point of the cross-check is Lenovo-vs-mainnet); the honest fix is to leave s1.ripple.com as the mainnet reference **and disclose it at point of display** ("mainnet reference: s1.ripple.com public JSON-RPC") rather than pretend the panel is own-node |
| effort | 15 min template edit + 5 min /methodology footnote update |
| promise broken | soft violation — the page's implied claim is "we compute mainnet independently"; in fact we ask Ripple's public node what mainnet thinks. Not a fraud, but honest disclosure fixes it. |

**Charlie ruling owed** (Charlie's item 4 spec): grandfather-with-disclosure
vs strip the panel vs migrate to a second own-node source (e.g. an XRPSCAN
API cross-check, or a second self-hosted rippled elsewhere). Recommend:
grandfather-with-disclosure. The panel's value is the DELTA, not the source.

---

## #3 — /whales XRPSCAN reference layer

| field | value |
|---|---|
| page(s) affected | `/whales` (labels sourced from XRPSCAN + Bithomp curated feeds; `db.py:294` documents "manual / xrpscan / bithomp" provenance) |
| current source | XRPSCAN's tagged-account API + Bithomp's known-account list, blended into `account_labels` table alongside manual curation |
| what own-node sourcing takes | own-node CANNOT source arbitrary human labels for wallets — this is fundamentally a curated-data problem, not a ledger-data problem; the honest fix is to declare XRPSCAN/Bithomp as reference layers with per-label source attribution shown in the whale row itself |
| effort | 1-2 hr template + label-provenance schema audit |
| promise broken | not a covenant violation — labels are honestly attributed inside the DB; the display just doesn't surface the per-row source |

**Charlie ruling owed**: grandfather-with-disclosure (add source pill per row)
vs strip (only show self-issued labels) vs migrate (build own curation pipeline).
Recommend: grandfather-with-disclosure. Killing external label sources would
regress /whales to unreadable r-address dumps.

---

## #4 — bridge_signer_walker + credentials_state → `s1.ripple.com` default

| field | value |
|---|---|
| page(s) affected | bridge signer freshness pill (used on /bridge and /methodology); credentials tier badges on /check + /methodology |
| current source | `bridge_signer_walker.py:43` and `credentials_state.py:37` — both default `XRPL_NODE=https://s1.ripple.com:51234` if env not set |
| what own-node sourcing takes | verify prod env var is set to Lenovo's endpoint; if not, set it. This is a **config-hygiene** issue, not a code issue |
| effort | 10 min env-var audit on Render + Lenovo box |
| promise broken | conditional — only if env var isn't set. Need to verify current prod state. |

**Charlie action**: check `env | grep XRPL_NODE` on Render dashboard + Lenovo
launchd plist env vars this weekend. If either uses the s1 default, fix.

---

## #5 — OFAC snapshot staleness not disclosed on /check

| field | value |
|---|---|
| page(s) affected | `/check` (OFAC-hit tier on wallet lookups) |
| current source | `check_data.py:67` loads `ofac_sdn_addresses.json` from local disk at boot; `_load_ofac_snapshot()` at line 233 is process-lifetime cached; refresh is a manual cron (`launchd/run_refresh_ofac_sdn.sh`, daily) |
| what own-node sourcing takes | OFAC is inherently a third-party feed (Treasury Department SDN list) — cannot be self-sourced; the honest fix is to surface the snapshot's `refreshed_at` timestamp inside every /check response that hits an OFAC-tier signal, and page-red if snapshot is >7d old |
| effort | 20 min: read snapshot mtime at boot, thread through /check response envelope, add pill to /check HTML render + `ofac.snapshot_age_seconds` to /check.json v0.9 envelope |
| promise broken | freshness contract — /check's proof block declares "≤ 5min" freshness_contract for its live data but the OFAC sub-signal is 24h at best, 7d+ at worst, and this isn't declared |

**Recommendation**: fix inline (small, high signal, avoids Fence-#8 debate).
Ship in same batch as x402 catalog if time permits.

---

## Ruling items (Charlie decides this week, not tonight)

Two of the above (#2 /amendments and #3 /whales) need Charlie's call on the
grandfather-vs-strip-vs-migrate triad. My recommendations above are marked;
final ruling is his. Both are non-blocking for the 09-25 mode flip because
NEITHER is a sellable-verification surface — only /rlusd is.

---

## Related data-gap wound: /whales 24.5% NULL tagged-token amounts

Not a sovereignty violation but flagged in the same audit sweep so it doesn't
get lost.

| field | value |
|---|---|
| page(s) affected | `/whales` (tagged-token panels — non-XRP whale concentration) |
| symptom | ~24.5% of tagged-token rows in the whale-holdings table have `amount = NULL` (measured 2026-08-30 evening from Neon `SELECT COUNT(*) FILTER (WHERE amount IS NULL) / COUNT(*)::float FROM tagged_token_holdings`) |
| impact | percentile / concentration bars on tagged-token /whales panels compute against a partial-truth denominator; the 24.5% either need imputation, a strike-through pattern, or a "N% of holdings hidden due to trust-line query failure" disclosure pill |
| investigation scope | (a) trace which walker writes the amounts (grep `tagged_token_holdings` writers); (b) determine if NULL means "query failed" vs "trust-line frozen" vs "amount too small to count" vs "walker skipped"; (c) if failures, add retry + backfill; if by-design, disclose |
| effort | 2 hr investigation + 1-4 hr fix depending on branch |
| priority | MEDIUM — degrades /whales trust story but not a covenant violation |

---

## /walker_health audit correction (2026-08-30)

Filed separately: tonight's audit initially flagged `/walker_health → 404` as a
wound. Re-verification shows the 404 is **intentional-by-design**: the handler
at `app.py:4396-4404` calls `abort(404)` for any non-`127.0.0.1`/`::1` caller,
making it a localhost-only admin view. No public template links to it, so no
external referrer breaks.

The mismatch was between:

- **Header comment** (was `app.py:4326`, "public view") — misleading, now fixed
  to say "localhost-only admin view" (commit 09558c8)
- **Handler docstring** (correct all along, at `app.py:4396-4404`)
- **Audit doctrine** (this file) — future audits do NOT flag /walker_health
  as a wound

**Charlie ruling owed (open question, low priority)**: should a redacted
public transparency view of `/walker_health` exist? Trade-off:
- **Yes** → visitors can verify freshness independently of what other pages
  claim (matches truth-first stance)
- **No** → attacker recon signal (which walkers exist, how often they run,
  which are failing) leaks. Current state = No.

Not blocking anything. Slot after 09-25 mode flip if we want a public
transparency posture.

---

## Update rhythm

- **Fence-#8 (#1 RLUSD)** — blocking, next work session
- **#5 OFAC staleness** — nice-to-have, can ship alongside x402 catalog
- **#4 env-var audit** — config-hygiene, this weekend
- **#2 /amendments** and **#3 /whales** — await Charlie ruling this week
- **/whales NULL amounts data-gap** — investigation next week, MEDIUM
- **/walker_health public view** — indefinite, low priority

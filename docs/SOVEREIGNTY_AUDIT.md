# XRPL Endpoint Sovereignty Audit

**Purpose:** every external XRPL data source the site touches, classified as
**own-node**, **disclosed third-party (correct)**, or **gap** (with proposed fix
+ date). This is the source-of-record for the /methodology sovereignty claim and
the weekly claim-verify check.

**Method:** grep of `*.py` / `*.html` / `*.js` for XRPL endpoints
(`s1/s2.ripple.com`, `xrplcluster.com`, `xrpscan.com`, `data.xrpl.org`,
`bithomp.com`, own-node `192.168.40.95` / `localhost:5005` / tunnel
`rpc.xrpldashboard.com`). Last run: **2026-09-25** (JJ, own-node switch session).

**Sovereign primitives:**
- **Own node (Lenovo rippled 3.3.0-rc1):** `XRPL_LOCAL_NODE` (LAN `192.168.40.95:5005/5006`), or via the CF-Access tunnel `rpc.xrpldashboard.com` for off-LAN (Render).
- **`SovereignFetcher`** (`sovereign_tunnel_client.py`): tunnel-first, retry, then cascade to `PUBLIC_NODES` with a `sourcing` flag (`sovereign` | `fallback-public-rpc` | `public-no-tunnel-configured`). Pages keying off `sourcing` render a disclosure banner on fallback.
- **`PUBLIC_NODES`** fallback cascade: `s1.ripple.com:51234` → `s2.ripple.com:51234`.

---

## Table — surface × source × role × classification

| Surface | What it reads | Source | Primary/Fallback | Own-node? | Classification |
|---|---|---|---|---|---|
| `/amendments` (`amendments_state.py`) | feature RPC + Amendments ledger object | SovereignFetcher → own node; s1/s2 fallback | own primary, public fallback | ✅ (sovereign-first) | **disclosed third-party fallback (correct)** — dynamic banner + footer (commit 32153ca) |
| `/wallet` (`wallet_data.py`) | account_info/lines/objects | SovereignFetcher | own primary, public fallback | ✅ | disclosed third-party fallback (correct) |
| `/token`, `/tokens` (`token_data.py`) | issuer/holder/trustline data | SovereignFetcher | own primary, public fallback | ✅ | disclosed third-party fallback (correct) |
| `/lending` (`lending_data.py`, `lending_amendment.py`) | lending amendment + vault/broker | SovereignFetcher | own primary, public fallback | ✅ | disclosed third-party fallback (correct) |
| price (`xrp_price.py`) | XRP price feed | SovereignFetcher | own primary, public fallback | ✅ | disclosed third-party fallback (correct) |
| `/network` (`network_pulse.py`) | server_info/ledger | SovereignFetcher (4) + public (5) | mixed | ⚠️ partial | **GAP-1** — some calls raw public, not all via SovereignFetcher |
| live stream relay (`live_stream_relay.py`) | ledger/transactions stream | `ws://127.0.0.1:6006` (own node, LAN) | own only | ✅ own-node | own-node (correct) — no public in the read path |
| browser live feed (`static/js/live_stream.js`) | wss ledger stream | `wss.xrpldashboard.com` primary; `wss://xrplcluster.com` fallback | own primary, public fallback | ✅ | disclosed third-party fallback (correct) — tracked in walker_node_fallback |
| forward walker (`app.py` own-node reads) | Ledger RPC (tx feed) | own node | own only | ✅ | own-node (correct) |
| `cross_check_walker.py` | vocab/amendment cross-check | own node (4) + public (6) | own + public by design | ✅ intentional | own-node primary; public is the *cross-check counterparty* (correct — the whole point is to compare) |
| `ledger_definitions_walker.py` | ledger definitions | own node (`XRPL_LOCAL_NODE`) | own only | ✅ | own-node (correct) |
| `bridge_signer_walker.py` | SignerList on bridge acct | `XRPL_NODE` default `s1.ripple.com` | public default | ❌ | **GAP-2** — walker defaults to s1, no SovereignFetcher |
| `credentials_state.py` (`/credentials`) | credential objects via Clio | `XRPL_CLIO_NODE` default `s2.ripple.com` | public default | ❌ | **GAP-3** — Clio read on public s2; own Clio (`192.168.40.95:5006`?) exists |
| `daily_snapshot.py`, `signed_snapshot.py` | snapshot metric reads | `XRPL_NODE` default s1; signed_snapshot has 1 own-node ref | mostly public default | ⚠️ | **GAP-4** — snapshot walkers default to s1; the SIGNED anchored metrics should be own-node-sourced per covenant |
| `rank_amms.py`, `scan_all_amms.py`, `amm_test.py` | AMM pool enumeration | `s1.ripple.com` (hardcoded in some) | public | ❌ | **GAP-5** — AMM ranking reads public s1 (amm_test.py/amm_scan_pools.py hardcoded) |
| `nft_activity_walker.py`, `scripts/nft_activity_gap_fill.py` | NFT activity | (uses xrpscan?) | third-party | ❓ | **GAP-6** — verify: NFT activity source; if xrpscan, disclose |
| `xrpscan_labels_import.py` | account labels | `xrpscan.com` | third-party (labels, not ledger) | n/a (metadata) | disclosed third-party (correct) — labels are xrpscan's data, not a ledger claim |
| amendment vote tallies (`amendments_network_votes.py`) | per-validator UNL vote counts | `data.xrpl.org/v1/network/amendments/vote/main` (VHS) | third-party | ❌ (no own equivalent) | **disclosed third-party (correct, but note)** — per-validator tallies not exposed by our node's feature RPC; VHS is the only source. Disclosed on /amendments footer. |
| explorer links (`bithomp.com`, `xrpscan.com` in templates) | outbound href only | third-party | n/a | n/a | display links, not data reads (correct) |

---

## Gaps summary (proposed fixes + dates)

| ID | Surface | Gap | Proposed fix | Target |
|---|---|---|---|---|
| GAP-1 | `network_pulse.py` | some server_info/ledger calls raw public, not all SovereignFetcher | route all reads through SovereignFetcher | post-Saturday relay deploy |
| GAP-2 | `bridge_signer_walker.py` | defaults to s1, no sovereign path | switch to `XRPL_LOCAL_NODE` primary + SovereignFetcher fallback | this sprint |
| GAP-3 | `credentials_state.py` | Clio read on public s2 | point `XRPL_CLIO_NODE` at own Clio; s2 fallback | this sprint (needs own-Clio confirm) |
| GAP-4 | snapshot walkers | anchored metrics default-source public s1 | own-node primary — the SIGNED metrics MUST be own-node per the covenant | **priority — covenant-relevant** |
| GAP-5 | AMM ranking scripts | hardcoded s1 | env-var + SovereignFetcher | this sprint |
| GAP-6 | NFT activity walkers | source unverified (xrpscan?) | confirm source, disclose if third-party | this sprint |

**GAP-4 is the sharpest:** the daily signed-snapshot anchored metrics are the ones cryptographically committed on-ledger and cited by press. Any of those seven metrics sourced from public s1 rather than our own node weakens the "originated from our own node" covenant claim. Audit each of the 7 anchored metrics' walker source before the next methodology copy pass.

---

## /methodology facts (for Charlie's wording)

Proposed sovereignty-claim definition to add to /methodology (Charlie owns final copy):

> "Sovereignty means every **ledger-state claim** on this site (balances, amendments, supply, pool ranks, the anchored snapshot metrics) is read from **our own rippled node** first; public XRPL infrastructure (s1/s2.ripple.com, xrplcluster) is a **labeled fallback** that triggers a disclosure banner when used. Third-party **metadata** we don't produce ourselves — account labels (xrpscan/bithomp) and per-validator amendment vote tallies (data.xrpl.org / VHS) — is disclosed as third-party at the point of use, because our own node does not expose it. The full endpoint audit, including known gaps, is at `docs/SOVEREIGNTY_AUDIT.md`."

---

## Weekly claim-verify check

Add to the Sunday weekly report (or a dedicated weekly walker):

1. Re-run the endpoint grep; diff against this table's surface list — **any NEW external endpoint not in the table = alert** (a surface added a public read without disclosure).
2. For each ✅ sovereign-first surface, hit it live and assert `sourcing == 'sovereign'` (or that the fallback banner renders when not). A surface silently stuck on `fallback-public-rpc` for a full week = alert.
3. Confirm the open GAP list only shrinks — a closed gap re-opening (env var reverted, hardcoded endpoint reintroduced) = alert.
4. Cross-check the /methodology sovereignty sentence still matches this table (claim-vs-reality drift guard).

*This file is the source-of-record. Update the table + gap list whenever an
endpoint is added, closed, or reclassified; the weekly check reads against it.*

---

## GAP-4 deep audit (2026-09-25, read-only, before the leaf)

**Finding: GAP-4 is a LATENT FRAGILITY, not a live breach.** All 7 anchored
data metrics resolve to our OWN node at runtime (via `XRPL_NODE=http://192.168.40.95:5006`
in `~/.config/xrpldashboard/env`, sourced by every walker's launchd wrapper).
Same-ledger value comparison own-node vs s1 at ledger 107233740: ledger hash
IDENTICAL (BBC4ED449FDFF262…), RLUSD obligations IDENTICAL (1087811209.031171).
The covenant ("originated from our own node") HOLDS today.

Per-metric source (runtime-resolved):
| Metric | Immediate | Underlying node | Own at runtime |
|---|---|---|---|
| xrpl_validated_ledger_index | JsonRpcClient(XRPL_NODE) | $XRPL_NODE=own | ✅ |
| amm_pools_count / total_tvl_usd | amm_ranked.json | rank_amms.py → $XRPL_NODE=own | ✅ |
| mpt_total_count | mpt_snapshot.json | mpt walker → $XRPL_NODE | ✅ |
| named_accounts_count | named_accounts.json | curated static (no ledger read) | n/a |
| rlusd_xrpl_supply | PG rlusd_state_cache | rlusd_live.py → $XRPL_NODE=own (labels 'own-node (LAN)') | ✅ |
| rwa trio | PG rwa_supply_nav_daily | rwa_supply_nav_walker → $XRPL_NODE | ✅ |

**The fragility:** the CODE DEFAULT in every one of these
(`XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")`) is
s1. If the env var is ever dropped from a plist — which is EXACTLY the
2026-09-24 incident that produced the rwa_onledger_supply_usd=0 leaf — the
signer silently falls back to s1 with NO sourcing flag and NO disclosure banner
(unlike the SovereignFetcher-based web surfaces). Own-node-first is
env-dependent, not code-enforced, on the signing path.

**Fix (tomorrow AM, before relay deploy — Charlie ruling 2026-09-25):**
own-node-first as CODE default, s1 an explicit LABELED fallback with a sourcing
flag on the signing path; dry-run proving identical values own vs s1; then a
normal 21:00 ET run as proof. Do NOT change the signer tonight — tonight's
01:00 UTC leaf runs on the current (correct, own-node-via-env) config.

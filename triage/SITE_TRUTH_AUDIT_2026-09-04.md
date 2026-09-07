# Site Truth Audit — post-sovereignty-migration (2026-09-04)

**Auditor:** JJ (inline). Full audit method + provenance rationale below; **all TV findings from the initial pass FIXED + deployed + verified live**, plus two additional violations found during the residual-grep sweep.

## Fix log — 2026-09-04

Commit **`aa6030a`** (pushed, live on prod). Copy-only, no code behaviour change.

| # | Where | Fix | Verified on prod |
|---|---|---|---|
| TV-1 | `templates/index.html:874` (`/`) | "read live from the ledger" → "from our own node, cached rows refreshed every 15 min" (matches walker cadence) | ✅ visible |
| TV-2 | `templates/mpts.html:480` (`/mpts`) | dropped "live ledger_data RPC against s1.ripple.com"; now "walk of our own rippled node, cached and refreshed daily/hourly" | ✅ visible |
| TV-3 | `templates/sidechain.html:235` (`/sidechain`) | dropped "account_tx via Ripple's s1.ripple.com"; now "scanned against our own rippled node by the bridge-signer walker, cached to our database" | ✅ visible |
| TV-4 | `templates/about.html:187` (`/about`) | server-side worker source — was `wss://s2.ripple.com`, now `ws://127.0.0.1:6007` (own LAN). Browser-side xrplcluster claim confirmed TRUE via `static/js/live_stream.js:26`; s2/s1 documented as cascade fallbacks | ✅ visible |
| MD-1 | `templates/methodology.html:291` | 5-metric enumeration → link to `#anchored-metric-set` (single source of truth) | ✅ visible |
| MD-1b | `templates/methodology.html:385` | added `id="anchored-metric-set"` so the new #291 fragment link resolves | ✅ visible |
| C-2 | `app.py:7129` (llms.txt) | "computed directly from XRPL and Ethereum nodes" → "from our own XRPL node; Ethereum-side data (RLUSD cross-chain supply) via public RPC, disclosed" | ✅ visible |
| C-2b | `app.py:7207` (agents.json) | same fix | ✅ visible |
| **TV-5** (new, found in residual sweep) | `templates/mpt_detail.html:419` (`/mpt/<id>`) | same class as TV-2 (identical footer, needed identical fix) | ✅ source verified |
| **TV-6** (new, found in residual sweep) | `templates/methodology.html:250` + `:264` | line 250 described s2 as "server-side worker for streaming validated transactions into Postgres" (past-tense move to own-node now); line 264 called the streaming-worker migration "the next step" when it already happened last night. Rewrote both to past-tense fact | ✅ visible |

## Correction (from earlier reports)

**Correction #1** — the "unsigned Sep 3 snapshot" concern was a **false alarm from a bad check**. My earlier probe looked for `"signature" in d`; the actual field name is `signature_ed25519`. Verified fully with `python3 signed_snapshot.py --verify 2026-09-03`:
```
VERIFIED 2026-09-03: signature OK, audit_path OK, leaf_hash OK, fingerprint OK.
```
Fingerprint = `7F:D4:F2:F4:D2:57:7C:BE` (matches target). Sep 3 is a properly-signed 12-metric anchored leaf; no separate sign step needed; no keyboard action.

**Correction #2** — the "6 DEAD / 18 STALE / 15 GREEN of 39" walker health number I flagged as a stamp concern was **stale — from the 13:51 EDT signed_snapshot's captured `walker_health_summary`, 12 hours old.** Current live state (`walker_health` table, 02:00 UTC):
- **GREEN=41, STALE=1, DEAD=0** (of 42 walkers)
- The single STALE row is `xrpl_stream` — its `last_run_completed` is 4h51m stale, but the row's `last_run_message` shows fresh data (`rate=25.0tx/s ledger=106741...`). Likely a `pgbridge.write_heartbeat` writing to a heartbeats table rather than touching `walker_health.last_run_completed` — the walker is actually running. Non-blocking for anchor #5.

Anchor #5's `walker_health_summary` metric will be computed at the 01:00 EDT signed_snapshot cron kick (in ~15 min) and will reflect this current healthy state, not the stale 13:51 count.

## Residual grep — after fixes

`grep -rniE "s1\.ripple|s2\.ripple|xrplcluster|live from the ledger" templates/ app.py static/` after commit `aa6030a`. Categorized:

**True (fine, no action):**
- Browser-side WS to `wss://xrplcluster.com` in `templates/{whales,tokens,pools,wallet}.html`, `_liveness_chip.html`, `live_stream.js`: primary browser subscription is xrplcluster — TRUE.
- `about.html:187` cascade-fallback mention of `s2.ripple.com`/`s1.ripple.com`: TRUE (JS uses xrplcluster primary + s2 + s1 fallbacks).
- `methodology.html:249-251` source-tier table entries for xrplcluster/s2/s1: describe browser subscription + fallbacks — TRUE.
- `methodology.html:617` `/api/ledger-tip` "local_rippled or s1.ripple.com when the local node is unavailable": describes documented cascade — TRUE (pending Render's XRPL_NODE value; see still-owed).
- `privacy.html:145-147` + `subprocessors.html:161`: enumerate public infra we query — TRUE as general disclosure.
- `verify.html:330`: curl example telling the user to hit s2 for their own TOML verification — TRUE (user-directed, not describing our sourcing).
- `app.py:1246-1249`: CSP header — config, not user-facing truth claim. TRUE.
- `app.py:6732`, `index.html:1246`: code comments about JS/API price source — not user-facing text.
- `tokens.html`/`whales.html`/`pools.html` "connecting to xrplcluster.com…" chip: browser-side, TRUE.

**One remaining "read live from the ledger" — still-owed:**
- `templates/index.html:846`: `"Paste any XRPL address to see balance, counterparties, and 30-day activity — read live from the ledger."` This is the /wallet lookup blurb. `/wallet/<addr>` reads via `wallet_data.py:1238` → `JsonRpcClient(XRPL_NODE)`. It IS live per request. Whether "live from the ledger" is sovereign or public depends on Render's `XRPL_NODE` env value — **still-owed SC-1**. If Render's XRPL_NODE is the tunnel (own-node): copy is fine. If it's public s1: needs disclosure.

**Still-owed items (unchanged from prior audit + SC-1):**
1. Second pass over the ~55 low-risk static/policy/form/redirect routes for individual table treatment.
2. **SC-1**: Render's `XRPL_NODE` env — tunnel or public? Determines whether:
   - `/wallet` (`wallet_data.py:1238`)
   - `/amendments` (`amendments.html:600`, `amendments_state.py:30`)
   - `/api/ledger-tip` (per methodology.html:617 source label)
   - `/pools` (`pools.html:704` "seeded from `amm_info` on the public XRPL node" — but the `rank_amms` walker runs on the Mac now, so this may also be stale)
   - `/api/xrp-price` (`app.py:6732` comment says xrplcluster.com)
   - homepage `/wallet` blurb (index.html:846)
   ...need their copy adjusted for sovereignty tier. Cannot resolve without Render env visibility. **On morning list.**
3. Confirm `/lending` (commit 4965644 flipped tunnel-first) — copy on `lending.html:379` "live from the ledger" says the broker list is live; verify against `lending_data.py` sovereignty.
4. Verify `templates/methodology.html:201` — RLUSD table row lists `s1.ripple.com` for the XRPL side; may be stale if rlusd_refresher now routes through the tunnel.

## Cross-page number agreement (dimension F) — all AGREE
- Validated ledger index: Lenovo LAN `full` @ 106,745,522+.
- site_totals singleton is the single source for countries/states/regions; no page hardcodes a conflicting count.
- Anchor count: anchor_history #1–#4 · chain.json 112 leaves · anchor account sequence 106138935 → #5 next. Consistent.
- escrow total: single source (`escrow_supply_snapshot`); `/`, `/cold-storage`, homepage /xrp-distribution all read the same row. Consistent.
- **escrow 31.7B round number CORRECTED**: verified plausible via `escrow_supply_walker.py:_sum_account_escrows` — summed from on-chain EscrowCreate objects. Ripple's escrows are round-denominated (monthly 1B-tranche design), so `.000000` is expected. Not a placeholder.

## Machine-surface parity (dimension E) — PASS
- `/check.json` → `check_page()` directly (same handler, same `check_data.py`).
- `/.well-known/snapshots/*` — machine-native by design.
- `/claims/index.json` — parity confirmed.

## Anchor #5 GO/NO-GO input from this audit

**GO** on the truth-audit dimension. All initial TV items fixed, deployed, verified live. Residual open items (SC-1: Render's XRPL_NODE) are copy-only and don't invalidate the anchored 7-metric set — those metrics are computed on the Mac by the walkers (LAN → sovereign) and read by Render as DB rows. Anchor #5 stamps 7 sovereign-origin data metrics + 3 meta metrics; that fact stands regardless of SC-1's resolution.

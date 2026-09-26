# Anchor history

On-ledger anchor transaction log. Each row is a Type A (fresh) or Type B
(correction) anchor per `ONLEDGER_ANCHOR_SPEC.md v1`. Verification: decode
MemoData from hex and compare chain_root to `/.well-known/snapshots/chain.json`
`root_history[<date>]` (or `current_root` for the latest date).

---

## Anchor #1 — 2026-08-07

| Field | Value |
|-------|-------|
| Type | A (genesis) |
| Tx hash | `01D0BB9D230955F43DB35703E2EB7F5DFA43CEB69CCBBF57FBC8F17407E50DF8` |
| Ledger | 106140698 |
| Close time | 2026-08-07 21:49:32 UTC |
| From | `rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ` (Anchor) |
| To | `rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd` (Dashboard) |
| Amount | 0.000001 XRP |
| Day of week | Friday (verified from ledger close_time — chain is authoritative) |
| MemoData (decoded) | `xrpldashboard/anchor/v1\|2026-08-07\|c73d65ae5927243b86ee9ddbfd02b967451dc75a6b4678a5a05dadc9dbfdf86a` |
| Verified | Genesis fixture — verifier must pass this tx |

---

## Anchor #2 — 2026-08-14

| Field | Value |
|-------|-------|
| Type | A (weekly) |
| Tx hash | `73951F479EDE071067FEA423FD2E67D8268470C8A3530B91AEA9826B469DC003` |
| Ledger | 106290824 |
| Close time | 2026-08-14 15:14:20 UTC |
| From | `rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ` (Anchor) |
| To | `rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd` (Dashboard) |
| Amount | 0.000001 XRP |
| MemoData (decoded) | `xrpldashboard/anchor/v1\|2026-08-14\|c92c377855cbaebbbaa0d034546f3c36975c86a2c03b84a9b881afc1271e7237` |
| chain_root verified | `c92c377855cbaebbbaa0d034546f3c36975c86a2c03b84a9b881afc1271e7237` matches live chain.json `current_root` at time of stamp |
| Day of week | Friday (verified from ledger close_time — same day-of-week as anchor #1, exactly 7 days later) |
| On-ledger result | `tesSUCCESS` |
| Notes | Stamped day-of 2026-08-14 (Friday, weekly cadence). Session included 8/8 fault-injection drill pass + anchor canary L1.5 install + Glama/Anthropic MCP directory submissions. Second power outage this week preceded the stamp (17h 38min gap 2026-08-13→14); chain.json leaf for 2026-08-14 confirmed fresh before stamping. |

### Pre-cadence verification

Before beginning the weekly cadence, we ran an external adversarial audit (zero false-data findings; anchor #1 independently verified from the raw ledger) and a fault-injection drill (8/8 alarms caught; one latent blind spot found and fixed during the drill itself). This anchor commits the audited chain.

---

## Anchor #3 — 2026-08-21

| Field | Value |
|-------|-------|
| Type | A (weekly) |
| Tx hash | `35E101A926867A96965BFA7705EA1045792BAC44F2EEEACA928D21892BAF5C45` |
| Ledger | 106451826 |
| Close time | 2026-08-21 19:50:01 UTC |
| From | `rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ` (Anchor) |
| To | `rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd` (Dashboard) |
| Amount | 0.000001 XRP (1 drop) |
| Fee | 12 drops |
| Sequence | 106138933 |
| MemoData (decoded) | `xrpldashboard/anchor/v1\|2026-08-21\|14a8e28420c37dadd952a57d3487034bd29b6522af1765253019ba5c17e0016f` |
| chain_root verified | `14a8e28420c37dadd952a57d3487034bd29b6522af1765253019ba5c17e0016f` matches `/.well-known/snapshots/chain.json` `root_history[2026-08-21]` (verified 2026-08-22 12:31 UTC via s1.ripple.com + Render-direct fetch) |
| Day of week | Friday (verified from ledger close_time — third consecutive Friday cadence, 7d after #2) |
| On-ledger result | `tesSUCCESS`, `validated=true` |
| Notes | Signed Fri 2026-08-21 15:50 EDT in prior session; recorded to git Sat AM (recording lag, no on-chain gap). Sequence continuity confirmed 106138931 (#1) → 106138932 (#2) → 106138933 (#3), monotonic. Genesis fixture (#1) intact. Week bracketed by the 2026-08-19 flap-storm (Neon `statement_timeout` fix landed Tue; 7-day stability clock reset to Day 0 and restarted 2026-08-20) and the Phase 2 memory-aware cache primitive PR #1 (`919f416`) shipping guard-only Sat 07:31 EDT. |

---

## Anchor #5 — 2026-09-04

| Field | Value |
|-------|-------|
| Type | A (weekly) |
| Tx hash | `BB72E91012DA3B05C060FAD64DD405DC09A8BAB3EC1A079AFD82367D65A3B44B` |
| Ledger | 106764148 |
| Close time | 2026-09-04 21:17:30 UTC |
| From | `rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ` (Anchor) |
| To | `rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd` (Dashboard) |
| Amount | 0.000001 XRP (1 drop) |
| Fee | 12 drops |
| Sequence | 106138935 |
| MemoData (decoded) | `xrpldashboard/anchor/v1\|2026-09-04\|8e259732deab4050bab0c2af4a6e184949627670c3109fccbe049007d162abbb` |
| chain_root verified | `8e259732deab4050bab0c2af4a6e184949627670c3109fccbe049007d162abbb` matches live `/.well-known/snapshots/chain.json` `current_root` at time of stamp (validated from own Lenovo LAN node, not public s1 — first anchor whose validation lookup was end-to-end sovereign) |
| Day of week | Friday (verified from ledger close_time — fifth consecutive Friday cadence, 7d after #4) |
| On-ledger result | `tesSUCCESS`, `validated=true` |
| Notes | **First 7-metric anchor** (Option A landed 2026-09-03: `rlusd_eth_supply` and `rlusd_total_supply` dropped from the anchored set — Ethereum data is public-RPC-sourced and can't meet the "originated from our own node" covenant; both still appear on `/rlusd` under a labeled sovereignty-note per `docs/methodology.html#anchored-metric-set`). The 10-metric leaf breakdown = 7 data metrics + 3 v4 meta metrics. `walker_health_summary` at stamp time = `{green=41, stale=1, dead=0}` — captures the post-cleanup state after this morning's cadence-declaration fixes on `rank_amms`/`mcp_*`/`nft_activity_backfill` walkers. **A double-run chain-link defect in the 09-04 snapshot was found by the L2 inspector pre-stamp and corrected before this stamp.** Root cause: `sign_snapshot` computed `previous_root` from `chain["current_root"]` before `append_or_replace_leaf`, so a same-date re-run stored `previous_root` pointing at the first-attempt's chain_root (overwritten and unreachable) instead of the prior day's. Six historical files carry the same defect (05-14/15/16, 06-14, 08-12, 08-30) and are already anchored via #1-4; documented in `docs/CHAIN_LINK_DEFECT_HISTORICAL_2026-09-04.md`. Fix landed in commit `60e9925` — `sign_snapshot` now computes `previous_root = merkle_root(all_leaves[:leaf_index])` after replace, `verify_envelope` gains a fifth `chain_link OK` check, `append_or_replace_leaf` prints a loud stderr on replace so future double-runs are never silent, and a regression test reproduces the scenario end-to-end. `docs/ONLEDGER_ANCHOR_SPEC.md` gains a standing pre-stamp checklist (5-OKs local `--verify` · L2 green · published==local · no `kickstart -k` on `signed_snapshot`). Sequence continuity confirmed 106138931 (#1) → 106138932 (#2) → 106138933 (#3) → 106138934 (#4) → 106138935 (#5), monotonic. Genesis fixture (#1) intact. |

---

## Anchor #4 — 2026-08-28

| Field | Value |
|-------|-------|
| Type | A (weekly) |
| Tx hash | `CC5F770EB2C6CAF798EB83ACCE67909A00EE8ED2CB66B0BA665CA96C860794FA` |
| Ledger | 106607271 |
| Close time | 2026-08-28 20:18:10 UTC |
| From | `rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ` (Anchor) |
| To | `rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd` (Dashboard) |
| Amount | 0.000001 XRP (1 drop) |
| Fee | 12 drops |
| Sequence | 106138934 |
| MemoData (decoded) | `xrpldashboard/anchor/v1\|2026-08-28\|8548493f82cfb208515c00601570e82f9f796b3239680c1448ac49484fe194dc` |
| chain_root verified | `8548493f82cfb208515c00601570e82f9f796b3239680c1448ac49484fe194dc` matches live `/.well-known/snapshots/chain.json` `current_root` at time of stamp (independently confirmed by anchor canary v3.0 via full-history witness `s2-clio.ripple.com:51234` — Shape C ledger-derived verify, no local registry file) |
| Day of week | Friday (verified from ledger close_time — fourth consecutive Friday cadence, 7d after #3) |
| On-ledger result | `tesSUCCESS`, `validated=true` |
| Notes | **Today's anchor seals the day the Quadfecta audit machine-bug repair list hit ZERO** — six Batch 3 kills live-verified this morning (walker_health/scope, cold-crawler triage, /docs scraper visibility, /analytics cache posture, +2) on top of the week's ten, the most-repaired most-honest day the site's had. First ceremony under the **Shape C close ritual**: `docs/anchor_registry.json` deleted + `anchor_registry_append.py` archived (commit `4ff8080`); the canary reads Clio `account_tx` directly and reports fresh — its ceremony debut as the watching eye that closes the loop (not just a tripwire). `--dry-run --force-heartbeat` verified 4 anchors discovered, latest root matched, no alerts fired: **"The chain IS the registry. The one check no thief can silence is alive."** Sequence continuity confirmed 106138931 (#1) → 106138932 (#2) → 106138933 (#3) → 106138934 (#4), monotonic. Genesis fixture (#1) intact. Small observability item filed post-ceremony: canary has no `--print-view` for silent-on-green dry-runs, `--force-heartbeat` used as workaround this cycle. |

---

## Anchor #6 — 2026-09-11

| Field | Value |
|-------|-------|
| Type | A (weekly) |
| Tx hash | `54C2D7569598277DF39F9EB8B2137AE8546971A57C3894E9628753E9E89BB6C7` |
| Ledger | 106922141 |
| Close time | 2026-09-11 22:54:10 UTC |
| From | `rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ` (Anchor) |
| To | `rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd` (Dashboard) |
| Amount | 0.000001 XRP (1 drop) |
| Fee | 12 drops |
| Sequence | 106138936 |
| MemoData (decoded) | `xrpldashboard/anchor/v1\|2026-09-11\|cd0545a4685cdf8a7cab2f3507aef7bc043ba7f28e79ff85f6b4044a88c47c75` |
| chain_root verified | `cd0545a4685cdf8a7cab2f3507aef7bc043ba7f28e79ff85f6b4044a88c47c75` matches live `/.well-known/snapshots/chain.json` `current_root` at time of stamp (validated end-to-end from own Lenovo LAN node — `rippled tx` returned `validated=true`, `tesSUCCESS`, and the MemoData hex decoded byte-for-byte to the third-segment root; no public s1/s2, no external oracle). |
| Day of week | Friday (verified from ledger close_time — sixth consecutive Friday cadence, 7d after #5) |
| On-ledger result | `tesSUCCESS`, `validated=true` |
| Notes | **First anchor stamped under the Tier 0 `pre_stamp_git_clean_is_a_gate` rule.** Ceremony was held mid-flow: point 9 of the 9-point pre-flight returned 4 modified + 6 untracked paths in `~/xrpl_test` (walker output, one un-committed intended edit to `db.py`'s `BOT_UA_PATTERNS` from 09-09 evening, one one-shot in `scripts/`, and true scratch). Per Charlie's ruling — an anchor signs a fully known state, so every path got a WALKER OUTPUT / INTENDED CODE / STRAY disposition before we resumed. Walker output added to `.gitignore` (`docs/SITE_TOTALS.json` via `git rm --cached`, `launchd_logs/`, `tmp/`); intended edits committed (`db.py` self-canary UA classifier fix, `token_names.json` walker-curated MPT metadata, `scripts/clear_gatehub_collision_flags.py` one-shot); scratch moved to `~/xrpl_test_private_triage/tmp_2026-09-11/`. Commit `e893044` also closed a genuine serving gap: `signed_registry_snapshots/2026-09-{08,09,10,11}.json` were disk-served by `well_known_signed_registry` (`app.py:5714`, no PG fallback) but never committed after 09-08 — Render was returning 404 for those three dates until this commit landed. Full nine-point pre-flight re-ran clean before the stamp: verify 2026-09-11 5-of-5 OKs, one leaf per date 09-05→09-11, published==local, own node full (`state=full·validated_seq=106,921,567·peers=10`), anchor sequence 106138936 monotonic (`106138931(#1)→106138932(#2)→106138933(#3)→106138934(#4)→106138935(#5)→106138936(#6)`), 0 `walker_node_fallback` events since the `af09750` `/health`-always-200 deploy, git clean at 0 uncommitted paths. Adjacent housekeeping filed the same session: (a) BetterStack Uptime configured with two monitors (`/api/heartbeat-age` upgraded from `Immediate start` to 5-min Confirmation = 2-consecutive-fails semantic; new `/healthz` monitor added at 3-min interval + 5-min Confirmation, e-mail-only) with `xrpl_test_private_infra/DEPLOY.md` Phase 6 re-written to reflect the current setup — replaces the stale UptimeRobot plan that was never wired; (b) `neondb_owner` password rotated across 5 surfaces (Mac env×2, Lenovo env, Render env, all services restarted) after 09-02/07 owner-cred exposures, with a new read-only `jj_ro` role created and JJ's query wrapper switched to source `~/.config/xrpldashboard/jj_env` — the guardrail was proven with a SELECT + INSERT-refused test (`ERROR: permission denied for table page_views`); (c) GateHub canonical-issuer whitelist added to `ticker_canonical_issuers.json` (32 addresses × 17 tickers, cited from `gatehub.net/legal/xrpl-addresses`) — flipped 5 stale collision-flagged rows (all cold wallets) off the curator queue via a one-shot walker wrapper (`scripts/clear_gatehub_collision_flags.py`). Post-freeze work filed: PG-first read pattern for `well_known_signed_registry` so Render stops depending on Mac git commits, plus the `signed_snapshot` debounce for RunAtLoad double-fire, plus backup consolidation to drop the redundant 04:15 EDT `dockvault_neon_dump` pull-back. |

---

## Chain gap — 2026-07-15 (launchd session restart; recorded 2026-09-26)

No leaf exists for 2026-07-15. Reconstructed 2026-09-26 from `signed_snapshots.written_at`, `launchd_logs/signed_snapshot.out.log`, and the 2026-07-15 launchd incident record — the walker itself logged no error:

- The signing job ran on a `StartInterval` cadence at the time: write times drift by ~3 s/day from 08:25:49 ET (2026-07-01) to 08:26:27 ET (2026-07-14). A calendar job would not drift.
- No run fired on the morning of 2026-07-15. The out log goes straight from `date=2026-07-14 … leaf_index=61/62` to `date=2026-07-16 … leaf_index=62/63`; the err log is clean.
- A new drift series starts at **20:13:01 ET on 2026-07-15** (then 20:13:04, 20:13:08, 20:13:12 on the following days): the job was re-bootstrapped at that moment. Its first run landed after 00:00 UTC, so `snapshot_date_utc` became 2026-07-16.
- Same day as the recorded launchd incident in which the NFT walkers were found silent because their plists lived only in `~/xrpl_test/launchd/` and a session logout/reboot after 19:40 ET on 2026-07-14 unloaded them; the signing job most plausibly went down with them and came back during that repair. No commit touched `signed_snapshot.py` between 2026-07-10 and 2026-07-20.
- **Chain intact:** the 2026-07-16 leaf's `previous_root` = `c4181f5f73d1facd…` = 2026-07-14's `chain_root` (leaf 61 → leaf 62). No fabricated intermediate leaf; the gap is the record. Never sign past dates.

## Chain gap — 2026-09-16 → 2026-09-18 (Mac power outage)

Signed_snapshot walker offline **Tue 2026-09-15 ~11:15 AM ET → Sat 2026-09-19 ~11:20 AM ET** (~96 hours; Mac lost power). No leaves were signed for 2026-09-16, 2026-09-17, or 2026-09-18. Signing resumed on Sat 2026-09-19; that day's leaf `previous_root` = `0f13f9e1a51dd941d50303c1b286b6513921066010cb0ed8574790488c4e930d` = 2026-09-15's `chain_root`, bridging the four-day gap directly with no fabricated intermediate leaves. Verification runs clean: `signed_snapshot.py --verify 2026-09-19` returns `signature OK, audit_path OK, leaf_hash OK, fingerprint OK, chain_link OK`.

**Anchor #7 delayed one day** — the Friday 2026-09-18 ceremony was skipped because no fresh chain_root existed to anchor. It happened Sat 2026-09-19 at 20:13:51 UTC (ledger 107098884, tx hash `A59DE2FAFD517EEFC4C2B85381773A108C2E75FA50290460326F172A76702A5C`) — recorded here as delayed with the outage as the on-record reason. Sequence 106138937 on the anchor account, chain_root of the 2026-09-19 leaf. Charlie ruling 2026-09-19: **workers offline Sep 15–19 (power outage). The gap IS the record. Never sign past dates.**

---

## Anchor #7 — 2026-09-19 (delayed one day; Mac power outage)

| Field | Value |
|-------|-------|
| Type | A (weekly, **delayed one day** — Fri 2026-09-18 skipped, held Sat 2026-09-19) |
| Tx hash | `A59DE2FAFD517EEFC4C2B85381773A108C2E75FA50290460326F172A76702A5C` |
| Ledger | 107098884 |
| Close time | 2026-09-19 20:13:51 UTC |
| From | `rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ` (Anchor) |
| To | `rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd` (Dashboard) |
| Amount | 0.000001 XRP (1 drop) |
| Fee | 12 drops |
| Sequence | 106138937 |
| MemoData (decoded) | `xrpldashboard/anchor/v1\|2026-09-19\|b202095ae84d2456e29444f92736b38fb93eee18b8380f449c796b3e3d2e93ab` |
| chain_root verified | `b202095ae84d2456e29444f92736b38fb93eee18b8380f449c796b3e3d2e93ab` matches live `/.well-known/snapshots/chain.json` `current_root` at time of stamp; today's leaf `previous_root` = `0f13f9e1a51dd941d50303c1b286b6513921066010cb0ed8574790488c4e930d` = 2026-09-15's `chain_root`, bridging the four-day outage gap with zero fabricated intermediate leaves. Validated end-to-end from own Lenovo LAN node (`192.168.40.95:5006`) — `rippled tx` returned `validated=true`, `tesSUCCESS`, `ledger_index=107098884`, `Sequence=106138937`; MemoData hex decoded byte-for-byte to the third-segment root; no public s1/s2, no external oracle. |
| Day of week | Saturday (verified from ledger close_time — cadence break: first non-Friday anchor, driven by the outage) |
| On-ledger result (⍐7) | `tesSUCCESS`, `validated=true` |
| Notes | **First anchor stamped after a chain gap; first anchor whose sealed leaf was later restored from backup.** Signing walker was offline Tue 2026-09-15 ~11:15 AM ET → Sat 2026-09-19 ~11:20 AM ET (~96 hours; Mac lost AC power). Anchor #7 was originally scheduled for Fri 2026-09-18 and was skipped because no fresh chain_root existed to anchor — Charlie ruling 2026-09-19: **never sign past dates; the gap IS the record**. **Post-stamp incident (2026-09-19 17:28 ET):** the same-day pull-plug test that validated `autorestart=1` triggered `RunAtLoad=true` on `signed_snapshot`'s plist post-boot; the walker recomputed the 09-19 leaf with fresh post-boot walker/registry/editorial inputs and produced `chain_root=4d8c2c9a6bec9484ae6fbef983511555fd54c37b9dfc52cc73e38505d88ffe53`, silently orphaning anchor #7's on-ledger `b202095a...` claim. Recovered by restoring `signed_snapshots/2026-09-19.json` from the 11:27 DockVault mirror backup, rebuilding `chain.json` (`current_root`, `leaves[-1]`, `root_history[-1]` all set to `b202095a...` with `leaf_hash=4bd01371f0b5b0fc84dc5f9c46d498a3771c65045dfa0b7c76e99de2d49114a3`), re-writing the envelope + chain head to Postgres via `db.write_signed_snapshot`, and flushing Render's 300 s TTL. `signed_registry_snapshots/2026-09-19.json` was double-signed the same way and restored from DockVault (canonical_hash matches the git-committed `a9f819b` version). Post-recovery proofs (2026-09-19 18:2x ET): `signed_snapshot.py --verify 2026-09-19` returns 5-of-5 OKs; anchor #7 memo `chain_root` = `chain.json.current_root` = `b202095a...`; Render `/.well-known/snapshots/chain.json` matches disk. Tier 0 debounce landed in the same session (commit `16360af`): `signed_snapshot.py` and `signed_registry_snapshot.py` refuse to write a leaf for a date that already has one unless `--force`; skip path exits 0 without touching walker_health. The post-anchor-#6 debounce work-item that was filed and never landed cost an hour today. See the `Chain gap — 2026-09-16 → 2026-09-18` section above and `~/xrpl_test_private_triage/MAC_OUTAGE_2026-09-15.md`. Recovery completed today: 5 daily walkers kicked back to green, tunnel/sig-service/dockvault TCC survived, `nft_activity_summary` plist restored to `~/Library/LaunchAgents/` (had never been install-copied and did not survive cold boot — `PRE_MACOS_UPDATE_2026-09-12.md` §1 corrected 52→53 expected loaded), meta-watcher backlog left to self-clear (wrapper-stamp legacy pattern filed for post-anchor systematic normalization). Full nine-point pre-flight re-ran clean before the stamp: verify 2026-09-19 5-of-5 OKs (`signature OK, audit_path OK, leaf_hash OK, fingerprint OK, chain_link OK`); leaves 09-12/13/14/15/19 present, 09-16/17/18 correctly missing per outage; published==local (Render `current_root` == disk); own node full (`state=full · validated_seq=107,098,250 · peers=10 · complete_ledgers 107018362-107098250`); anchor sequence 106138937 monotonic (`106138931(#1)→106138932(#2)→106138933(#3)→106138934(#4)→106138935(#5)→106138936(#6)→106138937(#7)`); anchor account balance 23.999933 XRP with OwnerCount=0; sig-service state=running (PID 1256); git clean at 0 uncommitted paths after four dispositions (walker diff on `token_names.json` reverted to preserve Render name lookups; scratch `REGULATION_HARD_TRIGGER_2026-09-14.txt` moved to `~/xrpl_test_private_triage/tmp_2026-09-19/`; `docs/scope/LOCAL_LLM_2026-09-13.md` committed as `011fd7c`; `signed_registry_snapshots/2026-09-15.json` committed as `f61e4d6` per the standing daily-registry carve-out — pre-outage signed 2026-09-15T02:15:04Z, missed push window when Mac lost power). Sequence-continuity proof relies on Clio + own Lenovo node returning the same `ledger_index` / `Sequence` / `TxnResult` — chain of custody preserved end-to-end with no public-s1/s2 or external oracle in the verification path. |

---

## Anchor #8 — 2026-09-25 (back on Friday cadence)

| Field | Value |
|-------|-------|
| Type | A (weekly, **Friday** — cadence restored after #7's one-off Saturday outage exception) |
| Tx hash | `3CA7C4E34274C5C742FF0DA924B6DC3EC6C6FC0BC7983494A53C72BFF5130619` |
| Ledger | 107233208 |
| Close time | 2026-09-25 20:13:11 UTC (16:13 ET) |
| From | `rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ` (Anchor) |
| To | `rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd` (Dashboard) |
| Amount | 0.000001 XRP (1 drop) |
| Fee | 12 drops |
| Sequence | 106138938 |
| MemoData (decoded) | `xrpldashboard/anchor/v1\|2026-09-25\|34be89f4c274f1438709c416367517df455f53da7d16f00511951db740f5186d` |
| chain_root verified | `34be89f4c274f1438709c416367517df455f53da7d16f00511951db740f5186d` matches live `/.well-known/snapshots/chain.json` `current_root` AND local `signed_snapshots/chain.json` `current_root` at stamp time (both `34be89f4…`, pre-flight point 3+4 MATCH). Validated end-to-end from own Lenovo LAN node (`192.168.40.95:5006`) — `rippled tx` returned `validated=true`, `tesSUCCESS`, `ledger_index=107233208`, `Sequence=106138938`; MemoData hex decoded byte-for-byte to the third-segment root; no public s1/s2, no external oracle. |
| Day of week | Friday (verified from ledger close_time — **cadence restored**; #7 was the one-off Saturday outage exception, not a schedule change) |
| On-ledger result | `tesSUCCESS`, `validated=true` |
| Notes | Routine Friday anchor — first clean weekly ceremony since the Sep-15–19 outage saga. Full pre-flight GREEN on all gates: verify 2026-09-25 5-of-5 OKs (`signature OK, audit_path OK, leaf_hash OK, fingerprint OK, chain_link OK`); published==local for `2026-09-25.json` MATCH; local `chain.json` `current_root` == published `current_root` == `34be89f4…` (the earlier pre-flight `?` was a wrong path — local chain lives at `signed_snapshots/chain.json`, resolved before stamp); own node full (`state=full · validated_seq=107,231,924`); anchor sequence 106138938 monotonic (`106138931(#1)→106138932(#2)→106138933(#3)→106138934(#4)→106138935(#5)→106138936(#6)→106138937(#7)→106138938(#8)`); anchor account balance 23.99992 XRP with OwnerCount=0 pre-stamp (23.999907 post-stamp); git clean at 0 uncommitted paths after four dispositions (commit `712dba6`: `token_names.json` walker-curated MPT metadata committed — TRACKED, standing pattern; `signed_registry_snapshots/2026-09-{22,23,24,25}.json` daily-registry carve-out committed; `signed_verified_tokens_latest.json` + `static/coin_emblems/` walker output gitignored). Session context: same day as the /amendments BatchV1_1 majority-reset verification + flag-ledger presentation fix (commits `8b54cf2`, `395d0f0`) and the walker_health `run_in_progress != ok=false` tri-state fix (`2ddbe0c`). Sequence-continuity proof relies on own Lenovo node returning `validated=true`/`Sequence=106138938`/`tesSUCCESS` — chain of custody preserved end-to-end with no public s1/s2 or external oracle in the verification path. |

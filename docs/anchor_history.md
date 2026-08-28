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

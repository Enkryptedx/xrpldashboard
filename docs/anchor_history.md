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

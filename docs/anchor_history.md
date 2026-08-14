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

### Session note

**Why anchor #2 fired a day late — a note for the record**

Our second weekly anchor was intended for Thursday, August 13 — one week after anchor #1 landed on Friday, August 7. It fired the following day, Friday, August 14 at 15:14:20 UTC. Gaps in a trust record deserve explanations, not silence, so here is exactly what happened.

We held the stamp on purpose. Before settling into a weekly cadence of anchoring our audit trail to the XRP Ledger, we ran two checks: an external AI audit instructed to assume everything we publish is wrong until independently proven, and a fault-injection drill — deliberately breaking our own systems to confirm every alarm actually fires. An anchor makes our history permanent. Before making a habit of permanence, we wanted adversarial proof that what we anchor is worth anchoring.

Then the power went out. An electrical interruption at our operations site Thursday evening shut down the machines that run our data collection and the drill itself. The website stayed up — it runs on cloud infrastructure — and data collection paused and resumed honestly, with the 17h 38min gap noted in our records, same as always.

The results: the external audit found zero false-data findings across every surface it checked and independently verified anchor #1 against the raw ledger. The fault-injection drill caught 8 of 8 injected failures — most within one second — and surfaced one latent blind spot (orphaned heartbeat rows from May were masking one detection path), which was found, fixed, and re-verified during the drill itself.

This anchor — one day late, but landing exactly seven days after anchor #1, on the same day of week (Friday to Friday, verified from ledger close_time) — commits a chain audited harder than anything we had published before.

Anchor #2: `73951F479EDE071067FEA423FD2E67D8268470C8A3530B91AEA9826B469DC003`, ledger 106,290,824, 2026-08-14 15:14:20 UTC.

One day late. We'd make the same trade every time: the cadence is ours to define, the protocol records honest delays cleanly, and a checked stamp beats a punctual one.

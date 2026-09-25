# New-Accounts Block — Design Spec (2026-09-25)

Status: **APPROVED with edits** (Charlie 2026-09-25). Design only. Build
gated: **do not build before item 10 (Option B) reaches its first
milestone** (Charlie ruling 2026-09-25).

## Goal

A surface tracking newly-created XRPL accounts (network-growth / activation
signal), analogous to the existing snapshot blocks. **Count only** for v1;
funder concentration is **v2**.

Placement (Charlie ruling): **homepage block + a /methodology definition**
(not /registry). The /methodology entry states exactly how we count and the
provenance boundary below.

## Detection primitive (already available)

Account creation on XRPL = a `Payment` funding a never-before-seen address,
which emits a **`CreatedNode` of `LedgerEntryType: AccountRoot`** in tx
metadata. `xrpl_stream.py` already parses `CreatedNode`/`ModifiedNode`/
`DeletedNode` for AccountRoot (line ~461). No new XRPL capability needed —
a walker (or a stream hook) records each AccountRoot CreatedNode.

## Data-source reality (the pre-write check, Charlie 2026-09-25)

Question posed: since `xrpl_stream` moved to Lenovo (~Aug 31), where has it
written transactions, and does that close the Aug31→now gap?

**Answer: it writes to the PG `events` table (`db.write_event`), which is
continuous BY DATE May 8 → now (2.38M rows). There is no date gap. BUT the
capture SCOPE changed on Sep 9.**

| Window | Daily rows | Scope | Complete for new-accounts? |
|---|---|---|---|
| May 8 – Sep 8 | ~90k–484k/day | high-volume stream | ✅ plausibly complete |
| **Sep 9 – now** | **~3–7.8k/day** | watchlisted-account + large-transfer scope (still includes small payments FOR watchlisted accts — confirmed: Sep-20 sample 1,264 under-100-XRP vs 314 over) | ❌ **under-counts** — not all network-wide funding Payments captured |

The sqlite `events.db` (separate, stops Aug 31) was a red herring; the PG
`events` table is the Lenovo-era target and is continuous by date.

### Backfill verdict

Per Charlie's edit ("backfill yes if our own capture is continuous from May,
else forward-only with stated tracking-since"):

- Capture is continuous by DATE but **NOT continuously COMPLETE** — the Sep-9
  scope reduction breaks completeness for network-wide account creation.
- **Therefore: FORWARD-ONLY, with a stated "tracking since <walker-start>"
  date.** This is the fallback branch Charlie specified, now confirmed as the
  correct one.
- A May 8 – Sep 8 backfill is *optionally* possible from the PG `events`
  table, but it CANNOT be presented as continuous: it would need an explicit
  "complete May 8–Sep 8; scoped/partial thereafter" disclosure. Deferred —
  not part of v1 unless Charlie wants the longer chart with that disclosure.

## Copy / claims discipline (Charlie ruling)

- **No "all-time high" claim** without full history. Only
  **"highest daily since <date>"**, where <date> = the walker's tracking-start
  (or the backfill floor IF a disclosed backfill ships).
- The homepage block and /methodology entry both state the tracking-since
  date and that counts are from our own-node stream capture.

## v1 shape (forward-only)

New table `new_accounts` (append-only), one row per AccountRoot CreatedNode:

```
address           TEXT PRIMARY KEY   -- the funded (new) account
funding_tx        TEXT NOT NULL
funder            TEXT               -- source account (kept for v2 concentration; not surfaced in v1)
amount_drops      BIGINT
ledger_index      BIGINT NOT NULL
close_time        BIGINT NOT NULL    -- XRPL epoch seconds
first_seen_iso    TEXT NOT NULL
source            TEXT NOT NULL DEFAULT 'own_node_stream'
```

- Walker or stream hook records each new AccountRoot.
- Homepage block: "accounts created (24h / 7d)" + "highest daily since
  <tracking-since>".
- /methodology: definition + provenance boundary + tracking-since date.
- v2 (later): funder concentration (exchange-driven onboarding signal) from
  the `funder` column already captured.

## Open (deferred to build time)

- Confirm the walker vs. stream-hook implementation choice against the
  current Lenovo stream scope (a stream hook inherits the Sep-9 filtering;
  a dedicated walker reading validated ledgers' AccountRoot CreatedNodes
  from our own node would be complete GOING FORWARD regardless of the
  large-transfer stream scope — likely the better primary).
- Decide whether to ship the disclosed May 8 – Sep 8 backfill for a longer
  chart.

## Build gate

Do not implement before item 10 (Option B) hits its first milestone.

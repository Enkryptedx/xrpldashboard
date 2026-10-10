-- Evernorth daily treasury snapshot (2026-10-10).
--
-- One row per calendar day, written after the 21:00 ET signed snapshot.
-- PRIMARY KEY on the date makes a re-run idempotent: a retry overwrites
-- that day instead of appending a second row, which would otherwise show
-- two points for one day on the /institutional card's history line.
--
-- balances keeps the 13 per-wallet readings next to the total so a later
-- correction can be audited against what was actually read, not just the
-- aggregate. A wallet that could not be read is stored as null rather than
-- 0 - counting an unreadable balance as zero would understate the total
-- and look like an outflow that never happened.
--
-- Applied automatically by db.init_schema() (same statement lives in
-- SCHEMA_DDL), so no owner-run step is required.

CREATE TABLE IF NOT EXISTS evernorth_daily_snapshot (
    snapshot_date  DATE PRIMARY KEY,
    taken_at       BIGINT NOT NULL,
    total_xrp      NUMERIC NOT NULL,
    readable_count INTEGER NOT NULL,
    wallet_count   INTEGER NOT NULL,
    balances       JSONB NOT NULL
);

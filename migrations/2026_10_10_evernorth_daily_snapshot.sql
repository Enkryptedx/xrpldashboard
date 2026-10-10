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
-- Applied by the nightly job itself: scripts/evernorth_daily_snapshot.py
-- calls db.ensure_evernorth_daily_snapshot_table() at the top of every
-- tick, so no owner-run step is required.
--
-- Do NOT rely on SCHEMA_DDL alone for this. The same statement lives
-- there, but init_schema() is a manual one-off (only backfill_amm_pools.py
-- calls it) and nothing runs it at app boot. Verified 2026-10-10 against
-- production: the table did not exist, so every insert would have been
-- swallowed by the writer's best-effort except - a silently missing row
-- rather than a visible failure.

CREATE TABLE IF NOT EXISTS evernorth_daily_snapshot (
    snapshot_date  DATE PRIMARY KEY,
    taken_at       BIGINT NOT NULL,
    total_xrp      NUMERIC NOT NULL,
    readable_count INTEGER NOT NULL,
    wallet_count   INTEGER NOT NULL,
    balances       JSONB NOT NULL
);

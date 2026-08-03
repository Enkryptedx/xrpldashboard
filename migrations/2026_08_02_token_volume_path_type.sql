-- Migration 2026-08-02: token_volume path_type per row
--
-- Adds path_type column to token_volume so each hourly bucket is
-- classified by settlement venue: AMM, CLOB, MIXED, DIRECT, AMM_LP,
-- or UNKNOWN. Pre-2026-08-02 rows stay NULL — the past isn't taggable,
-- and house law is that we don't backfill guesses.
--
-- Unblocks the value-weighted CLOB/AMM floor analysis staged in
-- docs/FLOOR_RERUN_2026-08.md (Correction 2: walker migration required
-- to produce value-weighted CLOB/AMM share).
--
-- The old PRIMARY KEY (currency, issuer, hour_bucket) is dropped and
-- replaced with a UNIQUE INDEX that includes path_type. Postgres treats
-- NULLs as distinct in unique indexes, so legacy rows (path_type IS
-- NULL) remain uniquely constrained by (currency, issuer, hour_bucket)
-- with respect to each other, matching the old PK's semantics.
--
-- Idempotent; safe to re-run. SCHEMA_DDL in db.py re-declares these
-- statements so fresh installs stay in sync.

ALTER TABLE token_volume ADD COLUMN IF NOT EXISTS path_type TEXT;
ALTER TABLE token_volume DROP CONSTRAINT IF EXISTS token_volume_pkey;
CREATE UNIQUE INDEX IF NOT EXISTS token_volume_path_uniq_idx
    ON token_volume (currency, issuer, hour_bucket, path_type);

-- After this migration, the writer at xrpl_stream.token_event_handler
-- (via db.upsert_token_volume) starts populating path_type on every
-- new bucket. Value-weighted analysis becomes available:
--
--   -- Floor analysis: value-weighted CLOB share (per trailing N days,
--   -- filtering out DIRECT transfers and AMM_LP liquidity ops):
--   WITH trading AS (
--     SELECT path_type, SUM(volume_xrp) AS v
--     FROM token_volume
--     WHERE hour_bucket >= <cutoff>
--       AND path_type IN ('AMM', 'CLOB', 'MIXED')
--     GROUP BY path_type
--   )
--   SELECT path_type, v, v / (SELECT SUM(v) FROM trading) AS share
--   FROM trading ORDER BY v DESC;
--
-- Clocks (all measured from ship date):
--   +7-14d  distribution readable for trend-shape
--   +30d    kill-criterion measurable (5% CLOB threshold, rolling 30d)

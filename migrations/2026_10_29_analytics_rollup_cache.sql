-- 2026-10-29  Hour-keyed rollup cache for the heavy /analytics aggregates,
--             plus a covering index for the /whales radar HUD counters.
--
-- Motivation (owner-side pg_stat_statements, window opening 2026-10-27
-- 03:12 ET): page_views analytics aggregates were the largest non-backup
-- consumer of compute on the instance.
--
--   1,576 calls @ 3.9s   SELECT COUNT(*) FROM page_views p WHERE p.country
--                        IS NOT NULL AND (country, path, day) IN (...)
--                          -> db.scan_burst_cohorts  (reclassified receipts)
--   1,617 calls @ 1.8s   WITH daily_counts AS (SELECT country, path, ...)
--                          -> db.scan_burst_cohorts  (cohort detection)
--   2,423 calls @ 2.2s   SELECT COUNT(DISTINCT country) FROM page_views
--     710 calls @ 1.9s     ... (all-time and 24h variants)
--                          -> db.read_country_count, 2x per /analytics render
--   2,933 calls          SELECT COUNT(*), COUNT(DISTINCT visitor_hash) ...
--                          -> db.read_page_view_stats / read_cta_click_stats
--   7,777 calls          SELECT COUNT(*) FROM events WHERE type = ...
--                        AND amount_drops >= ... AND ts >= ...
--                          -> db.read_whale_radar_stats, via /whales and
--                             /api/whales/radar-stats (60s client poll)
--
-- ===================================================================
-- PART 1 - safe to run inside a transaction. Also created on first use
-- by db._ensure_rollup_cache_table(), so a deploy needs no manual step
-- for this part; it is written out here for the record and for a fresh
-- provision via init_schema().
-- ===================================================================

CREATE TABLE IF NOT EXISTS analytics_rollup_cache (
    cache_key   TEXT    PRIMARY KEY,
    hour_bucket BIGINT  NOT NULL,
    payload     JSONB   NOT NULL,
    computed_at BIGINT  NOT NULL,
    compute_ms  INTEGER
);

CREATE INDEX IF NOT EXISTS analytics_rollup_cache_hour_idx
    ON analytics_rollup_cache (hour_bucket);

-- ===================================================================
-- PART 2 - OWNER-RUN, and NOT inside a transaction.
--
-- `events` is 2.34M rows / 7.5 GB. CREATE INDEX CONCURRENTLY cannot run
-- inside a transaction block and must therefore be executed on its own,
-- by the owner, through a walker wrapper or psql session that holds
-- write credentials. Do NOT fold this into init_schema() (which wraps
-- SCHEMA_DDL in one transaction) and do NOT run it from an interactive
-- agent session -- jj_ro is SELECT-only by design.
--
-- Why this index: read_whale_radar_stats runs two statements, both
-- filtering `type = 'large_xfer' AND amount_drops >= $1`, one adding
-- `ts >= $2` and the other `ORDER BY ts DESC LIMIT 1`. The existing
-- events_type_ts_idx (type, ts DESC) serves the type+ts part but leaves
-- amount_drops to a heap fetch per candidate row. Adding amount_drops as
-- a trailing key column lets both statements filter inside the index
-- (index-only scan), which is what makes the remaining ~7.8k calls cheap
-- rather than merely less frequent.
--
-- Run as:
--   CREATE INDEX CONCURRENTLY ... ;        -- may take a few minutes
--   -- then confirm it is valid, not left INVALID by an interrupted build:
--   SELECT indexrelid::regclass, indisvalid FROM pg_index
--    WHERE indexrelid = 'events_type_ts_amount_idx'::regclass;
--
-- If it reports indisvalid = false, DROP INDEX CONCURRENTLY it and re-run.
-- events_type_ts_idx is left in place deliberately: it is a prefix of the
-- new index and therefore redundant, but dropping it is a separate,
-- reversible decision and not part of this change.

-- CREATE INDEX CONCURRENTLY IF NOT EXISTS events_type_ts_amount_idx
--     ON events (type, ts DESC, amount_drops);

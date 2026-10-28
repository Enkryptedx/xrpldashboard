-- events + nft_activity: per-table autovacuum/autoanalyze tuning.
--
-- ALREADY APPLIED on prod 2026-10-28 08:12 ET by Charlie via the Neon SQL
-- Editor. This file is the record, not the application. Both statements are
-- idempotent -- ALTER TABLE ... SET (...) overwrites the same reloptions on a
-- re-run -- so replaying this migration against prod is a safe no-op.
--
-- WHY
-- ---
-- Autovacuum thresholds are ROW-COUNT based, never byte-based. A table can
-- carry arbitrary bulk in TOAST and stay permanently invisible to autovacuum
-- because its row count never crosses the trigger. That is exactly how
-- nft_activity accumulated 31 GB of orphaned TOAST unnoticed after its `raw`
-- column was dropped (migrations/2026_10_26_drop_nft_activity_raw.sql);
-- VACUUM FULL on 2026-10-28 took it from 39,439,040,512 -> 4,561,813,504
-- bytes, an 88.4% reduction, reclaiming ~34.88 GB.
--
-- Measured on 2026-10-28 before this change (PostgreSQL 17.11, all autovacuum
-- settings at cluster defaults, autovacuum=on, track_counts=on):
--
--   events        7,487 MB total -- heap 803 MB, indexes 400 MB,
--                 TOAST 6,284 MB across 2,519,244 rows (raw_json jsonb,
--                 ~2,344 bytes/row, LIVE data -- not reclaimable)
--   nft_activity  4,352 MB total after the VACUUM FULL, 9,943,878 rows
--
-- Both tables are append-only (n_tup_upd = 0, n_tup_del = 0), so dead tuples
-- never accumulate and the dead-tuple autovacuum trigger can never fire. PG13+
-- added an insert-triggered path, but at the default scale factor the bar is
-- `1000 + 0.2 * reltuples`, which neither table was approaching:
--
--   table         inserts in epoch   old insert-vacuum bar   old analyze bar
--   events                  47,790                 506,516           252,808
--   nft_activity           966,008               1,989,919           994,510
--
-- events sat at 9% of its vacuum threshold and 19% of its analyze threshold.
-- Its planner statistics were correspondingly stale: n_live_tup read 47,780
-- against 2,519,244 actual rows, a 53x under-estimate, until Charlie ran a
-- manual ANALYZE on 2026-10-28 08:05 ET.
--
-- Dropping both scale factors to 0.02 lowers the bars ~10x:
--
--   table         new insert-vacuum bar   new analyze bar
--   events                       51,552            50,602
--   nft_activity                199,892           198,942
--
-- NOT A FIX FOR WRAPAROUND
-- ------------------------
-- This does not address transaction-ID freezing. As of 2026-10-28 events and
-- token_volume sit at xid_age ~67.17M against autovacuum_freeze_max_age of
-- 200M (33.6%), and every other table is at ~32% -- so the whole database
-- reaches its forced anti-wraparound vacuum at roughly the same time. That
-- remains a scheduled-maintenance item, tracked separately.
--
-- token_volume already carried its own reloptions (all three scale factors at
-- 0.05) before this change and is deliberately untouched here.
--
-- VERIFICATION (2026-10-28, read-only via jj_ro after apply):
--   SELECT relname, array_to_string(reloptions, ', ')
--   FROM pg_class WHERE relname IN ('events','nft_activity');
--   events       | autovacuum_vacuum_insert_scale_factor=0.02, autovacuum_analyze_scale_factor=0.02
--   nft_activity | autovacuum_vacuum_insert_scale_factor=0.02, autovacuum_analyze_scale_factor=0.02

ALTER TABLE events SET (
    autovacuum_vacuum_insert_scale_factor = 0.02,
    autovacuum_analyze_scale_factor       = 0.02
);

ALTER TABLE nft_activity SET (
    autovacuum_vacuum_insert_scale_factor = 0.02,
    autovacuum_analyze_scale_factor       = 0.02
);

-- Re-verify after apply:
--   SELECT c.relname, array_to_string(c.reloptions, ', ') AS reloptions
--   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
--   WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.reloptions IS NOT NULL
--   ORDER BY c.relname;

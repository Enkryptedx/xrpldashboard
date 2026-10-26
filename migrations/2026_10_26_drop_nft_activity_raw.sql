-- nft_activity: drop the `raw` column (full tx JSON, kept since launch
-- for later re-derivation without re-fetching Clio).
--
-- Charlie GO (2026-10-26), after:
--   1. Full-column archive to B2 completed and independently verified
--      THAT SAME DAY, separate from this PR:
--        - 9,716,760 / 9,716,760 rows exported (exact match against the
--          snapshot taken before export started)
--        - every batch's sha256 recomputed from the DockVault copy and
--          diffed against manifest.jsonl's export-time sha256: 20/20 match
--        - rclone cryptcheck (full content verification through the
--          b2crypt encrypted remote, not just rclone check's size-only
--          comparison for crypt remotes) against B2: 0 differences,
--          23/23 files match
--        - script: xrpl_test_private_infra/scripts/archive_nft_activity_raw.py
--        - cursor/DONE: /Volumes/DockVault/nft_activity_archive/{cursor.json,DONE}
--   2. Re-confirmed nothing in the repo reads `raw` off nft_activity:
--      grepped every .py file for SELECT ... FROM nft_activity (none
--      select raw or `SELECT *`), and for any row["raw"] / row.raw access
--      downstream of an nft_activity query. The ONLY reference anywhere
--      was the INSERT in db.py:insert_nft_activity_batch, which wrote it.
--   3. This same PR stops that INSERT writing `raw` (db.py,
--      insert_nft_activity_batch) and removes it from the row shape built
--      by nft_activity_walker.py:_nft_rows_from_ledger, BEFORE this
--      migration runs — so by the time this DROP COLUMN applies, nothing
--      is writing to the column either.
--
-- Fence-post rule: apply this AFTER the walker code in this same PR has
-- deployed and is confirmed running (no new `raw` writes), same as every
-- other migration here. A column with live writes still landing when
-- DROP COLUMN runs would 500 the walker.
--
-- Table is otherwise large (per nft_activity_archive's snapshot_total:
-- 9,716,760+ rows at archive time, growing). DROP COLUMN in Postgres on
-- a column with no NOT NULL/CHECK referencing it and no dependent
-- index is a fast catalog-only operation (no table rewrite) — same
-- "no full-table rewrite" shape as the amm_ranked_pools_staging rename
-- swap shipped the same day, just via DROP COLUMN's own metadata-only
-- path rather than a rename.
--
-- Idempotent: IF EXISTS guards both lines so a re-run (or running this
-- against a DB where the column was already manually dropped) is a
-- no-op rather than an error.

BEGIN;

ALTER TABLE nft_activity DROP COLUMN IF EXISTS raw;

COMMIT;

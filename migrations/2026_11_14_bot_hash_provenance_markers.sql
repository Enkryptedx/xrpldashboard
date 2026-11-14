-- Provenance markers on page_view_bot_hashes, so the pattern arms of
-- refresh_bot_hash_tables() can run on a forward-only ts watermark while the
-- windowed arms keep their full recompute + diff (2026-11-14).
--
-- WHY
--
-- The two PATTERN arms (row_pred: 152 path LIKE + 55 user_agent ILIKE) had no
-- ts bound at all and rescanned the whole of page_views every 5 minutes --
-- measured as a Parallel Seq Scan over 1,024,161 rows / 626 MB, plan cost
-- 251,671. It tripped the statement_timeout once on 2026-11-10 and then three
-- times consecutively on 2026-11-14 (~25-26 s against a ~25 s cap), flapping
-- as the table grew past the limit.
--
-- row_pred is MONOTONIC -- a per-row predicate, and page_views rows are never
-- updated or deleted, so once a hash has one matching row it matches forever.
-- A forward-only ts window is therefore EXACT for those arms, not an
-- approximation.
--
-- The WINDOWED arms (growth 30 d, contact 400 d, claims 400 d) are NOT
-- monotonic: their HAVING clauses use exact equality, so a visitor who gains
-- a second pageview has to be un-flagged. They must keep recomputing and
-- diffing. Both kinds of row live in one table, so the DELETE needs to know
-- which arm owns a row: it may only remove rows the windowed arms own and the
-- pattern arms do not.
--
-- WHY THIS IS CHEAP
--
-- Both columns take a CONSTANT default, which Postgres 11+ stores as table
-- metadata -- no table rewrite, no backfill scan, no index build. Existing
-- rows (196,219 at time of writing, 32 MB) are marked by the default alone.
-- Starting both at FALSE is also the safe order of operations: until the
-- first writer run marks rows, the new DELETE owns nothing and so removes
-- nothing. The first run after deploy finds no stored pattern signature,
-- which forces one full rescan (under a raised statement_timeout applied with
-- SET LOCAL, so it covers that run only) and that run is what establishes
-- true provenance.
--
-- NO OWNER STEP REQUIRED, AND NO DDL PER TICK: refresh_bot_hash_tables()
-- reads information_schema.columns first and issues these statements ONLY on
-- a database that genuinely lacks a column -- production today runs one
-- catalog read and no DDL at all. ADD COLUMN takes ACCESS EXCLUSIVE even when
-- the column already exists (Postgres must lock the relation to decide there
-- is nothing to do), and the lock queue is FIFO, so a bare per-tick ALTER
-- parks every later reader behind itself inside the 22:00 ET pg_dump window.
-- That cost amendment_majority_walker 126 aborted ticks before its catalog
-- pre-check went in (owner ruling 2026-11-02); this walker fires 288x/day, so
-- it follows the same rule. When the DDL does have to run it is preceded by
-- SET LOCAL lock_timeout = '2s', which turns a collision into "deferred,
-- retries in 5 minutes" instead of a stall.
--
-- This file exists so the change is reviewable and runnable by hand in the
-- Neon editor if preferred. It is idempotent either way. Running it yourself
-- is in fact the quieter path: it means the walker's catalog read finds both
-- columns on its very first tick and never issues DDL at all.

ALTER TABLE page_view_bot_hashes
  ADD COLUMN IF NOT EXISTS pattern_owned BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE page_view_bot_hashes
  ADD COLUMN IF NOT EXISTS window_owned BOOLEAN NOT NULL DEFAULT FALSE;

-- No index is added on purpose. The DELETE and both UPSERTs are driven by the
-- existing PRIMARY KEY (hash_type, hash) anti-join; the markers only narrow
-- rows already located by that key, on a 32 MB table. A partial index on
-- window_owned would add write amplification for no read benefit -- the same
-- reasoning that retired idx_pvbh_lookup on 2026-10-10 (do not re-add).

-- Watermark bookkeeping lives in the existing key/value meta table and needs
-- no DDL. For reference, the writer maintains these two keys:
--   bot_hash_pattern_sig           -- sha256 prefix of the sorted pattern lists
--   bot_hash_pattern_watermark_ts  -- max(page_views.ts) the arms have covered
-- Deleting either row is the supported way to force a full rescan by hand:
--   DELETE FROM page_view_classification_meta
--    WHERE key IN ('bot_hash_pattern_sig', 'bot_hash_pattern_watermark_ts');

-- Escrow incremental tracking, stage 2: make the seed's rejection tally
-- DURABLE (2026-10-15).
--
-- WHY. Stage 1's walker correctly REJECTS entries that are not XRP Escrow
-- objects (TokenEscrow, and IOU amounts arriving as a dict) but never COUNTED
-- them, so "were any rejected?" had no answer on disk. A one-night stopgap
-- wrapper tallied them in-process instead. Then the thing the design did not
-- allow for happened: the 00:30 ET attempt was SIGKILLed by its 7200s job cap
-- after 59,148 pages, and the tally died with the process. `escrow_set` stores
-- ACCEPTED rows only, so no table could answer the question either --
-- recovering the number cost a full read-only re-walk of the same pinned
-- ledger (675 rejections, all iou_amount, confirmed by a self-check that the
-- re-walk's accepted count came back at exactly 11,094).
--
-- Worse, the number the resumed run DID report was silently partial: its 261
-- rejections covered only its own 19,110 pages. A tally that lives in process
-- memory is therefore wrong in two independent ways -- it vanishes on a kill,
-- and it under-reports after a resume.
--
-- So the tally travels WITH the checkpoint. escrow_seed_checkpoint writes it
-- alongside the marker after every page, and escrow_seed_resumable returns it
-- so a resumed walk SEEDS its counters instead of restarting them. The final
-- number then describes the whole walk regardless of how many times the
-- attempt was interrupted.
--
-- Columns are additive and nullable, so existing rows (seed_id=1, the
-- 2026-10-14/15 attempt) stay valid and simply read NULL -- that attempt's
-- real figures are recorded in memory/2026-10-15.md rather than backfilled,
-- because a backfill would be indistinguishable from a measurement.
--
-- Applied by the seed job itself: db.ensure_escrow_set_tables() carries these
-- same ALTERs and runs before every walk, so there is NO owner-run step. That
-- function sets lock_timeout = '3s' FIRST, because ADD COLUMN takes ACCESS
-- EXCLUSIVE and Postgres's lock queue is FIFO -- during the 22:00 ET pg_dump
-- an un-timed-out ALTER would park every subsequent reader behind itself.

ALTER TABLE escrow_seed_progress
    ADD COLUMN IF NOT EXISTS rejections JSONB;

ALTER TABLE escrow_seed_progress
    ADD COLUMN IF NOT EXISTS rejections_total INTEGER;

ALTER TABLE escrow_seed_progress
    ADD COLUMN IF NOT EXISTS entry_types JSONB;

-- 2026-10-07 — amendment_majority_history: record ENABLED separately from LOST.
--
-- Before: amendment_majority_walker stamped removed_* for ANY open row whose
-- hash left the ledger's Majorities array. An amendment that completed its
-- 14-day window and turned ON also leaves Majorities (it moves to the
-- Amendments array), so activation was indistinguishable from a lost
-- majority and /amendments would have labelled it "majority lost — superseded".
--
-- After: three nullable columns. The walker fills enabled_* when the hash is
-- found in Amendments; removed_* only when it is in neither array.
-- A row is "active" when BOTH removed_seen_ledger and enabled_seen_ledger are NULL.
--
-- Safe: additive, nullable, no backfill needed (no amendment has activated
-- since the table was created on 2026-09-25). Idempotent. Run with owner creds
-- BEFORE deploying the app/walker code that reads these columns.

ALTER TABLE amendment_majority_history ADD COLUMN IF NOT EXISTS enabled_seen_ledger BIGINT;
ALTER TABLE amendment_majority_history ADD COLUMN IF NOT EXISTS enabled_close_time  BIGINT;
ALTER TABLE amendment_majority_history ADD COLUMN IF NOT EXISTS enabled_iso         TEXT;

COMMENT ON COLUMN amendment_majority_history.enabled_seen_ledger IS
  'First validated ledger at which our walker saw this hash in the Amendments (enabled) array. NULL = not (yet) enabled. Mutually exclusive with removed_seen_ledger.';

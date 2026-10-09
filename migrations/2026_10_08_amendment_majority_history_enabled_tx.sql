-- 2026-10-08: record the EnableAmendment pseudo-transaction that actually
-- enabled an amendment. When enabled_tx_hash is set, enabled_seen_ledger /
-- enabled_close_time / enabled_iso are the true enable point (ledger after
-- the flag ledger), not the first ledger the 15-minute walker observed.
-- Idempotent; also applied by db.ensure_schema via ALTER ... IF NOT EXISTS.
ALTER TABLE amendment_majority_history ADD COLUMN IF NOT EXISTS enabled_tx_hash TEXT;

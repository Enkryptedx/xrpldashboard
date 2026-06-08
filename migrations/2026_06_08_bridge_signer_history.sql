-- bridge_signer_history — append-only ledger of Axelar XRPL Gateway
-- SignerList state. Source for /sidechain verifier-set rotation history.
--
-- Two row kinds, distinguished by tx_hash:
--   1) Bootstrap (tx_hash = 'BOOTSTRAP'): one row per gateway, written
--      once by bridge_signer_walker.py on its first run. Captures the
--      current SignerList state observed via account_objects when
--      there is no underlying SignerListSet transaction to anchor to.
--   2) Steady-state (tx_hash = actual SignerListSet tx hash): one row
--      per observed rotation. Walker scans account_tx in the validated
--      ledger range and INSERTs on PK conflict-skip.
--
-- Query patterns:
--   - "current state": ORDER BY ledger_index DESC LIMIT 1
--   - "rotations only": WHERE tx_hash != 'BOOTSTRAP' ORDER BY ledger_index
--   - "rotation count": SELECT COUNT(*) WHERE tx_hash != 'BOOTSTRAP'
--
-- Idempotent; SCHEMA_DDL in db.py re-declares this so fresh installs stay
-- in sync.
CREATE TABLE IF NOT EXISTS bridge_signer_history (
    ledger_index   BIGINT NOT NULL,
    close_time     TIMESTAMPTZ NOT NULL,
    quorum         INTEGER,
    signer_count   INTEGER,
    signer_entries JSONB,
    tx_hash        TEXT NOT NULL,
    written_at     BIGINT NOT NULL,
    PRIMARY KEY (ledger_index, tx_hash)
);
CREATE INDEX IF NOT EXISTS bridge_signer_history_ledger_desc_idx
    ON bridge_signer_history (ledger_index DESC);

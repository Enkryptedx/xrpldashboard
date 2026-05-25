-- RLUSD supply history (daily append).
--
-- Companion to rlusd_state_cache: the cache is a 30s live singleton
-- (overwrites on every refresh, 2 gunicorn workers × every 30s ≈ 5,760
-- writes/day). RLUSD itself only meaningfully changes ~20×/day via
-- mints/burns, so flipping the cache to history-append would codify
-- noise as data (99% duplicate consecutive rows).
--
-- Instead: keep the singleton cache for SSR fallback, and append one
-- daily row here from signed_snapshot.py's collect_metrics() pass,
-- using the same in-memory payload that's already extracted for
-- rlusd_xrpl_supply / rlusd_eth_supply / rlusd_total_supply metrics.
--
-- See feedback_history_flip_cadence_rule.md for the underlying rule.

CREATE TABLE IF NOT EXISTS rlusd_supply_history (
    snapshot_date    DATE     NOT NULL,
    xrpl_supply      NUMERIC  NOT NULL,
    eth_supply       NUMERIC  NOT NULL,
    total_supply     NUMERIC  NOT NULL,
    xrpl_holders     INTEGER,
    eth_holders      INTEGER,
    xrpl_mints_24h   NUMERIC,
    xrpl_burns_24h   NUMERIC,
    eth_mints_24h    NUMERIC,
    eth_burns_24h    NUMERIC,
    written_at_iso   TEXT     NOT NULL,
    PRIMARY KEY (snapshot_date)
);

CREATE INDEX IF NOT EXISTS rlusd_supply_history_date_idx
    ON rlusd_supply_history (snapshot_date DESC);

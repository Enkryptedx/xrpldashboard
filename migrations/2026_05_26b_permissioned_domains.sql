-- 2026-05-26: /permissioned-domains Phase 1 (walker + schema, no UI yet).
-- XLS-80 PermissionedDomains enabled on mainnet 2026-02-04. On-chain
-- adoption essentially zero today; this Phase 1 stands up the data layer
-- so a future Phase 2 ships a UI against accumulating daily history.
--
-- Three tables:
--   permissioned_domains            — append-only per-(date, domain) snapshot
--   permissioned_domain_events      — Phase 1: created empty. Phase 2/3 fills
--                                      from PermissionedDomainSet/Delete tx scan
--   permissioned_domain_walker_runs — one row per walker pass (audit trail so
--                                      empty `permissioned_domains` is not
--                                      indistinguishable from "walker never ran")
--
-- Append-only history shape from day 1 (per the cadence-rule memory — do NOT
-- repeat the credentials_snapshot singleton-overwrite mistake; the trajectory
-- IS the data we want).

CREATE TABLE IF NOT EXISTS permissioned_domains (
    snapshot_date         DATE NOT NULL,
    domain_id             TEXT NOT NULL,
    owner_account         TEXT NOT NULL,
    sequence              INTEGER NOT NULL,
    accepted_credentials  JSONB NOT NULL,
    cred_count            SMALLINT NOT NULL,
    previous_txn_id       TEXT,
    ledger_close_time     BIGINT NOT NULL,
    fetched_at_iso        TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, domain_id)
);
CREATE INDEX IF NOT EXISTS permissioned_domains_history_idx
    ON permissioned_domains (domain_id, snapshot_date DESC);
CREATE INDEX IF NOT EXISTS permissioned_domains_owner_idx
    ON permissioned_domains (owner_account);

CREATE TABLE IF NOT EXISTS permissioned_domain_events (
    event_id          BIGSERIAL PRIMARY KEY,
    tx_hash           TEXT NOT NULL UNIQUE,
    tx_type           TEXT NOT NULL,
    domain_id         TEXT NOT NULL,
    owner_account     TEXT NOT NULL,
    ledger_index      BIGINT NOT NULL,
    ledger_close_time BIGINT NOT NULL,
    payload           JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS permissioned_domain_events_domain_idx
    ON permissioned_domain_events (domain_id, ledger_close_time DESC);
CREATE INDEX IF NOT EXISTS permissioned_domain_events_time_idx
    ON permissioned_domain_events (ledger_close_time DESC);

CREATE TABLE IF NOT EXISTS permissioned_domain_walker_runs (
    snapshot_date      DATE PRIMARY KEY,
    fetched_at_iso     TEXT NOT NULL,
    seed_set_size      INTEGER NOT NULL,
    accounts_queried   INTEGER NOT NULL,
    domains_found      INTEGER NOT NULL,
    exhausted          BOOLEAN NOT NULL,
    walker_duration_ms INTEGER NOT NULL,
    notes              TEXT
);

-- 2026-10-30 — xrp_usd_history: append-only XRP/USD anchor samples.
--
-- NOT RUN BY ANY WALKER OR BY THE APP. Pasted by the owner in the Neon SQL
-- Editor. The writer (db.write_xrp_usd_history_sample, called from
-- scripts/tokens_summary_walker.py every 300s) ships BEFORE this file is
-- applied and reports `skipped_no_table` on every tick until it exists, so
-- deploying the code and running this DDL are independent steps in either
-- order.
--
-- WHY THIS TABLE EXISTS
--
-- price_oracle.xrp_usd() is never persisted anywhere: `grep xrp_usd db.py`
-- returned nothing before this change. The rate lived in a ~60s in-memory
-- cache and nowhere else. token_prices has history (PK currency, issuer,
-- snapshot_ts) but stores token->XRP, not USD; oracles_snapshot is
-- replace-on-write (DELETE then INSERT) so it holds no history at all.
--
-- Consequence: there was no way to answer "what was XRP worth when this
-- payment closed?", so /wallet could only ever convert a historical payment
-- at the RENDER-time anchor. The same 185,017,485.5 XRP transfer read
-- $276.9M on one page and $232.1M on another (reported 2026-10-30), i.e.
-- $1.4966/XRP vs $1.2545/XRP. PR #110 made every /wallet USD figure state
-- the rate it used and when that rate was sampled; this table is what will
-- eventually allow pricing at the transaction's own close time instead.
--
-- THIS CANNOT BACKFILL. The anchor was never recorded, so every payment
-- before the first row below keeps the render-time label permanently.
-- Tx-time pricing applies only going forward, and the two must never be
-- mixed in one figure without saying which.
--
-- ALL THREE ANCHORS ARE STORED, not just the chosen rate, so a later reader
-- can see what the sanity checks said at that moment -- including the case
-- where RLUSD diverged from BOTH and we refused to serve.
--
-- FAIL-CLOSED ROWS ARE KEPT ON PURPOSE. chosen_rate IS NULL is meaningful
-- and must never be backfilled or interpolated:
--   * chosen_rate NULL + check_failed TRUE  -> RLUSD resolved but diverged
--     from every sanity check by >PRICE_CHECK_MAX_ABS_PCT (default 3.0).
--   * chosen_rate NULL + check_failed FALSE -> RLUSD did not resolve at all
--     (fail-closed on the primary).
-- Dropping these would leave a gap a future reader could mistake for "the
-- walker was down", and would invite interpolation across a window where we
-- deliberately had no price. Same editorial rule as token_prices: the
-- absence of a usable number IS the signal.
--
-- APPEND-ONLY. The writer uses INSERT ... ON CONFLICT DO NOTHING on the
-- (snapshot_ts) primary key, and nothing in db.py ever UPDATEs or DELETEs a
-- row here. snapshot_ts is the PRICE's own sample time
-- (price_oracle.xrp_usd_fetched_at()), so re-observing the same cached rate
-- is idempotent; it falls back to the observation time for the
-- RLUSD-unavailable path, where no successful fetch time exists but the
-- failure is still worth recording.

CREATE TABLE IF NOT EXISTS xrp_usd_history (
    snapshot_ts        BIGINT           NOT NULL,
    chosen_rate        DOUBLE PRECISION,
    rlusd_rate         DOUBLE PRECISION,
    usd_gh_rate        DOUBLE PRECISION,
    usd_bitstamp_rate  DOUBLE PRECISION,
    check_failed       BOOLEAN          NOT NULL DEFAULT FALSE,
    derivation_method  TEXT             NOT NULL,
    PRIMARY KEY (snapshot_ts)
);

-- Powers the "nearest sample at or before this ledger close" lookup that
-- tx-time pricing will use.
CREATE INDEX IF NOT EXISTS xrp_usd_history_ts_idx
    ON xrp_usd_history (snapshot_ts DESC);

-- Partial index for "when did pricing fail?" audits without scanning the
-- good rows.
CREATE INDEX IF NOT EXISTS xrp_usd_history_failed_idx
    ON xrp_usd_history (snapshot_ts DESC)
    WHERE check_failed OR chosen_rate IS NULL;

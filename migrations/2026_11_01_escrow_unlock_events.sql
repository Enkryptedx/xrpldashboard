-- Monthly escrow release history: one row per observed escrow lifecycle
-- transaction for the Ripple monthly-release cohort (2026-11-01).
--
-- Backs the "monthly release history" section on /cold-storage and its
-- machine twin /cold-storage/unlocks.json. The existing locked/liquid hero
-- is untouched and keeps reading escrows_snapshot via escrow_snapshot.py.
--
-- WHY THIS TABLE EXISTS AND WHAT IT IS NOT
--
-- escrow_walker.py marked this door on 2026-10-14 ("if a 'realized releases
-- per month' chart is ever wanted...") and proposed an escrows_history table
-- keyed by (owner, sequence, observed_ledger_index). Owner ruling
-- 2026-11-01 chose tx_hash as the primary key instead, because:
--   * a transaction hash is already globally unique and immutable, so the
--     UPSERT is idempotent with no composite-key reasoning;
--   * Escrow ledger objects carry no top-level Sequence field (see the
--     escrows_snapshot comment in db.py), so reconstructing `sequence` costs
--     an extra account_tx round trip per row for no gain here;
--   * this table records TRANSACTIONS, not successive states of an object,
--     so "one row per tx" is the natural grain.
--
-- SCOPE: escrow_owner must be in the Ripple monthly-release cohort —
-- category=ripple in named_accounts.json MINUS the RLUSD issuer, i.e. the
-- same 20-account filter escrow_supply.py and escrow_walker.py use. This
-- scope is load-bearing, not cosmetic: EscrowFinish may be submitted by any
-- account, and the third-party submitter seen on the Sept 2026 cohort
-- releases (rPw6JAdM9BGwH4y4LxJQBDybaGNhFLCspU) finished 364 unrelated
-- escrows that same month. Scoping by SUBMITTER would put all 364 on the
-- page; scoping by OWNER yields the 3 cohort releases.
--
-- NOT BOUND BY THE STAGE-2 INCREMENTAL POLICY (owner ruling 2026-11-01).
-- docs/ESCROW_INCREMENTAL_DESIGN_2026-10-14.md requires that an incremental
-- escrow SUPPLY total is never served unverified (resync-required / amber /
-- reconcile-walk). That policy governs a derived running total. This table
-- is a separate observed-fact surface: every row is one transaction read
-- from ledger metadata, and nothing here is ever used to derive or serve a
-- running supply total. A missing row makes a month incomplete; it can
-- never silently corrupt a headline supply figure.
--
-- SOURCE PRECEDENCE: 'own-node' outranks any 'witness:%'. The daily walker
-- (own node, 3-day lookback) and the one-time 12-month backfill (full-history
-- witnesses) overlap on the recent window and will see the same tx_hash. The
-- UPSERT in db.record_escrow_unlock_events() upgrades a witness row to
-- own-node and NEVER downgrades, so re-running the backfill cannot overwrite
-- a sovereign read with a third-party one.
--
-- Applied by the owning job: scripts/escrow_unlock_walker.py calls
-- db.ensure_escrow_unlock_events_table() at tick start, so no owner-run step
-- is required after this file is applied. Do NOT rely on SCHEMA_DDL /
-- init_schema() -- init_schema() is a manual one-off that nothing runs at
-- app boot, which is how evernorth_daily_snapshot ended up absent from
-- production on 2026-10-10 while every insert was swallowed by a
-- best-effort except.

CREATE TABLE IF NOT EXISTS escrow_unlock_events (
    tx_hash            TEXT PRIMARY KEY,
    tx_type            TEXT        NOT NULL,
    ledger_index       BIGINT      NOT NULL,
    close_time         TIMESTAMPTZ NOT NULL,

    -- Derived, never written by hand, so it cannot drift from close_time.
    -- timezone('UTC', timestamptz) and date_trunc(text, timestamp) are both
    -- IMMUTABLE, which is what makes this legal in a STORED generated column.
    -- The month label is deliberately UTC-based: a monthly unlock lands at
    -- 00:00:10 UTC, which is the previous evening in ET.
    unlock_month       TIMESTAMP
        GENERATED ALWAYS AS (date_trunc('month', close_time AT TIME ZONE 'UTC'))
        STORED,

    -- denom/amount_drops/amount_json mirror escrows_snapshot exactly and are
    -- produced by the SAME mapper (escrow_walker._classify_amount), so the
    -- two surfaces cannot disagree about how an Amount was read.
    -- amount_json holds the full XRPL Amount value: a string of drops for
    -- XRP, {currency, issuer, value} for IOU, {mpt_issuance_id, value} for
    -- MPT. amount_drops is XRP-only and NULL for IOU/MPT.
    denom              TEXT        NOT NULL,
    amount_drops       BIGINT,
    amount_json        JSONB       NOT NULL,

    escrow_owner       TEXT        NOT NULL,
    escrow_destination TEXT,
    submitter          TEXT,
    tx_result          TEXT        NOT NULL,
    source             TEXT        NOT NULL,
    observed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT escrow_unlock_events_tx_type_chk
        CHECK (tx_type IN ('EscrowFinish', 'EscrowCreate')),
    CONSTRAINT escrow_unlock_events_denom_chk
        CHECK (denom IN ('XRP', 'IOU', 'MPT')),
    -- Pattern rather than a two-value list: tools/anchor_nodes.py documents a
    -- SECOND full-history witness (xrplcluster.com) that the backfill falls
    -- back to, so pinning the constraint to 'witness:s2-clio' alone would
    -- reject a legitimate fallback read mid-backfill.
    CONSTRAINT escrow_unlock_events_source_chk
        CHECK (source = 'own-node' OR source LIKE 'witness:%'),
    -- XRP escrows must carry drops; IOU/MPT must not.
    CONSTRAINT escrow_unlock_events_amount_drops_chk
        CHECK ((denom = 'XRP' AND amount_drops IS NOT NULL)
               OR (denom <> 'XRP' AND amount_drops IS NULL))
);

-- The page renders newest month first and groups by month.
CREATE INDEX IF NOT EXISTS escrow_unlock_events_unlock_month_idx
    ON escrow_unlock_events (unlock_month DESC);

-- Per-account drill-down and the cohort scope filter.
CREATE INDEX IF NOT EXISTS escrow_unlock_events_owner_idx
    ON escrow_unlock_events (escrow_owner);

-- The walker's lookback window queries by close_time.
CREATE INDEX IF NOT EXISTS escrow_unlock_events_close_time_idx
    ON escrow_unlock_events (close_time DESC);

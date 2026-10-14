-- Escrow incremental tracking, stage 1: authoritative escrow set + a
-- resumable seed checkpoint (2026-10-14).
--
-- See docs/ESCROW_INCREMENTAL_DESIGN_2026-10-14.md. Short version:
-- supply_escrow_walker recomputed XRP escrow supply by enumerating the whole
-- ledger state tree via ledger_data(type=escrow) every hour. Cost scales with
-- total state size, not escrow count - measured 123 escrow objects in 500
-- pages against a ~78,215-page walk, so ~99.75% of the traversal is
-- discarded. Throughput fell 90.9 -> 23.7 pages/s while state grew only
-- +0.4%, so the walk stopped finishing inside its cap and the walker was
-- paused.
--
-- escrow_set is the seed's output: one row per Escrow ledger object as of a
-- SINGLE pinned ledger_index. The stream then applies deltas from N+1 onward
-- (stage 2, shadow mode) instead of re-enumerating.
--
-- escrow_seed_progress is what makes a ~30-60 minute walk survivable without
-- a hard cap. The cap is exactly what was truncating walks mid-flight, so the
-- seed has none; instead it checkpoints its marker after each page and
-- resumes against the SAME pinned ledger. One row per seed attempt.
--
-- WHY pinned_ledger is NOT NULL and is carried on both tables: a walk that
-- lets the ledger advance underneath it yields a blend of several ledger
-- states - an escrow finished mid-walk is missed or double-counted depending
-- on where the marker was, and the resulting total reconciles against
-- nothing. Pinning makes the seed a true snapshot of one ledger, which is the
-- defined starting point the delta stream needs. If the pinned ledger falls
-- out of the node's retained history (measured 2026-10-14:
-- complete_ledgers spanned 90,397 ledgers, ~100.4 h) the attempt is marked
-- 'abandoned' and a NEW one starts - a set stitched across two pinned
-- ledgers is the blended-state bug above, so it is never patched.
--
-- Applied by the seed job itself: scripts/escrow_seed_walk.py calls
-- db.ensure_escrow_set_tables() before it starts, so no owner-run step is
-- required. Do NOT rely on SCHEMA_DDL alone - init_schema() is a manual
-- one-off that nothing runs at app boot, which is how evernorth_daily_snapshot
-- ended up absent from production on 2026-10-10 while every insert was
-- swallowed by a best-effort except.

CREATE TABLE IF NOT EXISTS escrow_set (
    escrow_index    TEXT PRIMARY KEY,
    owner           TEXT    NOT NULL,
    destination     TEXT,
    amount_drops    NUMERIC NOT NULL,
    sequence_num    BIGINT,
    condition       TEXT,
    finish_after    BIGINT,
    cancel_after    BIGINT,
    destination_tag BIGINT,
    source_tag      BIGINT,
    pinned_ledger   BIGINT  NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The reconcile/aggregate path reads by pinned_ledger, and stage 3 compares a
-- shadow total against a full walk, so both want this.
CREATE INDEX IF NOT EXISTS escrow_set_pinned_ledger_idx
    ON escrow_set (pinned_ledger);

-- Destination lookups support the EscrowFinish credit path in stage 2.
CREATE INDEX IF NOT EXISTS escrow_set_destination_idx
    ON escrow_set (destination);

CREATE TABLE IF NOT EXISTS escrow_seed_progress (
    seed_id        BIGSERIAL PRIMARY KEY,
    pinned_ledger  BIGINT      NOT NULL,
    marker         JSONB,
    pages_done     INTEGER     NOT NULL DEFAULT 0,
    objects_seen   INTEGER     NOT NULL DEFAULT 0,
    status         TEXT        NOT NULL DEFAULT 'running',
    message        TEXT,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at   TIMESTAMPTZ,
    CONSTRAINT escrow_seed_progress_status_chk
        CHECK (status IN ('running', 'completed', 'abandoned'))
);

-- Resume picks the newest attempt still in 'running'; a partial unique index
-- would be wrong here because an abandoned attempt must be allowed to
-- coexist with its replacement on the same pinned_ledger.
CREATE INDEX IF NOT EXISTS escrow_seed_progress_status_idx
    ON escrow_seed_progress (status, seed_id DESC);

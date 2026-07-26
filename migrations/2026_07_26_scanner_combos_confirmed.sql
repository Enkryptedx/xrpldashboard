-- Persistent classifier memory for the scanner arm of the is_bot filter.
--
-- Why: page_view_scanner_combos is refreshed from a trailing 7-day window
-- (`ts > now - 7d`). When a scanner burst decays, its (path, ua) combo
-- falls out of the refresh, but the is_bot=TRUE stamps the writer already
-- applied to those historical rows remain. The canary's live subquery
-- can't re-derive them (same 7-day window) and reports drift — the
-- CheckHost residual of 2026-07-26 (delta=-45 on rows 2026-07-19 →
-- 2026-07-23) was the founding incident.
--
-- Fix: once a (path, user_agent) combo crosses the confirmation threshold
-- (the existing ratio+floor rule), it persists in this ledger. The writer
-- ratchets on first detection (`confirmed_by='auto'`), and the initial
-- backfill inserts historically-detected combos with human review
-- (`confirmed_by='reviewed'`). Both writer and canary read from this
-- ledger. Detection remains transient (scanner_combos still exists for
-- the pre-column-flip `_bot_filter_sql` reader); conviction is permanent.
--
-- Design: docs/IS_BOT_SCANNER_MEMORY_FIX_2026-07-26.md
-- Parent design: docs/IS_BOT_COLUMN_DESIGN.md
--
-- Idempotent: CREATE IF NOT EXISTS everywhere. Safe to re-run.

CREATE TABLE IF NOT EXISTS page_view_scanner_combos_confirmed (
    path                    TEXT        NOT NULL,
    user_agent              TEXT        NOT NULL,
    confirmed_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_by            TEXT        NOT NULL,   -- 'auto' | 'reviewed' | 'manual'
    evidence_ratio          NUMERIC,                -- hits / distinct_visitors at first confirmation
    evidence_row_count      INTEGER,                -- hits at first confirmation
    evidence_window_start   BIGINT,                 -- unix ts of the 7d window that triggered
    evidence_window_end     BIGINT,                 -- unix ts_end of that window
    last_seen_at            TIMESTAMPTZ,            -- most recent refresh cycle that saw this combo hot
    notes                   TEXT,                   -- reviewer notes for 'reviewed' entries
    PRIMARY KEY (path, user_agent),
    CHECK (confirmed_by IN ('auto', 'reviewed', 'manual'))
);

CREATE INDEX IF NOT EXISTS page_view_scanner_combos_confirmed_by_idx
    ON page_view_scanner_combos_confirmed (confirmed_by, confirmed_at DESC);

CREATE INDEX IF NOT EXISTS page_view_scanner_combos_confirmed_last_seen_idx
    ON page_view_scanner_combos_confirmed (last_seen_at DESC NULLS LAST);

-- Sunday-audit hook (documented, not enforced):
--   SELECT path, user_agent, confirmed_at, evidence_ratio, evidence_row_count
--     FROM page_view_scanner_combos_confirmed
--    WHERE confirmed_by = 'auto'
--      AND confirmed_at > now() - interval '7 days'
--    ORDER BY confirmed_at DESC;

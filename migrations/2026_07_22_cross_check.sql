-- Layer 3 (External Legitimacy) storage: cross_check_results.
--
-- Truth Audit Phase 3 per docs/TRUTH_AUDIT_DESIGN.md (706bc0a §Layer 3).
-- Compares the answers we compute against independent public sources.
-- Disagreement is an INVESTIGATION TRIGGER, never an auto-correction.
--
-- Append-only. One row per pair per walker cycle. "Latest state per
-- pair" = ORDER BY run_at DESC LIMIT 1 per pair_key. "Live alarms" =
-- WHERE status = 'disagree' AND run_at > NOW() - INTERVAL '1 day' (the
-- Sunday queue audit reads this surface).
--
-- Loud-failure model: network errors write status = 'external_unreachable'
-- with the exception message in note. Missing local state writes
-- 'local_unavailable'. Only walker crashes or DB unreachability flip
-- walker_health.ok = FALSE; disagreements and unreachable externals are
-- business signal, not walker health.
--
-- Idempotent: CREATE IF NOT EXISTS. Safe to re-run on any node.

CREATE TABLE IF NOT EXISTS cross_check_results (
    id                BIGSERIAL   PRIMARY KEY,
    run_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pair_key          TEXT        NOT NULL,
    check_type        TEXT        NOT NULL,  -- penny_exact | count_exact | band | set_equal
    local_value       TEXT,
    external_value    TEXT,
    external_source   TEXT        NOT NULL,
    tolerance         NUMERIC,                -- absolute for penny/count, fractional for band
    delta             NUMERIC,                -- |local - external| for numeric checks, set-diff size for set_equal
    status            TEXT        NOT NULL,   -- agree | disagree | external_unreachable | local_unavailable
    note              TEXT
);
CREATE INDEX IF NOT EXISTS cross_check_results_pair_run_idx
    ON cross_check_results (pair_key, run_at DESC);
CREATE INDEX IF NOT EXISTS cross_check_results_status_run_idx
    ON cross_check_results (status, run_at DESC);

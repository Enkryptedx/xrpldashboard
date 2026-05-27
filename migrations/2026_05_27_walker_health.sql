-- Walker health table — one row per scheduled background walker. Every
-- walker writes a "start" row at the top of its run and updates the same
-- row with the outcome at the end (try/finally — fires even on uncaught
-- exception). Lets /mpts (and future per-walker pages) render an honest
-- staleness banner instead of silently serving last-good when the worker
-- has been failing for weeks. Triggered by 2026-05-27 mpt_snapshot
-- investigation: 11 of last 13 walks failed silently, dashboard kept
-- showing "all 210 MPTs" while walker hadn't completed in 2 weeks.
--
-- Idempotent; SCHEMA_DDL re-declares this so fresh installs stay in sync.
CREATE TABLE IF NOT EXISTS walker_health (
    walker_name           TEXT PRIMARY KEY,
    last_run_started      TIMESTAMPTZ NOT NULL,
    last_run_completed    TIMESTAMPTZ,
    last_run_ok           BOOLEAN NOT NULL,
    last_run_message      TEXT,
    last_success_at       TIMESTAMPTZ,
    last_failure_at       TIMESTAMPTZ,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0
);

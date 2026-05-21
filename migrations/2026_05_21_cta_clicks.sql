-- 2026-05-21 — cta_clicks table for /institutional launch-partner CTA tracking.
-- Server-side click logger writes one row per click on the "Become a launch
-- partner" button (and any future instrumented CTA). Mirrors the table block
-- in db.py SCHEMA_DDL one-to-one. Idempotent; safe to re-run.

CREATE TABLE IF NOT EXISTS cta_clicks (
    id            BIGSERIAL PRIMARY KEY,
    ts            BIGINT NOT NULL,
    cta_id        TEXT NOT NULL,
    ref_param     TEXT,
    referrer      TEXT,
    visitor_hash  TEXT,
    user_agent    TEXT,
    country       TEXT
);
CREATE INDEX IF NOT EXISTS cta_clicks_ts_idx ON cta_clicks (ts DESC);
CREATE INDEX IF NOT EXISTS cta_clicks_cta_ts_idx ON cta_clicks (cta_id, ts DESC);

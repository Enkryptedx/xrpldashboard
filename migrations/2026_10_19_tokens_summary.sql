-- /tokens precomputed summary row. Source of truth for the shape; APPLIED by
-- scripts/tokens_summary_walker.py via db.ensure_tokens_summary_table() at
-- the top of every tick (SET lock_timeout='3s' first). Nothing on any app or
-- walker path calls db.init_schema(); this file is for review, not deploy.
CREATE TABLE IF NOT EXISTS tokens_summary (
    id                  SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    aggregates_24h      JSONB,
    aggregates_7d       JSONB,
    aggregates_all      JSONB,
    hero                JSONB,
    bucket_stats        JSONB,
    warnings_recent     JSONB,
    tier_lookup         JSONB,
    price_map           JSONB,
    label_lookup_json   TEXT,
    latest_bucket       BIGINT,
    col_hashes          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    changed_at          TIMESTAMPTZ,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

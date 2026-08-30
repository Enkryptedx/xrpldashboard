-- 2026-08-30 — /check.json v0.9 metering: track anonymous IP-bucketed
-- requests alongside keyed requests in api_request_counters.
--
-- Motivation: Wave-1 spec (Charlie 2026-08-30 18:45 EDT) — /check.json v0.9
-- ships free anon tier (60/hr per IP) AND keyed tier. Metering table writes
-- happen synchronously on every response so future paid-tier math is
-- honest from day one; no "we'll add metering later" pattern.
--
-- Current table (db.py:1067-1074): PK (key_id, hour_bucket).
-- Prod state: empty (no keys issued yet — verified 2026-08-30). Safe to
-- recreate. If any rows exist at apply time, this migration is destructive.
-- Charlie reviews in staging before apply per fence-post rule.
--
-- New shape:
--   key_id      = api_keys.id when keyed, NULL when anonymous
--   ip_hash     = SHA-256(remote_addr) when anonymous, NULL when keyed
--   hour_bucket = unix_ts // 3600
--   request_count = count of requests in that (identity, hour) bucket
--
-- Exactly one of (key_id, ip_hash) is populated per row. Enforced by CHECK.
-- Uniqueness across the effective identity + hour uses COALESCE index
-- since PostgreSQL PKs cannot include expressions.
--
-- Retention: unchanged. Sweep DELETE WHERE hour_bucket < now/3600 - 168.
--
-- Apply-in-staging: BEGIN; \i migrations/2026_08_30_api_request_counters_ip_hash.sql
--                   SELECT COUNT(*) FROM api_request_counters;  -- expect 0
--                   ROLLBACK;  -- then apply-for-real after Charlie signs off

BEGIN;

-- Drop old table (safe because empty in prod as of 2026-08-30).
DROP TABLE IF EXISTS api_request_counters;

CREATE TABLE api_request_counters (
    id            BIGSERIAL PRIMARY KEY,
    key_id        BIGINT,                        -- NULL for anonymous
    ip_hash       TEXT,                          -- NULL for keyed
    hour_bucket   BIGINT NOT NULL,
    request_count INT    NOT NULL DEFAULT 0,
    CONSTRAINT api_request_counters_identity_xor
        CHECK ((key_id IS NULL) <> (ip_hash IS NULL))
);

-- Unique per (identity, hour). COALESCE lets one index cover both classes;
-- the CHECK above guarantees exactly one side is non-null.
CREATE UNIQUE INDEX api_request_counters_identity_hour_uniq
    ON api_request_counters (
        COALESCE(key_id, 0),
        COALESCE(ip_hash, ''),
        hour_bucket
    );

-- Retention sweep helper.
CREATE INDEX api_request_counters_bucket_idx
    ON api_request_counters (hour_bucket);

COMMIT;

-- After apply, db.py:1067 CREATE TABLE block also updated in the same
-- commit so a fresh `CREATE TABLE IF NOT EXISTS` cold-boot builds the new
-- shape. Existing (staging/prod) instances get the migration.

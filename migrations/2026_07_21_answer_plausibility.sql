-- Layer 2 (Answer Plausibility) storage: alarms + watermarks.
--
-- answer_plausibility_alarms is append-only. Each row is one rule firing
-- for one metric at one point in time. The named-failure format matches
-- docs/TRUTH_AUDIT_DESIGN.md so /health can render it verbatim once the
-- surface exists.
--
-- answer_plausibility_watermarks stores one row per metric — the last
-- value the walker observed. R4 (monotonic-violated) compares each fresh
-- read against the stored watermark; a decrease not explained by an
-- accepted cause (e.g. burst-cohort reclassification) trips the rule.
--
-- Idempotent: CREATE IF NOT EXISTS + tolerant column adds. Safe to
-- re-run on any node.

CREATE TABLE IF NOT EXISTS answer_plausibility_alarms (
    id                   BIGSERIAL PRIMARY KEY,
    fired_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric               TEXT        NOT NULL,
    rule                 TEXT        NOT NULL,
    observed             TEXT        NOT NULL,
    expected_behavior    TEXT        NOT NULL,
    consecutive_cycles   INTEGER,
    last_change_at       TIMESTAMPTZ,
    note                 TEXT
);
CREATE INDEX IF NOT EXISTS answer_plausibility_alarms_fired_idx
    ON answer_plausibility_alarms (fired_at DESC);
CREATE INDEX IF NOT EXISTS answer_plausibility_alarms_metric_idx
    ON answer_plausibility_alarms (metric, fired_at DESC);

CREATE TABLE IF NOT EXISTS answer_plausibility_watermarks (
    metric        TEXT        PRIMARY KEY,
    last_value    NUMERIC,
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    extra         JSONB
);

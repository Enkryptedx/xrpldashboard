-- Archived DDL snapshot: feedback table (orphan) as it existed in Neon
-- immediately before the 2026-07-08 drop.
--
-- Context: `feedback` was a fully-shaped 14-column table that predated the
-- current `contact_inquiries` table. Zero code references in the current
-- tree at drop time (grep across .py / .html / .sql). Sole surviving row
-- was a QA test from 2026-06-11.
--
-- Row content is NOT in this file. User-submitted content — even test data
-- from the maintainer — goes to _private/archives/ under the 2026-07-08
-- storage rule (schema/DDL to repo, row data to private). The paired file
-- for this snapshot: _private/archives/feedback_orphan_2026_07_08.txt.
--
-- If this table ever needs to be resurrected, the CREATE statements below
-- are executable as-is; the row can be restored from the private archive.

CREATE TABLE IF NOT EXISTS feedback (
    id                  BIGSERIAL PRIMARY KEY,
    ts                  BIGINT NOT NULL,
    visitor_hash        TEXT,
    submitter_hash      TEXT,
    country             TEXT,
    original_text       TEXT NOT NULL,
    original_lang       TEXT,
    english_text        TEXT,
    translation_engine  TEXT,
    optional_email      TEXT,
    category            TEXT,
    page_referrer       TEXT,
    status              TEXT NOT NULL DEFAULT 'new',
    status_updated_at   BIGINT
);
CREATE INDEX IF NOT EXISTS feedback_ts_idx
    ON feedback (ts DESC);
CREATE INDEX IF NOT EXISTS feedback_status_ts_idx
    ON feedback (status, ts DESC);

-- Sequence state at archive time: last_value=4, is_called=true.
-- (rows 1, 2, 3 were previously deleted; row 4 was the sole survivor)

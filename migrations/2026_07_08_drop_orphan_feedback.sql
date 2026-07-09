-- Drop orphan feedback table.
--
-- Context: `feedback` existed in Neon but was NOT in db.py SCHEMA_DDL —
-- reverse of the tx_type_hourly drift caught 2026-07-07. Zero code
-- references at drop time (grep across .py / .html / .sql). Sole surviving
-- row was a QA test from 2026-06-11 (id=4, "hi send-anyway-bypass").
--
-- Superseded by contact_inquiries (db.py:725), which serves the general
-- on-site contact form with `data-correction` and `methodology-discrepancy`
-- among its purposes. Different table from institutional_inquiries (db.py:698),
-- which serves the qualified-sales-lead form only.
--
-- Archives before drop:
--   migrations/archived/2026_07_08_feedback_orphan_snapshot.sql — DDL
--   _private/archives/feedback_orphan_2026_07_08.txt — sole row (git-invisible)

DROP TABLE IF EXISTS feedback;
DROP SEQUENCE IF EXISTS feedback_id_seq;

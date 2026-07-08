-- 2026-07-08 — lock the hand-cleaned "Reaper Financial" name against
-- the weekly TOML rerun's last-write-wins clobber.
--
-- Address r3qWgpz2ry3BhcRJ8JE6rxM8esrfhuKp4R issues BOTH RPR and ASC
-- from reaper.financial's [[TOKENS]] block. verify_toml_accounts.py
-- iterates that block and calls upsert_account_label per token; the
-- second write overwrites the first. With no ORGANIZATION.name in the
-- TOML, the derived shape "reaper.financial (Ascension issuer)" wins.
--
-- db.upsert_account_label now honors extra->>'name_locked'; setting it
-- here preserves "Reaper Financial" while still refreshing category,
-- confidence, and the domain/verified_via metadata on every rerun.
--
-- Idempotent: jsonb_set with create_if_missing=true. Safe to re-run.

UPDATE account_labels
SET extra = jsonb_set(
    COALESCE(extra, '{}'::jsonb),
    '{name_locked}',
    'true'::jsonb,
    true
  ),
  updated_at = EXTRACT(EPOCH FROM NOW())::bigint
WHERE address = 'r3qWgpz2ry3BhcRJ8JE6rxM8esrfhuKp4R'
  AND (extra->>'name_locked' IS DISTINCT FROM 'true');

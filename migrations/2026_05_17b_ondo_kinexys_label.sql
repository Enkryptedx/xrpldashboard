-- 2026-05-17 — micro-refinement: name Kinexys as JPMorgan's
-- tokenization platform so the reader doesn't have to have
-- JPMorgan's product taxonomy memorized.
-- Idempotent UPDATE; safe to re-run.

UPDATE rwa_family
SET description = 'OUSG (Short-Term US Government Treasury Fund) on XRPL. Canonical issuer attested via two-way TOML chain at ondo.finance. In May 2026, Ondo Finance — with JPMorgan''s Kinexys tokenization platform, Mastercard, and Ripple — completed the first cross-border, cross-bank redemption of a tokenized US Treasury fund, settled on XRPL in under five seconds. No standing AMM liquidity yet — institutional presence with proven cross-border settlement flow.'
WHERE family_slug = 'ondo';

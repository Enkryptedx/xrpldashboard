-- 2026-06-10 — align /rwa Ondo family description with the wording used
-- on /rlusd for the May 2026 Ondo/Kinexys/Mastercard/Ripple pilot.
-- /rlusd describes it as the "first near real-time cross-border, cross-bank
-- redemption of a tokenized U.S. Treasury fund"; bringing /rwa in line.
-- Idempotent UPDATE; safe to re-run.

UPDATE rwa_family
SET description = 'OUSG (Short-Term US Government Treasury Fund) on XRPL. Canonical issuer attested via two-way TOML chain at ondo.finance. In May 2026, Ondo Finance — with JPMorgan''s Kinexys, Mastercard, and Ripple — completed the first near real-time cross-border, cross-bank redemption of a tokenized US Treasury fund on XRPL. No standing AMM liquidity yet — institutional presence with proven cross-border settlement flow.'
WHERE family_slug = 'ondo';

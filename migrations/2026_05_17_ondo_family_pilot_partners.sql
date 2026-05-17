-- 2026-05-17 — update Ondo family description with the May 6, 2026
-- cross-border, cross-bank tokenized Treasury redemption pilot
-- (Ondo Finance / Kinexys by J.P. Morgan / Mastercard / Ripple).
-- Source: ondo.finance/blog/ondo-jpmorgan-mastercard-ripple-tokenization
-- Settlement leg: Ripple redeemed OUSG → Ondo → Mastercard MTN →
--   Kinexys debited Ondo deposit account → USD to Ripple's bank
--   in Singapore. XRPL leg under 5 seconds.
-- Idempotent UPDATE; safe to re-run.

UPDATE rwa_family
SET description = 'OUSG (Short-Term US Government Treasury Fund) on XRPL. Canonical issuer attested via two-way TOML chain at ondo.finance. In May 2026, Ondo Finance — with JPMorgan''s Kinexys, Mastercard, and Ripple — completed the first cross-border, cross-bank redemption of a tokenized US Treasury fund, settled on XRPL in under five seconds. No standing AMM liquidity yet — institutional presence with proven cross-border settlement flow.'
WHERE family_slug = 'ondo';

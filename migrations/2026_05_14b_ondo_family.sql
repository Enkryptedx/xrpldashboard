-- 2026-05-14b — Ondo Finance as 3rd verified family (no AMM liquidity)
-- Two-way TOML chain at ondo.finance attests canonical OUSG issuer
-- rHuiXXjHLpMP8ZE9sSQU5aADQVWDwv6h5p. No rwa_pool_attribution rows —
-- canonical issuer has zero AMM presence (institutional only).
-- Idempotent; safe to re-run.

INSERT INTO rwa_family (family_slug, family_name, description, external_url, attestation_level)
VALUES (
  'ondo',
  'Ondo Finance',
  'OUSG (Short-Term US Government Treasury Fund) on XRPL. Canonical issuer attested via two-way TOML chain at ondo.finance. No AMM liquidity yet — institutional presence only.',
  'https://ondo.finance',
  'verified'
)
ON CONFLICT (family_slug) DO UPDATE SET
  family_name = EXCLUDED.family_name,
  description = EXCLUDED.description,
  external_url = EXCLUDED.external_url,
  attestation_level = EXCLUDED.attestation_level;

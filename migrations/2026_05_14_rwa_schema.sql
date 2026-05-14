-- 2026-05-14 — RWA attribution schema for /rwa page
-- rwa_family + rwa_pool_attribution: institutional-RWA attestation pairing
-- AMM pools with verified issuer families. Idempotent; safe to re-run.

CREATE TABLE IF NOT EXISTS rwa_family (
  family_slug TEXT PRIMARY KEY,
  family_name TEXT NOT NULL,
  description TEXT,
  external_url TEXT,
  attestation_level TEXT NOT NULL
    CHECK (attestation_level IN ('verified','inferred','preliminary')),
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rwa_pool_attribution (
  pool_address TEXT NOT NULL,
  family_slug TEXT NOT NULL REFERENCES rwa_family(family_slug),
  confidence TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
  provenance TEXT NOT NULL,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (pool_address, family_slug)
);

CREATE INDEX IF NOT EXISTS idx_rwa_pool_attribution_family
  ON rwa_pool_attribution (family_slug);

INSERT INTO rwa_family (family_slug, family_name, description, external_url, attestation_level)
VALUES
  ('openeden',
   'OpenEden',
   'Tokenized US Treasury Bills via TBILL Vault. Issued natively on XRPL with verified xrpscan-labeled issuer wallet.',
   'https://openeden.com',
   'verified'),
  ('midas',
   'Midas (bridged via Axelar)',
   'Tokenized assets (mTBILL) bridged to XRPL from native Midas issuance on Ethereum/Base. Bridge issuer wallet labeled "Axelar Bridge" by xrpscan.',
   'https://midas.app',
   'verified')
ON CONFLICT (family_slug) DO UPDATE SET
  family_name = EXCLUDED.family_name,
  description = EXCLUDED.description,
  external_url = EXCLUDED.external_url,
  attestation_level = EXCLUDED.attestation_level;

-- OpenEden pool attributions (4 pools, all high-confidence)
INSERT INTO rwa_pool_attribution (pool_address, family_slug, confidence, provenance, notes)
VALUES
  ('rnrKmMgQBW3ogK5cTZps1XQeMMA6oyE1Rq', 'openeden', 'high',
   'amm-name-direct-openeden-token',
   'BITx/OpenEden — counterparty issuer verified against OpenEden anchor wallet'),
  ('rJ82irQfSL1MLdgr55gNdu7yMXy5rdQdUD', 'openeden', 'high',
   'amm-name-direct-openeden-token',
   'XRP/OpenEden — counterparty issuer verified against OpenEden anchor wallet'),
  ('rhrRNEk9MCPRYcEbJc3vKUT3LKZCFu2Euh', 'openeden', 'high',
   'amm-name-tbill-verified-issuer',
   'BITx/TBILL — TBILL issuer wallet matches OpenEden anchor'),
  ('rp36eHzxwJgidFQCMuiT2hhZsyopkPhihY', 'openeden', 'high',
   'amm-name-tbill-verified-issuer',
   'XRP/TBILL — TBILL issuer wallet matches OpenEden anchor')
ON CONFLICT (pool_address, family_slug) DO UPDATE SET
  confidence = EXCLUDED.confidence,
  provenance = EXCLUDED.provenance,
  notes = EXCLUDED.notes;

-- Midas (bridged via Axelar) pool attributions — 3 pools
-- All three share mTBILL issuer rfmS3zqrQrka8wVyhXifEeyTwe8AMz2Yhw
-- (matches earlier "rfmS3zqr…" hint; verified against amm_ranked.json)
INSERT INTO rwa_pool_attribution (pool_address, family_slug, confidence, provenance, notes)
VALUES
  ('rHyhbuVrmSvtAis2MJJXRAsXU94W8izAyn', 'midas', 'high',
   'amm-name-mtbill-axelar-bridge',
   'BITx/mTBILL — issuer = Axelar Bridge (xrpscan-labeled)'),
  ('rBVrmZ4PCLh3DMSz7aKYnZY3eXCzXdSZCh', 'midas', 'high',
   'amm-name-mtbill-axelar-bridge',
   'XRP/mTBILL — issuer = Axelar Bridge (xrpscan-labeled)'),
  ('rwswrv168GfwukhR6S8mrp3m6vxF566rwu', 'midas', 'high',
   'amm-name-mtbill-axelar-bridge',
   'XUSD/mTBILL — issuer = Axelar Bridge (xrpscan-labeled)')
ON CONFLICT (pool_address, family_slug) DO UPDATE SET
  confidence = EXCLUDED.confidence,
  provenance = EXCLUDED.provenance,
  notes = EXCLUDED.notes;

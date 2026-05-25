-- Migration: token_prices history-append flip
-- Date: 2026-05-25
-- Rationale: Flip token_prices from overwrite (DELETE + INSERT) to
-- history-append (INSERT with ON CONFLICT DO NOTHING). Forward-only
-- accumulation; every daily walker run adds a new row per token.
-- See: investigation report 2026-05-25, db.write_token_prices,
-- db.read_token_prices_map, db.read_token_price.

ALTER TABLE token_prices DROP CONSTRAINT token_prices_pkey;
ALTER TABLE token_prices ADD PRIMARY KEY (currency, issuer, snapshot_ts);
CREATE INDEX token_prices_latest_idx ON token_prices
  (currency, issuer, snapshot_ts DESC);

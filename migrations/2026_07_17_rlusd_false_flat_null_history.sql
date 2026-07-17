-- Null out false-zero rows in rlusd_supply_history.
--
-- Root cause: two independent silent-fabricate bugs in rlusd_live's 24h
-- mint/burn aggregate paths wrote plausible-looking $0s instead of
-- signalling failure. The daily snapshot pass then persisted those fake
-- zeros into rlusd_supply_history via write_rlusd_supply_history().
--
--   XRPL side (2026-05-25 → 2026-07-17, 52 rows):
--     _fetch_xrpl_24h_aggregates() only inspected Payment tx to/from the
--     issuer. That sweep produced 0 mints and 0 burns on every single
--     day since the history table was created, including days with
--     known nonzero supply changes. Detection path is broken — needs a
--     Step 3 rewrite anchored to actual supply-delta days with sample
--     tx pulled from Clio as regression fixtures.
--
--   Ethereum side (2026-06-20 → 2026-07-17, 28 rows):
--     Public RPC providers tightened eth_getLogs to ≤50 blocks; our
--     1000-block chunks were rejected. The chunk helper caught the
--     RuntimeError and returned 0.0, so partial holes were summed as
--     "no activity". Last row with real nonzero activity was
--     2026-06-19 ($20,499,499.93 burns), matching Etherscan.
--
-- We NULL the affected windows because a zero and an unknown are
-- different truths — leaving fake zeros keeps lying to every future
-- chart and API consumer. The rlusd_state_cache singleton is
-- rewritten by the next walker run (rlusd_refresher, 5-min cadence)
-- with null mint/burn values, so no explicit cache migration needed.
--
-- Backfill of the affected windows with real data is tracked as
-- Fable Steps 2c (ETH: derivable from Etherscan) and 3c (XRPL: gated
-- on the detection rewrite). This migration establishes the honest
-- "unavailable" state; backfill fills back in against it as data
-- becomes derivable.

BEGIN;

-- XRPL: entire history window (2026-05-25 onward). Every row is a
-- false zero produced by the broken Payment-only sweep.
UPDATE rlusd_supply_history
   SET xrpl_mints_24h = NULL,
       xrpl_burns_24h = NULL
 WHERE snapshot_date >= DATE '2026-05-25';

-- Ethereum: 2026-06-20 onward. 2026-06-19 was the last row with real
-- activity ($20.5M burns matching Etherscan); everything after that
-- is the eth_getLogs range-limit silent-fabricate.
UPDATE rlusd_supply_history
   SET eth_mints_24h = NULL,
       eth_burns_24h = NULL
 WHERE snapshot_date >= DATE '2026-06-20';

COMMIT;

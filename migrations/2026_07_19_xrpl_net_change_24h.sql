-- Add XRPL net-supply-change column to rlusd_supply_history.
--
-- Companion migration to 2026_07_17_rlusd_false_flat_null_history.sql,
-- which NULL'd the xrpl_mints_24h / xrpl_burns_24h columns after the
-- 53-day silent-fabricate outage. The Option A rewrite (gateway_balances
-- snapshot-diff at UTC-day-boundary ledgers) doesn't produce mints and
-- burns separately — it produces a single net-change scalar per day.
-- Different truths, different columns; the old columns stay NULL where
-- they are.
--
-- Why not overwrite xrpl_mints_24h / xrpl_burns_24h with the net:
--   1. Semantic mismatch — a $10M net on a day with $50M minted and $40M
--      burned is not the same as a day with $10M minted and $0 burned.
--      Stuffing net into "mints" would fabricate a component-level fact
--      the ledger doesn't prove.
--   2. Sign — net is signed (negative = net redemption). Neither mints
--      nor burns can be negative without breaking every downstream
--      chart consumer.
--   3. Reversibility — if a future detection path (Clio tx enumeration,
--      trace-level ledger walker, etc.) resolves mints/burns
-- separately, the columns are ready. Overwriting closes that door.
--
-- Column name mirrors the ETH pattern (eth_mints_24h / eth_burns_24h)
-- structurally, with the "net_change" prefix signalling the different
-- semantic. Rendered on /rlusd as "Net supply change · 24h" with a
-- tooltip ("mints minus redemptions — we show the net because it's what
-- the ledger proves directly").
--
-- Semantics of the value:
--   * For a fully-closed calendar day: net supply change between the
--     last ledger with close_time <= day 00:00Z and the last ledger with
--     close_time <= (day+1) 00:00Z.
--   * For today's (partial) row: net change between day 00:00Z boundary
--     ledger and the latest validated ledger at snapshot time. Matches
--     the ETH "calendar_today" semantic that already ships.
--   * write_rlusd_supply_history() finalizes yesterday's row each cycle
--     with the fully-closed number, same as eth_mints_24h /
--     eth_burns_24h finalization landed 2026-07-18.

BEGIN;

ALTER TABLE rlusd_supply_history
    ADD COLUMN IF NOT EXISTS xrpl_net_change_24h NUMERIC;

COMMIT;

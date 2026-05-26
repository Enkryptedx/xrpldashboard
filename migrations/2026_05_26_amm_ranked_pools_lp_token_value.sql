-- 2026-05-26: persist real lp_token.value alongside tvl_usd in amm_ranked_pools.
-- Replaces the LPTokenBalance-as-tvl_usd sort proxy (db.read_amm_index_entries),
-- which silently zeroed any pool without a USD price feed (BITx, micro-pairs, etc.).
-- Nullable: backfilled on the next rank_amms.py cycle (every 4h).
ALTER TABLE amm_ranked_pools
    ADD COLUMN IF NOT EXISTS lp_token_value NUMERIC;

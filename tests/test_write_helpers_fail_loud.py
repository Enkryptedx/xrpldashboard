"""Forced-failure tests for the 8 fail-loud write helpers migrated 2026-09-05.

Each write helper in this list has been migrated from silent-swallow
(`except Exception as e: _log_err(...); return X`) to fail-loud
(`except Exception as e: _log_err_and_raise(...)`). Plus the
"conn is None" silent-return path (from _get_writer_conn returning None
on an unlogged connect failure) was fixed to raise WriterConnUnavailable.

This suite forces synthetic Postgres failures at every entry point and
asserts each helper propagates the exception instead of swallowing it.

Why this matters: pre-migration, a Neon write failure inside any of
these helpers returned None/False/0 silently. The caller (usually a
walker) assumed success and wrote walker_health.ok=True with a green
message. Downstream consumers then served stale data under a green
health signal — the exact shape of the 2026-09-05 is_bot canary
incident (see docs/IS_BOT_COLUMN_DESIGN.md § Wound 2026-09-05).

Post-migration, a helper failure raises out to the walker's outer
try/except, which sets walker_health.ok=False and BetterStack pages.

Run: `python3 tests/test_write_helpers_fail_loud.py`
    (bypasses conftest.py which imports app with flask_smorest)
"""
from unittest.mock import patch
import os
import sys

# Make repo root importable so `import db` works when running this file
# directly (bypasses conftest.py which imports flask_smorest).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeOperationalError(Exception):
    """Stand-in for psycopg.OperationalError so the test doesn't need
    psycopg installed to run."""
    pass


def _run():
    import db

    helpers = [
        ("write_signed_snapshot", lambda: db.write_signed_snapshot(
            envelope={"snapshot_date_utc": "2026-09-06", "leaf_hash": "x",
                      "leaf_index": 1, "chain_root": "y",
                      "signing_pubkey_fingerprint": "z"},
            current_root="c", leaves_total=1, schema_version=4)),
        ("write_snapshot_meta", lambda: db.write_snapshot_meta({
            "days_collected": 42, "accounts_tracked": 100,
            "pools_tracked": 50, "mpts_tracked": 10, "first_date": "2026-01-01"})),
        ("replace_amm_ranked_pools", lambda: db.replace_amm_ranked_pools([{
            "amm_account": "rT", "rank": 1, "amount_a": 1, "amount_b": 1,
            "asset_a": {"currency": "XRP"}, "asset_b": {"currency": "USD", "issuer": "rI"},
            "tvl_usd": 1, "tvl_status": "priced", "kind": "amm", "lp_token_value": 1.0}])),
        ("replace_escrows_snapshot", lambda: db.replace_escrows_snapshot([{
            "address": "rT", "owner": "rO", "sequence": 1, "destination": "rD",
            "amount_drops": 1000, "ledger_index": 1}], snap_ledger=1)),
        ("replace_cold_storage_snapshot", lambda: db.replace_cold_storage_snapshot([{
            "address": "rT", "balance_xrp": 1.0, "sequence": 1,
            "owner_count": 0, "ledger_index": 1, "fetch_ok": True}])),
        ("replace_oracles_snapshot", lambda: db.replace_oracles_snapshot([{
            "oracle_id": "rT:1", "provider": "test", "asset_class": "stable",
            "base_asset": "USD", "quote_asset": "USD", "decimals": 6, "raw": {}}],
            snap_ledger=1)),
        ("write_token_prices", lambda: db.write_token_prices([{
            "currency": "USD", "issuer": "rI", "snapshot_ts": 1, "xrp_price": 1.0,
            "pool_amm_account": "rT", "pool_xrp_reserve": 100,
            "pool_token_reserve": 100, "derivation_method": "amm"}])),
        ("ensure_is_bot_schema", lambda: db.ensure_is_bot_schema()),
    ]

    pass_count = 0
    fail_count = 0
    for name, fn in helpers:
        # Patch every DB entry point the 8 helpers use:
        # - pg_available()=True so the "not configured" early-return doesn't fire
        # - pg_connect raises synthetically (for helpers using `with pg_connect()`)
        # - _get_writer_conn returns None (for helpers using it → hits WriterConnUnavailable)
        # - rpc_loop_safe_pg_connect raises (for ensure_is_bot_schema)
        with patch.object(db, "pg_available", return_value=True), \
             patch.object(db, "pg_connect", side_effect=_FakeOperationalError("synthetic")), \
             patch.object(db, "_get_writer_conn", return_value=None), \
             patch.object(db, "rpc_loop_safe_pg_connect", side_effect=_FakeOperationalError("synthetic")):
            try:
                fn()
                print(f"  FAIL {name}: returned silently (silent-swallow regression)")
                fail_count += 1
            except (_FakeOperationalError, db.WriterConnUnavailable) as e:
                print(f"  PASS {name}: raised {type(e).__name__}")
                pass_count += 1
            except Exception as e:
                # Any other propagated exception is still fail-loud
                print(f"  PASS {name}: raised {type(e).__name__} (also fail-loud)")
                pass_count += 1

    # Contract test for _log_err_and_raise itself
    try:
        try:
            raise ValueError("synthetic-inner")
        except ValueError as e:
            db._log_err_and_raise("test_category", e)
        print("  FAIL _log_err_and_raise: no re-raise")
        fail_count += 1
    except ValueError as e:
        if str(e) == "synthetic-inner":
            print("  PASS _log_err_and_raise: re-raised original ValueError")
            pass_count += 1
        else:
            print(f"  FAIL _log_err_and_raise: wrong exception {e}")
            fail_count += 1

    print()
    print(f"== {pass_count} PASS / {fail_count} FAIL out of {pass_count + fail_count} ==")
    return fail_count


if __name__ == "__main__":
    sys.exit(_run())

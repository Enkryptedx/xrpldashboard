"""Signed-snapshot pre-sign gate tests — Ship A (Charlie ruling
2026-09-23 evening, shipped 2026-09-24).

Four assertions per the daily's "Tests to ship with A" list:

  CLOSED 1: metric-key-set missing one key → gate blocks
  CLOSED 2: any scalar metric is 0 (not in allowlist) → gate blocks
  CLOSED 3: env vars SIGNED_SNAPSHOT_AMENDMENTS_BLOCK_ENABLED +
            RWA_SUPPLY_NAV_IN_LEAF are NOT read anywhere in the signed
            snapshot source (their removal is the proof the env-drop
            fragility class is gone; a static source-grep is the
            cheapest possible regression).
  OPEN   : all 14 keys present, all scalars non-zero (or in allowlist)
            → gate passes.

Plus small guardrails:
  - null-valued scalar blocks
  - non-numeric scalar blocks
  - allowlisted zero (rwa_total_aum_usd) passes when nothing else fails
  - PRE_SIGN_NON_SCALAR_SCHEMA_5 entries skip the zero-check
"""
from __future__ import annotations

import os
import sys
import pathlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _minimal_valid_metrics_snap() -> dict:
    """Build a snap dict with every EXPECTED_METRIC_KEYS_SCHEMA_5 metric
    present, all non-zero, all with the right shape. The default
    "everything passes" case."""
    from signed_snapshot import (
        EXPECTED_METRIC_KEYS_SCHEMA_5,
        PRE_SIGN_NON_SCALAR_SCHEMA_5,
    )
    metrics = []
    for name in EXPECTED_METRIC_KEYS_SCHEMA_5:
        if name in PRE_SIGN_NON_SCALAR_SCHEMA_5:
            metrics.append({
                "name": name, "unit": "dict",
                "value": {"placeholder": True},
                "source": "test",
            })
        else:
            metrics.append({
                "name": name, "unit": "n/a",
                "value": 1.0,
                "source": "test",
            })
    return {
        "signing_domain": "test",
        "schema_version": 5,
        "snapshot_date_utc": "2026-09-24",
        "snapshot_taken_unix": 0,
        "metrics": metrics,
        "errors": [],
    }


def test_open_all_present_all_nonzero_gate_passes():
    from signed_snapshot import _pre_sign_gate
    snap = _minimal_valid_metrics_snap()
    reasons = _pre_sign_gate(snap)
    assert reasons == [], f"gate should pass; got: {reasons}"


def test_closed_1_missing_one_key_blocks():
    from signed_snapshot import _pre_sign_gate
    snap = _minimal_valid_metrics_snap()
    # Drop rwa_onledger_supply_usd — the exact metric that started this.
    snap["metrics"] = [m for m in snap["metrics"]
                       if m["name"] != "rwa_onledger_supply_usd"]
    reasons = _pre_sign_gate(snap)
    assert any(r.startswith("missing_metric_keys:") for r in reasons), reasons
    assert "rwa_onledger_supply_usd" in " ".join(reasons)


def test_closed_1_unexpected_key_blocks():
    from signed_snapshot import _pre_sign_gate
    snap = _minimal_valid_metrics_snap()
    # Add a stray metric — schema drift going the other direction.
    snap["metrics"].append({
        "name": "surprise_metric", "unit": "?", "value": 42, "source": "test",
    })
    reasons = _pre_sign_gate(snap)
    assert any(r.startswith("unexpected_metric_keys:") for r in reasons), reasons
    assert "surprise_metric" in " ".join(reasons)


def test_closed_2_zero_scalar_blocks():
    from signed_snapshot import _pre_sign_gate
    snap = _minimal_valid_metrics_snap()
    for m in snap["metrics"]:
        if m["name"] == "rwa_onledger_supply_usd":
            m["value"] = 0.0
    reasons = _pre_sign_gate(snap)
    assert "zero_scalar:rwa_onledger_supply_usd" in reasons, reasons


def test_closed_2_null_scalar_blocks():
    from signed_snapshot import _pre_sign_gate
    snap = _minimal_valid_metrics_snap()
    for m in snap["metrics"]:
        if m["name"] == "rlusd_xrpl_supply":
            m["value"] = None
    reasons = _pre_sign_gate(snap)
    assert "null_scalar:rlusd_xrpl_supply" in reasons, reasons


def test_closed_2_non_numeric_scalar_blocks():
    from signed_snapshot import _pre_sign_gate
    snap = _minimal_valid_metrics_snap()
    for m in snap["metrics"]:
        if m["name"] == "amm_pools_count":
            m["value"] = "not-a-number"
    reasons = _pre_sign_gate(snap)
    assert any(r.startswith("non_numeric_scalar:amm_pools_count:") for r in reasons), reasons


def test_allowlisted_zero_passes():
    from signed_snapshot import _pre_sign_gate
    snap = _minimal_valid_metrics_snap()
    # rwa_total_aum_usd is on the allowlist per Charlie's ruling —
    # legitimately zero when no pools are RWA-attributed.
    for m in snap["metrics"]:
        if m["name"] == "rwa_total_aum_usd":
            m["value"] = 0.0
    reasons = _pre_sign_gate(snap)
    assert reasons == [], f"allowlisted zero should pass; got: {reasons}"


def test_non_scalar_dict_metric_skips_zero_check():
    from signed_snapshot import _pre_sign_gate
    snap = _minimal_valid_metrics_snap()
    # A v4 dict-valued metric with an empty or falsy dict must NOT
    # trigger the zero-check.
    for m in snap["metrics"]:
        if m["name"] == "walker_health_summary":
            m["value"] = {}  # falsy dict — must not trigger zero_scalar
    reasons = _pre_sign_gate(snap)
    assert reasons == [], f"non-scalar metrics are exempt; got: {reasons}"


def test_closed_3_env_flags_not_read_anywhere():
    """CLOSED 3 — env vars SIGNED_SNAPSHOT_AMENDMENTS_BLOCK_ENABLED and
    RWA_SUPPLY_NAV_IN_LEAF must be entirely removed from the source.
    A static source-grep is the cheapest regression against the env-drop
    fragility class returning."""
    src = pathlib.Path(__file__).parent.parent / "signed_snapshot.py"
    text = src.read_text()
    # Comments/docstrings may cite the flag name for HISTORICAL reference
    # (the code-comment for the change explains what was removed). What
    # matters is that no live `os.environ.get(...)` reads them.
    for flag in ("SIGNED_SNAPSHOT_AMENDMENTS_BLOCK_ENABLED",
                 "RWA_SUPPLY_NAV_IN_LEAF"):
        assert f'os.environ.get("{flag}")' not in text, (
            f"live os.environ.get read of removed flag {flag} — "
            f"env-drop fragility class has returned"
        )
        assert f'os.environ.get("{flag}",' not in text, (
            f"live os.environ.get read (with default) of removed flag {flag}"
        )


def test_ship_c_source_excludes_zero_value_families():
    """Ship C fix (Charlie ruling 2026-09-24 10:07 ET): the top-level
    `source` string only names hostnames of families whose value_usd > 0.
    Zero-contribution families (Midas, OpenEden at $0) stay in
    metadata.families with their `reason`, but must not appear in the
    top-line source — that string names WHERE the number came from.
    Also asserts the suffix is the honest description of the derivation
    ("curator NAV × own-node gateway_balances"), not the internal
    table name."""
    from signed_snapshot import _derive_rwa_supply_nav_source
    per_family = [
        {"family_slug": "midas", "value_usd": 0.0,
         "nav_source_url": "https://midas.app"},
        {"family_slug": "ondo_finance", "value_usd": 191295581.77,
         "nav_source_url": "https://app.ondo.finance/assets/ousg"},
        {"family_slug": "openeden", "value_usd": 0.0,
         "nav_source_url": "https://openeden.com/tbill"},
    ]
    src = _derive_rwa_supply_nav_source(per_family)
    assert src == "app.ondo.finance (curator NAV × own-node gateway_balances)", src
    assert "midas.app" not in src
    assert "openeden.com" not in src
    assert "rwa_supply_nav_daily" not in src


def test_ship_c_source_falls_back_when_no_contributors():
    """If every family contributed 0 (walker degenerate case), the
    source string still names the derivation honestly."""
    from signed_snapshot import _derive_rwa_supply_nav_source
    per_family = [
        {"family_slug": "midas", "value_usd": 0.0,
         "nav_source_url": "https://midas.app"},
    ]
    src = _derive_rwa_supply_nav_source(per_family)
    assert src == "rwa_supply_nav_daily (curator NAV × own-node gateway_balances)"


def test_pre_sign_gate_blocked_exit_code_is_3():
    """PreSignGateBlocked subclasses SystemExit(3) so main()'s finally
    runs walker_health_end(ok=False) and the process exits with a
    distinct non-zero code that L1 can grep for in launchd logs."""
    from signed_snapshot import PreSignGateBlocked
    exc = PreSignGateBlocked(["zero_scalar:test"])
    assert isinstance(exc, SystemExit)
    assert exc.code == 3
    assert exc.reasons == ["zero_scalar:test"]


if __name__ == "__main__":
    test_open_all_present_all_nonzero_gate_passes()
    test_closed_1_missing_one_key_blocks()
    test_closed_1_unexpected_key_blocks()
    test_closed_2_zero_scalar_blocks()
    test_closed_2_null_scalar_blocks()
    test_closed_2_non_numeric_scalar_blocks()
    test_allowlisted_zero_passes()
    test_non_scalar_dict_metric_skips_zero_check()
    test_closed_3_env_flags_not_read_anywhere()
    test_ship_c_source_excludes_zero_value_families()
    test_ship_c_source_falls_back_when_no_contributors()
    test_pre_sign_gate_blocked_exit_code_is_3()
    print("ALL PASS")

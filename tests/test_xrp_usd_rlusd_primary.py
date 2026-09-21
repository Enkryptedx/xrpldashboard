"""RLUSD primary + divergence guard for xrp_usd()
(Charlie ruling 2026-09-21 afternoon).

The oracle:
- returns RLUSD's XRP/USD when it resolves and agrees with at least one
  sanity check within PRICE_CHECK_MAX_ABS_PCT (default 3%),
- returns None if RLUSD didn't resolve at all (fail-closed on primary),
- returns None + sets xrp_usd_check_failed() True if RLUSD diverged
  from EVERY sanity check by >threshold.

USD.GH + USD.Bitstamp are sanity checks only. We never fall back to
their median if RLUSD is unavailable — Charlie's ruling is
explicit: RLUSD is the source of truth for XRP/USD.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _prep_cache():
    """Bust price_oracle's in-memory cache so each test starts clean.
    The module holds a single lowercase `_cache` dict keyed by tuple."""
    import price_oracle
    price_oracle._cache.clear()


def _rlusd_key():
    return ("524C555344000000000000000000000000000000",
            "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De")


def _mock_amm(mapping: dict):
    """Return a mock _xrp_per_unit_via_amm that yields XRP-per-1-stable
    for each (currency, issuer) key in mapping and None for others."""
    def _fake(currency, issuer):
        return mapping.get((currency, issuer))
    return _fake


def test_rlusd_alone_is_the_source_when_it_agrees():
    """RLUSD $1.47 with USD.GH $1.4734 and USD.Bitstamp $1.4725 — all
    within 3%. Oracle returns RLUSD."""
    _prep_cache()
    import price_oracle
    mapping = {
        _rlusd_key(): 1.0 / 1.47,
        ("USD", "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq"): 1.0 / 1.4734,
        ("USD", "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B"): 1.0 / 1.4725,
    }
    with patch.object(price_oracle, "_xrp_per_unit_via_amm",
                      side_effect=_mock_amm(mapping)):
        rate = price_oracle.xrp_usd()
    assert rate is not None
    assert abs(rate - 1.47) < 0.001, f"expected RLUSD-derived 1.47, got {rate}"
    assert not price_oracle.xrp_usd_check_failed()
    labels = [l for l, _ in price_oracle.xrp_usd_sources()]
    assert "RLUSD" in labels


def test_rlusd_diverges_from_both_sanity_checks_returns_none_and_flags():
    """RLUSD $1.99 while GH+Bitstamp are $1.47. Both sanity checks
    disagree by >30%. Oracle returns None + xrp_usd_check_failed()."""
    _prep_cache()
    import price_oracle
    mapping = {
        _rlusd_key(): 1.0 / 1.99,
        ("USD", "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq"): 1.0 / 1.47,
        ("USD", "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B"): 1.0 / 1.47,
    }
    with patch.object(price_oracle, "_xrp_per_unit_via_amm",
                      side_effect=_mock_amm(mapping)):
        rate = price_oracle.xrp_usd()
    assert rate is None, (
        "Oracle should refuse to serve when RLUSD diverges from both"
    )
    assert price_oracle.xrp_usd_check_failed(), (
        "check_failed flag must be True so the chip renders 'price "
        "check failed' instead of falling back silently"
    )
    # Three sample values still exposed for the tooltip
    labels = [l for l, _ in price_oracle.xrp_usd_sources()]
    assert set(labels) == {"RLUSD", "USD.GH", "USD.Bitstamp"}


def test_rlusd_diverges_from_only_one_sanity_check_still_serves():
    """RLUSD $1.47, GH $1.6 (>8% off), Bitstamp $1.4725 (agrees). Since
    one sanity check agrees, we keep the RLUSD value. Only when EVERY
    sanity check disagrees do we fail-closed."""
    _prep_cache()
    import price_oracle
    mapping = {
        _rlusd_key(): 1.0 / 1.47,
        ("USD", "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq"): 1.0 / 1.6,
        ("USD", "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B"): 1.0 / 1.4725,
    }
    with patch.object(price_oracle, "_xrp_per_unit_via_amm",
                      side_effect=_mock_amm(mapping)):
        rate = price_oracle.xrp_usd()
    assert rate is not None
    assert abs(rate - 1.47) < 0.001
    assert not price_oracle.xrp_usd_check_failed()


def test_rlusd_unavailable_returns_none_even_with_sanity_checks():
    """RLUSD fails to resolve; USD.GH and USD.Bitstamp resolve. Charlie
    ruling: no fallback to sanity-check median. Return None."""
    _prep_cache()
    import price_oracle
    mapping = {
        # RLUSD absent
        ("USD", "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq"): 1.0 / 1.47,
        ("USD", "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B"): 1.0 / 1.4725,
    }
    with patch.object(price_oracle, "_xrp_per_unit_via_amm",
                      side_effect=_mock_amm(mapping)):
        rate = price_oracle.xrp_usd()
    assert rate is None, "no fallback: RLUSD is the source of truth"
    assert not price_oracle.xrp_usd_check_failed()


def test_rlusd_only_no_sanity_serves():
    """RLUSD resolves, both sanity checks fail. Nothing to disagree
    with — serve RLUSD."""
    _prep_cache()
    import price_oracle
    mapping = {_rlusd_key(): 1.0 / 1.47}
    with patch.object(price_oracle, "_xrp_per_unit_via_amm",
                      side_effect=_mock_amm(mapping)):
        rate = price_oracle.xrp_usd()
    assert rate is not None
    assert abs(rate - 1.47) < 0.001


if __name__ == "__main__":
    test_rlusd_alone_is_the_source_when_it_agrees()
    test_rlusd_diverges_from_both_sanity_checks_returns_none_and_flags()
    test_rlusd_diverges_from_only_one_sanity_check_still_serves()
    test_rlusd_unavailable_returns_none_even_with_sanity_checks()
    test_rlusd_only_no_sanity_serves()
    print("ALL PASS")

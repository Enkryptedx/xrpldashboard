"""Tests for amendments_network_votes — the VHS-backed live vote fetcher.

Covers the 9 gates in §7.1 of docs/LIVE_FETCH_AMENDMENTS_DESIGN.md, plus
the two field-semantics tests baked in per
`feedback_verify_field_semantics_before_reporting_movement.md`:

  - UNL-scoped parsing correctness: `voted.count` includes non-UNL nodes,
    the parser must return the UNL-only count.
  - Consensus cross-validation: when the aggregator's UNL filter and its
    `consensus` percentage disagree by more than 1 validator, skip the
    entry (schema-drift-safe).

All network calls are mocked; the suite must never touch the internet.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

import amendments_network_votes as anv


# ── Fixtures ────────────────────────────────────────────────────────────

def _amendment(
    hash_id: str,
    name: str,
    threshold: str = "28/35",
    consensus: str = "40.00%",
    total_yes: int = 27,
    unl_yes: int = 14,
    retired: bool = False,
    obsolete: bool = False,
):
    """Build a VHS-shaped amendment. `total_yes` may exceed `unl_yes`
    because the raw `voted.count` includes non-UNL broadcasters."""
    if unl_yes > total_yes:
        raise ValueError("unl_yes cannot exceed total_yes")
    validators = (
        [{"signing_key": f"n9UNL{i:03d}", "ledger_index": "100",
          "unl": "vl.ripple.com"} for i in range(unl_yes)]
        + [{"signing_key": f"n9OTH{i:03d}", "ledger_index": "100",
            "unl": False} for i in range(total_yes - unl_yes)]
    )
    return {
        "id": hash_id,
        "name": name,
        "threshold": threshold,
        "consensus": consensus,
        "voted": {"count": total_yes, "validators": validators},
        "retired": retired,
        "obsolete": obsolete,
        "rippled_version": "2.6.0",
    }


VAULT_HASH = "81BD09E6C4F7ED2D7D0E4A4A7A0D8B60F8C39F8B7A7B0E7D3F2C1B0A9E8D7C6B"
LENDING_HASH = "72CC0F7DC8A6DE3E8F1D5B6C9E0F1A2B3C4D5E6F7A8B9C0D1E2F3A4B5C6D7E8F"

VHS_HAPPY = {
    "result": "success",
    "count": 2,
    "amendments": [
        _amendment(VAULT_HASH, "SingleAssetVault",
                   consensus="40.00%", total_yes=27, unl_yes=14),
        _amendment(LENDING_HASH, "LendingProtocol",
                   consensus="37.14%", total_yes=23, unl_yes=13),
    ],
}


@pytest.fixture(autouse=True)
def _reset_cache():
    """Every test starts with a cold cache and default env."""
    anv._reset_cache_for_tests()
    # Ensure kill switch is off unless a test enables it.
    with patch.dict(
        "os.environ",
        {"NETWORK_VOTES_ENABLED": "true"},
        clear=False,
    ):
        yield
    anv._reset_cache_for_tests()


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


# ── Tests ───────────────────────────────────────────────────────────────

def test_happy_path_returns_ok_with_unl_scoped_counts():
    with patch(
        "amendments_network_votes.httpx.get",
        return_value=_FakeResp(200, VHS_HAPPY),
    ):
        env = anv.fetch_network_vote_tallies_cached()

    assert env["status"] == "ok"
    assert env["as_of_iso"] is not None
    assert env["source_url"].startswith("https://data.xrpl.org")

    data = env["data"]
    assert VAULT_HASH.upper() in data
    assert LENDING_HASH.upper() in data

    vault = data[VAULT_HASH.upper()]
    # 14 UNL yes-votes — NOT the raw voted.count of 27.
    assert vault["count"] == 14
    assert vault["validations"] == 35
    assert vault["threshold"] == 28


def test_voted_count_never_leaks_to_output():
    """Field-semantics guardrail: the parser must never surface raw
    voted.count. This is the exact bug the 2026-08-22 hard-stop caught."""
    with patch(
        "amendments_network_votes.httpx.get",
        return_value=_FakeResp(200, VHS_HAPPY),
    ):
        env = anv.fetch_network_vote_tallies_cached()

    for entry in env["data"].values():
        # raw voted.count in the fixture is 27 for vault, 23 for lending;
        # neither should appear as the published `count`.
        assert entry["count"] not in (27, 23), (
            "parser leaked raw voted.count instead of UNL-filter count"
        )


def test_consensus_divergence_skips_entry():
    """If the UNL-filter count and the aggregator's own `consensus`
    percentage disagree by more than 1, treat as schema drift and skip."""
    bogus = dict(VHS_HAPPY)
    bogus["amendments"] = [
        # Fixture claims 14/35 = 40% but sets consensus to 80% —
        # implies aggregator thinks 28 UNL yes. |14 - 28| = 14 > 1, skip.
        _amendment(VAULT_HASH, "SingleAssetVault",
                   consensus="80.00%", total_yes=27, unl_yes=14),
    ]
    with patch(
        "amendments_network_votes.httpx.get",
        return_value=_FakeResp(200, bogus),
    ):
        env = anv.fetch_network_vote_tallies_cached()

    assert env["status"] == "ok"
    assert VAULT_HASH.upper() not in env["data"]


def test_schema_drift_missing_threshold_skips_entry():
    """Amendment without a threshold string is treated as enabled — skip."""
    drift = {
        "amendments": [
            {"id": VAULT_HASH, "name": "SingleAssetVault",
             "voted": {"count": 14, "validators": []},
             "retired": False, "obsolete": False},
        ],
    }
    with patch(
        "amendments_network_votes.httpx.get",
        return_value=_FakeResp(200, drift),
    ):
        env = anv.fetch_network_vote_tallies_cached()

    assert env["status"] == "ok"
    assert env["data"] == {}


def test_retired_and_obsolete_are_skipped():
    payload = {
        "amendments": [
            _amendment(VAULT_HASH, "SingleAssetVault", retired=True),
            _amendment(LENDING_HASH, "LendingProtocol", obsolete=True),
        ],
    }
    with patch(
        "amendments_network_votes.httpx.get",
        return_value=_FakeResp(200, payload),
    ):
        env = anv.fetch_network_vote_tallies_cached()

    assert env["data"] == {}


def test_fetch_failure_no_prior_returns_unavailable():
    with patch(
        "amendments_network_votes.httpx.get",
        side_effect=Exception("connection refused"),
    ):
        env = anv.fetch_network_vote_tallies_cached()

    assert env["status"] == "unavailable"
    assert env["data"] == {}
    assert env["as_of_iso"] is None


def test_fetch_failure_with_prior_returns_stale():
    with patch(
        "amendments_network_votes.httpx.get",
        return_value=_FakeResp(200, VHS_HAPPY),
    ):
        first = anv.fetch_network_vote_tallies_cached()
    assert first["status"] == "ok"

    # Advance monotonic clock past TTL, then fail.
    later = time.monotonic() + anv.CACHE_TTL + 1
    with patch("amendments_network_votes._now_monotonic", return_value=later), \
         patch(
             "amendments_network_votes.httpx.get",
             side_effect=Exception("upstream 500"),
         ):
        env = anv.fetch_network_vote_tallies_cached()

    assert env["status"] == "stale"
    assert env["data"] == first["data"]
    assert env["stale_age_seconds"] >= anv.CACHE_TTL


def test_stale_ceiling_transitions_to_unavailable():
    with patch(
        "amendments_network_votes.httpx.get",
        return_value=_FakeResp(200, VHS_HAPPY),
    ):
        anv.fetch_network_vote_tallies_cached()

    # Past the 6h stale ceiling.
    later = time.monotonic() + anv.STALE_CEILING_SECONDS + 60
    with patch("amendments_network_votes._now_monotonic", return_value=later), \
         patch(
             "amendments_network_votes.httpx.get",
             side_effect=Exception("still down"),
         ):
        env = anv.fetch_network_vote_tallies_cached()

    assert env["status"] == "unavailable"
    assert env["data"] == {}


def test_cache_ttl_prevents_repeat_http_calls():
    """Two calls within TTL → exactly one HTTP round-trip."""
    with patch(
        "amendments_network_votes.httpx.get",
        return_value=_FakeResp(200, VHS_HAPPY),
    ) as mock_get:
        env1 = anv.fetch_network_vote_tallies_cached()
        env2 = anv.fetch_network_vote_tallies_cached()

    assert env1["status"] == "ok"
    assert env2["status"] == "ok"
    assert mock_get.call_count == 1
    assert env1["as_of_iso"] == env2["as_of_iso"]


def test_kill_switch_returns_disabled_without_fetching():
    with patch.dict("os.environ", {"NETWORK_VOTES_ENABLED": "false"}), \
         patch("amendments_network_votes.httpx.get") as mock_get:
        env = anv.fetch_network_vote_tallies_cached()

    assert env["status"] == "disabled"
    assert env["data"] == {}
    assert mock_get.call_count == 0


def test_non_200_response_treated_as_failure():
    with patch(
        "amendments_network_votes.httpx.get",
        return_value=_FakeResp(503, {}),
    ):
        env = anv.fetch_network_vote_tallies_cached()

    assert env["status"] == "unavailable"


def test_hashes_normalized_to_uppercase():
    lower = VAULT_HASH.lower()
    payload = {"amendments": [_amendment(lower, "SingleAssetVault")]}
    with patch(
        "amendments_network_votes.httpx.get",
        return_value=_FakeResp(200, payload),
    ):
        env = anv.fetch_network_vote_tallies_cached()

    assert lower.upper() in env["data"]
    assert lower not in env["data"]

"""Integration tests for the amendments_state ↔ amendments_network_votes
wiring (PR #2 of the live-fetch ship, per §7.1 gates 7-9 of
docs/LIVE_FETCH_AMENDMENTS_DESIGN.md).

Covers:
  - `fetch_amendments_state` calls the network-votes fetcher and attaches
    `network_vote` per in-flight entry keyed by uppercase hash.
  - Partial coverage: some in-flight amendments have network_vote, others
    don't; envelope counts are correct.
  - `INTERIM_VOTE_NOTES` symbol exists but is empty (one-release stub
    per Ruling 10.5) and does NOT influence any in-flight entry.
  - `/amendments` route renders the per-entry caption on-screen when
    network_vote is present, degrades honestly when unavailable, and
    surfaces a visible stale marker when the fetcher is serving prior data.

All network calls are mocked — the suite never touches the internet."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import amendments_network_votes as anv
import amendments_state


# ── Fixtures ────────────────────────────────────────────────────────────

VAULT_HASH = "81BD2619B6B3C862AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
LENDING_HASH = "565B90CA1AB2B9D4BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
OTHER_HASH = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

FEATURE_RESULT = {
    "features": {
        VAULT_HASH: {"name": "SingleAssetVault",
                     "enabled": False, "supported": True},
        LENDING_HASH: {"name": "LendingProtocol",
                       "enabled": False, "supported": True},
        OTHER_HASH: {"name": "SomeOtherAmendment",
                     "enabled": False, "supported": True},
    },
}

LEDGER_RESULT = {
    "node": {"Amendments": [], "Majorities": []},
    "ledger_index": 106467000,
}


def _envelope_ok():
    return {
        "data": {
            VAULT_HASH: {
                "count": 14, "validations": 35, "threshold": 28,
                "as_of_iso": "2026-08-22T13:32:17Z",
                "source_url": anv.ENDPOINT,
            },
            LENDING_HASH: {
                "count": 13, "validations": 35, "threshold": 28,
                "as_of_iso": "2026-08-22T13:32:17Z",
                "source_url": anv.ENDPOINT,
            },
        },
        "status": "ok",
        "as_of_iso": "2026-08-22T13:32:17Z",
        "source_url": anv.ENDPOINT,
    }


def _envelope_unavailable():
    return {"data": {}, "status": "unavailable",
            "as_of_iso": None, "source_url": anv.ENDPOINT}


def _envelope_stale():
    return {
        "data": {
            VAULT_HASH: {
                "count": 14, "validations": 35, "threshold": 28,
                "as_of_iso": "2026-08-22T13:32:17Z",
                "source_url": anv.ENDPOINT,
            },
        },
        "status": "stale",
        "as_of_iso": "2026-08-22T13:32:17Z",
        "stale_age_seconds": 900,
        "source_url": anv.ENDPOINT,
    }


@pytest.fixture(autouse=True)
def _clear_state_cache():
    """Ensure the amendments_state module cache doesn't leak fixtures
    across tests."""
    with amendments_state._cache_lock:
        amendments_state._cache["fetched_at"] = 0.0
        amendments_state._cache["data"] = None
    anv._reset_cache_for_tests()
    yield
    with amendments_state._cache_lock:
        amendments_state._cache["fetched_at"] = 0.0
        amendments_state._cache["data"] = None
    anv._reset_cache_for_tests()


# ── State-shape tests ───────────────────────────────────────────────────

def test_interim_vote_notes_is_empty_stub():
    """Ruling 10.5: symbol kept for one release but MUST be empty so no
    in-flight entry can accidentally receive a stale hardcoded note."""
    assert amendments_state.INTERIM_VOTE_NOTES == {}


def test_wired_state_attaches_network_vote_per_hash():
    with patch.object(amendments_state, "_post",
                      side_effect=[FEATURE_RESULT, LEDGER_RESULT]), \
         patch.object(anv, "fetch_network_vote_tallies_cached",
                      return_value=_envelope_ok()):
        state = amendments_state.fetch_amendments_state()

    assert state["ok"] is True
    in_flight_by_name = {e["name"]: e for e in state["in_flight"]}
    assert in_flight_by_name["SingleAssetVault"]["network_vote"]["count"] == 14
    assert in_flight_by_name["LendingProtocol"]["network_vote"]["count"] == 13
    # No hardcoded interim_vote_note key should ever appear on an entry.
    for entry in state["in_flight"]:
        assert "interim_vote_note" not in entry

    assert state["network_votes_source"]["status"] == "ok"
    assert state["network_votes_source"]["as_of_iso"] == "2026-08-22T13:32:17Z"


def test_partial_coverage_counts_are_correct():
    """Envelope has 2 of 3 in-flight hashes; the third must remain in the
    list without a network_vote key, and the envelope counters must
    reflect that split."""
    with patch.object(amendments_state, "_post",
                      side_effect=[FEATURE_RESULT, LEDGER_RESULT]), \
         patch.object(anv, "fetch_network_vote_tallies_cached",
                      return_value=_envelope_ok()):
        state = amendments_state.fetch_amendments_state()

    assert state["in_flight_count"] == 3
    assert state["in_flight_with_votes_count"] == 2
    assert state["in_flight_without_votes_count"] == 1

    other = next(e for e in state["in_flight"]
                 if e["name"] == "SomeOtherAmendment")
    assert "network_vote" not in other


def test_unavailable_envelope_leaves_all_entries_without_votes():
    with patch.object(amendments_state, "_post",
                      side_effect=[FEATURE_RESULT, LEDGER_RESULT]), \
         patch.object(anv, "fetch_network_vote_tallies_cached",
                      return_value=_envelope_unavailable()):
        state = amendments_state.fetch_amendments_state()

    assert state["network_votes_source"]["status"] == "unavailable"
    assert state["in_flight_with_votes_count"] == 0
    assert state["in_flight_without_votes_count"] == 3
    for entry in state["in_flight"]:
        assert "network_vote" not in entry


def test_hash_case_join_normalizes_to_uppercase():
    """The join uses uppercase; feature-RPC hashes vary in case across
    node builds. Envelope keys are always uppercase (guaranteed by the
    fetcher). The wiring must uppercase the feature-RPC hash before the
    lookup."""
    lower_features = {
        "features": {
            VAULT_HASH.lower(): {"name": "SingleAssetVault",
                                 "enabled": False, "supported": True},
        },
    }
    with patch.object(amendments_state, "_post",
                      side_effect=[lower_features, LEDGER_RESULT]), \
         patch.object(anv, "fetch_network_vote_tallies_cached",
                      return_value=_envelope_ok()):
        state = amendments_state.fetch_amendments_state()

    entry = state["in_flight"][0]
    assert entry["name"] == "SingleAssetVault"
    assert entry["network_vote"]["count"] == 14


# ── Render tests via Flask test client ──────────────────────────────────

def test_amendments_route_renders_per_entry_caption(client):
    with patch.object(amendments_state, "_post",
                      side_effect=[FEATURE_RESULT, LEDGER_RESULT]), \
         patch.object(anv, "fetch_network_vote_tallies_cached",
                      return_value=_envelope_ok()):
        resp = client.get("/amendments")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Compressed per-entry caption + primary attribution string
    assert "Trusted-validator vote:" in body
    assert "XRPL Foundation validator history" in body
    # Numeric render for the wired amendments
    assert "14/35" in body
    assert "13/35" in body
    # The old interim-note copy must be gone
    assert "Active validator voting" not in body
    assert "interim figures refresh manually" not in body


def test_amendments_route_degrades_honestly_when_unavailable(client):
    with patch.object(amendments_state, "_post",
                      side_effect=[FEATURE_RESULT, LEDGER_RESULT]), \
         patch.object(anv, "fetch_network_vote_tallies_cached",
                      return_value=_envelope_unavailable()):
        resp = client.get("/amendments")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Every in-flight entry gets the honest-degrade line — one per entry.
    assert body.count("Trusted-validator vote: not fetched") >= 3
    # And no fabricated numeric tally.
    assert "Trusted-validator vote:</strong> <strong>" not in body


def test_amendments_route_marks_stale_visibly(client):
    with patch.object(amendments_state, "_post",
                      side_effect=[FEATURE_RESULT, LEDGER_RESULT]), \
         patch.object(anv, "fetch_network_vote_tallies_cached",
                      return_value=_envelope_stale()):
        resp = client.get("/amendments")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The card must degrade honestly on-screen, not just in the envelope.
    assert "(stale)" in body
    assert "last successful fetch" in body
    # 900 stale seconds → 15 min.
    assert "15 min ago" in body

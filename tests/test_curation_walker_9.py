"""Curation of the 9 walker-discovered entries (2026-10-17).

A walker fired overnight and added 9 entries to token_names.json. Under the
automatic MPT rule they would all have rendered -- bare, with no curation --
because none of them contains a curated ticker, so the lookalike rule stays
silent on all 9. Rendering is therefore the DEFAULT for a freshly-walked
name, which is exactly why each one needs an explicit decision.

Decisions pinned here:

  7x SCPO (rM75v2Ssm6W7...) -> suppressed, never render.
     Six declare "test" in their own on-ledger metadata. The seventh,
     "Refresh Timing 1", does NOT -- the name predicate misses it. It is
     suppressed on ISSUER-CLUSTER evidence instead: same issuer, same
     SC.PO issuer_name, identical CanEscrow+CanClawback flags, empty
     on-ledger metadata, and sequential issuance IDs 107538901-107538933
     interleaved with the six. That reason is recorded as its own string so
     the judgement is auditable rather than hidden behind a substring match.

  CERT -> self_described. "CERTCRYPT Capacity", no issuer Domain,
     OutstandingAmount 0. No first-party source exists, so it renders with
     the unverified qualifier.

  XAGa -> tier toml. Its [[CURRENCIES]] block pairs code
     5841476100000000000000000000000000000000 with issuer
     rQJ1EbtepKKzx4sBCUtPjvSk8CfiAx2H8e and ticker XAGa -- a CURRENCY-level
     citation, not merely issuer-level, so it earns the full toml tier and
     renders bare like any other verified token.

TWO NEAR-MISSES that shaped this, both false negatives from mechanical
failure rather than from evidence:
  1. The first TOML fetch returned URLError. One transient blip would have
     demoted a legitimately sourced token to self-described.
  2. A parser looking for `currency =` found nothing, because this TOML uses
     `code =` / `ticker =`. That one fails DETERMINISTICALLY and looks like
     a clean negative -- a retry would not have saved it.
Both are addressed in the follow-up PR (shared fetch-with-retry + shared
[[CURRENCIES]] parser). This PR records the verdicts they produced.

Hermetic: reads token_names.json and pure token_label functions. No network,
no DB -- deliberately, since a CI test that fetches a third-party TOML would
be flaky and would re-create near-miss #1 inside the test suite.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
import token_label as tl  # noqa: E402

NAMES = os.path.join(REPO_ROOT, "token_names.json")
SCPO_ISSUER = "rM75v2Ssm6W7ZP3iKi8FCaMbSsUaiQhYKC"
XAGA_ISSUER = "rQJ1EbtepKKzx4sBCUtPjvSk8CfiAx2H8e"
XAGA_HEX = "5841476100000000000000000000000000000000"
XAGA_TOML = "https://silver.assetiko.com/.well-known/xrp-ledger.toml"
CLUSTER_REASON = "issuer-cluster: same issuer/test pattern"


@pytest.fixture(scope="module")
def raw():
    return json.load(open(NAMES, encoding="utf-8"))


def entries(raw, issuer=None, display=None):
    out = []
    for key, v in raw.items():
        if not isinstance(v, dict):
            continue
        if issuer and key.split(":", 1)[1] != issuer:
            continue
        if display and v.get("currency_display") != display:
            continue
        out.append(v)
    return out


# ── the SCPO cluster ────────────────────────────────────────────────
class TestScpoCluster:
    def test_all_seven_are_suppressed(self, raw):
        got = entries(raw, issuer=SCPO_ISSUER)
        assert len(got) == 7, [v.get("name") for v in got]
        for v in got:
            assert v.get("suppressed"), v.get("name")
            assert tl.render_decision(v) == (False, None)

    def test_six_cite_their_own_test_metadata(self, raw):
        got = entries(raw, issuer=SCPO_ISSUER)
        by_name = {v.get("name"): v.get("suppressed") for v in got}
        test_named = [n for n, r in by_name.items()
                      if r == "self_declared_test"]
        assert len(test_named) == 6, by_name

    def test_refresh_timing_uses_the_cluster_reason(self, raw):
        """The name predicate misses this one. The reason must say so
        explicitly, not borrow the self_declared_test label it did not earn."""
        got = [v for v in entries(raw, issuer=SCPO_ISSUER)
               if v.get("name") == "Refresh Timing 1"]
        assert len(got) == 1
        assert got[0]["suppressed"] == CLUSTER_REASON
        blob = " ".join(str(got[0].get(f, "")) for f in
                        ("name", "desc", "issuer_name")).lower()
        assert "test" not in blob, (
            "if the name now says 'test', use self_declared_test instead")

    def test_cluster_reason_is_used_exactly_once(self, raw):
        n = sum(1 for v in raw.values() if isinstance(v, dict)
                and v.get("suppressed") == CLUSTER_REASON)
        assert n == 1


# ── CERT ────────────────────────────────────────────────────────────
class TestCert:
    def test_renders_with_the_unverified_qualifier(self, raw):
        got = entries(raw, display="CERT")
        assert len(got) == 1
        v = got[0]
        assert v.get("tier") == tl.SELF_DESCRIBED
        assert not v.get("suppressed")
        ok, label = tl.render_decision(v)
        assert ok is True
        assert label == "self-described (unverified)"

    def test_is_not_a_lookalike(self, raw):
        """No curated ticker in "CERT", so the automatic rule stays silent."""
        assert tl.lookalike_asset(entries(raw, display="CERT")[0]) is None


# ── XAGa ────────────────────────────────────────────────────────────
class TestXaga:
    def test_tier_is_toml_not_domain_fallback(self, raw):
        """Currency-level citation: the [[CURRENCIES]] block pairs this exact
        code WITH this exact issuer, so it is not merely issuer-level."""
        got = [v for v in raw.values() if isinstance(v, dict)
               and v.get("currency_hex") == XAGA_HEX]
        assert len(got) == 1
        v = got[0]
        assert v.get("issuer") == XAGA_ISSUER
        assert v.get("tier") == "toml"
        assert v.get("verified_via") == XAGA_TOML

    def test_citation_is_the_subdomain_not_the_apex(self, raw):
        """silver.assetiko.com names the issuer 4x; assetiko.com names it 0x.
        Citing the apex would be an unreciprocated claim."""
        got = [v for v in raw.values() if isinstance(v, dict)
               and v.get("currency_hex") == XAGA_HEX][0]
        assert "silver.assetiko.com" in got["verified_via"]

    def test_renders_bare_like_any_verified_token(self, raw):
        got = [v for v in raw.values() if isinstance(v, dict)
               and v.get("currency_hex") == XAGA_HEX][0]
        ok, label = tl.render_decision(got)
        assert ok is True
        assert label is None, "a toml-tier token needs no qualifier"


# ── file-level invariants ───────────────────────────────────────────
class TestFileInvariants:
    def test_suppressed_total(self, raw):
        """5 from the earlier curation pass + 7 SCPO."""
        n = sum(1 for v in raw.values()
                if isinstance(v, dict) and v.get("suppressed"))
        assert n == 12

    def test_review_markers_still_three(self, raw):
        """This PR must not disturb the XRP NFT / XRP+ / XRP3D hold."""
        marked = sorted(v.get("currency_display") for v in raw.values()
                        if isinstance(v, dict) and v.get(tl.REVIEW_MARKER))
        assert marked == ["XRP NFT", "XRP+", "XRP3D"]

    def test_none_of_the_nine_is_a_lookalike(self, raw):
        """SCPO / CERT / XAGa contain no curated ticker, so rendering was the
        DEFAULT for all 9 -- the reason each needed an explicit decision."""
        for v in entries(raw, issuer=SCPO_ISSUER):
            assert tl.lookalike_asset(v) is None
        assert tl.lookalike_asset(entries(raw, display="CERT")[0]) is None

    def test_every_suppressed_entry_has_a_reason_string(self, raw):
        for v in raw.values():
            if isinstance(v, dict) and v.get("suppressed"):
                assert isinstance(v["suppressed"], str) and v["suppressed"]

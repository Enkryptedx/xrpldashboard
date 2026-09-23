"""Regression: no manifest note may deny the provenance the same row asserts.

Charlie ruling 2026-09-22 Tue 9:32 PM ET (Round 4, item 1). RLUSD shipped
tonight with `tier=verified` (per_issuer_tier proved via two-way TOML at
ripple.com) AND a row-level citation reading "is NOT Ripple's official
RLUSD ... Ripple has not confirmed an official XRPL RLUSD issuer to us."
Self-contradicting: the row's own per-issuer proof IS Ripple confirming.
This test blocks any future manifest from shipping a row-level citation
that denies its own per-issuer verification.

How it survived: `ticker_canonical_issuers.json` is edited by hand; the
walker re-signs whatever's in the yaml. Prior curator notes carried
across manifest widens without a check that the note still agreed with
the tier the walker would compute today.
"""
from __future__ import annotations
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

import signed_verified_tokens as SVT  # noqa: E402


BANNED_PHRASES_IN_VERIFIED_CITATION = (
    "is not ",
    "has not confirmed",
    "not confirmed",
    "not the official",
    "not the real",
    "impostor",
)


def test_verified_row_citation_does_not_deny_provenance():
    env = SVT.build_envelope()
    rows = env.get("tokens", [])
    assert rows, "envelope has no rows — cannot verify"
    offenders = []
    for row in rows:
        if row.get("tier") != "verified":
            continue
        cit = (row.get("citation_url") or "").lower()
        for phrase in BANNED_PHRASES_IN_VERIFIED_CITATION:
            if phrase in cit:
                offenders.append({
                    "ticker": row.get("ticker"),
                    "banned_phrase": phrase,
                    "citation_excerpt": cit[:300],
                })
                break
    assert not offenders, (
        "verified-tier rows contain citations that deny their own provenance "
        f"(the per_issuer_tier proof IS the confirmation): {offenders}"
    )

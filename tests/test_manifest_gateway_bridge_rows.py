"""Regression: gateways + bridges sections of ticker_canonical_issuers.json
produce per-(gateway, ticker) rows in the signed-verified-tokens manifest.

Charlie ruling 2026-09-23 Wed 06:52 ET (Round 4 item 6). Prior manifest
only carried a single 'gateways' / 'bridges' umbrella row per section
— a wallet looking up a Bitstamp-USD or GateHub-EUR pair couldn't
find a canonical_issuer_known=True match without walking the umbrella
row's citation text. Every named gateway and bridge should now emit
one row per ticker it issues, with canonical_issuer_known=True and the
full issuers list.
"""
from __future__ import annotations
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

import signed_verified_tokens as SVT  # noqa: E402


def test_bitstamp_gatehub_ripplefox_midas_all_present():
    env = SVT.build_envelope()
    rows = env.get("tokens", [])
    by_source = {}
    for r in rows:
        src = r.get("canonical_source", "")
        if src.startswith("gateways_registry:") or src.startswith("bridges_registry:"):
            key = src.split(":", 1)[1]
            by_source.setdefault(key, []).append(r)
    # Every named gateway/bridge with an issuers list should have >= 1 row
    assert set(by_source.keys()) >= {"bitstamp", "gatehub", "ripplefox", "midas_xrpl"}, (
        f"missing gateway/bridge rows: got sources {sorted(by_source.keys())}"
    )
    # Bitstamp issues USD/EUR/BTC/ETH — 4 tickers → up to 4 rows (some may
    # dedup against yaml if an entry there already covers a pair)
    bitstamp = by_source["bitstamp"]
    tickers = {r["ticker"] for r in bitstamp}
    assert tickers, "no Bitstamp rows emitted"
    # GateHub issues 17 tickers across 32 issuers
    gh = by_source["gatehub"]
    assert len(gh) >= 5, f"GateHub should emit multiple ticker rows; got {len(gh)}"


def test_gateway_bridge_rows_are_canonical_issuer_known_true():
    env = SVT.build_envelope()
    for r in env.get("tokens", []):
        src = r.get("canonical_source", "")
        if not (src.startswith("gateways_registry:") or src.startswith("bridges_registry:")):
            continue
        assert r.get("canonical_issuer_known") is True, (
            f"{r.get('ticker')} from {src} must have canonical_issuer_known=True; got {r.get('canonical_issuer_known')}"
        )
        assert r.get("gateway_or_bridge") is True, (
            f"{r.get('ticker')} from {src} must have gateway_or_bridge=True"
        )
        assert r.get("canonical_issuers"), (
            f"{r.get('ticker')} from {src} must have canonical_issuers list"
        )
        # per_issuer_tier must be a list of dicts, one per issuer
        pit = r.get("per_issuer_tier") or []
        assert len(pit) == len(r["canonical_issuers"]), (
            f"{r.get('ticker')} from {src}: per_issuer_tier count "
            f"{len(pit)} != issuers count {len(r['canonical_issuers'])}"
        )

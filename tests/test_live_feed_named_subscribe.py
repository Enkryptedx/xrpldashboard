"""Regression lock for the Option B Milestone 2 template swap (2026-09-26).

Each live-feed page subscribes to its NAMED relay feed on the primary
(own-node) socket and only sends the public cluster's raw shape on the
fallback branch. Static source checks + a Jinja parse of every touched
template (py_compile lints Python, not Jinja — feedback rule).
"""
from __future__ import annotations
import os
import re
import sys

import jinja2

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
TPL = os.path.join(ROOT, "templates")
sys.path.insert(0, ROOT)

EXPECT = {
    "pools.html": ["stream: 'amm_transactions'"],
    "tokens.html": ["stream: 'token_top100_transactions'", "stream: 'amm_transactions'"],
    "whales.html": ["stream: 'whale_transactions'", "RELAY_WHALE_TIER_DROPS = 100000000000"],
    "wallet.html": ["stream: 'wallet_transactions'", "accounts: [addr]",
                    "walker-node-fallback?source=browser_wss"],
}


def _src(name: str) -> str:
    with open(os.path.join(TPL, name), encoding="utf-8") as f:
        return f.read()


def test_named_feed_subscribe_present():
    for name, needles in EXPECT.items():
        s = _src(name)
        for n in needles:
            assert n in s, f"{name}: missing {n!r}"


def test_raw_transactions_only_on_fallback_branch():
    """Every remaining `streams: ['transactions']` must sit inside a
    fallback-conditional (wsUsingFallback / usingFallback) — never as the
    unconditional primary payload it was before the swap."""
    for name in ("pools.html", "tokens.html", "whales.html"):
        s = _src(name)
        for m in re.finditer(r"streams:\s*\['transactions'\]", s):
            line_start = s.rfind("\n", 0, m.start()) + 1
            if s[line_start:m.start()].lstrip().startswith("//"):
                continue  # prose in a comment, not a payload
            window = s[max(0, m.start() - 400):m.start()]
            assert ("wsUsingFallback" in window or "usingFallback" in window), \
                f"{name}: raw transactions sub at offset {m.start()} is not on a fallback branch"


def test_wallet_no_hardcoded_public_cluster_url():
    s = _src("wallet.html")
    assert "var WS_URL = 'wss://xrplcluster.com'" not in s
    assert "live_stream_wss_primary | tojson" in s


def test_templates_parse():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TPL),
                             extensions=["jinja2.ext.i18n"])
    env.install_null_translations()
    for name in EXPECT:
        env.get_template(name)  # raises TemplateSyntaxError on a broken template

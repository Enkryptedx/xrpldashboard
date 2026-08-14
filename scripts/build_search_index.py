#!/usr/bin/env python3
"""Generate static/search-index.json for the client-side search UI.

Run at deploy time. Exits non-zero if any required source file is missing or
malformed so the deploy fails rather than shipping a stale index.

Sources (in priority order):
  1. glossary.json            — canonical term definitions
  2. CLAIMS.yaml              — claim labels + pages
  3. PAGE_MAP                 — hardcoded page titles / excerpts / h2 sections

Output: static/search-index.json
Format: [{id, type, title, url, body}, ...]
  type: "page" | "section" | "term" | "claim"
"""

import json
import sys
import os
import re

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GLOSSARY_PATH = os.path.join(REPO_ROOT, "glossary.json")
CLAIMS_PATH = os.path.join(REPO_ROOT, "CLAIMS.yaml")
OUT_PATH = os.path.join(REPO_ROOT, "static", "search-index.json")

# ---------------------------------------------------------------------------
# Hardcoded page map: title, excerpt, and notable h2-level sections.
# Add a row when a new route ships; update excerpt when copy changes.
# ---------------------------------------------------------------------------
PAGE_MAP = [
    {
        "url": "/",
        "title": "Dashboard",
        "excerpt": "Live XRPL overview — AMM pools, whale transfers, token activity, MPTs, NFTs, RLUSD supply, and amendments. All data sourced directly from the ledger.",
        "sections": [],
    },
    {
        "url": "/methodology",
        "title": "Methodology",
        "excerpt": "Every data source, cache TTL, and known limitation — listed in plain text. The full methodology behind every number on xrpldashboard.",
        "sections": [
            ("Freshness contracts", "methodology#freshness"),
            ("Signed snapshots and on-ledger anchor", "methodology#signed-snapshots-xrpl-anchor"),
            ("For AI agents", "methodology#for-ai-agents"),
            ("Known limitations", "methodology#known-limitations"),
        ],
    },
    {
        "url": "/claims",
        "title": "Claims manifest",
        "excerpt": "Every public claim on xrpldashboard, catalogued in CLAIMS.yaml. Each claim has a permanent URI, a sovereignty tier, and a reference to the data source that backs it.",
        "sections": [],
    },
    {
        "url": "/whales",
        "title": "Whale transfers",
        "excerpt": "Every XRP payment above 100,000 XRP, streamed live from the ledger as transactions validate. Threshold is descriptive — identifies transaction size, not intent.",
        "sections": [],
    },
    {
        "url": "/tokens",
        "title": "Token registry",
        "excerpt": "Verified XRPL token supply — full token registry with domain-attested labels and on-ledger activity. Tokens identified by (currency code, issuer) pair.",
        "sections": [],
    },
    {
        "url": "/mpts",
        "title": "MPT registry",
        "excerpt": "Multi-Purpose Token (XLS-33) registry and issuer roll-ups. MPTs offer a smaller on-ledger footprint than trust-line tokens.",
        "sections": [],
    },
    {
        "url": "/nfts",
        "title": "NFT activity",
        "excerpt": "XLS-20 NFT activity on XRPL — mints, burns, offers, and sales. Historical backfill from 2026-04-01; residual holes disclosed and quantified.",
        "sections": [],
    },
    {
        "url": "/pools",
        "title": "AMM pools",
        "excerpt": "AMM pools ranked by TVL and 24-hour volume. Each pool uses a constant-product formula; reserves and LP token supply are readable on-ledger.",
        "sections": [],
    },
    {
        "url": "/rlusd",
        "title": "RLUSD supply",
        "excerpt": "RLUSD supply history — cross-chain (XRPL + Ethereum). Mint and burn events computed directly from gateway balance RPCs and Ethereum contract state.",
        "sections": [],
    },
    {
        "url": "/rwa",
        "title": "RWA registry",
        "excerpt": "Real-world-asset tokens on XRPL with issuer attestation status. Covers tokenized currencies, commodities, and securities issued on-ledger.",
        "sections": [],
    },
    {
        "url": "/amendments",
        "title": "Amendment tracker",
        "excerpt": "Current XRPL amendment status — enabled, voting, and vetoed amendments with validator support tallies. Includes full amendment history.",
        "sections": [
            ("Enabled amendments", "amendments#enabled"),
            ("Voting amendments", "amendments#voting"),
        ],
    },
    {
        "url": "/lending",
        "title": "Lending",
        "excerpt": "LendingProtocol amendment (XLS-66) status. Tracks the amendment that would add a native lending market to XRPL.",
        "sections": [],
    },
    {
        "url": "/regulation",
        "title": "Regulation tracker",
        "excerpt": "Plain-English CLARITY Act (H.R. 3633) status tracker. Bill text, progress, and what it would mean for XRPL-based tokens.",
        "sections": [],
    },
    {
        "url": "/network",
        "title": "Network",
        "excerpt": "XRPL network statistics — validator count, ledger close times, fee levels, and network health indicators.",
        "sections": [],
    },
    {
        "url": "/sidechain",
        "title": "Sidechain",
        "excerpt": "XRPL sidechain activity and bridge state. Covers the EVM sidechain bridge and cross-chain transfers.",
        "sections": [],
    },
    {
        "url": "/health",
        "title": "Infrastructure health",
        "excerpt": "Live infrastructure status — walker fleet heartbeats, data freshness, walker fail counts, and pager state for all data sources feeding the dashboard.",
        "sections": [],
    },
    {
        "url": "/cold-storage",
        "title": "Cold storage",
        "excerpt": "Known cold-wallet balances for major XRPL exchanges and custodians. Derived from on-ledger activity and public domain attestations.",
        "sections": [],
    },
    {
        "url": "/about",
        "title": "About",
        "excerpt": "Mission, funding, principles, and the people behind xrpldashboard. Covers the four-layer truth audit, sovereignty architecture, and the open-source commitment.",
        "sections": [],
    },
    {
        "url": "/connect",
        "title": "Connect an AI",
        "excerpt": "Connect Claude, GPT, or any MCP-compatible AI to xrpldashboard in 60 seconds. Free public MCP server — no auth, no payment required.",
        "sections": [
            ("Connect in 60 seconds", "connect#connect-in-60-seconds"),
            ("Proof annotation envelope", "connect#proof-annotation"),
        ],
    },
    {
        "url": "/learn",
        "title": "Learn",
        "excerpt": "Introductory guides to the XRP Ledger, its core primitives (AMM, MPT, NFT, escrow, trust lines), and how to read xrpldashboard data.",
        "sections": [],
    },
    {
        "url": "/institutional",
        "title": "Institutional",
        "excerpt": "Institutional access to xrpldashboard data — signed snapshots, the MCP server, the proof-annotation envelope, and the sovereignty architecture.",
        "sections": [],
    },
    {
        "url": "/glossary",
        "title": "Glossary",
        "excerpt": "Definitions for XRPL terms and xrpldashboard methodology concepts. AMM, amendment, trust line, signed snapshot, sovereignty tier, and more — in plain English.",
        "sections": [],
    },
    {
        "url": "/price-data",
        "title": "Price data",
        "excerpt": "XRP/USD price derived from XRPL stablecoin AMMs — self-sourced, no third-party price API. Methodology and source pool list disclosed.",
        "sections": [],
    },
    {
        "url": "/security",
        "title": "Security",
        "excerpt": "Security contact, responsible disclosure policy, and the signed snapshot verification guide for independent auditors.",
        "sections": [],
    },
]


def load_glossary():
    with open(GLOSSARY_PATH, encoding="utf-8") as f:
        terms = json.load(f)
    if not isinstance(terms, list) or not terms:
        raise ValueError(f"glossary.json must be a non-empty list; got {type(terms)}")
    return terms


def load_claims():
    with open(CLAIMS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "pages" not in data:
        raise ValueError("CLAIMS.yaml must have a top-level 'pages' key")
    return data


def build_index(glossary, claims_data):
    entries = []
    seen_ids = set()

    def add(entry):
        if entry["id"] in seen_ids:
            raise ValueError(f"Duplicate id in search index: {entry['id']}")
        seen_ids.add(entry["id"])
        entries.append(entry)

    # 1. Pages
    for page in PAGE_MAP:
        add({
            "id": f"page:{page['url']}",
            "type": "page",
            "title": page["title"],
            "url": page["url"],
            "body": page["excerpt"],
        })

        # 2. Sections from this page
        for section_title, section_anchor in page.get("sections", []):
            add({
                "id": f"section:{section_anchor}",
                "type": "section",
                "title": section_title,
                "url": f"/{section_anchor}",
                "body": f"{page['title']} \u2014 {section_title}",
            })

    # 3. Glossary terms
    for term in glossary:
        slug = term["slug"]
        add({
            "id": f"term:{slug}",
            "type": "term",
            "title": term["term"],
            "url": f"/glossary#{slug}",
            "body": term["definition"],
        })

    # 4. Claims (label only — link to /claims index)
    pages_block = claims_data.get("pages", {})
    for page_route, page_data in pages_block.items():
        for claim in page_data.get("claims", []):
            claim_id = claim.get("id", "")
            label = claim.get("label", "")
            if not claim_id or not label:
                continue
            # Trim any surrounding quotes from label
            label = label.strip('"')
            add({
                "id": f"claim:{claim_id}",
                "type": "claim",
                "title": label,
                "url": "/claims",
                "body": f"Claim on {page_route}: {label}",
            })

    return entries


def main():
    print(f"[build_search_index] reading glossary from {GLOSSARY_PATH}")
    glossary = load_glossary()
    print(f"[build_search_index] {len(glossary)} terms loaded")

    print(f"[build_search_index] reading claims from {CLAIMS_PATH}")
    claims_data = load_claims()
    claim_count = sum(
        len(p.get("claims", []))
        for p in claims_data.get("pages", {}).values()
    )
    print(f"[build_search_index] {claim_count} claims loaded")

    print(f"[build_search_index] building index ({len(PAGE_MAP)} pages in PAGE_MAP)")
    entries = build_index(glossary, claims_data)
    print(f"[build_search_index] {len(entries)} total entries")

    os.makedirs(os.path.join(REPO_ROOT, "static"), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = os.path.getsize(OUT_PATH) / 1024
    print(f"[build_search_index] wrote {OUT_PATH} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[build_search_index] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

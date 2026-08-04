"""Queryable claims layer — free-tier substrate.

Shipped 2026-08-04 per docs/PAID_MACHINE_TIER_DESIGN.md § 3.1 (promoted
from backlog after both Grok and ChatGPT external evaluations
independently proposed a queryable CLAIMS surface).

Purpose: expose CLAIMS.yaml — Layer 4 of the four-layer truth audit
— as agent-consumable URIs. Each of the 106+ claims in CLAIMS.yaml
gets a permanent resolvable URI. Fetching the URI returns per-claim
status JSON (green/yellow/red traffic light) so an agent can decide
whether a specific numeric claim is (a) currently backed by SOVEREIGN
data (green — signable, sellable through the future paid tier), (b)
public-node dependent or lacking independent cross-check (yellow —
free-tier only until upgraded), or (c) third-party derived (red —
permanently free-only per the site's own rule #3).

URI scheme (permanent — cannot break once agents cite it):

    /claims/xrpl.<domain>.<series>

- `xrpl` fixed namespace (future-proofs multi-chain expansion — every
  claim on the site today is XRPL-derived, but locking `xrpl.` now
  means the shape doesn't churn if we ever add an adjacent chain).
- `<domain>` = page slug derived from CLAIMS.yaml page key (e.g., the
  `/rlusd` page → `rlusd`; `/.well-known/agents.json` → `agents_json`).
- `<series>` = the CLAIMS.yaml claim id with the `<domain>_` prefix
  stripped when present, otherwise the id verbatim. Underscore-
  separated segments preserved (they name the metric).

Examples:
    rlusd_page_title  on /rlusd → xrpl.rlusd.page_title
    rlusd_xrpl_supply on /rlusd → xrpl.rlusd.xrpl_supply
    whale_page_title_and_scope on /whales → xrpl.whales.page_title_and_scope
    escrow_locked on / → xrpl.home.escrow_locked

Content negotiation: `/claims/<uri>` returns HTML by default; append
`.json` or send `Accept: application/json` for the machine-readable
status. `/claims/index.json` returns the machine-readable index.

Layer 4 discipline: this module's routes + templates are catalogued
in CLAIMS.yaml under the `/claims` page block. Copy claims (page
title, index description) move with the code in the same commit.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone
from typing import Any

import yaml


REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
CLAIMS_YAML_PATH = os.path.join(REPO_ROOT, "CLAIMS.yaml")

# Sovereignty-tier map — the traffic light derives from layer3_source.
# See docs/PAID_MACHINE_TIER_DESIGN.md § 3.1 for the semantic contract:
#   green  — SOVEREIGN + passing gap audit; sellable + signable
#   yellow — PUBLIC-INFRA-DEPENDENT (Batch B pending) OR gap audit
#            incomplete; free-tier only until upgraded
#   red    — THIRD-PARTY-DERIVED (permanently free-only per rule #3)
#            OR failing/paused/retired
_LAYER3_SOURCE_TIER = {
    # SOVEREIGN — our own infrastructure. Signable + sellable.
    "local_rippled_server_info": "green",
    "local_rippled_stream_capture": "green",
    "cryptographic-signature": "green",
    # PUBLIC-INFRA-DEPENDENT — XRPL RPC that currently traverses public
    # nodes for at least some walkers (Batch B post-soak migration
    # queued). Also self-attested-only, where the claim is backed by
    # our walker output but no independent cross-check path exists yet.
    "xrpl_gateway_balances_rpc": "yellow",
    "xrpl_amendments_ledger_object": "yellow",
    "xrpl-account-objects": "yellow",
    "xrpl-account-signerlist": "yellow",
    "xrpl-credentials-rpc": "yellow",
    "xrpl-priceoracle-rpc": "yellow",
    "xrpl-transaction-history": "yellow",
    "self-attested-only": "yellow",
    # THIRD-PARTY-DERIVED — permanently free-only under rule #3
    # ("only sell data I source and prove myself, from my own
    # infrastructure — never data derived from third-party APIs,
    # archives, or someone else's free public servers").
    "etherscan_totalSupply": "red",
    "ethereum_public_rpc+xrpl_public_rpc": "red",
    "toml_attested": "red",  # issuer's own TOML — third-party attestation
    "vl_ripple_com_vl_xrplf_org": "red",  # Ripple/XRPLF VLs
    "cf_ipcountry_header": "red",  # Cloudflare edge header
    "primary-legislative-sources": "red",  # Congress.gov, OFAC, RDAP, crt.sh
    "bill-text-hr-3633": "red",  # bill text upstream
}

_TIER_REASON = {
    "green": (
        "Sovereign source — our own infrastructure (local rippled node "
        "or Ed25519 signing key). Currently sellable through the future "
        "paid tier and signable in the daily snapshot chain."
    ),
    "yellow": (
        "Public-infrastructure dependent OR no independent cross-check "
        "path yet. Free-tier only until Batch B walker migration lands "
        "(post-soak, ~2026-08-31) and/or an independent verification "
        "walker is wired for this series."
    ),
    "red": (
        "Third-party-derived data (external chain RPC, issuer "
        "attestation, primary-legislative source, edge header). "
        "Permanently free-only under the site's rule #3 — never sold "
        "even after the paid tier ships."
    ),
    "unknown": (
        "Layer 3 source not yet classified for sovereignty tier. "
        "Defaulting to yellow-equivalent: treat as free-tier only "
        "pending explicit classification."
    ),
}

_URI_RE = re.compile(r"^xrpl\.[a-z0-9_]+\.[a-z0-9_]+$")

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"mtime": 0.0, "index": None, "by_uri": None, "raw": None}


def _page_to_domain(page_path: str) -> str:
    """Derive a URI-safe domain segment from a CLAIMS.yaml page key.

    Rules:
    - '/' → 'home' (root-page special case)
    - '/rlusd' → 'rlusd'
    - '/methodology#for-ai-agents' → 'methodology_for_ai_agents'
    - '/.well-known/agents.json' → 'well_known_agents_json'
    - '/llms.txt' → 'llms_txt'

    Output is lowercase, [a-z0-9_], collapses runs of separators.
    """
    if page_path == "/":
        return "home"
    normalized = page_path.lstrip("/").replace(".well-known/", "well_known_")
    normalized = normalized.replace("#", "_").replace("-", "_").replace("/", "_").replace(".", "_")
    normalized = re.sub(r"[^a-z0-9_]", "_", normalized.lower())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or "home"


def _claim_series(claim_id: str, domain: str) -> str:
    """Series suffix = claim id with the domain-prefix stripped.

    Falls back to the id verbatim if no prefix match. Preserves
    underscores inside the series (they carry meaning — e.g.,
    xrpl_supply names an XRPL-chain supply, not two segments).
    """
    lowered = claim_id.lower()
    if lowered.startswith(domain + "_"):
        return lowered[len(domain) + 1 :]
    return lowered


def _uri_for(claim_id: str, page_path: str) -> str:
    domain = _page_to_domain(page_path)
    series = _claim_series(claim_id, domain)
    return f"xrpl.{domain}.{series}"


def _classify(claim: dict) -> tuple[str, str, str]:
    """Return (status, reason, layer3_source_label).

    `layer3_source` in CLAIMS.yaml often carries an inline comment
    (YAML preserves the value up to the first `#`), so exact matching
    works. Values we haven't classified fall to 'unknown'/yellow-
    equivalent — deliberately conservative so an unclassified claim
    is never treated as sellable-green by mistake.
    """
    raw = claim.get("layer3_source") or ""
    src = str(raw).strip()
    status = _LAYER3_SOURCE_TIER.get(src)
    if status is None:
        return ("unknown", _TIER_REASON["unknown"], src)
    return (status, _TIER_REASON[status], src)


def _read_claims_yaml() -> dict:
    """Read CLAIMS.yaml with mtime-based caching.

    Cache invalidates when the file changes on disk. Thread-safe.
    """
    try:
        mtime = os.path.getmtime(CLAIMS_YAML_PATH)
    except OSError:
        return {}
    with _LOCK:
        if _CACHE["raw"] is not None and _CACHE["mtime"] == mtime:
            return _CACHE["raw"]
        with open(CLAIMS_YAML_PATH, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        _CACHE["raw"] = data
        _CACHE["mtime"] = mtime
        _CACHE["index"] = None
        _CACHE["by_uri"] = None
        return data


def _build_index() -> tuple[list[dict], dict[str, dict]]:
    """Materialize the per-claim index once per CLAIMS.yaml change.

    Each entry carries: uri, id, page_path, domain, label, status,
    reason, layer3_source, methodology_url, data_paths, risk_note,
    behavior, layer2_rules, layer3_source_raw.

    Deduplicates on URI (a claim id that collides after prefix-
    stripping keeps the first occurrence — CLAIMS.yaml doesn't
    currently produce collisions, but the guard is cheap).
    """
    raw = _read_claims_yaml()
    with _LOCK:
        if _CACHE["index"] is not None and _CACHE["by_uri"] is not None:
            return _CACHE["index"], _CACHE["by_uri"]

    entries: list[dict] = []
    by_uri: dict[str, dict] = {}
    pages = (raw.get("pages") or {}) if isinstance(raw, dict) else {}
    for page_path, block in pages.items():
        if not isinstance(block, dict):
            continue
        template = block.get("template")
        route = block.get("route")
        domain = _page_to_domain(page_path)
        for claim in block.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            claim_id = claim.get("id")
            if not isinstance(claim_id, str):
                continue
            uri = _uri_for(claim_id, page_path)
            if uri in by_uri:
                continue
            status, reason, layer3 = _classify(claim)
            entry = {
                "uri": uri,
                "id": claim_id,
                "page_path": page_path,
                "page_template": template,
                "page_route": route,
                "domain": domain,
                "label": claim.get("label") or "",
                "status": status,
                "reason": reason,
                "layer3_source": layer3,
                "behavior": claim.get("behavior"),
                "layer2_rules": claim.get("layer2_rules") or [],
                "data_paths": claim.get("data_paths") or [],
                "risk_note": (claim.get("risk_note") or "").strip() or None,
                "note": (claim.get("note") or "").strip() or None,
                "finalized_window": bool(claim.get("finalized_window")),
                "needs_trace": bool(claim.get("needs-trace")),
            }
            entries.append(entry)
            by_uri[uri] = entry

    entries.sort(key=lambda e: (e["domain"], e["uri"]))
    with _LOCK:
        _CACHE["index"] = entries
        _CACHE["by_uri"] = by_uri
    return entries, by_uri


def is_valid_uri(uri: str) -> bool:
    return bool(_URI_RE.match(uri))


def all_claims() -> list[dict]:
    entries, _ = _build_index()
    return entries


def by_domain() -> dict[str, list[dict]]:
    """Group claims by domain for the index page."""
    entries = all_claims()
    grouped: dict[str, list[dict]] = {}
    for e in entries:
        grouped.setdefault(e["domain"], []).append(e)
    return grouped


def get_claim(uri: str) -> dict | None:
    _, by_uri = _build_index()
    return by_uri.get(uri)


def status_totals() -> dict[str, int]:
    totals: dict[str, int] = {"green": 0, "yellow": 0, "red": 0, "unknown": 0}
    for e in all_claims():
        totals[e["status"]] = totals.get(e["status"], 0) + 1
    return totals


def claim_json(entry: dict, site_url: str, methodology_url: str) -> dict:
    """Build the per-claim JSON response.

    Shape mirrors the ProofAnnotationEnvelope pattern so agents get a
    consistent surface across all machine-readable endpoints on the
    site. `proof.source = 'claims_manifest'` names the manifest itself
    as the datum's source — the classification is a property of the
    manifest entry, not of the underlying live value.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "data": {
            "uri": entry["uri"],
            "claim_id": entry["id"],
            "page": {
                "path": entry["page_path"],
                "url": f"{site_url}{entry['page_path']}" if entry["page_path"].startswith("/") else None,
            },
            "label": entry["label"],
            "status": entry["status"],
            "status_reason": entry["reason"],
            "sovereignty": {
                "layer3_source": entry["layer3_source"],
                "tier": entry["status"],
            },
            "behavior": entry["behavior"],
            "layer2_rules": entry["layer2_rules"],
            "data_paths": entry["data_paths"],
            "risk_note": entry["risk_note"],
            "note": entry["note"],
            "finalized_window": entry["finalized_window"],
            "needs_trace": entry["needs_trace"],
            "verification": {
                "manifest_repo": (
                    "https://github.com/Enkryptedx/xrpldashboard/blob/main/CLAIMS.yaml"
                ),
                "walker_health": f"{site_url}/walker_health",
                "signed_snapshot_chain": f"{site_url}/.well-known/snapshots/chain.json",
            },
        },
        "proof": {
            "source": "claims_manifest",
            "as_of": now,
            "freshness_contract": "recomputed from CLAIMS.yaml on file change (mtime-cached)",
            "methodology_url": methodology_url,
            "claims_ref": "xrpl.claims.uri_scheme",
            "honest_partial": False,
        },
    }


def index_json(site_url: str, methodology_url: str) -> dict:
    entries = all_claims()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    totals = status_totals()
    return {
        "data": {
            "count": len(entries),
            "status_totals": totals,
            "uri_scheme": "xrpl.<domain>.<series>",
            "uri_scheme_stability": (
                "URIs are permanent once agents cite them. Additions "
                "are additive; existing URIs never change or delete."
            ),
            "claims": [
                {
                    "uri": e["uri"],
                    "label": e["label"],
                    "status": e["status"],
                    "page": e["page_path"],
                    "url": f"{site_url}/claims/{e['uri']}",
                }
                for e in entries
            ],
        },
        "proof": {
            "source": "claims_manifest",
            "as_of": now,
            "freshness_contract": "recomputed from CLAIMS.yaml on file change (mtime-cached)",
            "methodology_url": methodology_url,
            "claims_ref": "xrpl.claims.index",
            "honest_partial": False,
        },
    }

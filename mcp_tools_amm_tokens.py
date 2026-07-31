"""Day 4 AMM + token attestation tool batch for the xrpldashboard MCP server.

Six tools, in order of what they exercise:

  1. `get_amm_pool(amm_account)`  — one row from the amm_ranked_pools
                                     snapshot (curated by rank_amms_walker
                                     +amm_tvl_recorder). Raises when the
                                     account isn't found — absence of a
                                     ranked pool IS the signal.

  2. `get_amm_top_by_tvl(limit)`  — the top-N ranked pools by tvl_usd from
                                     the same snapshot. Same source, same
                                     freshness contract.

  3. `get_token_attestation(currency, issuer)`
                                   — attestation tier for one (currency,
                                     issuer) pair, sourced from the
                                     account_labels table populated by
                                     verify_toml_accounts + enrich_token_names.
                                     FIRST tool that names a THIRD PARTY
                                     (the issuer) — emits `dispute_contact_url`
                                     in the data payload so an issuer who
                                     disagrees with the label has a first-
                                     party channel to correct it. Sibling
                                     of the on-page attestation-dispute
                                     footer link (app.py:CONTACT_PURPOSES
                                     'attestation-dispute').

  4. `get_rwa_families()`         — every rwa_family row with an attributed
                                     pool count. Also third-party-naming;
                                     also carries `dispute_contact_url`.

  5. `get_rwa_pools()`            — every rwa_pool_attribution row with
                                     provenance + TVL (joined against
                                     amm_ranked_pools). Third-party-naming;
                                     carries `dispute_contact_url`.

  6. `get_mpt_snapshot()`         — the MPT snapshot dict (mpt_snapshot
                                     writer + mpt_holders_refresh). Non-
                                     third-party-naming — reports what
                                     the ledger contains without label
                                     opinions.

Every tool routes its return through `mcp_server.wrap_envelope(...)` —
no bypass. On success each tool calls `mcp_server.stamp_tool_call(tool_name)`
for the Q1 heartbeat-gap watermark. Failure paths intentionally leave the
watermark stale — absence of freshness IS the signal, same rule the Day 2/3
batches established.

Third-party-naming discipline (tools 3-5):
    Any tool whose data payload includes a NAME the site has assigned to
    a third party (issuer attestation, RWA family attribution) MUST carry
    a `dispute_contact_url` in the data payload. The URL routes to the
    same /contact form the human surfaces use, with purpose pre-selected.
    An agent that reads a label and wants to relay a correction has an
    explicit first-party channel — same rule the /tokens footer applies
    to human readers (app.py CONTACT_PURPOSES).
"""
from __future__ import annotations

import time
from typing import Any, Optional

import mcp_server

# Dispute-contact URL — same target the /tokens per-row + /rwa page-level
# footer links use. purpose=attestation-dispute is defined in
# app.py:CONTACT_PURPOSES; if that mapping is renamed the link will 404
# and the on-page footer will break in the same way, so a single fix
# closes both surfaces.
DISPUTE_CONTACT_URL = (
    "https://xrpldashboard.com/contact?purpose=attestation-dispute"
)


def _iso_utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ts_to_iso(ts_unix: Optional[int]) -> Optional[str]:
    if ts_unix is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts_unix)))


def _decimal_str(v: Any) -> Optional[str]:
    """Serialize NUMERIC/Decimal as string so JSON round-trip keeps
    precision — same discipline as mcp_tools_value_flows._decimal_or_none."""
    if v is None:
        return None
    return str(v)


# ─────────────────────────────────────────────────────────────────────
# 1. get_amm_pool
# ─────────────────────────────────────────────────────────────────────

def _serialize_amm_row(row: dict) -> dict:
    """Row layout mirrors db.read_amm_ranked_pools: dict with fields
    amm_account / pair / fee_pct / fee_raw / amount_a / amount_b /
    asset_a / asset_b / tvl_usd / tvl_status / kind / _snapshot_ts.
    Decimal fields serialized as strings."""
    return {
        "amm_account": row.get("amm_account"),
        "pair": row.get("pair"),
        "fee_pct": _decimal_str(row.get("fee_pct")),
        "fee_raw": row.get("fee_raw"),
        "amount_a": _decimal_str(row.get("amount_a")),
        "amount_b": _decimal_str(row.get("amount_b")),
        "asset_a": row.get("asset_a"),
        "asset_b": row.get("asset_b"),
        "tvl_usd": _decimal_str(row.get("tvl_usd")),
        "tvl_status": row.get("tvl_status"),
        "kind": row.get("kind"),
        "snapshot_ts_iso": _ts_to_iso(row.get("_snapshot_ts")),
    }


def tool_get_amm_pool(amm_account: str) -> dict:
    """Return the single ranked-pool row for one AMM account. Same source
    /pools reads. Raises when the account is absent from the snapshot —
    an unranked AMM (below thresholds, or newer than the last snapshot)
    is a real signal, not stubbed."""
    import db
    if not db.pg_available():
        raise RuntimeError("get_amm_pool: DATABASE_URL not configured")
    if not amm_account or not isinstance(amm_account, str):
        raise RuntimeError("get_amm_pool: amm_account must be a non-empty string")
    rows = db.read_amm_ranked_pools()
    match = next((r for r in rows if r.get("amm_account") == amm_account), None)
    if match is None:
        raise RuntimeError(
            f"get_amm_pool: {amm_account!r} not present in amm_ranked_pools "
            f"snapshot (unranked, below thresholds, or newer than last cycle)"
        )
    data = _serialize_amm_row(match)
    envelope = mcp_server.wrap_envelope(
        data,
        source="rank_amms_walker+amm_tvl_recorder",
        as_of=_iso_utc_now(),
        freshness_contract="≤ 30min",
        methodology_url="https://xrpldashboard.com/methodology#amm",
        claims_ref="amm_pool_by_address",
    )
    mcp_server.stamp_tool_call("get_amm_pool")
    return envelope


# ─────────────────────────────────────────────────────────────────────
# 2. get_amm_top_by_tvl
# ─────────────────────────────────────────────────────────────────────

def tool_get_amm_top_by_tvl(limit: int = 10) -> dict:
    """Return the top-N ranked AMM pools by tvl_usd from the same snapshot
    /pools reads. Rows with NULL tvl_usd sort last."""
    import db
    if not db.pg_available():
        raise RuntimeError("get_amm_top_by_tvl: DATABASE_URL not configured")
    limit = max(1, min(int(limit), 100))
    rows = db.read_amm_ranked_pools()
    def _tvl_sort_key(r):
        tvl = r.get("tvl_usd")
        if tvl is None:
            return (1, 0.0)  # NULL last
        try:
            return (0, -float(tvl))
        except (TypeError, ValueError):
            return (1, 0.0)
    ranked = sorted(rows, key=_tvl_sort_key)[:limit]
    data = {
        "pools": [_serialize_amm_row(r) for r in ranked],
        "count": len(ranked),
        "limit_requested": limit,
        "snapshot_ts_iso": _ts_to_iso(db.read_amm_snapshot_ts()),
    }
    envelope = mcp_server.wrap_envelope(
        data,
        source="rank_amms_walker+amm_tvl_recorder",
        as_of=_iso_utc_now(),
        freshness_contract="≤ 30min",
        methodology_url="https://xrpldashboard.com/methodology#amm",
        claims_ref="amm_top_by_tvl_rank",
    )
    mcp_server.stamp_tool_call("get_amm_top_by_tvl")
    return envelope


# ─────────────────────────────────────────────────────────────────────
# 3. get_token_attestation
# ─────────────────────────────────────────────────────────────────────

def _classify_attestation_tier(label: Optional[dict]) -> tuple:
    """Return (tier, reason) from an account_labels row.

    Tier ladder mirrors app.py:2589-2598 (the /tokens per-row rendering
    rule Charlie set in v3 §7):
      * `verified`       — source starts with 'toml' (verify_toml_accounts
                           walker successfully fetched + parsed the
                           issuer's .toml declaration).
      * `self-described` — labeled with a non-toml source (derived:xls-15,
                           enrich_token_names, etc.); the issuer has been
                           named by an evidence trail but not TOML-verified.
      * `null`           — no account_labels row exists for the issuer.
                           Absence IS the signal — the label is not
                           fabricated, and the field is explicitly null
                           in the response.
    """
    if label is None:
        return (None, "no account_labels row for issuer")
    source = label.get("source") or ""
    if source.startswith("toml"):
        return ("verified", None)
    if label.get("name"):
        return ("self-described", None)
    return (None, "account_labels row present but no name field")


def tool_get_token_attestation(currency: str, issuer: str) -> dict:
    """Return the attestation tier for one (currency, issuer) pair.

    First MCP tool that names a third party in its data payload — carries
    `dispute_contact_url` so any issuer disagreeing with the label has a
    first-party correction channel (same target the /tokens footer link
    uses; single-source via DISPUTE_CONTACT_URL constant).

    Note on currency: retained in the response payload for round-trip
    symmetry with what the agent asked. The classifier keys off issuer
    alone — verify_toml_accounts writes one label per issuer address,
    not one per (currency, issuer) pair, because TOML declaration is
    account-scoped."""
    import db
    if not db.pg_available():
        raise RuntimeError("get_token_attestation: DATABASE_URL not configured")
    if not currency or not isinstance(currency, str):
        raise RuntimeError("get_token_attestation: currency must be a non-empty string")
    if not issuer or not isinstance(issuer, str):
        raise RuntimeError("get_token_attestation: issuer must be a non-empty string")
    labels = db.read_account_labels([issuer])
    label = labels.get(issuer)
    tier, reason = _classify_attestation_tier(label)
    extra = (label or {}).get("extra") or {}
    domain = extra.get("domain") if isinstance(extra, dict) else None
    data = {
        "currency": currency,
        "issuer": issuer,
        "attestation_tier": tier,
        "attestation_tier_reason": reason,
        "issuer_name": (label or {}).get("name"),
        "issuer_category": (label or {}).get("category"),
        "issuer_label_source": (label or {}).get("source"),
        "issuer_domain": domain,
        "dispute_contact_url": DISPUTE_CONTACT_URL,
    }
    envelope = mcp_server.wrap_envelope(
        data,
        source="verify_toml_accounts+enrich_token_names",
        as_of=_iso_utc_now(),
        freshness_contract="daily",
        methodology_url="https://xrpldashboard.com/methodology#token-attestation",
        claims_ref="token_attestation_status",
    )
    mcp_server.stamp_tool_call("get_token_attestation")
    return envelope


# ─────────────────────────────────────────────────────────────────────
# 4. get_rwa_families
# ─────────────────────────────────────────────────────────────────────

def tool_get_rwa_families() -> dict:
    """Return every rwa_family row + attributed pool count. Third-party-
    naming; carries `dispute_contact_url` for the same reason as
    get_token_attestation."""
    import db
    if not db.pg_available():
        raise RuntimeError("get_rwa_families: DATABASE_URL not configured")
    families = db.read_rwa_families()
    if families is None:
        raise RuntimeError("get_rwa_families: PG read returned None")
    data = {
        "families": families,
        "count": len(families),
        "dispute_contact_url": DISPUTE_CONTACT_URL,
    }
    envelope = mcp_server.wrap_envelope(
        data,
        source="rwa_family+rwa_pool_attribution",
        as_of=_iso_utc_now(),
        freshness_contract="daily",
        methodology_url="https://xrpldashboard.com/methodology#rwa",
        claims_ref="rwa_families_count",
    )
    mcp_server.stamp_tool_call("get_rwa_families")
    return envelope


# ─────────────────────────────────────────────────────────────────────
# 5. get_rwa_pools
# ─────────────────────────────────────────────────────────────────────

def tool_get_rwa_pools() -> dict:
    """Return every attributed RWA pool with provenance + TVL (joined
    against amm_ranked_pools). Third-party-naming; carries
    `dispute_contact_url`."""
    import db
    if not db.pg_available():
        raise RuntimeError("get_rwa_pools: DATABASE_URL not configured")
    pools = db.read_rwa_pools_attributed()
    if pools is None:
        raise RuntimeError("get_rwa_pools: PG read returned None")
    serialized = [
        {
            "pool_address": p["pool_address"],
            "family_slug": p["family_slug"],
            "confidence": p["confidence"],
            "provenance": p["provenance"],
            "notes": p.get("notes"),
            "tvl_usd": _decimal_str(p.get("tvl_usd")),
            "tvl_status": p.get("tvl_status"),
        }
        for p in pools
    ]
    data = {
        "pools": serialized,
        "count": len(serialized),
        "dispute_contact_url": DISPUTE_CONTACT_URL,
    }
    envelope = mcp_server.wrap_envelope(
        data,
        source="rwa_pool_attribution+amm_ranked_pools",
        as_of=_iso_utc_now(),
        freshness_contract="≤ 30min",
        methodology_url="https://xrpldashboard.com/methodology#rwa",
        claims_ref="rwa_pools_attributed_count",
    )
    mcp_server.stamp_tool_call("get_rwa_pools")
    return envelope


# ─────────────────────────────────────────────────────────────────────
# 6. get_mpt_snapshot
# ─────────────────────────────────────────────────────────────────────

def tool_get_mpt_snapshot() -> dict:
    """Return the MPT snapshot dict (issuances / by_class / total).

    Not third-party-naming — this tool reports what the ledger contains,
    without opinion on issuer identity. No `dispute_contact_url` in the
    payload; a mislabel of a specific MPT issuer would land on
    get_token_attestation, not here."""
    import db
    if not db.pg_available():
        raise RuntimeError("get_mpt_snapshot: DATABASE_URL not configured")
    snapshot = db.read_mpt_snapshot()
    if snapshot is None:
        raise RuntimeError(
            "get_mpt_snapshot: mpt_snapshot table empty — walker has not "
            "produced a snapshot yet"
        )
    total = snapshot.get("total")
    by_class = snapshot.get("by_class") or {}
    data = {
        "total_active": total,
        "by_class": by_class,
        "snapshot_age_seconds": snapshot.get("snapshot_age_seconds"),
        "from_postgres": bool(snapshot.get("from_postgres")),
    }
    envelope = mcp_server.wrap_envelope(
        data,
        source="mpt_snapshot+mpt_holders_refresh",
        as_of=_iso_utc_now(),
        freshness_contract="daily",
        methodology_url="https://xrpldashboard.com/methodology#mpt",
        claims_ref="mpt_active_count",
    )
    mcp_server.stamp_tool_call("get_mpt_snapshot")
    return envelope

"""Flask app: render live XRPL AMM scan results.

Local dev:    python app.py  (binds 127.0.0.1:5001)
Production:   gunicorn app:app  (PORT from env, set by host)
"""

import hmac
import json
import math
import os
import secrets
import sqlite3
import threading
import time
from collections import Counter
from datetime import date, datetime, timezone

from flask import Flask, Response, abort, jsonify, make_response, redirect, render_template, request, send_from_directory, url_for
from flask_limiter import Limiter
from flask_smorest import Api
from werkzeug.middleware.proxy_fix import ProxyFix

from amm_scan_pools import (
    JsonRpcClient,
    XRPL_NODE,
    fetch_pool,
    fmt_money,
    fmt_num,
    scan_all_pools_cached,
)
from network_pulse import fetch_pulse_cached
from tx_type_mix import fetch_tx_type_mix, WINDOWS as TX_MIX_WINDOWS, DEFAULT_WINDOW as TX_MIX_DEFAULT_WINDOW
from xrp_price import fetch_xrp_price_cached
from cold_storage import fetch_cold_storage_cached
from escrow_supply import fetch_escrow_locked_cached
from total_supply import fetch_total_supply_cached, XRP_DESIGN_SUPPLY_FALLBACK
import amendments_state
from amendments_state import fetch_amendments_state_cached
import network_state
from network_state import fetch_network_state_cached, build_unl_diff_view
from credentials_state import get_credentials_state
from lending_amendment import fetch_lending_status_cached
from lending_data import CACHE_TTL as LENDING_CACHE_TTL
from lending_data import fetch_lending_data_cached, load_lending_snapshot
from mpt_data import fetch_mpt_data_cached, load_mpt_snapshot
from token_data import fetch_token_data_cached
from wallet_data import fetch_wallet_data_cached
from mcp_server import SERVER_VERSION as MCP_SERVER_VERSION
import geoip_state

try:
    import qrcode
    import qrcode.image.svg as _qr_svg
    from io import BytesIO as _BytesIO

    def _wallet_qr_svg(address: str) -> str:
        buf = _BytesIO()
        qrcode.make(
            f"xrpl:{address}",
            image_factory=_qr_svg.SvgPathImage,
            box_size=4,
            border=2,
        ).save(buf)
        svg = buf.getvalue().decode("utf-8")
        if svg.startswith("<?xml"):
            svg = svg[svg.index("<svg"):]
        return svg
except ImportError:
    def _wallet_qr_svg(address: str) -> str:
        return ""
from i18n import init_i18n
from flask_babel import gettext as babel_gettext
import db
import og_image
import price_oracle
from agent_tier_rate_limit import (
    AUDIT_URL_HEADER_NAME,
    AUDIT_URL_PATH,
    FLEET_BLOCK_RETRY_AFTER_SECONDS,
    agent_tier_limit_rate,
    classify_ai_crawler,
    fleet_signature,
    is_agent_tier_route,
    is_ai_crawler,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SCAN_STATE_PATH = os.path.join(HERE, "amm_scan_state.json")
STREAM_STATE_PATH = os.path.join(HERE, "xrpl_stream_state.json")
STREAM_LOG_PATH = os.path.join(HERE, "xrpl_stream.log")
SCAN_LOG_PATH = os.path.join(HERE, "amm_scan.log")
AMM_INDEX_PATH = os.path.join(HERE, "amm_index.json")
AMM_RANKED_PATH = os.path.join(HERE, "amm_ranked.json")
AMM_RANK_STATE_PATH = os.path.join(HERE, "amm_rank_state.json")
EVENTS_DB_PATH = os.path.join(HERE, "events.db")
VOLUMES_DB_PATH = os.path.join(HERE, "volumes.db")
NAMED_ACCOUNTS_PATH = os.path.join(HERE, "named_accounts.json")
TOKEN_NAMES_PATH = os.path.join(HERE, "token_names.json")
GLOSSARY_PATH = os.path.join(HERE, "glossary.json")
D1_HERO_SNAPSHOT_PATH = os.path.join(HERE, "d1_hero_snapshot.json")
AXELAR_BRIDGE_ISSUER = "rfmS3zqrQrka8wVyhXifEeyTwe8AMz2Yhw"
ISO_CONTINENT_PATH = os.path.join(HERE, "iso_country_to_continent.json")
SNAPSHOT_DIR = os.path.join(HERE, "historical_snapshots")
SIGNED_SNAPSHOTS_DIR = os.path.join(HERE, "signed_snapshots")
SNAPSHOT_PUBKEY_PEM_PATH = os.path.join(HERE, "snapshot_pubkey.pem")
SNAPSHOT_PUBKEY_FP_PATH = os.path.join(HERE, "snapshot_pubkey_fingerprint.txt")

WHALE_XRP_THRESHOLD = 100_000  # editorial display floor (100K).
# Capture is wider — see xrpl_stream.py WHALE_XRP_THRESHOLD_DROPS
# (default 50K). Display filters the wider capture set down to
# the editorial "whale" tier. See 46334fb for the rename + raise.

TAGGED_XRP_FLOOR = 100  # homepage "watchlist activity" visibility floor.
# Named accounts (exchanges, bridges, issuers) trigger the homepage
# Whale-moves card on any token-denominated tx (amount_drops IS NULL —
# IOU/MPT) OR any XRP tx >= 100 XRP. Meaningful-action floor: ~$200-300
# at current mid, one order below the 100K whale display floor and
# three orders above fee-scale dust. Keeps organic watchlist activity
# visible without letting sub-dollar transfers dominate the card.
# Homepage-only tonight; /whales predicate untouched by design (see
# scope guard on the follow-up: extending this to /whales adds rows
# to a page whose stated contract is "payments over 100,000 XRP", so
# it needs its own title/copy update in the same commit).
TAGGED_XRP_FLOOR_DROPS = TAGGED_XRP_FLOOR * 1_000_000

# Canonical production origin. Used for og:url / canonical / sitemap.xml.
# Override with SITE_URL env var if a preview deploy needs a different host.
SITE_URL = os.environ.get("SITE_URL", "https://xrpldashboard.com").rstrip("/")

# Public pages worth surfacing to crawlers. Order = sitemap order.
# Detail pages (/wallet/<addr>, /token/<cur>/<iss>) are excluded — the
# universe of valid addresses is unbounded and we don't want crawlers
# burning quota on every wallet they can guess.
PUBLIC_ROUTES = [
    "/",
    "/whales",
    "/tokens",
    "/pools",
    "/mpts",
    "/nfts",
    "/rlusd",
    "/cold-storage",
    "/price-data",
    "/health",
    "/about",
    "/institutional",
    "/security",
    "/subprocessors",
]


def ttl_cache(seconds=60):
    """Per-process TTL cache for hot helpers that re-fetch identical results
    within seconds. Sized to a single value per arg-tuple — fine for the
    no-arg snapshot loaders below. Race on simultaneous miss is benign:
    one wasted compute, last writer wins, no corruption. Returned values
    are shared by reference, so decorated functions must not return
    objects that callers mutate in place."""
    def decorator(fn):
        store = {}
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.time()
            entry = store.get(key)
            if entry is not None and now - entry[1] <= seconds:
                return entry[0]
            value = fn(*args, **kwargs)
            store[key] = (value, now)
            return value
        wrapper.__wrapped__ = fn
        return wrapper
    return decorator


@ttl_cache(seconds=60)
def _cached_db_mpt_snapshot():
    """60s cache wrapper around db.read_mpt_snapshot(). The snapshot is
    refreshed daily by the mpt_snapshot worker; rereading the multi-MB
    JSONB blob on every /mpts request was the dominant per-request
    allocation pre-cache."""
    return db.read_mpt_snapshot()


app = Flask(__name__)
# Flask needs a server-side secret for any feature that signs cookies or
# tokens. We don't use sessions today, but setting one defensively keeps
# future use safe. Prefer FLASK_SECRET_KEY from env (stable across deploys);
# fall back to a per-process random so dev/local always has *something*.
app.secret_key = (
    os.environ.get("FLASK_SECRET_KEY") or secrets.token_urlsafe(48)
)
_VISITOR_HASH_KEY = app.secret_key.encode("utf-8")

# Cloudflare → Render → Flask is a two-proxy chain: Cloudflare puts the
# visitor IP at the head of X-Forwarded-For, Render's edge appends its own
# hop. x_for=2 strips both trusted hops so request.remote_addr is the
# real client IP, not a shared proxy egress (which would collapse every
# visitor into one rate-limit bucket).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=2, x_proto=1)
app.jinja_env.globals.update(fmt_money=fmt_money, fmt_num=fmt_num)


def _unix_utc(ts):
    """Jinja filter: format a unix-seconds timestamp as `YYYY-MM-DD HH:MM UTC`.
    Empty / None / Jinja Undefined → empty string (matches the pre-filter
    template default so a caller that forgets to populate `now` still
    renders the surrounding prose). Non-numeric input that survives the
    int() cast falls back to str(ts) so the template never crashes.
    Added 2026-08-30 to close the raw-unix-in-prose leak on /learn and
    /coverage caught in the site audit."""
    try:
        ts_int = int(ts)
    except Exception:
        # None, Jinja Undefined, empty string, or a non-numeric string all
        # land here and render as empty prose. Broad except because
        # jinja2.Undefined raises UndefinedError (not TypeError) from int().
        return ""
    if ts_int == 0:
        return ""
    return datetime.fromtimestamp(ts_int, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


app.jinja_env.filters["unix_utc"] = _unix_utc


def _entity_encode(text):
    """Render a string as HTML numeric character entities. Every char becomes
    &#N; where N is its Unicode codepoint. Browsers decode entities natively,
    so the visible text is identical to the source string. Naive regex-based
    email scrapers that look for `[\\w.-]+@[\\w.-]+` in raw HTML miss it —
    they don't decode entities before pattern-matching. Intended use:
    obfuscate exposed email addresses in prose to break the harvesting tier
    of email-scraper bots. Not defense against sophisticated scrapers that
    parse HTML entities; those are a different threat model. Marked Markup
    so Jinja does not re-escape the ampersands."""
    from markupsafe import Markup
    if text is None:
        return Markup("")
    return Markup("".join(f"&#{ord(c)};" for c in str(text)))


app.jinja_env.filters["entity_encode"] = _entity_encode

# Wire Flask-Babel: cookie-based locale, /lang/<code> switcher, template
# helpers (language_list, current_locale, is_rtl). Strings still need _()
# wrapping per-template — this just makes the machinery available.
init_i18n(app)

# In-memory limiter — Render free tier is single-process / single-replica,
# so memory storage is correct. Counts reset on deploy (intentional: we're
# stopping curl loops, not enforcing daily quotas). If we ever scale to
# multiple replicas, swap storage_uri to Redis or a Neon-backed store.
# No global default — limits are applied explicitly per-route so health
# checks and HTML pages stay unthrottled.
def _client_ip_key():
    """Rate-limit key: prefer Cloudflare's CF-Connecting-IP (authoritative
    client IP, set by CF and not client-modifiable), fall back to
    request.remote_addr (ProxyFix-resolved). Defense in depth — even if
    the X-Forwarded-For chain changes shape, the per-IP bucket stays correct."""
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    return request.remote_addr or "anonymous"


def _client_ip():
    """Analytics client IP: prefer Cloudflare's CF-Connecting-IP (authoritative,
    not client-modifiable), fall back to ProxyFix-resolved request.remote_addr.
    Defense in depth — if the X-Forwarded-For hop count ever changes (Render
    infra shift, added edge), per-visitor dispersion stays correct instead of
    silently collapsing to a Cloudflare egress pool. Returns "" (not "anonymous")
    so downstream _visitor_hash / _ip_day_hash keep their empty-IP semantics."""
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    return request.remote_addr or ""


limiter = Limiter(
    app=app,
    key_func=_client_ip_key,
    storage_uri="memory://",
)


@app.context_processor
def inject_site_url():
    """Make {{ site_url }} available to every template, primarily for the
    shared _head_meta.html partial that builds canonical / og:url."""
    return {"site_url": SITE_URL}


# Manually curated — bump on every /regulation copy update. The page's
# freshness chip AND the homepage banner both read this via the
# inject_regulation_freshness context processor. Codified in CLAIMS.yaml
# claim regulation_freshness_chip.
LAST_VERIFIED_REGULATION = "2026-08-17"

# CLARITY-window homepage banner self-expires on this date (Senate
# recess-return day). After this, the strip disappears from any page
# that reads regulation_banner_live even if no one pulls it manually.
# Manual pull earlier is still preferred if the bill's status resolves
# before recess ends — the guard is defense-in-depth, not the plan.
REGULATION_BANNER_EXPIRES = "2026-09-14"

# Single source of truth for the agent-tier discovery layer freshness
# stamp. Read by three surfaces that must move together: _LLMS_TXT
# (served at /llms.txt), _AGENTS_JSON (served at /.well-known/agents.json),
# and templates/methodology.html §"For AI agents". Bump on every
# agent-tier surface change; three surfaces refresh from one edit.
# Codified in CLAIMS.yaml (agents_json_status_booleans,
# methodology_for_ai_agents_envelope_matches_agents_json siblings).
LAST_VERIFIED_AGENT_TIER_METHODOLOGY = "2026-08-29"  # identity-fix snack batch — x-mcp-tools sharpen + disambig boilerplate


@app.context_processor
def inject_regulation_freshness():
    """Expose the /regulation last-verified date + a banner-live flag
    to every template — so the homepage strip can show the same date
    the /regulation page shows, without hard-coding it in a second
    place (would violate the regulation_freshness_chip CLAIMS entry)."""
    try:
        expiry = datetime.strptime(REGULATION_BANNER_EXPIRES, "%Y-%m-%d").date()
        banner_live = date.today() <= expiry
    except Exception:
        banner_live = False
    return {
        "regulation_last_verified": LAST_VERIFIED_REGULATION,
        "regulation_banner_live": banner_live,
    }


@app.context_processor
def inject_agent_tier_freshness():
    """Expose LAST_VERIFIED_AGENT_TIER_METHODOLOGY to every template
    so the /methodology "For AI agents" section reads the same date
    that llms.txt and agents.json read. Single source of truth per
    the Day 1 commit-D constraint — bump the constant, three surfaces
    refresh from one edit."""
    return {
        "agent_tier_methodology_last_verified": LAST_VERIFIED_AGENT_TIER_METHODOLOGY,
    }


# ─────────────────────────────────────────────────────────────────────
# Agent Tier — OpenAPI decoration (Day 5, 2026-07-31)
#
# flask-smorest wires /openapi.json + Swagger UI at /docs. The spec
# documents the LIVE free surface only: the discovery and well-known
# endpoints that already exist on main, plus a documented pointer to
# the 15 MCP tools (envelope schema is the standard response wrapper
# every future JSON payload will carry — MCP tools today, HTTP
# read-only API when it lands).
#
# Fences (per Day 5 commit contract):
#   • Zero payment surface — no endpoints touch money, keys, or gating.
#   • Zero new HTTP routes — this is decoration of what already ships.
#   • parked/api-v1-scaffold stays parked; unpark-time rebase carries
#     these schemas forward. See MEMORY.md for the drift note.
#   • Error handlers untouched — smorest Api is used only to serve
#     /openapi.json + /docs and to register spec metadata; the
#     existing @app.errorhandler(404)/(500) HTML handlers stay dominant
#     because no smorest Blueprint is registered here.
#
# Freshness contract: LAST_VERIFIED_AGENT_TIER_METHODOLOGY governs
# this surface too — bump it whenever the OpenAPI spec changes shape.
# ─────────────────────────────────────────────────────────────────────

# Static MCP tool inventory — mirrors mcp_server._register_tools() and
# the tool_* function names in mcp_tools_*.py. Kept as data (not a live
# import) so the Flask app doesn't drag the FastMCP dependency in at
# web-app startup. Test tests/test_openapi.py::test_mcp_inventory_count
# holds this list in sync with the actual registered-tool count.
AGENT_TIER_MCP_INVENTORY = [
    {"name": "get_ledger_stats",      "source": "local_rippled",             "freshness": "≤ 5min",         "batch": "ledger-primitives"},
    {"name": "get_amendment_status",  "source": "local_rippled",             "freshness": "≤ 30min",        "batch": "ledger-primitives"},
    {"name": "get_unl_status",        "source": "local_rippled",             "freshness": "≤ 30min",        "batch": "ledger-primitives"},
    {"name": "get_whale_events",      "source": "local_rippled_stream_capture", "freshness": "≤ 5min",      "batch": "value-flows"},
    {"name": "get_whale_watchlist",   "source": "local_rippled_stream_capture", "freshness": "≤ 5min",      "batch": "value-flows"},
    {"name": "get_rlusd_supply",      "source": "neon_postgres",             "freshness": "≤ 5min",         "batch": "value-flows"},
    {"name": "get_rlusd_flow_24h",    "source": "neon_postgres",             "freshness": "finalized_only", "batch": "value-flows"},
    {"name": "get_amm_pool",          "source": "neon_postgres",             "freshness": "daily",          "batch": "amm-tokens"},
    {"name": "get_amm_top_by_tvl",    "source": "neon_postgres",             "freshness": "daily",          "batch": "amm-tokens"},
    {"name": "get_token_attestation", "source": "neon_postgres",             "freshness": "daily",          "batch": "amm-tokens"},
    {"name": "get_rwa_families",      "source": "neon_postgres",             "freshness": "daily",          "batch": "amm-tokens"},
    {"name": "get_rwa_pools",         "source": "neon_postgres",             "freshness": "daily",          "batch": "amm-tokens"},
    {"name": "get_mpt_snapshot",      "source": "neon_postgres",             "freshness": "daily",          "batch": "amm-tokens"},
    {"name": "get_signed_snapshot",   "source": "signed_snapshot_walker",     "freshness": "daily",          "batch": "signed-snapshot"},
    {"name": "verify_snapshot_signature", "source": "signed_snapshot.verify_envelope+pinned_pubkey", "freshness": "≤ 5min", "batch": "signed-snapshot"},
]


def _build_enriched_mcp_inventory():
    """Enrich AGENT_TIER_MCP_INVENTORY with FastMCP-derived `inputSchema`
    and `description` for each tool. Cross-references the static
    inventory rows (source / freshness / batch — properties FastMCP does
    not know about) with live tool metadata FastMCP derives from Python
    signatures and docstrings, so the OpenAPI spec surfaces one row per
    tool with both proof-envelope framing AND callable-shape schema.

    Cross-import happened cleanly on 2026-08-28 in the same venv the web
    app runs in (mcp_server already imported at line 48; tool modules
    are deferred-loaded inside _register_tools so bare import stays
    cheap). Extraction here calls _register_tools on a bare FastMCP
    probe and takes ~1s at module load — paid once, cached in module
    scope.

    Falls back to the sparse AGENT_TIER_MCP_INVENTORY on any failure
    (import fight, FastMCP shape change, tool module drift). Logs the
    failure so /openapi.json still ships rather than 500ing at boot,
    and the drift-guard tests in test_openapi.py catch the missing
    schemas on the next CI run.
    """
    try:
        import asyncio
        from mcp.server.fastmcp import FastMCP
        import mcp_server as _mcp_server_mod

        _probe = FastMCP("_openapi_inventory_probe")
        _mcp_server_mod._register_tools(_probe)
        _live = asyncio.run(_probe.list_tools())
        _live_by_name = {t.name: t for t in _live}

        _enriched = []
        for stub in AGENT_TIER_MCP_INVENTORY:
            tool = _live_by_name.get(stub["name"])
            if tool is None:
                _enriched.append(dict(stub))
                continue
            _enriched.append({
                **stub,
                "description": " ".join((tool.description or "").split()),
                "inputSchema": tool.inputSchema or {
                    "type": "object", "properties": {},
                },
            })
        return _enriched
    except Exception as _e:  # noqa: BLE001 — boot-time defense in depth
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "MCP inventory enrichment failed; falling back to sparse "
            "AGENT_TIER_MCP_INVENTORY. Error: %s: %s",
            type(_e).__name__, _e,
        )
        return list(AGENT_TIER_MCP_INVENTORY)


AGENT_TIER_MCP_INVENTORY_ENRICHED = _build_enriched_mcp_inventory()


app.config["API_TITLE"] = "xrpldashboard — Agent Tier (read-only)"
app.config["API_VERSION"] = "v1"
app.config["OPENAPI_VERSION"] = "3.0.3"
app.config["OPENAPI_URL_PREFIX"] = "/"
app.config["OPENAPI_JSON_PATH"] = "openapi.json"
app.config["OPENAPI_SWAGGER_UI_PATH"] = "docs"
app.config["OPENAPI_SWAGGER_UI_URL"] = "https://cdn.jsdelivr.net/npm/swagger-ui-dist/"
# Keep smorest's ETag machinery off — we don't use it and it'd add a
# response-header surface we haven't audited.
app.config["API_ETAG_DISABLED"] = True

# API_SPEC_OPTIONS is passed through to apispec.APISpec as **kwargs.
# Info fields, externalDocs, servers, and tags live here because
# apispec builds the info block at construction time, not by
# post-hoc mutation. Schema components and path items DO get added
# post-hoc via api.spec.components.schema()/api.spec.path().
app.config["API_SPEC_OPTIONS"] = {
    "info": {
        "description": (
            "Machine-readable index of xrpldashboard's LIVE read-only "
            "surface. Every endpoint listed here is publicly served "
            "today; every response either is or will be wrapped in the "
            "ProofAnnotationEnvelope schema (MCP tools today; HTTP "
            "read-only API when it lands). Free for humans and "
            "identified agents at reasonable volume — no accounts, no "
            "API keys, no payment rails. See /methodology#for-ai-agents "
            "for the full contract, and docs/AGENT_TIER_DESIGN.md in "
            "the source repo for the design behind this surface. "
            "Independent project — not affiliated with Ripple, the XRP "
            "Ledger Foundation, any exchange, or with xrpdashboard.com "
            "(note: missing 'L' — that's a separate XRP portfolio product)."
        ),
        "contact": {
            "name": "xrpldashboard",
            "url": f"{SITE_URL}/contact",
            "email": "contact@xrpldashboard.com",
        },
        "license": {
            "name": "MIT (source); data derived from public XRPL and Ethereum ledgers",
            "url": "https://github.com/Enkryptedx/xrpldashboard/blob/main/LICENSE",
        },
        "x-agent-tier-freshness": {
            "last_verified": LAST_VERIFIED_AGENT_TIER_METHODOLOGY,
            "source_of_truth": "app.py:LAST_VERIFIED_AGENT_TIER_METHODOLOGY",
            "note": (
                "Bump the constant whenever the OpenAPI spec, llms.txt, "
                "or agents.json shape changes. Three surfaces refresh "
                "from one edit."
            ),
        },
        "x-mcp-tools": {
            "server_name": "xrpldashboard-mcp",
            "server_version": MCP_SERVER_VERSION,
            "protocol": "MCP (Model Context Protocol) — stdio + streamable HTTP",
            "documentation": f"{SITE_URL}/methodology#for-ai-agents",
            "design_doc": (
                "https://github.com/Enkryptedx/xrpldashboard/blob/main/"
                "docs/AGENT_TIER_DESIGN.md"
            ),
            "envelope_schema_ref": "#/components/schemas/ProofAnnotationEnvelope",
            "tool_count": len(AGENT_TIER_MCP_INVENTORY_ENRICHED),
            "tools": AGENT_TIER_MCP_INVENTORY_ENRICHED,
            "status": (
                "Public-beta live at https://mcp.xrpldashboard.com/mcp "
                "through 2026-09 — server publicly reachable (streamable "
                "HTTP, MCP protocol 2025-06-18, 15 read-only tools, no "
                "auth, 600 tool calls/hour/session enforced). Backed by "
                "our own rippled node. Listed in the Anthropic MCP "
                "Registry (server id com.xrpldashboard/xrpldashboard-mcp, "
                "DNS-verified namespace, listed 2026-08-05) and Smithery "
                "(listed 2026-08-05). See mcp_server.py + mcp_tools_*.py "
                "in the source repo for implementation."
            ),
        },
    },
    "externalDocs": {
        "description": "Full methodology, per-surface freshness contracts, and the four-layer truth audit design.",
        "url": f"{SITE_URL}/methodology",
    },
    "servers": [
        {"url": SITE_URL, "description": "Production"},
    ],
    "tags": [
        {"name": "discovery", "description": "Discovery-layer surfaces — how agents find this site's machine-readable contracts."},
        {"name": "signed-snapshots", "description": "Tamper-evident daily Ed25519-signed database snapshots. Verify locally against the pinned public key."},
        {"name": "documentation", "description": "OpenAPI JSON and Swagger UI for this spec itself."},
    ],
}

api = Api(app)

# Envelope schema — the standard response wrapper every MCP tool
# returns, and the shape every future HTTP JSON endpoint will follow.
# Sourced from mcp_server.wrap_envelope() and matched by the
# proof-annotation contract in templates/methodology.html.
api.spec.components.schema(
    "ProofAnnotationEnvelope",
    {
        "type": "object",
        "description": (
            "Standard response envelope. Every MCP tool response is "
            "wrapped in this shape via mcp_server.wrap_envelope(). "
            "The `proof` block carries source, freshness, and "
            "cross-check metadata that lets agents verify the payload "
            "against ledger truth without a support ticket."
        ),
        "required": ["data", "proof", "server"],
        "properties": {
            "data": {
                "description": "Tool-specific payload. Shape varies per tool.",
            },
            "proof": {
                "type": "object",
                "required": [
                    "source", "as_of", "freshness_contract",
                    "methodology_url", "cross_check_status", "honest_partial",
                ],
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Data source identifier.",
                        "examples": ["local_rippled", "neon_postgres", "ethereum_1rpc", "local_rippled_stream_capture"],
                    },
                    "as_of": {
                        "type": "string",
                        "format": "date-time",
                        "description": "ISO 8601 UTC timestamp for when the payload was materialized.",
                    },
                    "freshness_contract": {
                        "type": "string",
                        "enum": ["≤ 5min", "≤ 30min", "daily", "finalized_only"],
                        "description": "The declared freshness bound for this payload. `finalized_only` means the tool refuses to serve partial-day rows (R1/R2 rule).",
                    },
                    "claims_ref": {
                        "type": ["string", "null"],
                        "description": "CLAIMS.yaml claim id where one exists. `null` when the datum isn't a public claim.",
                    },
                    "methodology_url": {
                        "type": "string",
                        "format": "uri",
                        "description": "Deep link to the /methodology section describing this source.",
                    },
                    "cross_check_status": {
                        "type": "string",
                        "enum": ["agree", "disagree", "not_applicable"],
                        "description": "Result of the tool's internal cross-check (where one is defined; `not_applicable` otherwise).",
                    },
                    "honest_partial": {
                        "type": "boolean",
                        "description": "True when the tool served a partial result because a dependency was unavailable. Always accompanied by `scope_note`.",
                    },
                    "scope_note": {
                        "type": ["string", "null"],
                        "description": "Required when honest_partial=True. Names exactly which slice is missing.",
                    },
                },
            },
            "server": {
                "type": "object",
                "required": ["name", "version", "public_key_fingerprint", "docs"],
                "properties": {
                    "name": {"type": "string", "examples": ["xrpldashboard-mcp"]},
                    "version": {"type": "string", "examples": ["1.0.0"]},
                    "public_key_fingerprint": {
                        "type": "string",
                        "description": "SHA-256 fingerprint of the signed-snapshot public key. Pin locally for tamper detection.",
                    },
                    "docs": {"type": "string", "format": "uri"},
                },
            },
        },
    },
)


def _register_agent_tier_openapi_paths(app_ref, spec):
    """Register path items for the LIVE well-known and discovery
    surfaces. These are documented under this OpenAPI spec but served
    by the existing @app.route decorators — smorest is not the router
    here, only the documenter. See fence #3 in the header above.

    Flask-smorest's FlaskPlugin requires a real werkzeug Rule (extracted
    from app.url_map) for every documented path — it inspects the rule
    to infer path parameters and to translate Flask's `<type:name>` to
    OpenAPI's `{name}`. So we resolve each path by matching url_map
    rules, and skip anything that didn't register (defensive — this
    file's routes should always be present, but a missed import
    shouldn't crash startup)."""

    def _text_ok(mime="text/plain"):
        return {
            "200": {
                "description": "OK",
                "content": {mime: {"schema": {"type": "string"}}},
            },
        }

    def _json_ok(schema=None):
        return {
            "200": {
                "description": "OK",
                "content": {
                    "application/json": {
                        "schema": schema or {"type": "object"},
                    },
                },
            },
        }

    def _rule_for(path_pattern):
        """Find the werkzeug Rule whose rule matches the given
        pattern. Returns None if not registered."""
        for r in app_ref.url_map.iter_rules():
            if r.rule == path_pattern:
                return r
        return None

    def _register(path_pattern, operations, parameters=None):
        rule = _rule_for(path_pattern)
        if rule is None:
            return  # defensive; missing routes just skip documentation
        # flask-smorest 0.47.0 FlaskPlugin.rule_to_params does
        # `argument not in rule.defaults`, which raises TypeError when
        # rule.defaults is None (werkzeug's default). Force to empty
        # dict so the plugin can iterate — no routing impact because
        # {} and None both mean "no defaults" for werkzeug matching.
        if rule.defaults is None:
            rule.defaults = {}
        # Path-level `parameters` (OpenAPI convention: shared across all
        # operations under a path). FlaskPlugin.path_helper merges its
        # rule-derived auto-params into whatever we pass here by matching
        # on (name, in) — same-name entries update in place, so our
        # richer schema (e.g. YYYY-MM-DD regex) survives and no duplicate
        # is emitted. Keeping this at path-level is what avoids the
        # Grok-flagged duplicate `parameters` block that appeared when
        # the same param was declared under `get.parameters` instead.
        kwargs = {"rule": rule, "operations": operations}
        if parameters is not None:
            kwargs["parameters"] = list(parameters)
        spec.path(**kwargs)

    _register(
        "/llms.txt",
        {
            "get": {
                "tags": ["discovery"],
                "summary": "llmstxt.org site directory",
                "description": (
                    "Markdown-formatted site directory following the "
                    "llmstxt.org convention. Every URL listed resolves "
                    "to a live public surface."
                ),
                "responses": _text_ok("text/markdown"),
            },
        },
    )
    _register(
        "/.well-known/agents.json",
        {
            "get": {
                "tags": ["discovery"],
                "summary": "Agent-discovery manifest (Wildcard-AI flavor)",
                "description": (
                    "Site identity, rate limits, trust surfaces, and "
                    "the proof-annotation envelope agents should "
                    "expect. Status booleans stay honest — each flips "
                    "to true only after the corresponding surface "
                    "responds."
                ),
                "responses": _json_ok(),
            },
        },
    )
    _register(
        "/.well-known/security.txt",
        {
            "get": {
                "tags": ["discovery"],
                "summary": "Security disclosure contact",
                "description": "RFC 9116 security.txt.",
                "responses": _text_ok(),
            },
        },
    )
    _register(
        "/robots.txt",
        {
            "get": {
                "tags": ["discovery"],
                "summary": "Crawler directives",
                "responses": _text_ok(),
            },
        },
    )
    _register(
        "/sitemap.xml",
        {
            "get": {
                "tags": ["discovery"],
                "summary": "XML sitemap (curated public routes + per-MPT detail pages)",
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/xml": {"schema": {"type": "string"}}},
                    },
                },
            },
        },
    )
    _register(
        "/.well-known/snapshots/chain.json",
        {
            "get": {
                "tags": ["signed-snapshots"],
                "summary": "Chain-linked list of daily signed snapshots",
                "description": (
                    "Each entry is chain-linked to the previous day, "
                    "giving a tamper-evident audit trail across the "
                    "full snapshot history."
                ),
                "responses": _json_ok(),
            },
        },
    )
    _register(
        "/.well-known/snapshots/<date>.json",
        {
            "get": {
                "tags": ["signed-snapshots"],
                "summary": "Ed25519-signed snapshot for a specific date (YYYY-MM-DD)",
                "responses": _json_ok(),
            },
        },
        parameters=[{
            "name": "date",
            "in": "path",
            "required": True,
            "schema": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
            "description": "UTC calendar date, YYYY-MM-DD.",
        }],
    )
    _register(
        "/.well-known/snapshots/pubkey.pem",
        {
            "get": {
                "tags": ["signed-snapshots"],
                "summary": "Ed25519 public key for snapshot verification",
                "description": "Pin locally. Verify snapshot signatures against this key.",
                "responses": _text_ok("application/x-pem-file"),
            },
        },
    )


# NOTE: the actual _register_agent_tier_openapi_paths(app, api.spec)
# call happens at the BOTTOM of this module — after all @app.route
# decorators have run — because the FlaskPlugin looks up rules in
# app.url_map, which is only populated as routes are registered.


_SNAPSHOT_FP_CACHE = {"path_mtime": None, "value": None}


# In-process 60s cache for /whales, keyed on the already-normalized
# (tier, filter_type) tuple — a closed 12-bucket address space (3 tiers ×
# 4 filter types incl. empty). Query-string salting can't mint new cache
# entries because unknown values snap to defaults before the key is built;
# scanners CAN'T address anything outside the 12 legit buckets. This is a
# tighter defense than canonical-path keying — the key space is closed.
# Shipped 2026-07-21 in response to observed distributed crawl (944 sources
# hitting /whales at ~1:1 probes/sources ratio). See feedback memories
# defenses_deploy_against_observed_attacks + adversary_behavior_not_
# mission_driver: this is CPU cost defense, not anti-scrape gate.
_WHALES_CACHE_LOCK = threading.Lock()
_WHALES_CACHE = {}  # (tier, filter_type) -> (expiry_ts, body_str, gen_ms)
_WHALES_CACHE_TTL_S = 60
_WHALES_CACHE_STATS = {
    "hits": 0,
    "misses": 0,
    "blocked": 0,
    "stale_serves": 0,  # SWR: stale body served + async rebuild triggered
    "hits_flushed": 0,
    "misses_flushed": 0,
    "blocked_flushed": 0,
    "last_flush_ts": 0.0,
}
# SWR (stale-while-revalidate): rebuild-in-flight guard per cache key. A stale
# hit serves the expired body immediately and fires ONE background rebuild;
# subsequent stale hits for the same key just serve stale until in_flight
# clears. Prevents a thundering herd from spawning N concurrent rebuilds.
_WHALES_REBUILD_LOCK = threading.Lock()
_WHALES_REBUILD_IN_FLIGHT = set()  # elements: (tier, filter_type) tuples
# Opportunistic hit-path flush interval. Miss-path-only flushing would
# systematically undercount hits on a warm cache day, and worker recycling
# would take unflushed residuals to the grave. Flushing on hit path too
# when it's been >5min bounds the residual undercount at ~5min of hits per
# recycle — honest and small enough that receipts stay decision-grade.
_WHALES_CACHE_FLUSH_INTERVAL_S = 300


def _maybe_flush_whales_receipts(force):
    """Flush accumulated hit/miss deltas to whales_cache_daily.

    force=True: always attempt (used from miss path — a miss just happened,
        so misses_delta is at least 1).
    force=False: only flush if hits_delta > 0 AND >_FLUSH_INTERVAL_S since
        last flush (opportunistic hit-path drain so worker recycles don't
        drop the accumulated hit tally).

    Best-effort; PG hiccup = no-op, deltas stay unflushed and roll into
    the next attempt.
    """
    now = time.time()
    with _WHALES_CACHE_LOCK:
        hits_delta = (
            _WHALES_CACHE_STATS["hits"] - _WHALES_CACHE_STATS["hits_flushed"]
        )
        misses_delta = (
            _WHALES_CACHE_STATS["misses"] - _WHALES_CACHE_STATS["misses_flushed"]
        )
        blocked_delta = (
            _WHALES_CACHE_STATS["blocked"] - _WHALES_CACHE_STATS["blocked_flushed"]
        )
        if hits_delta == 0 and misses_delta == 0 and blocked_delta == 0:
            return
        if not force:
            if hits_delta == 0 and blocked_delta == 0:
                return
            if now - _WHALES_CACHE_STATS["last_flush_ts"] < _WHALES_CACHE_FLUSH_INTERVAL_S:
                return
        _WHALES_CACHE_STATS["hits_flushed"] = _WHALES_CACHE_STATS["hits"]
        _WHALES_CACHE_STATS["misses_flushed"] = _WHALES_CACHE_STATS["misses"]
        _WHALES_CACHE_STATS["blocked_flushed"] = _WHALES_CACHE_STATS["blocked"]
        _WHALES_CACHE_STATS["last_flush_ts"] = now
    try:
        db.write_whales_cache_daily_delta(hits_delta, misses_delta, blocked_delta)
    except Exception:
        pass


# /analytics 60s in-process cache — mirrors the whales pattern above.
# Key space is a single bucket (no query params vary the view), so the dict
# is effectively single-slot; keeping the dict shape matches whales for
# consistency and leaves room if we ever add ?kind=bot as a variant later.
# The view is expensive (28 DB queries incl. 4 all-time scans + 22 through
# the heavy _bot_filter_sql with two IN-subqueries on page_views). One
# origin render per 60s is the goal; delta polling for right-now counts
# goes through the separate /analytics/live endpoint.
_ANALYTICS_CACHE_LOCK = threading.Lock()
_ANALYTICS_CACHE = {}  # "full" -> (expiry_ts, body_str, gen_ms)
_ANALYTICS_CACHE_TTL_S = 300
_ANALYTICS_CACHE_STATS = {
    "hits": 0,
    "misses": 0,
    "stale_serves": 0,  # SWR: stale body served + async rebuild triggered
    "hits_flushed": 0,
    "misses_flushed": 0,
    "last_flush_ts": 0.0,
}
_ANALYTICS_CACHE_FLUSH_INTERVAL_S = 300
# SWR guard — see _WHALES_REBUILD_LOCK. Single-key cache so a plain bool is
# enough; kept in a dict for stable identity across the closure.
_ANALYTICS_REBUILD_LOCK = threading.Lock()
_ANALYTICS_REBUILD_STATE = {"in_flight": False}
# Thread-local flag set by the SWR rebuild + warmer threads before they
# re-invoke the cached view. When True, the view skips the cache-read
# short-circuit and forces a real render — otherwise the background
# rebuild would itself take the stale-serve branch and never repopulate.
_CACHE_REBUILD_LOCAL = threading.local()


def _maybe_flush_analytics_receipts(force):
    """Same shape as _maybe_flush_whales_receipts, separate table
    (analytics_cache_daily). See that docstring for semantics."""
    now = time.time()
    with _ANALYTICS_CACHE_LOCK:
        hits_delta = (
            _ANALYTICS_CACHE_STATS["hits"]
            - _ANALYTICS_CACHE_STATS["hits_flushed"]
        )
        misses_delta = (
            _ANALYTICS_CACHE_STATS["misses"]
            - _ANALYTICS_CACHE_STATS["misses_flushed"]
        )
        if hits_delta == 0 and misses_delta == 0:
            return
        if not force:
            if hits_delta == 0:
                return
            if (
                now - _ANALYTICS_CACHE_STATS["last_flush_ts"]
                < _ANALYTICS_CACHE_FLUSH_INTERVAL_S
            ):
                return
        _ANALYTICS_CACHE_STATS["hits_flushed"] = _ANALYTICS_CACHE_STATS["hits"]
        _ANALYTICS_CACHE_STATS["misses_flushed"] = (
            _ANALYTICS_CACHE_STATS["misses"]
        )
        _ANALYTICS_CACHE_STATS["last_flush_ts"] = now
    try:
        db.write_analytics_cache_daily_delta(hits_delta, misses_delta)
    except Exception:
        pass


def _trigger_analytics_rebuild():
    """SWR: fire a background rebuild of the analytics cache iff no rebuild
    is already in flight. Called from the analytics() view when it detects
    a stale (expired) cache entry — the stale body is served immediately
    and this repopulates the cache asynchronously so the NEXT visitor gets
    a fresh body without paying the render cost themselves.

    The in_flight guard prevents thundering-herd: N concurrent stale hits
    fire ONE rebuild, not N. `finally` clears the flag so a rebuild error
    can't wedge the guard shut.
    """
    with _ANALYTICS_REBUILD_LOCK:
        if _ANALYTICS_REBUILD_STATE["in_flight"]:
            return
        _ANALYTICS_REBUILD_STATE["in_flight"] = True

    def _run():
        try:
            _CACHE_REBUILD_LOCAL.bypass = True
            with app.test_request_context("/analytics"):
                analytics()
        except Exception:
            pass
        finally:
            _CACHE_REBUILD_LOCAL.bypass = False
            with _ANALYTICS_REBUILD_LOCK:
                _ANALYTICS_REBUILD_STATE["in_flight"] = False

    threading.Thread(
        target=_run, daemon=True, name="analytics-swr-rebuild"
    ).start()


def _trigger_whales_rebuild(tier, filter_type):
    """SWR twin of _trigger_analytics_rebuild — see that docstring. Guard
    is per cache key (tier, filter_type) so a stale hit on one tier can't
    block a rebuild on another. filter_type may be '' (default view) — the
    test_request_context omits the query param in that case so the rebuild
    parses to the same normalized key the read path saw."""
    key = (tier, filter_type)
    with _WHALES_REBUILD_LOCK:
        if key in _WHALES_REBUILD_IN_FLIGHT:
            return
        _WHALES_REBUILD_IN_FLIGHT.add(key)

    qs_parts = [f"tier={tier}"]
    if filter_type:
        qs_parts.append(f"type={filter_type}")
    path = "/whales?" + "&".join(qs_parts)

    def _run():
        try:
            _CACHE_REBUILD_LOCAL.bypass = True
            with app.test_request_context(path):
                whales()
        except Exception:
            pass
        finally:
            _CACHE_REBUILD_LOCAL.bypass = False
            with _WHALES_REBUILD_LOCK:
                _WHALES_REBUILD_IN_FLIGHT.discard(key)

    threading.Thread(
        target=_run, daemon=True, name=f"whales-swr-rebuild-{tier}-{filter_type or 'default'}"
    ).start()


def _analytics_warmer_loop():
    """Background daemon: keeps the /analytics cache perpetually warm so
    no human visitor ever hits a cold 28-query render.

    Logic: every 30s check whether the cache entry expires within 30s (or
    is already expired). If so, rebuild by calling analytics() inside a
    test_request_context — that provides the app + request context the
    template render needs. analytics() itself has the cache-miss guard, so
    a concurrent hit from a real visitor while we're rebuilding is safe:
    the first to finish stores, the other returns immediately on the next
    lock check. Rate limiter sees 127.0.0.1 (2 hits/min, far under cap).

    Eliminates the recurring ~seconds-wait the first visitor each 60s
    window previously got — the original /analytics complaint (CF 25s
    timeout under load).
    """
    time.sleep(10)  # let gunicorn worker fully start before first build
    while True:
        try:
            # NOTE: bot-hash tables (page_view_bot_hashes,
            # page_view_scanner_combos) are refreshed exclusively by the
            # scripts/is_bot_writer.py walker on the Mac Mini (launchd, ~5min
            # cadence). The web tier used to call refresh_bot_hash_tables()
            # here every 30s, but that racked up cross-host TRUNCATE
            # contention with the walker and produced the deadlock storm
            # documented in docs/WALKER_WOUNDS_2026-08-22.md. Sole-writer
            # invariant: only is_bot_writer writes those tables. 5-min
            # staleness is negligible against the 7-day scanner window.
            with _ANALYTICS_CACHE_LOCK:
                entry = _ANALYTICS_CACHE.get("full")
                expiry = entry[0] if entry else 0
            if time.time() + 30 >= expiry:
                # Bypass the SWR short-circuit: with stale-while-revalidate
                # in place, the read path would otherwise return the stale
                # body without repopulating.
                _CACHE_REBUILD_LOCAL.bypass = True
                try:
                    with app.test_request_context("/analytics"):
                        analytics()
                finally:
                    _CACHE_REBUILD_LOCAL.bypass = False
        except Exception:
            pass
        time.sleep(30)


threading.Thread(
    target=_analytics_warmer_loop, daemon=True, name="analytics-warmer"
).start()


def _burst_cohort_scanner_loop():
    """Background daemon: runs scan_burst_cohorts() at startup (after a brief
    grace period) and then every 24h. Sets db._burst_cohort_table_ready = True
    on first success so _bot_filter_sql starts including the cohort predicate.
    Receipts: logs reclassified-row count at INFO level for evidence tracking.
    Review trigger: re-audit threshold when daily human traffic exceeds 1,000."""
    time.sleep(15)  # let Neon connection warm before the first scan
    while True:
        try:
            result = db.scan_burst_cohorts(lookback_days=90)
            if "error" not in result:
                app.logger.info(
                    "burst_cohort_scan: cohort_days=%d reclassified_rows=%d "
                    "inserted_or_updated=%d",
                    result.get("total_cohort_days", 0),
                    result.get("reclassified_rows", 0),
                    result.get("inserted", 0),
                )
        except Exception:
            pass
        time.sleep(86400)  # re-scan daily


threading.Thread(
    target=_burst_cohort_scanner_loop, daemon=True, name="burst-cohort-scanner"
).start()


@app.context_processor
def inject_snapshot_fingerprint():
    """Expose the Ed25519 signed-snapshot pubkey fingerprint to every
    template, so /about and /methodology can pin it inline without each
    route handler having to read the file. Cached by mtime — re-reads
    only after a fresh keygen rewrites the fingerprint file."""
    try:
        st = os.stat(SNAPSHOT_PUBKEY_FP_PATH)
    except FileNotFoundError:
        return {"snapshot_fingerprint": None}
    if _SNAPSHOT_FP_CACHE["path_mtime"] != st.st_mtime:
        try:
            with open(SNAPSHOT_PUBKEY_FP_PATH) as f:
                _SNAPSHOT_FP_CACHE["value"] = f.read().strip() or None
        except OSError:
            _SNAPSHOT_FP_CACHE["value"] = None
        _SNAPSHOT_FP_CACHE["path_mtime"] = st.st_mtime
    return {"snapshot_fingerprint": _SNAPSHOT_FP_CACHE["value"]}


# Per-page Open Graph card config. Slug -> (title, subtitle, accent_hex).
# Slugs are stable URL fragments served at /og/<slug>.png. Adding a new
# entry here + a path mapping below is all it takes to give a route its
# own social-share preview.
_OG_PAGES = {
    "home":          ("xrpldashboard",         "The XRP Ledger, made legible.",                       "#3ec8e0"),
    "whales":        ("Whale Activity",        "Live large-transfer monitor across the XRP Ledger.",  "#3ec8e0"),
    "pools":         ("AMM Pools",             "Every pool, ranked by depth and 24h volume.",         "#5ee08a"),
    "tokens":        ("Token Trades",          "Live trade tape across the XRP Ledger.",              "#b388f6"),
    "mpts":          ("MPT Registry",          "Every multi-purpose token on XRPL, decoded.",         "#ec4899"),
    "rlusd":         ("RLUSD",                 "Cross-chain treasury data — XRPL + Ethereum.",        "#3b82f6"),
    "lending":       ("XRPL Lending",          "Loan brokers, vaults, and TVL — XLS-66d.",            "#f59e0b"),
    "health":        ("System Health",         "Live operational status of every worker.",            "#10b981"),
    "methodology":   ("How It Works",          "Sources, methods, and freshness windows.",            "#94a3b8"),
    "about":         ("About xrpldashboard",   "What it is and why it exists.",                       "#94a3b8"),
    "institutional": ("For Institutions",      "Custom data feeds and direct access.",                "#fbbf24"),
    "terms":         ("Terms of Service",      "Plain-English terms for using xrpldashboard.",        "#94a3b8"),
    "privacy":       ("Privacy Policy",        "What we collect, what we don't.",                     "#94a3b8"),
    "security":      ("Security",              "How we handle data, harden surfaces, and disclose gaps.", "#10b981"),
    "subprocessors": ("Subprocessors",         "Every vendor that touches data, what they see, where they run.", "#94a3b8"),
}

# Path -> OG slug. Anything not in this map falls through to the generic
# /static/og-image.jpg (so subpages and detail routes still get *an* image,
# just the shared one).
_OG_PATH_SLUG = {
    "/":              "home",
    "/whales":        "whales",
    "/pools":         "pools",
    "/tokens":        "tokens",
    "/mpts":          "mpts",
    "/rlusd":         "rlusd",
    "/lending":       "lending",
    "/health":        "health",
    "/methodology":   "methodology",
    "/about":         "about",
    "/institutional": "institutional",
    "/terms":         "terms",
    "/privacy":       "privacy",
    "/security":      "security",
    "/subprocessors": "subprocessors",
}

# Cache of rendered PNG bytes by slug. The output is deterministic for the
# life of the process (config is static), so a single render-per-slug is all
# we ever need. ~36KB per slug × 13 slugs = ~470KB max footprint.
_OG_CACHE = {}


@app.context_processor
def inject_og_image():
    """Resolve the per-page og:image URL based on request.path.

    Templates pick this up via _head_meta.html. Pages without a config
    entry just see the existing /static/og-image.jpg — so adding a new
    page without an OG slug doesn't break sharing, it just keeps the
    generic card. Detail pages with dynamic slugs (e.g. /wallet/<addr>,
    /token/<cur>/<iss>) also fall through to the generic card on purpose
    — we don't want crawlers minting per-address renders."""
    slug = _OG_PATH_SLUG.get(request.path or "/")
    if slug:
        return {"og_image_url": f"{SITE_URL}/og/{slug}.png"}
    return {"og_image_url": f"{SITE_URL}/static/og-image.jpg"}


@app.route("/og/<slug>.png")
@limiter.limit("120 per minute")
def og_card(slug):
    """Serve the per-page OG image. Cached in-memory after first render.

    Three layers of fallback to /static/og-image.jpg:
      1. Unknown slug
      2. Pillow missing (og_image.render returns None)
      3. Any exception during render
    A failed render is cached as None for the rest of the process lifetime
    too — no point rebuilding a broken image on every social-card fetch."""
    cfg = _OG_PAGES.get(slug)
    if not cfg:
        return redirect("/static/og-image.jpg", code=302)
    if slug in _OG_CACHE:
        cached = _OG_CACHE[slug]
        if cached is None:
            return redirect("/static/og-image.jpg", code=302)
        return Response(cached, mimetype="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
    try:
        png = og_image.render(*cfg)
    except Exception:
        png = None
    _OG_CACHE[slug] = png
    if not png:
        return redirect("/static/og-image.jpg", code=302)
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.context_processor
def inject_xrp_usd():
    """Expose the live XRP/USD anchor (from price_oracle, derived from
    XRPL-native AMMs) to every template — so any page can render a live
    pricing chip without per-route plumbing. Cached ~60s in the oracle."""
    try:
        from price_oracle import xrp_usd, xrp_usd_sources
        return {
            "live_xrp_usd": xrp_usd(),
            "live_xrp_usd_sources": xrp_usd_sources(),
        }
    except Exception:
        return {"live_xrp_usd": None, "live_xrp_usd_sources": []}


# Allowlist of external origins our pages legitimately load. Keep narrow —
# every entry is a trust decision. jsdelivr serves vendored front-end deps
# (Geist fonts, Lenis, GSAP, CountUp) and is disclosed at /security; the
# self-host plan replaces it with /static/vendor/ (tracked as #93).
_CSP_SCRIPT_SRC = "'self' 'unsafe-inline' https://cdn.jsdelivr.net"
_CSP_STYLE_SRC = "'self' 'unsafe-inline' https://cdn.jsdelivr.net"
_CSP_FONT_SRC = "'self' https://cdn.jsdelivr.net data:"
_CSP_IMG_SRC = "'self' data:"
_CSP_CONNECT_SRC = (
    # Browsers connect to wss://xrplcluster.com (primary). s2 and s1 are
    # kept in the allowlist as automatic fallbacks so a cluster outage
    # can be mitigated without also pushing a CSP header change.
    "'self' wss://xrplcluster.com wss://s2.ripple.com wss://s1.ripple.com"
)

_CSP_VALUE = "; ".join([
    "default-src 'self'",
    f"script-src {_CSP_SCRIPT_SRC}",
    f"style-src {_CSP_STYLE_SRC}",
    f"img-src {_CSP_IMG_SRC}",
    f"font-src {_CSP_FONT_SRC}",
    f"connect-src {_CSP_CONNECT_SRC}",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
])

_PERMISSIONS_POLICY_VALUE = ", ".join([
    "camera=()",
    "microphone=()",
    "geolocation=()",
    "payment=()",
    "usb=()",
    "magnetometer=()",
    "gyroscope=()",
    "accelerometer=()",
    "interest-cohort=()",
])


@app.after_request
def apply_security_headers(response):
    """Browser-enforced security headers on every response.

    - X-Content-Type-Options: nosniff
        Stops the browser MIME-sniffing a response away from its declared
        Content-Type. Closes a class of "this image is actually JS" attacks.

    - X-Frame-Options: DENY (+ CSP frame-ancestors 'none')
        Disallows the site being framed. Mitigates clickjacking. We never
        embed our own pages in frames.

    - Referrer-Policy: strict-origin-when-cross-origin
        On same-origin nav, full referrer; cross-origin, origin only.
        Outbound links to XRPSCAN/Bithomp also carry rel="noreferrer".

    - Strict-Transport-Security (HSTS)
        Forces HTTPS for two years on every visitor's browser. Browsers
        ignore HSTS over HTTP, so this header is inert in local dev.

    - Content-Security-Policy
        Allowlists the exact external origins we load (jsdelivr only).
        'unsafe-inline' is required because templates carry large embedded
        <style> and <script> blocks; tightening to nonces is a future move.
        Even with 'unsafe-inline', CSP still blocks loading scripts/styles
        from non-allowlisted origins — i.e. an injected <script src="evil">
        is rejected, which is the highest-impact protection here.

    - Permissions-Policy
        Explicitly disables features we never use (camera, microphone,
        geolocation, payment, USB, motion sensors, FLoC interest-cohort).
        Even if compromised JS asks for them, the browser refuses.

    - Cross-Origin-Opener-Policy: same-origin
        Isolates our browsing context group from cross-origin popups so
        opener.window references can't leak across origins.

    - Cross-Origin-Embedder-Policy: credentialless
        Stricter than the default but more compatible than require-corp —
        cross-origin subresources (XRPSCAN links, xrpl.org docs) load
        without forcing them to send CORP headers, but with credentials
        stripped. Pairs with COOP to enable cross-origin isolation.

    - Cross-Origin-Resource-Policy: same-origin (with discovery-class exception)
        Stops cross-origin pages from embedding our resources as no-cors
        loads. Same-origin is the site-wide default; the agent-tier
        discovery surfaces (llms.txt, agents.json, openapi.json, the
        snapshots family, /docs, robots.txt, security.txt) AND the
        /claims/ family (index.json + every claim URI — citation
        payloads the receipts story rests on) are shared assets by
        design and get CORP: cross-origin plus Access-Control-Allow-
        Origin: * in the branch at the top of this hook so browser-
        side agent tooling can fetch them. Do NOT "fix" the discovery
        exception back to a wall — that premise is stale (the agent
        tier + claims URIs made these surfaces public read-only shared
        assets on purpose).

    - Content-Security-Policy-Report-Only (Trusted Types)
        Report-only enforcement of Trusted Types for sinks like innerHTML.
        Violations log to the browser console without breaking rendering.
        Promote to enforced (drop "-Report-Only") after a week of clean logs.
    """
    # Discovery-class exception (see CORP block in the docstring above).
    # Agent-tier surfaces + the /claims/ family are public read-only
    # shared assets by design; browser-side agent tooling needs to fetch
    # them from other origins. Direct assignment (not setdefault) so we
    # override the same-origin CORP default set below. COOP/COEP
    # untouched — irrelevant to fetch/XHR reads.
    _path = request.path or ""
    if (
        is_agent_tier_route(_path)
        or _path == "/claims/index.json"
        or _path.startswith("/claims/")
    ):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"

    # /analytics — Cache-Control: no-store. The page copy says "live";
    # machines should get live too. Prior state (2026-08-27) was no
    # explicit Cache-Control at all, which let intermediate caches
    # freeze the page for 2+ hours for non-browser fetchers while
    # browsers saw fresh via the WS refresh path. Explicit no-store
    # makes the "live" promise structural — absence-of-posture was
    # the disease; explicit posture is the cure. Origin's own 60s
    # in-process cache (see analytics() docstring) is orthogonal: it
    # smooths render cost under load; no-store only tells clients
    # and CDNs not to cache the response body.
    if _path == "/analytics":
        response.headers["Cache-Control"] = "no-store"

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Referrer-Policy", "strict-origin-when-cross-origin"
    )
    response.headers.setdefault(
        "Strict-Transport-Security",
        "max-age=63072000; includeSubDomains; preload",
    )
    response.headers.setdefault("Content-Security-Policy", _CSP_VALUE)
    response.headers.setdefault(
        "Permissions-Policy", _PERMISSIONS_POLICY_VALUE
    )
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault(
        "Cross-Origin-Embedder-Policy", "credentialless"
    )
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy-Report-Only",
        "require-trusted-types-for 'script'; "
        "trusted-types default 'allow-duplicates'",
    )
    return response


# Paths excluded from page_view logging — assets, healthchecks, JSON APIs,
# the admin surface itself, and crawler probes. Anything not in this set
# (and returning HTML) gets one row in page_views per hit.
_PAGEVIEW_SKIP_PREFIXES = (
    "/static/",
    "/healthz",
    "/api/",
    "/admin/",
    "/og/",
    # Delta-polling endpoint — fires every 15s from the /analytics JS
    # interval; must not appear in page_views or it inflates RIGHT NOW
    # counts and clogs the Recent Visits feed.
    "/analytics/live",
    # CTA click trackers — logged separately via cta_clicks so they don't
    # double-count in page_views.
    "/click/",
    # Detail pages whose URL contains a user's address/token identifier.
    # The pages are public, but logging the exact address contradicts the
    # "no per-visitor tracking" pledge in /privacy and /about.
    "/wallet/",
    "/token/",
)
_PAGEVIEW_SKIP_EXACT = {
    "/favicon.ico", "/robots.txt", "/sitemap.xml",
    "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
}


def _visitor_hash(ip, ua):
    """Stable per-day fingerprint for unique-visitor counting. HMAC-SHA256
    with the server-side _VISITOR_HASH_KEY (== app.secret_key) so the
    stored hash can't be brute-forced back to an IP even if the DB leaks:
    without the key the IP-space search is meaningless. Truncated to 32
    hex (128 bits) — collision-resistant at our visitor volume, half the
    bytes of a full SHA256. Day-bucketed so one person = one unique per day."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    msg = f"{ip or '?'}|{ua or '?'}|{day}".encode("utf-8", "replace")
    return hmac.new(_VISITOR_HASH_KEY, msg, "sha256").hexdigest()[:32]


def _ip_day_hash(ip):
    """Per-(IP, day) fingerprint used ONLY for bot-burst session linking
    in /analytics. Same key, same truncation as _visitor_hash so the two
    can be compared in the bot-filter session join without leaking either
    back to an IP. Drops UA so a single client cycling through Chrome /
    Firefox / Safari user-agents inside a credential-probe burst collapses
    to one ip_day_hash row, not N."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    msg = f"{ip or '?'}|{day}".encode("utf-8", "replace")
    return hmac.new(_VISITOR_HASH_KEY, msg, "sha256").hexdigest()[:32]


# Internal-traffic exclusion: comma-separated client IPs whose page hits
# are dropped from page_views entirely. Configure via Render env. Empty
# (the default) keeps the prior behaviour — all hits logged. IPs are
# matched against ProxyFix-resolved request.remote_addr (== Cloudflare
# CF-Connecting-IP after the two-hop strip). Travel/mobile networks fall
# outside this set by design — exclusion is "while at the Mac mini's home
# network," not "any device Charlie ever uses."
_ANALYTICS_EXCLUDED_IPS = frozenset(
    ip.strip() for ip in os.environ.get("ANALYTICS_EXCLUDE_IPS", "").split(",")
    if ip.strip()
)


# AI training-only crawlers blocked per three-audience policy (2026-07-21):
# "AI retrieval crawlers (fetch-to-cite): NEVER blocked. AI training-only
# crawlers (bulk-ingest, no citation path): Charlie's discretion, per-crawler,
# documented." meta-externalagent = Meta AI training; no citation path. Every
# entry here must be explicitly approved and noted. See memory:
# feedback_three_audience_rule.md.
_BLOCKED_UA_FRAGMENTS = ("meta-externalagent",)


@app.before_request
def _block_ai_crawlers():
    """Fast-path 403 for AI training-only crawlers (no citation path). UA is
    unique enough that a substring match is safe; legitimate browsers and
    retrieval crawlers never include these tokens. Runs before page-view
    logging so denied requests aren't counted."""
    ua = (request.headers.get("User-Agent") or "").lower()
    for fragment in _BLOCKED_UA_FRAGMENTS:
        if fragment in ua:
            return "Forbidden", 403


@app.before_request
def _agent_tier_fleet_block():
    """Day 6: extend the proven /whales fleet-block signature to the
    agent-tier surfaces (llms.txt, agents.json, snapshot well-knowns,
    OpenAPI, /docs). Same fingerprint as the /whales inline block
    (IL + Chrome/142 residential 2026-07); factored into
    agent_tier_rate_limit.fleet_signature() so a future second-fleet
    observation adds ONE line covering /whales + the agent-tier surface
    at once. Returns 429 + Retry-After (well-behaved compliance signal;
    the same shape /whales serves). Non-agent-tier paths fall through
    untouched — /whales retains its own inline block by design so this
    hook cannot regress that surface."""
    if not is_agent_tier_route(request.path):
        return
    label = fleet_signature()
    if label:
        return Response(
            "",
            status=429,
            headers={
                "Retry-After": str(FLEET_BLOCK_RETRY_AFTER_SECONDS),
                "X-Fleet-Signature": label,
            },
        )


@app.after_request
def _agent_tier_audit_header(response):
    """Day 6: identified AI-crawler responses on agent-tier routes get
    an X-XRPL-Dashboard-Audit-URL header pointing at /coverage — the
    doc's "warm citations" touch (docs/AGENT_TIER_DESIGN.md §Rate
    limiting + abuse posture). Anonymous responses don't get the
    header (no citation-graph value to expose). Non-agent-tier paths
    unchanged.

    Phase 2 (2026-08-02): piggyback ai_crawler_hits telemetry here.
    Same classification pass covers both surfaces — one call to
    classify_ai_crawler(), header on allowlisted UAs, row written for
    any bot-shaped UA (allowlisted OR UNLISTED). Normal browsers /
    empty UAs get None back and are skipped (that's what page_views
    counts). Failures never break the response."""
    if not is_agent_tier_route(request.path):
        return response
    ua = request.headers.get("User-Agent", "")
    ua_class = classify_ai_crawler(ua)
    if ua_class is None:
        return response
    if ua_class != "UNLISTED":
        response.headers[AUDIT_URL_HEADER_NAME] = f"{SITE_URL}{AUDIT_URL_PATH}"
    try:
        if db.pg_available():
            db.write_ai_crawler_hit(
                ts=int(time.time()),
                ua_class=ua_class,
                path=(request.path or "/")[:300],
                status=int(response.status_code),
            )
    except Exception:
        pass
    return response


@app.before_request
def _log_page_view():
    """Best-effort page-view logger feeding /admin/stats. Inline insert
    via the cached writer connection — fast at our request volume, and
    swallows every exception so a Postgres hiccup never breaks a page."""
    try:
        path = request.path or "/"
        if path in _PAGEVIEW_SKIP_EXACT:
            return
        for prefix in _PAGEVIEW_SKIP_PREFIXES:
            if path.startswith(prefix):
                return
        if request.method != "GET":
            return
        if not db.pg_available():
            return
        ip = _client_ip()
        if ip and ip in _ANALYTICS_EXCLUDED_IPS:
            return
        ua = (request.user_agent.string or "")[:300] or None
        ref = (request.referrer or "")[:300] or None
        country = request.headers.get("CF-IPCountry") \
            or request.headers.get("X-Vercel-IP-Country") \
            or request.headers.get("X-Country-Code")
        # Region code (ISO 3166-2, "US-CA" style — country-dash-subdivision).
        # Primary source: self-hosted MaxMind GeoLite2 lookup on the client
        # IP. Local .mmdb file, sub-microsecond per call. Fetched at
        # container start from MaxMind keyed by MAXMIND_LICENSE_KEY.
        # Fallback: legacy CF-Region-Code / X-CF-Region-Code headers. Both
        # sources were parked (Managed Transform empirically dead on Free
        # plan; Worker deploy blocked by Custom Domain DNS conflict). Left
        # in-chain so if either ever comes back the writer still catches
        # them without a redeploy.
        region_code = geoip_state.lookup_region_code(ip) or (
            request.headers.get("CF-Region-Code")
            or request.headers.get("X-CF-Region-Code")
            or None
        )
        utm = request.args.get("utm_source")
        utm = utm[:100] if utm else None
        db.log_page_view(
            path=path[:300],
            visitor_hash=_visitor_hash(ip, ua),
            referrer=ref,
            user_agent=ua,
            country=country,
            region_code=region_code,
            utm_source=utm,
            ip_day_hash=_ip_day_hash(ip),
        )
    except Exception:
        # Logging must never break a page render.
        pass


@app.context_processor
def inject_whale_thresholds():
    """Expose whale threshold + window constants to every template, so
    editorial copy describing them ("100,000 XRP", "last 7 days",
    "30-day window", "last 1,000 account_tx entries") renders from a
    single source of truth instead of mirroring the constants as literals.
    Codifies the hardcoded-numbers-mirror-constants anti-pattern fix at
    the global injection level."""
    from wallet_data import (
        WHALE_WINDOW_DAYS, LOOKBACK_DAYS,
        MAX_TX_PAGES, TX_PAGE_LIMIT,
    )
    return {
        "whale_xrp_threshold": WHALE_XRP_THRESHOLD,
        "whale_window_days": WHALE_WINDOW_DAYS,
        "lookback_days": LOOKBACK_DAYS,
        "max_tx_pages": MAX_TX_PAGES,
        "tx_page_limit": TX_PAGE_LIMIT,
    }


@app.context_processor
def inject_liveness():
    """Make a slim ledger-heartbeat dict available to every template via
    {{ liveness }}. Drives the pulsing chip in the shared nav. Pull is cheap
    (cached 20s in network_pulse) and degrades silently if the node is down."""
    try:
        p = fetch_pulse_cached()
    except Exception:
        return {"liveness": None}
    if not p or p.get("error"):
        return {"liveness": None}
    return {"liveness": {
        "ledger_index": p.get("ledger_index"),
        "last_close_age_seconds": max(0, p.get("last_close_age_seconds") or 0),
        "status": p.get("status"),
        "status_text": p.get("status_text"),
        "fetched_at_unix": int(time.time()),
    }}


def _recent_whale_events(limit=3):
    """Latest N whale events, formatted via _resolve_event.

    Dual-read: prefers Postgres when DATABASE_URL is set, falls back to
    the committed SQLite snapshot otherwise (or when Postgres errors).
    Returns [] when no source is available."""
    rows = None
    if db.pg_available():
        try:
            rows = db.read_recent_events(
                limit, tagged_floor_drops=TAGGED_XRP_FLOOR_DROPS,
            )
        except Exception:
            rows = None  # fall through to SQLite
    if rows is None:
        if not os.path.exists(EVENTS_DB_PATH):
            return []
        try:
            conn = sqlite3.connect(f"file:{EVENTS_DB_PATH}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT tx_hash, ledger_index, ts, type, from_addr, to_addr, "
                    "amount_drops, currency, issuer, raw_json FROM events "
                    "WHERE type != 'trustset' "
                    "  AND (type != 'tagged' "
                    "       OR amount_drops IS NULL "
                    "       OR amount_drops >= ?) "
                    "ORDER BY ts DESC LIMIT ?",
                    (TAGGED_XRP_FLOOR_DROPS, limit),
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return []
    named = _load_named_accounts_dict()
    tokens = _load_token_names_dict()
    # Layer PG account_labels for the addresses on this page. File-based
    # named_accounts always wins (first-party Ripple-escrow verification);
    # PG fills the long tail (XRPSCAN-curated exchanges + derived AMM/MPT
    # issuers). Failure is swallowed — card renders fine with file-only.
    if db.pg_available():
        page_addrs = set()
        for r in rows:
            if r[4]:
                page_addrs.add(r[4])
            if r[5]:
                page_addrs.add(r[5])
        page_addrs.difference_update(named.keys())
        if page_addrs:
            try:
                for addr, info in db.read_account_labels(list(page_addrs)).items():
                    named[addr] = {
                        "name": info.get("name"),
                        "category": info.get("category"),
                        "_source": info.get("source"),
                        "_extra": info.get("extra"),
                    }
            except Exception:
                pass
    return [_resolve_event(r, named, tokens) for r in rows]


def _whales_snapshot_label():
    """Friendly date of the latest event in events.db.
    Used to label snapshot-mode panels honestly. Returns None on missing/empty."""
    if not os.path.exists(EVENTS_DB_PATH):
        return None
    try:
        conn = sqlite3.connect(f"file:{EVENTS_DB_PATH}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT MAX(ts) FROM events").fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return None
        dt = datetime.fromtimestamp(int(row[0]), tz=timezone.utc)
        return f"{dt.strftime('%B')} {dt.day}, {dt.year}"
    except Exception:
        return None


def _format_age_seconds(age):
    """Friendly 'X ago' label from a non-negative integer seconds delta.
    Returns None when age is None or negative (clock skew)."""
    if age is None or age < 0:
        return None
    if age < 60:
        return f"{age}s ago"
    if age < 3600:
        return f"{age // 60}m ago"
    if age < 86400:
        return f"{age // 3600}h ago"
    return f"{age // 86400}d ago"


def _events_db_age_seconds():
    """Seconds since the most recent row in events. Prefers Postgres when
    DATABASE_URL is set so the badge reflects what users actually see in
    prod; falls back to local events.db. None on missing/empty/error."""
    if db.pg_available():
        try:
            latest = db.read_max_event_ts()
            if latest is not None:
                return max(0, int(time.time()) - latest)
        except Exception:
            pass
    if not os.path.exists(EVENTS_DB_PATH):
        return None
    try:
        conn = sqlite3.connect(f"file:{EVENTS_DB_PATH}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT MAX(ts) FROM events").fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return None
        return max(0, int(time.time()) - int(row[0]))
    except Exception:
        return None


def _volumes_db_age_seconds():
    """Seconds since the most recent hourly bucket in token_volume. Prefers
    Postgres; falls back to local volumes.db. None on missing/empty/error."""
    if db.pg_available():
        try:
            latest_bucket = db.read_max_token_bucket()
            if latest_bucket is not None:
                return max(0, int(time.time()) - latest_bucket * 3600)
        except Exception:
            pass
    if not os.path.exists(VOLUMES_DB_PATH):
        return None
    try:
        conn = sqlite3.connect(f"file:{VOLUMES_DB_PATH}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT MAX(hour_bucket) FROM token_volume"
            ).fetchone()
        finally:
            conn.close()
        if not row or row[0] is None:
            return None
        latest_ts = int(row[0]) * 3600
        return max(0, int(time.time()) - latest_ts)
    except Exception:
        return None


_VERIFIED_BRANDS_CACHE = {"data": None, "ts": 0.0}


def _verified_brands():
    """Return dict[currency_hex] -> set of TOML-attested issuers.

    Source of truth is token_names.json's verified_via field: any entry
    pointing to an xrp-ledger.toml (or xrpscan@ provenance) marks its
    issuer as canonical for that currency_hex. Null-canonical brands
    (composite key '<hex>:') register an empty issuer set — every XRPL
    issuer of that currency_hex then fails membership and flags as a
    brand-protection violation. Cached for 5 min — the file mutates
    rarely. Mirror of rank_amms.load_verified_brands()."""
    now = time.time()
    cached = _VERIFIED_BRANDS_CACHE
    if cached["data"] is not None and now - cached["ts"] < 300:
        return cached["data"]
    brands = {}
    raw = _safe_load_json(TOKEN_NAMES_PATH) or {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        ver = entry.get("verified_via") or ""
        if not any(tier in ver for tier in ("xrp-ledger.toml", "xrpscan@")):
            continue
        try:
            cur_hex, iss = key.split(":", 1)
        except ValueError:
            continue
        cur_hex = cur_hex.strip()
        iss = iss.strip()
        if not cur_hex:
            continue
        brands.setdefault(cur_hex, set())
        if iss:
            brands[cur_hex].add(iss)
    cached["data"] = brands
    cached["ts"] = now
    return brands


def _annotate_unverified_brands(rows):
    """Stamp unverified_brand on asset_a/asset_b for ranked-pool rows whose
    display label comes from decode_currency() (hex → ASCII) rather than
    a peg-table lookup. If a side's currency_hex is a TOML-attested brand
    but its issuer isn't on the per-brand allowlist, the label is a
    leaky-abstraction spoof — set unverified_brand=True and surface the
    canonical issuer list (truncated for tooltip display) so the template
    can render an attribution warning without hardcoding addresses.
    Idempotent — safe to call repeatedly."""
    brands = _verified_brands()
    if not brands:
        return rows
    for r in rows:
        for side_key in ("asset_a", "asset_b"):
            side = r.get(side_key)
            if not isinstance(side, dict) or "unverified_brand" in side:
                continue
            cur = side.get("currency")
            iss = side.get("issuer")
            unverified = bool(
                cur and iss and cur in brands and iss not in brands[cur]
            )
            side["unverified_brand"] = unverified
            if unverified:
                canonical = brands[cur]
                side["is_null_canonical"] = not canonical
                side["canonical_issuers"] = ", ".join(
                    f"{a[:6]}…{a[-4:]}" for a in sorted(canonical)
                )
    return rows


@ttl_cache(seconds=60)
def _ranked_amm_snapshot():
    """Single source of truth for AMM ranking data on /pools and the
    homepage AMM card. Prefers Postgres (the Mac-hosted ranker dual-writes
    its snapshot there so prod sees the same data the file does), falls
    back to the local JSON files.

    Returns (rows, meta) where rows is a list of dicts in the shape of
    amm_ranked.json entries, and meta has keys: indexed_count,
    started_at, finished_at, snapshot_ts, source ('postgres'|'file')."""
    if db.pg_available():
        try:
            rows = db.read_amm_ranked_pools()
        except Exception:
            rows = []
        if rows:
            try:
                hb = db.read_heartbeat("amm_ranker") or {}
            except Exception:
                hb = {}
            extra = hb.get("extra") if isinstance(hb, dict) else None
            extra = extra if isinstance(extra, dict) else {}
            try:
                snap_ts = db.read_amm_snapshot_ts()
            except Exception:
                snap_ts = None
            return _annotate_unverified_brands(rows), {
                "indexed_count": extra.get("indexed_count") or len(rows),
                "started_at": extra.get("started_at"),
                "finished_at": extra.get("finished_at"),
                "snapshot_ts": snap_ts,
                "source": "postgres",
            }
    rows = _safe_load_json(AMM_RANKED_PATH) or []
    index = _safe_load_json(AMM_INDEX_PATH) or []
    state = _safe_load_json(AMM_RANK_STATE_PATH) or {}
    snap_ts = None
    try:
        if os.path.exists(AMM_RANKED_PATH):
            snap_ts = int(os.path.getmtime(AMM_RANKED_PATH))
    except Exception:
        snap_ts = None
    return _annotate_unverified_brands(rows), {
        "indexed_count": len(index) if isinstance(index, list) else 0,
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "snapshot_ts": snap_ts,
        "source": "file",
    }


_XRP_ESCROW_STALE_AFTER = 3600  # 60 min
_XRP_AMM_STALE_AFTER = 7200     # 2 h (rank_amms cadence is 1h; stay 2× in lockstep with the plist)


def _build_xrp_distribution(ranked_full):
    """Build the XRP supply-distribution payload shared by the index
    render (constellation viz) and the /api/xrp-distribution endpoint.
    Backed by the same in-process caches the rest of the homepage uses,
    so calling it is essentially free on the hot path unless a walker
    landed a new snapshot since the last hit. Every bucket carries an
    age_seconds and is_stale flag; the endpoint uses them, the current
    homepage viz does not (retained shape in case a future viz wants
    the freshness signal without recomputing it)."""
    try:
        esc = fetch_escrow_locked_cached()
    except Exception:
        esc = None
    escrowed_xrp = float(esc.get("total_xrp") or 0) if esc else 0.0
    escrow_age = float(esc.get("cached_age_seconds")) if esc and esc.get(
        "cached_age_seconds") is not None else None

    amm_xrp = 0.0
    amm_snap_ts = None
    try:
        _rows, meta = _ranked_amm_snapshot()
        amm_snap_ts = meta.get("snapshot_ts") if isinstance(meta, dict) else None
        for p in ranked_full or []:
            a = p.get("asset_a") or {}
            b = p.get("asset_b") or {}
            if a.get("currency") == "XRP":
                amm_xrp += float(p.get("amount_a") or 0)
            elif b.get("currency") == "XRP":
                amm_xrp += float(p.get("amount_b") or 0)
    except Exception:
        pass
    amm_age = (time.time() - amm_snap_ts) if amm_snap_ts else None

    try:
        tot = fetch_total_supply_cached()
    except Exception:
        tot = None
    total_xrp = float(tot.get("total_xrp") or 0) if tot else 0.0
    if total_xrp <= 0:
        total_xrp = XRP_DESIGN_SUPPLY_FALLBACK
    total_age = float(tot.get("cached_age_seconds")) if tot and tot.get(
        "cached_age_seconds") is not None else None
    total_is_fallback = bool(tot.get("is_fallback")) if tot else True

    locked = escrowed_xrp + amm_xrp
    wallets_xrp = max(0.0, total_xrp - locked)

    burned_since_genesis_xrp = None
    if not total_is_fallback and total_xrp > 0:
        diff = XRP_DESIGN_SUPPLY_FALLBACK - total_xrp
        if diff > 0:
            burned_since_genesis_xrp = diff

    # Derived-basin age tracks the STALER of its two inputs — an honest
    # answer to "when was this reading taken." If either input is
    # missing, we can't honestly stamp the derivation.
    ages = [a for a in (escrow_age, amm_age) if a is not None]
    wallets_age = max(ages) if ages else None

    escrow_stale = escrow_age is not None and escrow_age > _XRP_ESCROW_STALE_AFTER
    amm_stale = amm_age is not None and amm_age > _XRP_AMM_STALE_AFTER
    wallets_stale = escrow_stale or amm_stale

    return {
        "total_xrp": total_xrp,
        "escrowed_xrp": escrowed_xrp,
        "amm_xrp": amm_xrp,
        "wallets_xrp": wallets_xrp,
        "escrowed_pct": (escrowed_xrp / total_xrp) * 100,
        "amm_pct": (amm_xrp / total_xrp) * 100,
        "wallets_pct": (wallets_xrp / total_xrp) * 100,
        "escrow_object_count": (esc.get("object_count") if esc else 0) or 0,
        "escrow_age_seconds": escrow_age,
        "amm_age_seconds": amm_age,
        "wallets_age_seconds": wallets_age,
        "total_age_seconds": total_age,
        "total_is_fallback": total_is_fallback,
        "burned_since_genesis_xrp": burned_since_genesis_xrp,
        "escrow_stale": escrow_stale,
        "amm_stale": amm_stale,
        "wallets_stale": wallets_stale,
        "server_time": int(time.time()),
    }


def _pools_snapshot_label():
    """Friendly date the AMM ranking last completed. Tries local rank/scan
    state first (Mac), then falls back to the ranker's heartbeat in Postgres
    (Render)."""
    candidates = []
    for path, key in (
        (AMM_RANK_STATE_PATH, "finished_at"),
        (AMM_RANK_STATE_PATH, "started_at"),
        (SCAN_STATE_PATH, "finished_at"),
        (SCAN_STATE_PATH, "started_at"),
    ):
        try:
            d = _safe_load_json(path) or {}
            iso = d.get(key)
            if iso:
                candidates.append(iso)
        except Exception:
            continue
    try:
        hb = db.read_heartbeat("amm_ranker") or {}
        extra = hb.get("extra") if isinstance(hb, dict) else None
        if isinstance(extra, dict):
            for key in ("finished_at", "started_at"):
                iso = extra.get(key)
                if iso:
                    candidates.append(iso)
    except Exception:
        pass
    for iso in candidates:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return f"{dt.strftime('%B')} {dt.day}, {dt.year}"
        except Exception:
            continue
    return None


def _top_tokens_recent(limit=5, hours_back=24 * 7):
    """Top N tokens by trade count over the last `hours_back` hours.
    Mirrors the /tokens route but trimmed for the homepage preview.

    Dual-read: prefers Postgres when DATABASE_URL is set, falls back to
    the local volumes.db. On Render the worker and web are separate
    containers so local volumes.db is empty there — PG is the only source
    that crosses the gap."""
    rows = None
    if db.pg_available():
        try:
            agg = db.read_token_volume_aggregates(hours_back=hours_back,
                                                  limit=limit)
            rows = [(cur, iss, trades) for cur, iss, trades, _hours in agg]
        except Exception:
            rows = None
    if rows is None:
        if not os.path.exists(VOLUMES_DB_PATH):
            return []
        try:
            conn = sqlite3.connect(f"file:{VOLUMES_DB_PATH}?mode=ro", uri=True)
            try:
                cutoff = int(time.time() // 3600) - hours_back
                rows = conn.execute(
                    "SELECT currency, issuer, SUM(trade_count) AS trades "
                    "FROM token_volume WHERE hour_bucket >= ? "
                    "GROUP BY currency, issuer ORDER BY trades DESC LIMIT ?",
                    (cutoff, limit),
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return []
    tokens_meta = _load_token_names_dict()
    pg_labels = {}
    if db.pg_available():
        try:
            pg_labels = db.read_account_labels({iss for _c, iss, _t in rows if iss})
        except Exception:
            pg_labels = {}
    out = []
    seen_issuers: dict[str, str] = {}
    for cur, iss, trades in rows:
        meta = tokens_meta.get((cur, iss)) or {}
        if meta:
            display = meta.get("currency_display") or cur
        else:
            decoded = _decode_currency_hex(cur)
            display = decoded or (cur[:8] + "…" if cur and len(cur) > 8 else (cur or "?"))
        lbl = pg_labels.get(iss) or {}
        attested_domain = None
        if lbl.get("source") == "toml":
            attested_domain = (lbl.get("extra") or {}).get("domain")
        sibling_of = seen_issuers.get(iss) if iss else None
        if iss and iss not in seen_issuers:
            seen_issuers[iss] = display
        out.append({
            "display": display,
            "issuer": iss,
            "issuer_short": _short_addr(iss),
            "issuer_label": lbl.get("name"),
            "issuer_attested_domain": attested_domain,
            "sibling_of": sibling_of,
            "trades": trades,
        })
    return out


@app.route("/")
def index():
    """The public landing page. Mosaic of every subsystem so visitors
    immediately see the full scope of the dashboard, not just AMM pools."""
    pulse = fetch_pulse_cached()
    # Render-time heartbeat for the hidden cached-meta hook that drives the
    # 30s [data-live] panel refresh in templates/index.html. Visible label
    # was removed (was claiming cache-age from a now-removed scan call);
    # backlog item tracks adding an honest freshness indicator.
    _now = datetime.now(timezone.utc)
    timestamp_str = _now.strftime("%Y-%m-%d %H:%M:%S UTC")
    timestamp_iso = _now.strftime("%Y-%m-%dT%H:%M:%SZ")
    cached_age = 0.0

    # Pull from the full ranked index so the homepage's headline AMM stat
    # and Top-pools card match what /pools shows. Without this the homepage
    # used the curated ~19-pool snapshot and visibly disagreed with /pools.
    ranked_full, _ranked_meta = _ranked_amm_snapshot()
    ranked_full = sorted(
        ranked_full,
        key=lambda r: (
            0 if (r.get("tvl_usd") or 0) > 0 else 1,
            -(r.get("tvl_usd") or 0),
        ),
    )
    ranked_pool_count = len(ranked_full)
    ranked_total_tvl_usd = sum(
        (r.get("tvl_usd") or 0)
        for r in ranked_full
        if r.get("tvl_status") in ("exact", "estimated")
    )
    # Pools whose TVL we can actually price (XRP-side or stablecoin-side
    # reference). The aggregate above silently excludes anything else, so
    # the homepage must disclose the split to stay truth-first.
    priced_pool_count = sum(
        1 for r in ranked_full
        if r.get("tvl_status") in ("exact", "estimated")
        and (r.get("tvl_usd") or 0) > 0
    )
    ranked_top5 = [r for r in ranked_full if (r.get("tvl_usd") or 0) > 0][:5]

    try:
        cold = fetch_cold_storage_cached()
    except Exception:
        cold = None

    # "Where is XRP?" supply constellation — three on-ledger buckets:
    #   - escrowed:  sum of EscrowCreate objects owned by Ripple's ~20
    #                monthly-release accounts (escrow_supply.py). >99%
    #                of all XRP escrow on the ledger; the small non-
    #                Ripple remainder sits inside "Held by wallets."
    #   - amm:       XRP-side of every AMM pool's reserves
    #   - wallets:   100B design supply − the two locked buckets above.
    # The design supply is a known constant; the live total drifts down
    # by ~0.02%/year via transaction-fee burns, well below display rounding.
    xrp_distribution = _build_xrp_distribution(ranked_full)

    return render_template(
        "index.html",
        timestamp_str=timestamp_str,
        timestamp_iso=timestamp_iso,
        cached_age=cached_age,
        pulse=pulse,
        ranked_top5=ranked_top5,
        ranked_pool_count=ranked_pool_count,
        priced_pool_count=priced_pool_count,
        ranked_total_tvl_usd=ranked_total_tvl_usd,
        recent_whales=_recent_whale_events(limit=3),
        whales_snapshot_at=_whales_snapshot_label(),
        top_tokens=_top_tokens_recent(limit=5),
        cold_storage=cold,
        xrp_distribution=xrp_distribution,
    )


@app.route("/lookup")
def lookup():
    """Legacy AMM-pool finder. The homepage form was retired during the
    landing-page redesign — anyone hitting /lookup directly gets sent to
    the pool browser, which subsumes the use case."""
    return redirect(url_for("pools"), code=302)


def _compute_tvl_shares(pools, top_n=5):
    """Compute donut-chart segments: top N pools + 'Other' bucket, with
    cumulative percentage offsets so each SVG arc starts where the previous
    one ended."""
    if not pools:
        return []
    total = sum(p["total_tvl_usd"] for p in pools)
    if total <= 0:
        return []
    palette = ["#22d3ee", "#3b82f6", "#a855f7", "#ec4899", "#f59e0b", "#5a6680"]
    shares = []
    cum = 0.0
    for i, p in enumerate(pools[:top_n]):
        share = p["total_tvl_usd"] / total
        shares.append({
            "label": p["pair"],
            "tvl": p["total_tvl_usd"],
            "pct": round(share * 100, 2),
            "color": palette[i],
            "offset_pct": round(cum * 100, 2),
        })
        cum += share
    other_total = sum(p["total_tvl_usd"] for p in pools[top_n:])
    if other_total > 0:
        shares.append({
            "label": "Other",
            "tvl": other_total,
            "pct": round((other_total / total) * 100, 2),
            "color": palette[-1],
            "offset_pct": round(cum * 100, 2),
        })
    return shares


def _tier_pools(pools):
    """Split pools into Featured / Top-by-TVL / Other based on whether the
    token's issuer is in named_accounts.json. The pool list as returned
    by scan_all_pools_cached() is already sorted by total_tvl_usd desc."""
    from amm_scan_pools import KNOWN_TOKENS
    named = _load_named_accounts_dict()
    name_to_token = {t["name"]: t for t in KNOWN_TOKENS}

    enriched = []
    for p in pools:
        parts = p["pair"].split("/")
        token_name = parts[1] if len(parts) == 2 else p["pair"]
        token = name_to_token.get(token_name) or {}
        issuer = token.get("issuer")
        named_entry = named.get(issuer) if issuer else None
        ep = dict(p)
        ep["token_name"] = token_name
        ep["token_category"] = token.get("category")
        ep["token_issuer"] = issuer
        ep["named_label"] = (named_entry or {}).get("name")
        ep["is_featured"] = bool(named_entry)
        enriched.append(ep)

    featured = [p for p in enriched if p["is_featured"]]
    rest = [p for p in enriched if not p["is_featured"]]
    top = rest[:10]
    other = rest[10:]
    return featured, top, other, enriched


@app.route("/v2")
def v2_preview():
    """Legacy preview route. The v2 design was promoted to / so /v2 just
    redirects to keep old links working."""
    return redirect(url_for("index"), code=301)


def _safe_load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _safe_count_table(db_path, table):
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()
    except Exception:
        return None


def _file_age_seconds(path):
    if not os.path.exists(path):
        return None
    return max(0, int(datetime.now().timestamp() - os.path.getmtime(path)))


def _tail_lines(path, n=8):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = min(size, 8192)
            f.seek(size - block)
            data = f.read().decode("utf-8", errors="replace")
        return data.splitlines()[-n:]
    except Exception:
        return []


def _humanize_seconds(s):
    if s is None:
        return "—"
    # Clamp clock-skew negatives. Workers on a host whose clock is ahead
    # of the Flask host will produce timestamps "in the future" — that's
    # not a freshness problem, it's a clock-skew artifact. Show as "0s".
    if s < 0:
        s = 0
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m}m"


def _iso_to_age_seconds(iso_str):
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
        return None


def _max_event_ts_localfirst():
    """Latest events.ts honoring the same local-first preference the
    substrate row counts use: read local SQLite when present (authoritative
    on the Mac), otherwise fall back to the mirrored Neon table. Without
    this, a temporarily unreachable Neon would leave Card 5 falsely "idle"
    even while local writes are landing every minute.
    """
    if os.path.exists(EVENTS_DB_PATH):
        try:
            conn = sqlite3.connect(f"file:{EVENTS_DB_PATH}?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT MAX(ts) FROM events").fetchone()
                if row and row[0] is not None:
                    return int(row[0])
            finally:
                conn.close()
        except Exception:
            pass
    try:
        return db.read_max_event_ts()
    except Exception:
        return None


def _health_degrade_state():
    """Read every freshness signal once and derive scan/stream/mirror
    liveness + the overall degrade flag.

    PG + local-file reads only — no XRPL RPC — so `/healthz` and
    `/api/health` can call this without burning the pulse helper on every
    monitor poll. `/health` calls the same function for its UI booleans,
    which guarantees the human page and the machine endpoints can never
    report different degrade states for the same request.
    """
    now = int(time.time())

    pg_hb = db.read_heartbeat_prefix("xrpl_stream")
    pg_hb_age = max(0, now - pg_hb["ts"]) if pg_hb else None

    last_event_ts = _max_event_ts_localfirst()
    pg_events_age = max(0, now - last_event_ts) if last_event_ts else None

    ranker_hb = db.read_heartbeat("amm_ranker")
    ranker_hb_age = max(0, now - ranker_hb["ts"]) if ranker_hb else None

    scan_state = _safe_load_json(SCAN_STATE_PATH) or {}
    stream_state = _safe_load_json(STREAM_STATE_PATH) or {}
    ranker_extra = (ranker_hb.get("extra") if isinstance(ranker_hb, dict) else None) or {}
    scan_finished = scan_state.get("finished_at") or ranker_extra.get("finished_at")
    scan_log_age = _file_age_seconds(SCAN_LOG_PATH)
    stream_log_age = _file_age_seconds(STREAM_LOG_PATH)

    scan_alive_local = scan_finished is None and (scan_log_age or 999) < 300
    ranker_alive_remote = ranker_hb_age is not None and ranker_hb_age < 21600
    scan_alive = scan_alive_local or ranker_alive_remote

    stream_alive_local = (stream_log_age or 999) < 600
    stream_alive_remote = pg_hb_age is not None and pg_hb_age < 900
    stream_alive = stream_alive_local or stream_alive_remote

    mirror_threshold = int(os.environ.get("MIRROR_DEGRADED_AGE_SEC", "18000"))
    mirror_alive = ranker_hb_age is not None and ranker_hb_age < mirror_threshold

    overall = "ok" if scan_alive and stream_alive and mirror_alive else "degraded"
    status_code = 503 if overall == "degraded" else 200

    return {
        "overall": overall,
        "status_code": status_code,
        "scan_alive": scan_alive,
        "stream_alive": stream_alive,
        "mirror_alive": mirror_alive,
        "pg_hb": pg_hb,
        "pg_hb_age": pg_hb_age,
        "last_event_ts": last_event_ts,
        "pg_events_age": pg_events_age,
        "ranker_hb": ranker_hb,
        "ranker_hb_age": ranker_hb_age,
        "scan_state": scan_state,
        "stream_state": stream_state,
        "scan_finished": scan_finished,
        "scan_log_age": scan_log_age,
        "stream_log_age": stream_log_age,
        "mirror_threshold": mirror_threshold,
    }


def _stored_data_state(pg_events_age):
    """Card 5 ("Stored data") freshness state.

    Anchors on `pg_events_age` (last events row's ts vs now) — Card 3
    watches the stream heartbeat, Card 5 watches whether the stream's
    output is actually landing on disk. Distinct signals: a stream whose
    WebSocket is alive but whose DB writes are silently failing would
    leave Card 3 green and grow `pg_events_age` indefinitely.

    Thresholds calibrated against 7-day write cadence in events.db
    (avg gap 88s, max legitimate quiet-period gap ~40m): 1h ok comfortably
    above normal idle, 6h matches the ranker-alive ceiling used elsewhere.
    """
    if pg_events_age is None:
        return "idle"
    if pg_events_age < 3600:
        return "ok"
    if pg_events_age < 21600:
        return "warn"
    return "err"


@app.route("/health")
def health():
    """Operational status page for the background workers.

    Public on purpose — the project's trust positioning says we publish
    real metrics about real infrastructure. Anyone can see whether the
    bootstrap scan is making progress, whether the live watcher is
    catching transactions, and how much data has accumulated.
    """
    # Cross-machine truth: workers on the Mac dual-write heartbeats + ranked
    # snapshots to Neon, so prod (Render) — which has none of the Mac's JSON
    # files — can still report real state. The shared `_health_degrade_state`
    # helper performs every freshness read once so /health (human UI) and
    # /healthz (monitor JSON) can never disagree about the degrade verdict.
    state = _health_degrade_state()
    scan_state = state["scan_state"]
    stream_state = state["stream_state"]
    pg_hb = state["pg_hb"]
    pg_hb_age = state["pg_hb_age"]
    pg_hb_extra = (pg_hb.get("extra") if isinstance(pg_hb, dict) else None) or {}
    last_event_ts = state["last_event_ts"]
    pg_events_age = state["pg_events_age"]
    ranker_hb = state["ranker_hb"]
    ranker_hb_age = state["ranker_hb_age"]
    ranker_hb_extra = (ranker_hb.get("extra") if isinstance(ranker_hb, dict) else None) or {}
    scan_finished = state["scan_finished"]
    scan_log_age = state["scan_log_age"]
    stream_log_age = state["stream_log_age"]
    scan_alive = state["scan_alive"]
    stream_alive = state["stream_alive"]
    mirror_alive = state["mirror_alive"]
    mirror_threshold = state["mirror_threshold"]
    overall = state["overall"]
    status_code = state["status_code"]
    mirror_last_success_age = ranker_hb_age

    scan_started = scan_state.get("started_at") or ranker_hb_extra.get("started_at")
    scan_uptime = _iso_to_age_seconds(scan_started)
    scan_pages = scan_state.get("pages", 0)
    scan_rate = round(scan_pages / scan_uptime, 2) if scan_uptime else None

    stream_started = stream_state.get("started_at") or pg_hb_extra.get("started_at")
    stream_uptime = _iso_to_age_seconds(stream_started)

    amm_index = _safe_load_json(AMM_INDEX_PATH) or []
    amms_in_index = len(amm_index) if isinstance(amm_index, list) and amm_index \
        else ranker_hb_extra.get("indexed_count")

    # Bootstrap-scan freshness: amm_index.json is the full-crawl snapshot that
    # xrpl_stream.py extends incrementally. The bootstrap re-scan isn't on a
    # launchd timer (intentional — it's a multi-hour walk of the entire ledger
    # AMM space), so stream-driven discovery is the canonical path. Surfacing
    # the index file's age lets visitors see when the last full reconciliation
    # ran, so silent stream-filter drops would become visible as the gap grows.
    bootstrap_age_sec = _file_age_seconds(AMM_INDEX_PATH)

    # On hosts without local bootstrap-scanner state (Render), the tech block
    # used to show pages/objects/scan-rate as 0/—, which read as a stuck
    # worker even though the ranker cron was keeping a 29k-pool catalogue
    # fresh (2026-05-13 audit). When mode == "ranker", the template surfaces
    # ranker cadence instead of scanner counters. com.charliebruce.xrpldashboard.rank_amms
    # runs every 3600s (verified via `launchctl print`).
    scan_mode = "scanner" if scan_state else "ranker"
    ranker_next_in = (
        max(0, 3600 - ranker_hb_age) if ranker_hb_age is not None else None
    )

    # Pool tracker is "finished" (catalogue available) whenever the ranker
    # has produced a snapshot — even if this host has no local scan state.
    pool_finished = scan_finished is not None or (
        ranker_hb is not None and (amms_in_index or 0) > 0
    )
    # Catalogue exists but the worker hasn't touched it recently. The page
    # used to render this as a healthy "ready" badge with an "all systems
    # normal" banner, which lies if the ranker has been silent for hours.
    pool_stale = pool_finished and not scan_alive

    stored_state = _stored_data_state(pg_events_age)

    pulse = fetch_pulse_cached()

    return render_template(
        "health.html",
        overall=overall,
        pulse=pulse,
        scan={
            "alive": scan_alive,
            "finished": pool_finished,
            "stale": pool_stale,
            "mode": scan_mode,
            "uptime": _humanize_seconds(scan_uptime),
            "pages": scan_pages,
            "objects_scanned": scan_state.get("raw_objects_scanned", 0),
            "rate": scan_rate,
            "ledger_index": scan_state.get("ledger_index"),
            "log_age": _humanize_seconds(scan_log_age if scan_log_age is not None else ranker_hb_age),
            "ranker_age": _humanize_seconds(ranker_hb_age) if ranker_hb_age is not None else None,
            "ranker_next_in": _humanize_seconds(ranker_next_in) if ranker_next_in else None,
            "amms_in_index": amms_in_index,
            "snapshot_at": _pools_snapshot_label(),
            "bootstrap_age": _humanize_seconds(bootstrap_age_sec),
            "bootstrap_age_sec": bootstrap_age_sec,
        },
        stream={
            "alive": stream_alive,
            "uptime": _humanize_seconds(stream_uptime),
            "txns_seen": stream_state.get("txns_seen") or (pg_hb.get("txns_seen") if pg_hb else 0) or 0,
            "amm_creates": stream_state.get("amm_creates_seen") or pg_hb_extra.get("amm_creates_seen", 0) or 0,
            "whale_events": stream_state.get("whale_events_seen") or pg_hb_extra.get("whale_events_seen", 0) or 0,
            "token_events": stream_state.get("token_events_seen") or pg_hb_extra.get("token_events_seen", 0) or 0,
            "new_tokens": stream_state.get("new_tokens_seen") or pg_hb_extra.get("new_tokens_seen", 0) or 0,
            "last_ledger": stream_state.get("last_ledger_index") or (pg_hb.get("last_ledger") if pg_hb else None),
            "log_age": _humanize_seconds(
                pg_events_age if pg_events_age is not None
                else (stream_log_age if stream_log_age is not None else pg_hb_age)
            ),
            "seen_tokens_count": len(stream_state.get("seen_tokens", []) or []),
        },
        mirror={
            "alive": mirror_alive,
            "last_success_age": _humanize_seconds(mirror_last_success_age) if mirror_last_success_age is not None else None,
            "last_success_age_sec": mirror_last_success_age,
            "threshold": _humanize_seconds(mirror_threshold),
            "threshold_sec": mirror_threshold,
        },
        substrate={
            # Prefer local SQLite (authoritative on the Mac). On Render the
            # files don't exist, so fall back to the mirrored Neon tables —
            # otherwise the page renders "—" and looks broken.
            "events_rows": (
                _safe_count_table(EVENTS_DB_PATH, "events")
                if os.path.exists(EVENTS_DB_PATH)
                else (db.count_table("events") if db.pg_available() else None)
            ),
            "volumes_rows": (
                _safe_count_table(VOLUMES_DB_PATH, "token_volume")
                if os.path.exists(VOLUMES_DB_PATH)
                else (db.count_table("token_volume") if db.pg_available() else None)
            ),
            # Row counts alone are cumulative — they only grow, so a frozen
            # write path leaves the card visually identical. `stored_state`
            # is anchored on `pg_events_age` so a stream that's silently
            # failing to land rows downgrades this card even while Card 3
            # (stream heartbeat) still reads alive.
            "stored_state": stored_state,
            "events_age": _humanize_seconds(pg_events_age),
            "events_age_sec": pg_events_age,
        },
        recent_log=_tail_lines(STREAM_LOG_PATH, n=8),
    ), status_code


@ttl_cache(seconds=60)
def _load_named_accounts_dict():
    return _safe_load_json(NAMED_ACCOUNTS_PATH) or {}


@ttl_cache(seconds=3600)
def _load_continent_map():
    """ISO-3166 alpha-2 → continent (UN M49 5-region + Antarctica).
    Cloudflare special codes (T1 Tor, ? no header) are handled by the
    caller — this map only covers real ISO codes."""
    raw = _safe_load_json(ISO_CONTINENT_PATH) or {}
    return {k: v for k, v in raw.items() if len(k) == 2 and k.isupper()}


def _continent_aggregate(country_rows):
    """Fold a (country, views, uniques) breakdown into a continent
    breakdown. `country_rows` is what read_country_breakdown returns
    with a high limit (so the aggregate covers ALL origins, not just
    the top-10 table). Tor exit nodes (T1) and rows with no CF header
    (?) are bucketed as 'Unknown' — they're real people but their
    physical continent is unknowable from the data we have. Returned
    list is sorted by uniques desc."""
    cmap = _load_continent_map()
    agg = {}
    for country, views, uniques in country_rows:
        continent = cmap.get(country) if country else None
        if not continent:
            continent = "Unknown"
        entry = agg.setdefault(continent, {"views": 0, "uniques": 0})
        entry["views"] += int(views or 0)
        entry["uniques"] += int(uniques or 0)
    return sorted(
        [(c, v["views"], v["uniques"]) for c, v in agg.items()],
        key=lambda r: -r[2],
    )


@ttl_cache(seconds=300)
def _load_d1_hero_snapshot():
    """Spec-locked D1 v5 hero data (Fable's numbers table).

    Regenerated by scripts/gen_d1_hero_snapshot.py from Charlie's paired
    scratch/ files after each spec version. Prod reads this instead of
    live-computing so the render matches the review numbers exactly. The
    30-day walker re-anchor triggers the next regen against fresh volume
    aggregates.
    """
    return _safe_load_json(D1_HERO_SNAPSHOT_PATH) or {}


@ttl_cache(seconds=60)
def _load_token_names_dict():
    """Build {(currency_hex, issuer): entry} for fast lookup. Entries
    still tagged `TODO_curation_pass` are excluded from the live render
    per TOKEN_NAMES.md policy ("never publish a name we can't back to a
    first-party source"). They stay in token_names.json as curation
    history — when a `verified_via` lands, the entry goes live with no
    code change."""
    raw = _safe_load_json(TOKEN_NAMES_PATH) or {}
    out = {}
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("verified_via") == "TODO_curation_pass":
            continue
        out[(entry.get("currency_hex"), entry.get("issuer"))] = entry
    return out


def _short_addr(addr):
    if not addr:
        return None
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 14 else addr


def _format_xrp_price(p):
    """Render an XRP-per-token price as a short string across the full
    magnitude range we see on /tokens (5e-8 dust → 56,000+ for wrapped BTC).
    Returns None when price is missing so the template can branch on it."""
    if p is None:
        return None
    try:
        pf = float(p)
    except (TypeError, ValueError):
        return None
    if pf >= 1000:
        return f"{pf:,.0f}"
    if pf >= 1:
        return f"{pf:,.4f}".rstrip("0").rstrip(".")
    if pf >= 0.0001:
        return f"{pf:.6f}".rstrip("0").rstrip(".")
    # Below 0.0001 XRP — scientific avoids a long string of leading zeros.
    return f"{pf:.3g}"


def _disambiguate_labels(parties):
    """Given [(label, addr), ...], append a 6-char address tail to any
    label that appears more than once in the row — so Binance → Binance
    (hot → cold) reads as Binance · …xxxxxx → Binance · …yyyyyy instead
    of an apparent self-send. Generalizes to any N-party row. Returns
    the adjusted label list in input order; unlabeled entries pass
    through untouched."""
    counts = {}
    for lbl, _addr in parties:
        if lbl:
            counts[lbl] = counts.get(lbl, 0) + 1
    out = []
    for lbl, addr in parties:
        if lbl and counts.get(lbl, 0) > 1 and addr and len(addr) >= 6:
            out.append(f"{lbl} · …{addr[-6:]}")
        else:
            out.append(lbl)
    return out


def _decode_currency_hex(hex_str):
    """XRPL non-standard currencies are 40-char hex with NUL padding.
    If the bytes are printable ASCII, render that; otherwise return None
    and the caller can show a truncated hex."""
    if not hex_str or len(hex_str) != 40:
        return None
    try:
        b = bytes.fromhex(hex_str).rstrip(b"\x00")
    except ValueError:
        return None
    if not b or not all(32 <= c < 127 for c in b):
        return None
    try:
        return b.decode("ascii")
    except UnicodeDecodeError:
        return None


def _format_xrp(drops):
    if drops is None:
        return None
    return f"{drops / 1_000_000:,.2f} XRP"


def _format_token_amount(value, currency_display):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return f"? {currency_display}"
    if v >= 1000:
        return f"{v:,.2f} {currency_display}"
    if v >= 1:
        return f"{v:,.4f} {currency_display}"
    return f"{v:.8f} {currency_display}"


def _resolve_event(row, named_accounts, token_names):
    """Turn a sqlite events row into a dict ready for the template.

    Falls back to raw_json when the dedicated columns are sparse — handles
    cases like RLUSD payments where the tx field is `DeliverMax`, not
    `Amount`, so the writer left currency/issuer NULL.
    """
    (tx_hash, ledger_index, ts, etype, from_addr, to_addr,
     amount_drops, currency, issuer, raw_json) = row

    raw = {}
    try:
        raw = json.loads(raw_json) if raw_json else {}
    except Exception:
        pass
    tx = raw.get("transaction") or raw.get("tx_json") or {}
    delivered = (raw.get("meta") or {}).get("delivered_amount")

    amount_obj = delivered if delivered is not None else (
        tx.get("Amount") or tx.get("DeliverMax")
    )

    amount_display = None
    if isinstance(amount_obj, str):
        try:
            amount_display = _format_xrp(int(amount_obj))
        except ValueError:
            pass
    elif isinstance(amount_obj, dict):
        cur = amount_obj.get("currency")
        iss = amount_obj.get("issuer")
        val = amount_obj.get("value")
        token = token_names.get((cur, iss))
        cur_disp = (token or {}).get("currency_display") or (
            (cur[:6] + "…") if cur and len(cur) > 6 else cur
        ) or "?"
        amount_display = _format_token_amount(val, cur_disp)
        currency = currency or cur
        issuer = issuer or iss
    elif amount_drops is not None:
        amount_display = _format_xrp(amount_drops)

    if etype == "trustset" and amount_display is None:
        token = token_names.get((currency, issuer)) if (currency and issuer) else None
        cur_disp = (token or {}).get("currency_display") or (
            (currency[:6] + "…") if currency and len(currency) > 6 else currency
        ) or "?"
        amount_display = f"trustline → {cur_disp}"

    if amount_display is None:
        amount_display = "—"

    def _label(addr):
        info = named_accounts.get(addr) if addr else None
        return (info or {}).get("name")

    def _attested_domain(addr):
        info = named_accounts.get(addr) if addr else None
        if not info or info.get("_source") != "toml":
            return None
        extra = info.get("_extra") or {}
        return extra.get("domain")

    age_seconds = max(0, int(time.time() - ts))
    type_labels = {
        "large_xfer": "whale",
        "tagged":     "tagged",
        "trustset":   "trustline",
    }
    # Reader-facing pill category. 'tagged' is DB shorthand for "named
    # account touched this tx"; 'watchlist' is what a first-time visitor
    # can parse. Kept as its own field so /whales' existing badge (which
    # still renders type_display) is untouched by this homepage change.
    row_type_pill = {
        "large_xfer": "whale",
        "tagged":     "watchlist",
    }.get(etype)

    from_label_raw = _label(from_addr)
    to_label_raw = _label(to_addr)
    from_label, to_label = _disambiguate_labels([
        (from_label_raw, from_addr),
        (to_label_raw, to_addr),
    ])

    return {
        "tx_hash": tx_hash,
        "tx_hash_short": (tx_hash[:10] + "…") if tx_hash else "?",
        "ledger": ledger_index,
        "age": _humanize_seconds(age_seconds),
        "type": etype,
        "type_display": type_labels.get(etype, etype),
        "from_addr": from_addr,
        "from_addr_short": _short_addr(from_addr),
        "from_label": from_label,
        "from_attested_domain": _attested_domain(from_addr),
        "to_addr": to_addr,
        "to_addr_short": _short_addr(to_addr),
        "to_label": to_label,
        "to_attested_domain": _attested_domain(to_addr),
        "amount_display": amount_display,
        "row_type_pill": row_type_pill,
        "xrpscan_url": f"https://xrpscan.com/tx/{tx_hash}" if tx_hash else None,
    }


@app.route("/whales")
def whales():
    """Public stream of large XRP moves and watchlisted-account activity.

    Source: events.db (xrpl_stream.py whale_handler). Read-only sqlite
    open so the live writer is never blocked.
    """
    filter_type = (request.args.get("type") or "").strip().lower()
    valid_types = {"large_xfer", "tagged", "trustset"}
    if filter_type not in valid_types:
        filter_type = ""

    # Tier filter — SQL pre-filter only knows about `amount_drops`. Tagged
    # events whose Amount is a token live with amount_drops=NULL, so they
    # slip past the SQL gate and get sized in Python below via price_oracle.
    tier_map = {
        "1m":   ("≥1M XRP",   1_000_000 * 1_000_000),
        "100k": ("≥100K XRP",   100_000 * 1_000_000),
        "50k":  ("≥50K XRP",     50_000 * 1_000_000),
    }
    # Default tier = 100k to match the page's meta description and the audience's
    # intuition of "whale" (a 1M-XRP default surfaced ≥1M only, leaving the
    # 100K-1M band invisible unless explicitly opted into via ?tier=100k —
    # which contradicted the "every payment over 100,000 XRP" meta copy).
    tier = (request.args.get("tier") or "100k").strip().lower()
    if tier not in tier_map:
        tier = "100k"
    tier_label, tier_drops = tier_map[tier]

    # TEMPORARY FLEET BLOCK — revert when CF Managed Challenge is live on /whales.
    # Fingerprint: IL country + Chrome/142 UA = rotating residential proxy fleet
    # confirmed 2026-07-22 (2,036 unique IPs, single UA string, 3am-7am IDT cron).
    # Retry-After: 86400 tells well-behaved scrapers to back off 24h; evidence of
    # compliance lands in whales_cache_daily.blocked each morning.
    _req_country = request.headers.get("CF-IPCountry", "")
    _req_ua = request.headers.get("User-Agent", "")
    if _req_country == "IL" and "Chrome/142" in _req_ua:
        with _WHALES_CACHE_LOCK:
            _WHALES_CACHE_STATS["blocked"] += 1
        _maybe_flush_whales_receipts(force=True)
        return Response("", status=429, headers={"Retry-After": "86400"})

    # 60s in-process cache — closed 12-bucket key space, see module-level
    # _WHALES_CACHE comment. Lookup happens after normalization so the key
    # is guaranteed to be one of the 12 legit tuples regardless of what
    # the client sent. Hit path returns the cached response body directly;
    # miss path continues into the render logic below and caches at return.
    _whales_cache_key = (tier, filter_type)
    _whales_now = time.time()
    _whales_serve_stale = False
    _cached_body = None
    if not getattr(_CACHE_REBUILD_LOCAL, "bypass", False):
        with _WHALES_CACHE_LOCK:
            _cached_entry = _WHALES_CACHE.get(_whales_cache_key)
            if _cached_entry:
                _cached_body = _cached_entry[1]
                if _cached_entry[0] > _whales_now:
                    _WHALES_CACHE_STATS["hits"] += 1
                else:
                    # SWR: expired but body exists — serve it now, rebuild in bg.
                    _WHALES_CACHE_STATS["stale_serves"] += 1
                    _whales_serve_stale = True
    if _cached_body is not None:
        if _whales_serve_stale:
            _trigger_whales_rebuild(tier, filter_type)
        _maybe_flush_whales_receipts(force=False)
        return _cached_body
    _whales_render_start = time.perf_counter()

    # Default view = value movement. Trustset events have no Amount and are
    # signal, not movement — they read as noise alongside ≥1M XRP transfers.
    # Users opt in to them via the "trustlines" pill (filter_type='trustset'),
    # which short-circuits the tier gate (trustset rows have amount_drops=NULL
    # and would otherwise be excluded by it).
    if filter_type == "trustset":
        clauses = ["type = 'trustset'"]
        params = []
    else:
        # XRP-denominated tagged events have amount_drops populated and can be
        # gated cheaply in SQL alongside large_xfer. Token-denominated tagged
        # events have amount_drops=NULL and slip through to be priced in Python.
        clauses = [
            "((type = 'tagged' AND amount_drops IS NULL) "
            "OR amount_drops >= ?)"
        ]
        params = [tier_drops]
        if filter_type:
            clauses.append("type = ?")
            params.append(filter_type)
    where_clause = "WHERE " + " AND ".join(clauses)

    events = []
    radar_blips = []
    type_counts = {"large_xfer": 0, "tagged": 0, "trustset": 0, "_total": 0}
    rows = None

    # Prefer Postgres (worker dual-writes); fall back to the local/committed
    # SQLite snapshot on any error or when DATABASE_URL is unset.
    if db.pg_available():
        try:
            type_counts = db.read_whale_type_counts(tier_drops)
            rows = db.read_whale_events(tier_drops, filter_type or None)
        except Exception:
            rows = None
            type_counts = {"large_xfer": 0, "tagged": 0, "trustset": 0, "_total": 0}

    if rows is None and os.path.exists(EVENTS_DB_PATH):
        try:
            conn = sqlite3.connect(f"file:{EVENTS_DB_PATH}?mode=ro", uri=True)
            try:
                for r in conn.execute(
                    "SELECT type, COUNT(*) FROM events "
                    "WHERE type = 'trustset' "
                    "   OR (type = 'tagged' AND amount_drops IS NULL) "
                    "   OR amount_drops >= ? "
                    "GROUP BY type",
                    (tier_drops,),
                ):
                    if r[0] in type_counts:
                        type_counts[r[0]] = r[1]
                    # _total = the count shown on the default "All" tile, which
                    # mirrors the default-view row list — value movement only.
                    if r[0] != "trustset":
                        type_counts["_total"] += r[1]
                rows = conn.execute(
                    f"SELECT tx_hash, ledger_index, ts, type, from_addr, to_addr, "
                    f"amount_drops, currency, issuer, raw_json FROM events "
                    f"{where_clause} ORDER BY ts DESC LIMIT 100",
                    params,
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            rows = None

    if rows:
        named = _load_named_accounts_dict()
        tokens = _load_token_names_dict()
        # Layer PG `account_labels` on top of the file-based named_accounts.
        # File entries (xrp-ledger.toml-verified Ripple escrows etc.) always
        # win — they're first-party verified. PG fills the long tail:
        # curated xrpscan paste-ins, plus derived:amm / derived:mpt labels
        # generated from on-chain state. Failure here is swallowed; the
        # page renders fine with file-only labels if PG hiccups.
        if db.pg_available():
            page_addrs = set()
            for _r in rows:
                if _r[4]:
                    page_addrs.add(_r[4])
                if _r[5]:
                    page_addrs.add(_r[5])
            page_addrs.difference_update(named.keys())
            if page_addrs:
                try:
                    pg_labels = db.read_account_labels(list(page_addrs))
                    for addr, info in pg_labels.items():
                        named[addr] = {
                            "name": info.get("name"),
                            "category": info.get("category"),
                            "_source": info.get("source"),
                        }
                except Exception:
                    pass
        # Apply the user's tier threshold to `tagged` events too (previously
        # they bypassed all size gating, which let sub-penny dust from named
        # wallets surface alongside 1M-XRP transfers). For XRP-denominated
        # tagged events we can decide from amount_drops alone; for token
        # amounts we price-convert via the AMM-backed oracle. Unknown prices
        # are kept so we don't silently hide a possibly-large move.
        tier_xrp = tier_drops / 1_000_000
        filtered_rows = []
        for r in rows:
            etype = r[3]
            if etype != "tagged":
                filtered_rows.append(r)
                continue
            drops = r[6]
            if isinstance(drops, int) and drops > 0:
                if drops >= tier_drops:
                    filtered_rows.append(r)
                continue
            # Token-denominated tagged event: pull Amount/delivered from raw_json
            # and convert to XRP. Skip if priced and below threshold.
            raw_json = r[9]
            amount_obj = None
            if raw_json:
                try:
                    raw = json.loads(raw_json)
                    tx = raw.get("transaction") or raw.get("tx_json") or {}
                    amount_obj = (raw.get("meta") or {}).get("delivered_amount") or \
                                 tx.get("Amount") or tx.get("DeliverMax")
                except Exception:
                    amount_obj = None
            xrp_value = None
            if amount_obj is not None:
                try:
                    xrp_value = price_oracle.value_amount_xrp(amount_obj)
                except Exception:
                    xrp_value = None
            if xrp_value is not None and xrp_value < tier_xrp:
                continue
            filtered_rows.append(r)
        rows = filtered_rows
        events = [_resolve_event(r, named, tokens) for r in rows]
        # Radar blips: log-scale magnitudes from raw rows (amount_drops
        # at index 6) so 1M XRP ≈ 0.3 and 100M+ ≈ 1.0.
        for r in rows[:30]:
            drops = r[6]
            if not isinstance(drops, int) or drops <= 0:
                continue
            xrp = drops / 1_000_000.0
            mag = max(0.2, min(1.0, (math.log10(xrp + 10) - 5.0) / 3.0 + 0.4))
            radar_blips.append({"mag": round(mag, 3), "kind": r[3] or "large_xfer"})

    # Cap historical blips: more than ~8 makes the radar look constantly active
    # on page load (each blip spawns 1100ms apart, so 8 = ~9s of animation).
    radar_blips = radar_blips[:8]

    # Real readings for the two HUD corners (replace the old fake
    # bearing/range/contacts text). Always reflect the canonical 100K-XRP
    # whale floor so the numbers don't shift with UI tier filters.
    radar_floor_drops = WHALE_XRP_THRESHOLD * 1_000_000
    radar_stats = {"last_24h": 0, "last_amount_drops": None}
    if db.pg_available():
        try:
            radar_stats = db.read_whale_radar_stats(radar_floor_drops)
        except Exception:
            radar_stats = {"last_24h": 0, "last_amount_drops": None}
    if radar_stats["last_24h"] == 0 and radar_stats["last_amount_drops"] is None \
            and os.path.exists(EVENTS_DB_PATH):
        try:
            conn = sqlite3.connect(f"file:{EVENTS_DB_PATH}?mode=ro", uri=True)
            try:
                cutoff_ts = time.time() - 24 * 3600
                row = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE type = 'large_xfer' "
                    "AND amount_drops >= ? AND ts >= ?",
                    (radar_floor_drops, cutoff_ts),
                ).fetchone()
                if row:
                    radar_stats["last_24h"] = int(row[0] or 0)
                row = conn.execute(
                    "SELECT amount_drops FROM events WHERE type = 'large_xfer' "
                    "AND amount_drops >= ? ORDER BY ts DESC LIMIT 1",
                    (radar_floor_drops,),
                ).fetchone()
                if row and row[0]:
                    radar_stats["last_amount_drops"] = int(row[0])
            finally:
                conn.close()
        except Exception:
            pass

    last_drops = radar_stats.get("last_amount_drops")
    if last_drops:
        xrp = last_drops / 1_000_000.0
        if xrp >= 1_000_000:
            radar_stats["last_label"] = f"{xrp / 1_000_000:.1f}M XRP"
        elif xrp >= 1_000:
            radar_stats["last_label"] = f"{xrp / 1_000:.0f}K XRP"
        else:
            radar_stats["last_label"] = f"{xrp:,.0f} XRP"
    else:
        radar_stats["last_label"] = "—"

    _whales_body = render_template(
        "whales.html",
        events=events,
        filter_type=filter_type,
        tier=tier,
        tier_label=tier_label,
        tier_drops=tier_drops,
        type_counts=type_counts,
        threshold_xrp=WHALE_XRP_THRESHOLD,
        named_accounts_count=len(_load_named_accounts_dict()),
        radar_blips=radar_blips,
        radar_stats=radar_stats,
        data_age_label=_format_age_seconds(_events_db_age_seconds()),
    )
    _whales_gen_ms = int(
        (time.perf_counter() - _whales_render_start) * 1000
    )
    with _WHALES_CACHE_LOCK:
        _WHALES_CACHE[_whales_cache_key] = (
            _whales_now + _WHALES_CACHE_TTL_S,
            _whales_body,
            _whales_gen_ms,
        )
        _WHALES_CACHE_STATS["misses"] += 1
        _hits_total = _WHALES_CACHE_STATS["hits"]
        _misses_total = _WHALES_CACHE_STATS["misses"]
    app.logger.info(
        "whales_cache: hit=%d miss=%d gen_ms=%d key=%s",
        _hits_total, _misses_total, _whales_gen_ms, _whales_cache_key,
    )
    _maybe_flush_whales_receipts(force=True)
    return _whales_body


@app.route("/tokens")
def tokens():
    """Token activity ranked by recent on-ledger trade count.

    Source: volumes.db (xrpl_stream.py token_event_handler). Counts every
    Payment carrying a token amount + every AMMDeposit / AMMWithdraw,
    bucketed hourly. XRP-equivalent volume is a 0.0 placeholder until
    token_prices.py exists — for now we rank on raw trade count, which is
    already a useful "what's people are touching" signal.
    """
    valid_ranges = {"24h": 24, "7d": 24 * 7, "all": None}
    range_key = (request.args.get("range") or "24h").strip().lower()
    if range_key not in valid_ranges:
        range_key = "24h"
    hours_back = valid_ranges[range_key]

    rows = None
    earliest_bucket = None
    latest_bucket = None
    total_buckets = 0

    # Prefer Postgres (worker dual-writes); fall back to local volumes.db.
    if db.pg_available():
        try:
            rows = db.read_token_volume_aggregates(
                hours_back=hours_back, limit=50
            )
            stats = db.read_token_volume_bucket_stats()
            earliest_bucket, latest_bucket, total_buckets = (
                stats[0], stats[1], stats[2]
            )
        except Exception:
            rows = None
            earliest_bucket = latest_bucket = None
            total_buckets = 0

    if rows is None and os.path.exists(VOLUMES_DB_PATH):
        try:
            conn = sqlite3.connect(f"file:{VOLUMES_DB_PATH}?mode=ro", uri=True)
            try:
                where = ""
                params = []
                if hours_back is not None:
                    cutoff = int(time.time() // 3600) - hours_back
                    where = "WHERE hour_bucket >= ?"
                    params.append(cutoff)
                rows = conn.execute(
                    f"SELECT currency, issuer, "
                    f"       SUM(trade_count) AS trades, "
                    f"       COUNT(*) AS hours_active "
                    f"FROM token_volume {where} "
                    f"GROUP BY currency, issuer "
                    f"ORDER BY trades DESC LIMIT 50",
                    params,
                ).fetchall()
                stats = conn.execute(
                    "SELECT MIN(hour_bucket), MAX(hour_bucket), "
                    "COUNT(DISTINCT hour_bucket) FROM token_volume"
                ).fetchone()
                earliest_bucket, latest_bucket, total_buckets = (
                    stats[0], stats[1], stats[2]
                )
            finally:
                conn.close()
        except Exception:
            rows = []

    if rows is None:
        rows = []

    tokens_meta = _load_token_names_dict()
    hero_snapshot = _load_d1_hero_snapshot()
    tier_lookup = (hero_snapshot.get("tiers") or {}).get("lookup") or {}
    # Single read of the per-token XRP price snapshot — rendered as a sub-line
    # on each row. Absent rows render "—" in the template; per token_prices.py,
    # the absence IS the signal (no XRP pool above the 1,000-XRP dust floor),
    # not a placeholder to backfill.
    price_map = db.read_token_prices_map() if db.pg_available() else {}
    enriched = []
    for cur, iss, trades, hours_active in rows:
        meta = tokens_meta.get((cur, iss)) or {}
        if meta:
            display = meta.get("currency_display") or cur
            category = meta.get("category")
            labeled = True
        else:
            decoded = _decode_currency_hex(cur)
            display = decoded or (cur[:8] + "…" if cur and len(cur) > 8 else (cur or "?"))
            category = None
            labeled = False
        # BROAD Axelar reading (D1 v5 §2): every currency the Axelar gateway
        # issues counts as bridge, not just tokens hand-tagged in
        # token_names.json (only 1 of 9 currently is). Backfill deferred.
        if iss == AXELAR_BRIDGE_ISSUER:
            category = "bridge"
            labeled = True
        # Labeled-but-no-category tokens go in the "other named" bar. Distinct
        # from unlabeled (see Zone A hero note): these have identities, they
        # just don't fit a standard bucket.
        if labeled and (category is None or category == "no_category"):
            category = "other"
        # v3 §7 attestation shape — verified / self-described / (bare).
        # DOMAIN_ONLY + ANONYMOUS both display bare per Charlie's editorial
        # rule (never say "verified" for lower tiers).
        tier_raw = tier_lookup.get(f"{cur}|{iss}")
        if tier_raw == "VERIFIED":
            attestation = "verified"
        elif tier_raw == "SELF_DESCRIBED":
            attestation = "self-described"
        else:
            attestation = None
        price = price_map.get((cur, iss))
        enriched.append({
            "rank": len(enriched) + 1,
            "currency_raw": cur,
            "issuer": iss,
            "issuer_short": _short_addr(iss),
            "display": display,
            "category": category,
            "labeled": labeled,
            "attestation": attestation,
            "trades": trades,
            "hours_active": hours_active,
            "xrp_price": price,
            "xrp_price_str": _format_xrp_price(price),
        })

    # Sibling-issuer accent — a single issuer can mint multiple tokens
    # (e.g. r3qWgp…Kp4R issues both RPR and ASC). Without a visual cue,
    # two adjacent rows with the same truncated address read as unrelated.
    # Assign a left-border color to every row whose issuer appears more
    # than once in the visible list; cycle through a small palette so
    # multiple sibling groups stay distinguishable. Palette avoids the
    # category-badge colors so the two signals don't visually collide.
    _SIBLING_PALETTE = ["#c084fc", "#14b8a6", "#f97316", "#6366f1"]
    _issuer_counts = Counter(t["issuer"] for t in enriched)
    _multi_issuers = sorted(iss for iss, c in _issuer_counts.items() if c > 1)
    _sibling_color = {iss: _SIBLING_PALETTE[i % len(_SIBLING_PALETTE)]
                      for i, iss in enumerate(_multi_issuers)}
    for t in enriched:
        t["sibling_color"] = _sibling_color.get(t["issuer"])

    earliest_iso = None
    if earliest_bucket is not None:
        earliest_iso = datetime.fromtimestamp(
            earliest_bucket * 3600, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
    latest_iso = None
    if latest_bucket is not None:
        latest_iso = datetime.fromtimestamp(
            latest_bucket * 3600, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")

    # Category bars hero — group enriched tokens by category, compute share
    # of total trade count per category, and expose top-N segments per bar.
    # Order intentionally fixed so the hero layout is stable across page
    # loads. "other" is unlabeled tokens (no curated category).
    # Bars cover labeled categories only. Unlabeled tokens dominate raw
    # trade volume on XRPL (memecoins issued by unknown accounts, etc.) and
    # would visually flatten every labeled category to invisible slivers.
    # The full list below the hero still includes unlabeled rows.
    # D1 v5 hero uses a fixed 30-day window regardless of the page-level
    # range selector — the hero tells a stable 30d composition story,
    # while the range selector still drives the sortable list below.
    # A separate wide-limit pull is needed because the range-selector
    # pull caps at 50 rows and misses long-tail Zone A share.
    hero_enriched = enriched
    if db.pg_available():
        try:
            hero_rows_30d = db.read_token_volume_aggregates(
                hours_back=24 * 30, limit=1500
            )
            hero_enriched = []
            for cur, iss, trades, hours_active in hero_rows_30d:
                meta = tokens_meta.get((cur, iss)) or {}
                if meta:
                    display = meta.get("currency_display") or cur
                    category = meta.get("category")
                    labeled = True
                else:
                    category = None
                    labeled = False
                if iss == AXELAR_BRIDGE_ISSUER:
                    category = "bridge"
                    labeled = True
                if labeled and (category is None or category == "no_category"):
                    category = "other"
                hero_enriched.append({
                    "display": display if meta else (
                        _decode_currency_hex(cur)
                        or (cur[:8] + "…" if cur and len(cur) > 8 else (cur or "?"))
                    ),
                    "category": category,
                    "labeled": labeled,
                    "trades": trades,
                })
        except Exception:
            hero_enriched = enriched

    # D1 v5 hero — Zone A. 7 named-category bars. Order shown top-down.
    # `other` = labeled tokens that don't fit a named bucket (see coerce
    # step above); NOT the same as `Unlabeled` (which has no identity at
    # all). Both are rendered but visually + linguistically separated.
    cat_order = [
        ("stablecoin",     "stablecoins",        "34,197,94"),   # green
        ("native_utility", "utility",            "34,211,238"),  # cyan
        ("memecoin",       "memecoins",          "236,72,153"),  # pink
        ("wrapped_major",  "wrapped majors",     "245,158,11"),  # amber
        ("bridge",         "bridge tokens",      "168,85,247"),  # violet
        ("fiat",           "fiat tokens",        "16,185,129"),  # emerald
        ("other",          "other named tokens", "148,163,184"), # slate
    ]
    cat_groups = {key: [] for key, _, _ in cat_order}
    unlabeled_members = []
    for t in hero_enriched:
        if t["category"] in cat_groups:
            cat_groups[t["category"]].append(t)
        elif not t["labeled"]:
            unlabeled_members.append(t)
    # Total across everything visible (named + unlabeled) — this is the
    # denominator for Zone A share_pct so bars sum to <=100% including
    # the Unlabeled bar. Uses 30d aggregate to match Fable's v3 §3 table.
    zone_a_total = sum(t["trades"] or 0 for t in hero_enriched) or 1

    def _build_bar(key, label, rgb, members, tier_bar_note=None):
        cat_total = sum(t["trades"] or 0 for t in members)
        if cat_total == 0:
            return None
        members_sorted = sorted(
            members, key=lambda t: t["trades"] or 0, reverse=True
        )
        top = members_sorted[:5]
        rest_total = sum(t["trades"] or 0 for t in members_sorted[5:])
        segments = [
            {"label": t["display"], "trades": t["trades"] or 0,
             "share_in_cat": round(((t["trades"] or 0) / cat_total) * 100, 1)}
            for t in top
        ]
        if rest_total > 0:
            segments.append({
                "label": f"+{len(members_sorted) - 5} more"
                         if len(members_sorted) > 5 else "other",
                "trades": rest_total,
                "share_in_cat": round((rest_total / cat_total) * 100, 1),
            })
        return {
            "key": key,
            "label": label,
            "rgb": rgb,
            "trades": cat_total,
            "share_pct": round((cat_total / zone_a_total) * 100, 2),
            "token_count": len(members),
            "segments": segments,
            "tier_note": tier_bar_note,
        }

    category_bars = []
    for key, label, rgb in cat_order:
        bar = _build_bar(key, label, rgb, cat_groups[key])
        if bar:
            category_bars.append(bar)

    # Unlabeled bar — visually + textually distinct from "other named
    # tokens". Different rgb (muted grey, no fill saturation) and its own
    # explainer copy in the template. This is the tokens with NO identity;
    # named-but-uncategorized ("other") is on the opposite side of the
    # attestation story.
    unlabeled_bar = _build_bar(
        "unlabeled", "unlabeled",
        "100,116,139",  # slate-500 dimmer than "other"
        unlabeled_members,
    )

    # Zone B — spec-locked LP TVL bar. Sized in USD, not trades — mixed
    # unit label is intentional (see Fable's Zone B constraint). Concen-
    # tration hover is required (v5 §3a) because one pool holds 81% of
    # the bar.
    lp_zone_raw = hero_snapshot.get("lp_zone") or {}
    zone_b_lp = {
        "total_usd": lp_zone_raw.get("total_tvl_usd", 0),
        "pool_count": lp_zone_raw.get("pool_count", 0),
        "pools": lp_zone_raw.get("pools", []),
        "top_pool_pair": None,
        "top_pool_share_pct": None,
    }
    if zone_b_lp["pools"]:
        top_pool = zone_b_lp["pools"][0]
        zone_b_lp["top_pool_pair"] = top_pool.get("pair")
        zone_b_lp["top_pool_share_pct"] = top_pool.get("share_pct")

    # Attestation summary strip — spec-locked from v3 §4a via v5 hero
    # snapshot. Displayed as three counts in the hero: verified pill
    # (green), self-described pill (grey), bare (DOMAIN_ONLY + ANONYMOUS
    # merged per Charlie's editorial: never label lower tiers as verified).
    tier_counts = ((hero_snapshot.get("tiers") or {}).get("counts") or {})
    tier_summary = {
        "verified": int(tier_counts.get("VERIFIED", 0)),
        "self_described": int(tier_counts.get("SELF_DESCRIBED", 0)),
        "bare": int(tier_counts.get("DOMAIN_ONLY", 0))
                + int(tier_counts.get("ANONYMOUS", 0)),
        "total_pairs": int(
            (hero_snapshot.get("tiers") or {}).get("total_pairs", 0)
        ),
    }
    rwa_caption = hero_snapshot.get("rwa_caption") or {"named_count": 0}
    floor_pct = ((hero_snapshot.get("floor") or {}).get("pct")) or 20.5

    # Floor-inside-the-bar (Gate 3 v2): the 20.5% floor is a subset story
    # rendered INSIDE the Unlabeled bar, not as a separate caption. Two
    # visitor-facing numbers (91.83% Unlabeled vs 20.5% floor) read as
    # contradictory when placed side-by-side; nesting resolves it. The
    # inner segment width represents floor_pct as a share of the Unlabeled
    # bar itself, so the visual math is honest: floor is a subset of
    # Unlabeled, not a competing total.
    if unlabeled_bar and unlabeled_bar["share_pct"] > 0:
        # Postgres SUM() returns Decimal; coerce so /(float) doesn't TypeError.
        _bar_share = float(unlabeled_bar["share_pct"])
        _floor = float(floor_pct)
        unlabeled_bar["floor_seg_share_of_bar"] = round(
            min(_floor / _bar_share * 100, 100), 2
        )
        unlabeled_bar["floor_pct"] = _floor

    # Honeycomb hero — top labeled tokens laid out as a pointy-top hex grid.
    # Each cell key (currency_raw|issuer) is matched against live WS trades
    # to pulse in real time. Pure CSS/SVG positioning, server-rendered so
    # the layout is identical no-JS or first paint.
    HEX_COLS = 8
    HEX_ROWS = 4
    HEX_S = 32  # hex "size" (center → vertex distance), pointy-top
    sqrt3 = 3 ** 0.5
    col_w = HEX_S * sqrt3
    row_h = HEX_S * 1.5
    pad_x = HEX_S * sqrt3 / 2
    pad_y = HEX_S
    hex_cells = []
    labeled_pool = [t for t in enriched if t["labeled"]]
    # Force float — Postgres SUM() returns Decimal, which mixes badly with
    # float arithmetic in the Jinja template (TypeError → 500).
    max_trades = float(
        max((t["trades"] or 0) for t in labeled_pool)
    ) if labeled_pool else 1.0
    for idx, t in enumerate(labeled_pool[: HEX_COLS * HEX_ROWS]):
        row = idx // HEX_COLS
        col = idx % HEX_COLS
        cx = pad_x + col * col_w + (col_w / 2 if row % 2 else 0)
        cy = pad_y + row * row_h
        trades_int = int(t["trades"] or 0)
        baseline = trades_int / max_trades if max_trades else 0.0
        hex_cells.append({
            "key": f"{t['currency_raw']}|{t['issuer']}",
            "display": t["display"],
            "category": t["category"] or "other",
            "trades": trades_int,
            "currency_raw": t["currency_raw"],
            "issuer": t["issuer"],
            "cx": round(cx, 2),
            "cy": round(cy, 2),
            "baseline": round(baseline, 3),
        })
    hex_view_w = round(pad_x * 2 + (HEX_COLS - 1) * col_w + col_w / 2, 2)
    hex_view_h = round(pad_y * 2 + (HEX_ROWS - 1) * row_h, 2)

    # Lookup table for the live trade-tape JS: every labeled token (not just
    # top 32) so the tape can resolve display name + category color for any
    # incoming WS trade. Plain JSON, embedded inline in the page.
    label_lookup = {
        f"{t['currency_raw']}|{t['issuer']}": {
            "display": t["display"],
            "category": t["category"] or "other",
        }
        for t in enriched if t["labeled"]
    }
    label_lookup_json = json.dumps(label_lookup, separators=(",", ":"))

    return render_template(
        "tokens.html",
        tokens=enriched,
        earliest_iso=earliest_iso,
        latest_iso=latest_iso,
        total_buckets=total_buckets,
        labeled_count=sum(1 for t in enriched if t["labeled"]),
        range_key=range_key,
        category_bars=category_bars,
        unlabeled_bar=unlabeled_bar,
        zone_b_lp=zone_b_lp,
        tier_summary=tier_summary,
        rwa_caption=rwa_caption,
        floor_pct=floor_pct,
        hex_cells=hex_cells,
        hex_view_w=hex_view_w,
        hex_view_h=hex_view_h,
        hex_size=HEX_S,
        label_lookup_json=label_lookup_json,
        data_age_label=_format_age_seconds(_volumes_db_age_seconds()),
    )


# /nfts cache — 5-min TTL keeps ledger queries cheap under crawler traffic.
# Single-entry, closed-key (no query params on this route). SWR twin of the
# /whales pattern (_WHALES_REBUILD_LOCK, app.py:707-708): on TTL expiry, serve
# the stale body immediately and fire ONE background rebuild. Prior plain-cache
# path paid the full render synchronously on the first miss per worker after
# any deploy or TTL boundary — sampled 22.6s cold-render 2026-08-17.
_NFTS_CACHE = {"body": None, "at": 0.0}
_NFTS_CACHE_TTL_S = 300
# Single-slot cache so a plain bool guard is enough; dict wrapper keeps
# stable identity across the closure. See _ANALYTICS_REBUILD_STATE.
_NFTS_REBUILD_LOCK = threading.Lock()
_NFTS_REBUILD_STATE = {"in_flight": False}


def _trigger_nfts_rebuild():
    """SWR twin of _trigger_analytics_rebuild — see that docstring. Bypasses
    the read-path short-circuit via _CACHE_REBUILD_LOCAL so the rebuild
    actually re-renders instead of taking the stale-serve branch itself."""
    with _NFTS_REBUILD_LOCK:
        if _NFTS_REBUILD_STATE["in_flight"]:
            return
        _NFTS_REBUILD_STATE["in_flight"] = True

    def _run():
        try:
            _CACHE_REBUILD_LOCAL.bypass = True
            with app.test_request_context("/nfts"):
                nfts()
        except Exception:
            pass
        finally:
            _CACHE_REBUILD_LOCAL.bypass = False
            with _NFTS_REBUILD_LOCK:
                _NFTS_REBUILD_STATE["in_flight"] = False

    threading.Thread(
        target=_run, daemon=True, name="nfts-swr-rebuild"
    ).start()

# NFT historical backfill coverage numbers, frozen 2026-08-10 gap audit
# (2026-04-01 → head at ~21:44 UTC). All four constants move together — a
# fresh sampling round must update all four in lockstep (audit-trail:
# project_xrpldashboard_nft_backfill_gap_audit_2026-08-10.md).
#
# The 95.6% denominator is the FULL range (RANGE_TOTAL). The apparent
# range/observed gap (~764K ledgers) splits into two classes:
#   • LEGITIMATELY_EMPTY — no NFT transactions occurred at all (consensus
#     data, nothing to capture; counted as covered).
#   • RESIDUAL_HOLES — public-Clio 503 responses our walker persisted past
#     during backfill (recoverable NFT activity we did not persist; counted
#     as missed). Class split derived from stratified sampling: 16.1%
#     hole-rate in small-delta gaps, 35.7% in large-delta gaps, weighted
#     to ~4.4% of the range → ~95.6% coverage.
#
# Surfaced on /nfts + /methodology per SELLABLE_REQUIRES_SOVEREIGN_SOURCE
# doctrine (docs/X402_RAILS_DARK_SCOPING.md).
_NFT_BACKFILL_RANGE_TOTAL_EST         = 2_958_523
_NFT_BACKFILL_OBSERVED_EST            = 2_194_433
_NFT_BACKFILL_LEGITIMATELY_EMPTY_EST  =   635_090  # range_total - observed - residual
_NFT_BACKFILL_RESIDUAL_HOLES_EST      =   129_000
_NFT_BACKFILL_COVERAGE_PCT            = 95.6


@app.route("/nfts")
def nfts():
    """XLS-20 NFT activity on the XRPL — free forever, honestly source-labeled.

    Live data reads from our own rippled forward-walker (own-node source).
    Historical backfill (2026-04-01 → head) was read from Ripple's public
    Clio archive (third-party source) — labeled at point of display, kept
    free-tier permanently under SELLABLE_REQUIRES_SOVEREIGN_SOURCE.

    Cached 5min in-process. Every query is by-index (tx_type + close_time,
    or issuer + close_time). Never touches the XRPL node from this route."""
    now_mono = time.monotonic()
    if not getattr(_CACHE_REBUILD_LOCAL, "bypass", False):
        _cached_body = _NFTS_CACHE["body"]
        if _cached_body is not None:
            _cached_age = now_mono - _NFTS_CACHE["at"]
            if _cached_age < _NFTS_CACHE_TTL_S:
                _r = make_response(_cached_body)
                _r.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
                return _r
            # SWR: expired but body exists — serve now, rebuild in bg.
            _trigger_nfts_rebuild()
            _r = make_response(_cached_body)
            _r.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
            return _r

    # Defaults — the page still renders honestly (dashes / zero rows) if PG
    # is unavailable at request time. Nothing crashes.
    totals = {"total_events": 0}
    counts_24h = {}
    counts_7d = {}
    counts_all = {}
    top_issuers = []
    range_start_ledger = 103_252_853  # 2026-04-01 UTC floor, from nft_walker_state.backfill_target
    range_end_ledger = 103_252_853
    range_start_date = "2026-04-01"
    range_end_date = "—"
    freshness_seconds = None
    freshness_label = None

    if db.pg_available():
        try:
            with db.pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*), MIN(ledger_index), MAX(ledger_index), "
                        "MIN(close_time), MAX(close_time) FROM nft_activity"
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        totals["total_events"] = int(row[0])
                        range_start_ledger = int(row[1] or range_start_ledger)
                        range_end_ledger = int(row[2] or range_end_ledger)
                        if row[3]:
                            range_start_date = row[3].strftime("%Y-%m-%d")
                        if row[4]:
                            range_end_date = row[4].strftime("%Y-%m-%d")
                            freshness_seconds = max(0, int(time.time() - row[4].timestamp()))
                            freshness_label = _format_age_seconds(freshness_seconds)

                    # `INTERVAL %s` does not parse — Postgres wants a literal
                    # after INTERVAL, not a bound parameter. Casting the bound
                    # value via `(%s)::interval` is the shape that works.
                    for label, interval in (("24h", "24 hours"), ("7d", "7 days")):
                        cur.execute(
                            "SELECT tx_type, COUNT(*) FROM nft_activity "
                            "WHERE close_time >= NOW() - (%s)::interval "
                            "GROUP BY tx_type",
                            (interval,),
                        )
                        bucket = {r[0]: int(r[1]) for r in cur.fetchall()}
                        if label == "24h":
                            counts_24h = bucket
                        else:
                            counts_7d = bucket

                    cur.execute(
                        "SELECT tx_type, COUNT(*) FROM nft_activity GROUP BY tx_type"
                    )
                    counts_all = {r[0]: int(r[1]) for r in cur.fetchall()}

                    cur.execute(
                        "SELECT issuer, COUNT(*) AS events FROM nft_activity "
                        "WHERE issuer IS NOT NULL "
                        "AND close_time >= NOW() - INTERVAL '7 days' "
                        "GROUP BY issuer ORDER BY events DESC LIMIT 10"
                    )
                    top_issuers = [(r[0], int(r[1])) for r in cur.fetchall()]
        except Exception:
            app.logger.exception("nfts: PG read failed; rendering empty state")

    body = render_template(
        "nfts.html",
        totals=totals,
        counts_24h=counts_24h,
        counts_7d=counts_7d,
        counts_all=counts_all,
        top_issuers=top_issuers,
        range_start_ledger=range_start_ledger,
        range_end_ledger=range_end_ledger,
        range_start_date=range_start_date,
        range_end_date=range_end_date,
        freshness_seconds=freshness_seconds,
        freshness_label=freshness_label,
        gap_audit={
            "range_total_est":        _NFT_BACKFILL_RANGE_TOTAL_EST,
            "observed_est":           _NFT_BACKFILL_OBSERVED_EST,
            "legitimately_empty_est": _NFT_BACKFILL_LEGITIMATELY_EMPTY_EST,
            "residual_holes_est":     _NFT_BACKFILL_RESIDUAL_HOLES_EST,
            "coverage_pct":           _NFT_BACKFILL_COVERAGE_PCT,
        },
    )
    _NFTS_CACHE["body"] = body
    _NFTS_CACHE["at"] = now_mono
    resp = make_response(body)
    resp.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
    return resp


@app.route("/about")
def about():
    """Public-facing 'what is this' page. Mission, principles, methodology,
    funding model. Copy lives in the template — review before launch."""
    # Live count of TOML-attested accounts. Was hard-coded "28+" — the
    # number doesn't drift on its own as we onboard more, so it has to
    # read from the source of truth at render time.
    attested_count = None
    try:
        with open(os.path.join(HERE, "named_accounts.json")) as f:
            attested_count = len(json.load(f))
    except Exception:
        pass
    amendments_in_flight = None
    try:
        amendments_in_flight = (
            fetch_amendments_state_cached().get("in_flight_count")
        )
    except Exception:
        pass
    rwa_family_count = None
    try:
        if db.pg_available():
            with db.pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM rwa_family "
                        "WHERE attestation_level = 'verified'"
                    )
                    rwa_family_count = cur.fetchone()[0]
    except Exception:
        pass
    return render_template(
        "about.html",
        attested_count=attested_count,
        amendments_in_flight=amendments_in_flight,
        rwa_family_count=rwa_family_count,
    )


@app.route("/rlusd")
def rlusd():
    """Cross-chain treasury watch for Ripple's RLUSD stablecoin.

    Live cross-chain feed: Ethereum totalSupply via public JSON-RPC,
    XRPL issuer obligations via Ripple's public node. The page polls
    /api/rlusd/state and falls back to a preview animation if the feed
    is unavailable.

    Three-branch SSR preflight: try the live cross-chain cache first;
    if it's missing or partial, fall back to Postgres' last-good mirror
    (rlusd_live dual-writes on every successful refresh); only when both
    are empty does the page render its graceful empty state. Visitors
    get a freshness chip — "Live · updated Ns ago" or "Last updated · X
    ago · reconnecting" — and the JS poller continues from there."""
    initial = None
    cached_at = None
    fresh = False

    # Branch 1: live fetch. Validate non-null supplies so a partial RPC
    # blip can't pass a truthy gate and leak eth.supply=None into the
    # page as a literal "$0".
    try:
        from rlusd_live import fetch_state
        candidate = fetch_state()
        if (candidate
                and candidate.get("eth", {}).get("supply") is not None
                and candidate.get("xrpl", {}).get("supply") is not None):
            initial = candidate
            cached_at = candidate.get("fetched_at") or int(time.time())
            fresh = True
    except Exception:
        pass

    # Branch 2: PG last-good fallback. Same completeness check as Branch 1
    # — the writer gates on it too, but the stricter read keeps us honest
    # against any future code path that ever persists partial state.
    if initial is None:
        try:
            from db import read_rlusd_state_cache
            cached_payload, cached_ts = read_rlusd_state_cache()
            if (cached_payload
                    and cached_payload.get("eth", {}).get("supply") is not None
                    and cached_payload.get("xrpl", {}).get("supply") is not None):
                initial = cached_payload
                cached_at = cached_ts
                fresh = False
        except Exception:
            pass

    # Branch 3: PG empty too — initial stays None, template renders the
    # graceful empty state (dashes + JS-driven preview animation).

    age_seconds = None
    if cached_at:
        age_seconds = max(0, int(time.time() - cached_at))

    import rlusd_live
    refresh_interval_minutes = max(1, rlusd_live.REFRESH_INTERVAL // 60)

    # Cross-check label surface. Same derivation the MCP tool
    # get_rlusd_flow_24h uses (mcp_tools_value_flows._derive_flow_cross_check),
    # so the on-page label and the machine-readable envelope report the
    # same three-state verdict — one source of truth, two surfaces.
    # None on any failure so the template omits the label rather than lie.
    try:
        from mcp_tools_value_flows import derive_rlusd_flow_cross_check_for_display
        cross_check = derive_rlusd_flow_cross_check_for_display()
    except Exception:
        cross_check = None

    return render_template(
        "rlusd.html",
        initial=initial,
        cached_at=cached_at,
        age_seconds=age_seconds,
        fresh=fresh,
        refresh_interval_minutes=refresh_interval_minutes,
        cross_check=cross_check,
    )


@app.route("/api/rlusd/state")
@limiter.limit("30 per minute")
def api_rlusd_state():
    """Combined Ethereum + XRPL RLUSD treasury state.

    Returns supply totals from both chains plus recent mint/burn events,
    TTL-cached server-side so client polling stays cheap."""
    from rlusd_live import fetch_state
    return fetch_state()


@app.route("/rwa")
def rwa():
    """Institutional RWA on XRPL — verified issuer families with on-chain
    liquidity. Two tiers: (A) TOML-attested MPT issuers (no trust-line
    liquidity yet), (B) AMM pools whose counterparty issuer matches a
    verified-brand allowlist. Plus an open exclusion list naming what we
    chose NOT to surface and why — the truth-first credibility multiplier."""
    families = []
    mpt_attested = []
    curation_last_updated = None
    if db.pg_available():
        try:
            with db.pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT f.family_slug, f.family_name, f.description,
                               f.external_url, f.attestation_level,
                               COALESCE(
                                 array_agg(p.pool_address)
                                   FILTER (WHERE p.pool_address IS NOT NULL),
                                 '{}'
                               ) AS pool_addresses
                          FROM rwa_family f
                     LEFT JOIN rwa_pool_attribution p
                            ON f.family_slug = p.family_slug
                      GROUP BY f.family_slug, f.family_name, f.description,
                               f.external_url, f.attestation_level
                      ORDER BY array_length(array_agg(p.pool_address), 1)
                                 DESC NULLS LAST,
                               f.family_name
                    """)
                    for row in cur.fetchall():
                        families.append({
                            "slug": row[0],
                            "name": row[1],
                            "description": row[2],
                            "external_url": row[3],
                            "attestation_level": row[4],
                            "pool_addresses": list(row[5] or []),
                            "pool_details": [],
                            "total_tvl_usd": 0.0,
                            "tokens": [],
                        })
                    cur.execute("""
                        SELECT address, name, extra
                          FROM account_labels
                         WHERE source = 'toml' AND category = 'mpt_issuer'
                      ORDER BY name
                    """)
                    for row in cur.fetchall():
                        extra = row[2] if isinstance(row[2], dict) else {}
                        mpt_attested.append({
                            "address": row[0],
                            "name": row[1],
                            "domain": extra.get("domain"),
                        })
                    cur.execute(
                        "SELECT MAX(created_at) FROM rwa_pool_attribution"
                    )
                    _row = cur.fetchone()
                    curation_last_updated = (
                        _row[0].strftime("%Y-%m-%d")
                        if _row and _row[0] else None
                    )
        except Exception:
            curation_last_updated = None

    try:
        ranked_rows, ranked_meta = _ranked_amm_snapshot()
    except Exception:
        ranked_rows, ranked_meta = [], {}
    by_address = {
        r.get("amm_account"): r
        for r in (ranked_rows or [])
        if r.get("amm_account")
    }

    for family in families:
        tokens = set()
        for addr in family["pool_addresses"]:
            pool = by_address.get(addr)
            if not pool:
                continue
            family["pool_details"].append(pool)
            family["total_tvl_usd"] += pool.get("tvl_usd") or 0.0
            for side_key in ("asset_a", "asset_b"):
                side = pool.get(side_key) or {}
                disp = side.get("display")
                if disp and disp != "XRP":
                    tokens.add(disp)
        family["tokens"] = sorted(tokens)

    verified_families = [
        f for f in families if f["attestation_level"] == "verified"
    ]
    total_family_count = len(verified_families)
    total_pool_count = sum(len(f["pool_addresses"]) for f in verified_families)
    total_tvl = sum(f["total_tvl_usd"] for f in verified_families)

    snap_ts = ranked_meta.get("snapshot_ts") if isinstance(ranked_meta, dict) else None
    snapshot_age = (
        max(0, int(time.time()) - int(snap_ts)) if snap_ts else None
    )

    exclude_list = [
        {"name": "BlackRock", "status": "excluded",
         "reason": "Vanity wallet rBLACK… exists on-ledger, but BlackRock "
                   "issues no trust-line tokens on XRPL. BUIDL is Ethereum-only "
                   "via Securitize."},
        {"name": "Ripple Treasury (3 variants)", "status": "excluded",
         "reason": "Three spellings (Ripple Treasury / XRPLTreasury / "
                   "XRPTreasury) across unlabeled wallets. No Ripple Labs "
                   "attestation."},
        {"name": "GTreasury", "status": "excluded",
         "reason": "Real treasury-management software company at gtreasury.com "
                   "but no XRPL tokenization announcement. Suspected brand "
                   "spoof."},
        # Ondo Finance moved to verified families (TOML chain closed at ondo.finance).
        {"name": "Franklin Templeton (sgBENJI)", "status": "excluded",
         "reason": "Wallet sets Domain to franklinresources.com but no "
                   "xrp-ledger.toml exists at that host — vanity-domain spoof. "
                   "No on-XRPL Franklin Templeton attestation chain published."},
        {"name": "Archax", "status": "pending",
         "reason": "Archax is a real institutional broker (archax.com). "
                   "Re-checked 2026-08-08 after CoinDesk cited a $55.4M "
                   "position via RWA.xyz aggregation — that figure is a "
                   "third-party estimate and not our measurement. archax.com "
                   "returns 403 on the .well-known probe (anti-bot wall, not "
                   "a clean absence) and no Archax-labeled MPT or trust-line "
                   "issuer wallet appears in our own dataset. Promote pending "
                   "an accessible xrp-ledger.toml and a verifiable issuer "
                   "wallet."},
        {"name": "Aviva Investors", "status": "pending",
         "reason": "Aviva Investors US Dollar Liquidity Fund MPT "
                   "(064D94DE…F95EA90A, issuer r9o37ZXw…mHmr2) declares "
                   "'Aviva Investors Liquidity Funds plc' via on-chain MPT "
                   "metadata — self-attested one direction. Neither aviva.com "
                   "nor avivainvestors.com publishes a xrp-ledger.toml that "
                   "closes the attestation loop back to that wallet. Visible "
                   "on /mpts as the raw ledger surface; withheld from the "
                   "verified /rwa tier until Aviva publishes a TOML pinning "
                   "the issuer address."},
        {"name": "Societe Generale (SG-FORGE)", "status": "pending",
         "reason": "CoinDesk (2026-08-07) cites RWA.xyz aggregating ~$11.6M "
                   "of Societe Generale tokenized assets on XRPL — a "
                   "third-party estimate we cannot restate as our own. Checked "
                   "sgforge.com, societegenerale.com, and socgen.com: no "
                   "xrp-ledger.toml at any host. No SG-labeled MPT or "
                   "trust-line issuer wallet appears in our own dataset. "
                   "Promote pending an accessible TOML and a verifiable "
                   "issuer wallet."},
        {"name": "Justoken (JMWH)", "status": "pending",
         "reason": "Real project with legitimate press coverage (YPF Luz energy "
                   "partnership, tokenized-energy claims via the JMWH token). "
                   "However the issuer wallet r976xbKc6om7WYTFwZHByvxnYFi1y5hJXH "
                   "sets no on-chain Domain field, and Justoken publishes no "
                   "xrp-ledger.toml at justoken.com. Cannot verify the wallet "
                   "actually belongs to Justoken — promote to verified once "
                   "attestation chain lands."},
    ]

    return render_template(
        "rwa.html",
        families=verified_families,
        mpt_attested=mpt_attested,
        exclude_list=exclude_list,
        total_pool_count=total_pool_count,
        total_family_count=total_family_count,
        total_tvl=total_tvl,
        snapshot_age=snapshot_age,
        curation_last_updated=curation_last_updated,
    )


@app.route("/methodology")
def methodology():
    """Per-surface freshness, cache TTLs, data sources, known limitations.
    The differentiator page — no other XRPL dashboard discloses its
    caching/source dependencies in one public document."""
    return render_template("methodology.html")


@app.route("/connect")
def connect():
    """The 60-second onboarding page for AI agents wiring into the live
    public MCP endpoint. Two config paths (Custom Connector UI, mcp-remote
    bridge), three sample prompts (primitive / aggregation / verify), the
    beta window, the enforced 600/hr limit, and the honest-limits section.
    Anchor #connect-in-60-seconds is stable — /agents.json.mcp_servers[0]
    .connect_docs, /llms.txt, and the directory-submission specs all
    point at it."""
    return render_template(
        "connect.html",
        last_verified=LAST_VERIFIED_AGENT_TIER_METHODOLOGY,
    )


# ─────────────────────────────────────────────────────────────────
# Per-directory /connect/<slug> redirect layer (added 2026-08-12 for
# the 3→9 MCP directory expansion). Two purposes:
#
# 1. Belt-and-suspenders backup to ?ref=<slug> URLs. If any directory
#    strips query strings when displaying or handing the URL to a
#    client, the /connect/<slug> path 302s to the ref-tagged endpoint
#    server-side — the query re-attaches at the transport hop the
#    client can't strip. Verified 2026-08-12: mcp-remote@latest
#    (Cursor + Claude Desktop's bridge) preserves query strings, so
#    the primary path already works; this is defense in depth.
#
# 2. Attribution watermark on the redirect itself. The click stamps
#    a walker_health row `mcp_connect_redirect` with ref=<slug> BEFORE
#    the 302, so a click that never completes the MCP handshake (agent
#    fetched the URL then closed) is still counted at the directory
#    layer. The MCP-session-start stamp counts sessions that actually
#    call a tool; the redirect stamp counts arrivals at the door.
#
# Allowlist is the closed set of nine directories (three live + six
# expansion). New directory ⇒ add the slug here + submit. Slugs match
# the ref-tag capture rules in mcp_session_rate_limit._normalize_ref.
# ─────────────────────────────────────────────────────────────────

CONNECT_REDIRECT_SLUGS: frozenset[str] = frozenset({
    # Three live (2026-08-05):
    "anthropic",     # Official MCP Registry (registry.modelcontextprotocol.io)
    "smithery",      # smithery.ai
    "glama",         # glama.ai/mcp/servers
    # Six-directory expansion (2026-08-12):
    "mcpso",         # mcp.so
    "allmcps",       # allmcps.com
    "mcpmarket",     # mcpmarket.com
    "pulse",         # pulsemcp.com — pending waitlist as of 2026-08-12
    "cursor",        # docs.cursor.com/tools/mcp (future submission)
    "openai",        # OpenAI directory (research task)
    # Reserved (organic + operational):
    "direct",        # URL typed / shared in DMs — not from a directory
    "readme",        # arrivals from README badge click
})
MCP_PUBLIC_URL = "https://mcp.xrpldashboard.com/mcp"


@app.route("/connect/<slug>")
def connect_redirect(slug: str):
    """302-redirect a directory click to the ref-tagged MCP endpoint.

    Unknown slugs fall through to /connect (the human onboarding page)
    with a 302 too — no 404 for typos, so a mildly-mangled URL still
    reaches the docs. The stamp only fires on slugs in the allowlist
    (unknown slugs are user-typo noise, not counted).
    """
    slug_norm = (slug or "").strip().lower()
    if slug_norm in CONNECT_REDIRECT_SLUGS:
        _stamp_connect_redirect(slug_norm)
        return redirect(f"{MCP_PUBLIC_URL}?ref={slug_norm}", code=302)
    return redirect("/connect#connect-in-60-seconds", code=302)


def _stamp_connect_redirect(slug: str) -> None:
    """Best-effort walker_health stamp for the redirect-time click.

    Silent-skip on any DB failure — attribution is observability, not
    a gate; a Neon outage must not break the redirect path. Same
    discipline as :func:`stamp_rate_limit_hit`.
    """
    try:
        import db as _db
        _db.write_walker_health_end(
            "mcp_connect_redirect",
            ok=True,
            message=f"ref={slug}",
        )
    except Exception:  # noqa: BLE001 — best-effort attribution
        pass


# ─────────────────────────────────────────────────────────────────
# Queryable claims layer (shipped 2026-08-04 per docs/PAID_MACHINE_TIER_DESIGN.md
# § 3.1). Every CLAIMS.yaml entry gets a permanent resolvable URI
# under /claims/xrpl.<domain>.<series>. See claims_endpoint.py for
# the URI scheme, sovereignty classification, and JSON shape. Also
# emits a machine-readable index at /claims/index.json (content-
# negotiated: JSON when Accept: application/json OR path suffix
# `.json`; HTML otherwise).
import claims_endpoint  # noqa: E402


def _claims_wants_json() -> bool:
    accept = (request.headers.get("Accept") or "").lower()
    return "application/json" in accept and "text/html" not in accept


@app.route("/claims")
def claims_index():
    """Human-readable index of every catalogued public claim on the
    site. Groups by page/domain, colors by sovereignty tier (green =
    sovereign / signable, yellow = public-node dependent OR no
    independent cross-check yet, red = third-party derived and
    permanently free-only per rule #3). This is free-tier substrate
    for the future paid tier — agents discover which claims are
    currently backed by SOVEREIGN data before hitting anything paid.
    See docs/PAID_MACHINE_TIER_DESIGN.md § 3.1."""
    if _claims_wants_json():
        resp = make_response(jsonify(claims_endpoint.index_json(SITE_URL, f"{SITE_URL}/methodology#for-ai-agents")))
        resp.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
        return resp
    grouped = claims_endpoint.by_domain()
    totals = claims_endpoint.status_totals()
    resp = make_response(render_template(
        "claims_index.html",
        grouped=grouped,
        totals=totals,
        last_verified=LAST_VERIFIED_AGENT_TIER_METHODOLOGY,
    ))
    resp.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
    return resp


@app.route("/claims/index.json")
def claims_index_json():
    """Machine-readable claims index. Same payload as
    /claims with `Accept: application/json`."""
    resp = make_response(jsonify(claims_endpoint.index_json(SITE_URL, f"{SITE_URL}/methodology#for-ai-agents")))
    resp.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
    return resp


@app.route("/claims/<uri>")
def claim_detail(uri):
    """Per-claim status page. Content-negotiated — JSON when
    `Accept: application/json` OR the URI carries a `.json` suffix;
    HTML otherwise. The URI scheme is permanent (once agents cite
    a URI, it will keep resolving; new URIs are additive only)."""
    wants_json = False
    if uri.endswith(".json"):
        wants_json = True
        uri = uri[:-5]
    else:
        wants_json = _claims_wants_json()

    if not claims_endpoint.is_valid_uri(uri):
        if wants_json:
            return jsonify({
                "error": "invalid_uri",
                "expected_scheme": "xrpl.<domain>.<series>",
                "index_url": f"{SITE_URL}/claims/index.json",
            }), 400
        abort(404)

    entry = claims_endpoint.get_claim(uri)
    if entry is None:
        if wants_json:
            return jsonify({
                "error": "unknown_claim",
                "uri": uri,
                "index_url": f"{SITE_URL}/claims/index.json",
            }), 404
        abort(404)

    if wants_json:
        resp = make_response(jsonify(claims_endpoint.claim_json(entry, SITE_URL, f"{SITE_URL}/methodology#for-ai-agents")))
        resp.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
        return resp
    resp = make_response(render_template(
        "claim_detail.html",
        claim=entry,
        last_verified=LAST_VERIFIED_AGENT_TIER_METHODOLOGY,
    ))
    resp.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
    return resp


@app.route("/regulation")
def regulation():
    """Plain-English legislative-status tracker for the CLARITY Act
    (H.R. 3633). Manually curated. Every claim links to a primary
    source. Explicitly non-advisory: no price, no prediction, no
    investment framing. See CLAIMS.yaml /regulation block."""
    try:
        last_dt = datetime.strptime(LAST_VERIFIED_REGULATION, "%Y-%m-%d").date()
        days_since = (date.today() - last_dt).days
    except Exception:
        days_since = 0
    return render_template(
        "regulation.html",
        last_verified_iso=LAST_VERIFIED_REGULATION,
        days_since=days_since,
    )


@app.route("/learn")
def learn():
    """Plain-language XRPL explainer for visitors who don't already know
    what a blockchain is. Hero shows the current validated ledger index
    server-side with an 'as of' timestamp — truthful static signal, not a
    fake liveness label. Phase 2 will swap in a real live heartbeat widget."""
    p = None
    try:
        p = fetch_pulse_cached()
    except Exception:
        p = None
    if p and not p.get("error"):
        ledger_index = p.get("ledger_index")
        as_of_unix = int(time.time())
    else:
        ledger_index = None
        as_of_unix = None
    return render_template(
        "learn.html",
        ledger_index=ledger_index,
        as_of_unix=as_of_unix,
    )


@app.route("/amendments")
def amendments():
    """Live in-flight amendment tracker. Reads the public `feature` RPC
    + the Amendments ledger object so the page reflects exactly what
    validators are currently voting on — including the rare case where
    a hash sits in Majorities but the responding node has no definition
    for it (i.e. validators on a newer build voting on something current
    released rippled binaries don't carry). Plain-English summaries are
    hand-edited per amendment; truth-first guardrail: the page never
    speculates on what an unrecognized hash does, only on the verifiable
    facts (hash, majority close time, projected activation if majority
    holds, link to off-ledger metadata if any)."""
    state = fetch_amendments_state_cached()
    resp = make_response(render_template(
        "amendments.html",
        state=state,
        cache_ttl_seconds=amendments_state.CACHE_TTL,
    ))
    # Align browser + edge cache with backend TTL: fetch_amendments_state_cached
    # refreshes every AMENDMENTS_CACHE_TTL (default 300s), so re-hitting the
    # origin at 60s just returned the same cached state 5× per real refresh.
    resp.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
    return resp


def _load_latest_bridge_signers():
    """Shape the latest bridge_signer_history row for the /sidechain
    hero. Computes is_uniform / required_signers (the M-of-N framing)
    when all signer weights are identical — defensive against a future
    weighted rotation where 'X of N' would be misleading."""
    row = db.read_latest_bridge_signers()
    if not row:
        return None
    entries = row.get("signer_entries") or []
    weights = [int(e.get("weight", 0)) for e in entries if e.get("weight")]
    is_uniform = bool(weights) and len(set(weights)) == 1
    uniform_weight = weights[0] if is_uniform else None
    # Ceiling division: M signers each at uniform_weight must SUM >= quorum.
    # Floor division understates by 1 whenever quorum is not a clean multiple
    # of weight (e.g. 1223320 / 65535 = 18.66 → must be 19, not 18).
    required = (
        -(-int(row["quorum"]) // uniform_weight)
        if is_uniform and uniform_weight else None
    )
    return {
        **row,
        "is_uniform": is_uniform,
        "uniform_weight": uniform_weight,
        "required_signers": required,
    }


@app.route("/network")
def network():
    """Live view of the two canonical XRPL UNLs (Ripple + Foundation),
    with validator counts, expiration status, and the pubkey overlap.
    Editorial purpose: the Foundation UNL has been expired since
    2026-01-18 yet XRPL kept producing ledgers — because most operators
    carry both lists and the overlap means the Ripple UNL alone has the
    quorum every node needs. Reads each list's signed manifest directly
    and decodes server-side; cached up to 10 minutes."""
    state = fetch_network_state_cached()
    diffs = {
        "ripple": build_unl_diff_view("ripple"),
        "xrplf": build_unl_diff_view("xrplf"),
    }
    valid_windows = {k for k, _h in TX_MIX_WINDOWS}
    requested_window = request.args.get("mix", TX_MIX_DEFAULT_WINDOW)
    if requested_window not in valid_windows:
        requested_window = TX_MIX_DEFAULT_WINDOW
    tx_mix = fetch_tx_type_mix(requested_window)
    resp = make_response(render_template(
        "network.html",
        state=state,
        diffs=diffs,
        cache_ttl_seconds=network_state.CACHE_TTL,
        tx_mix=tx_mix,
    ))
    resp.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
    return resp


@app.route("/sidechain")
def sidechain():
    """Live view of the XRPL ↔ XRPL EVM Sidechain bridge multisig.
    Axelar's Amplifier verifier set signs every bridge operation via
    a SignerList on the canonical XRPL bridge account. We snapshot
    that SignerList into bridge_signer_history; this page renders the
    latest row — quorum, signer count, and (for uniform weights) the
    M-of-N framing visitors actually understand.

    Freshness pill tracks walker_health.last_success_at, not the
    table's last write — SignerListSet rotations are rare events and
    written_at would drift to 'months ago' while the dashboard is
    actively working as designed."""
    state = _load_latest_bridge_signers()
    rotations = db.read_bridge_signer_rotations()
    walker = db.read_walker_health("bridge_signer_walker")
    walker_age = walker.get("last_success_age_seconds") if walker else None
    data_age_label = _format_age_seconds(
        int(walker_age) if walker_age is not None else None
    )
    # Gateway r-address mirrors bridge_signer_walker.AXELAR_GATEWAY.
    gateway_address = "rfmS3zqrQrka8wVyhXifEeyTwe8AMz2Yhw"
    resp = make_response(render_template(
        "sidechain.html",
        state=state,
        rotations=rotations,
        data_age_label=data_age_label,
        gateway_address=gateway_address,
    ))
    resp.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
    return resp


# ─────────────────────────────────────────────────────────────────────
# /walker_health — localhost-only admin view of every instrumented
# background walker. Reads the walker_health table (populated by each
# walker's try/finally instrumentation) and renders a single sortable
# severity table. Public callers hit 404 by design (see abort in
# handler). Whether to open a redacted public transparency view is
# owed a Charlie ruling — see docs/SOVEREIGNTY_COVENANT_VIOLATIONS_
# 2026-08-30.md §/walker_health.
# ─────────────────────────────────────────────────────────────────────

WALKER_SEVERITY_WARN_MULTIPLE = 2   # >2× cadence since last success → yellow
WALKER_SEVERITY_CRIT_MULTIPLE = 4   # >4× cadence since last success → red


def _walker_severity(row):
    """Map a walker_health row to a (severity, sort_rank) tuple.
    severity ∈ {"red", "yellow", "green", "idle"}. sort_rank is used
    by the page to put broken walkers at the top of the table."""
    cf = row.get("consecutive_failures") or 0
    age = row.get("last_success_age_seconds")
    cadence = row.get("cadence_seconds")
    last_success_at = row.get("last_success_at")

    if last_success_at is None:
        # Row exists but walker has never succeeded yet. Could be brand-new
        # (first run still in progress) or persistently failing. Treat as
        # idle when last_run_completed is also NULL (run truly in flight),
        # otherwise red.
        if row.get("last_run_completed") is None:
            return ("idle", 1)
        return ("red", 3)

    if cf >= 2:
        return ("red", 3)
    if cadence is not None and age is not None and age > WALKER_SEVERITY_CRIT_MULTIPLE * cadence:
        return ("red", 3)
    if cf == 1:
        return ("yellow", 2)
    if cadence is not None and age is not None and age > WALKER_SEVERITY_WARN_MULTIPLE * cadence:
        return ("yellow", 2)
    return ("green", 0)


def _humanize_cadence(seconds):
    """Render an integer second count as a friendly cadence string for
    the /walker_health table. None → 'unknown'."""
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"every {seconds}s"
    if seconds < 3600:
        return f"every {seconds // 60} min"
    if seconds < 86400:
        hours = seconds / 3600
        return f"every {int(hours)} hr" if hours == int(hours) else f"every {hours:.1f} hr"
    days = seconds / 86400
    return f"daily" if days == 1 else f"every {int(days)} days"


def _humanize_age(seconds):
    """Render a 'seconds ago' duration in plain English. None → 'never'."""
    if seconds is None:
        return "never"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} hr ago"
    return f"{int(seconds // 86400)} days ago"


@app.route("/walker_health")
def walker_health_page():
    """Localhost-only admin view. 404s for any request not originating
    from 127.0.0.1 / ::1 — which means on Render (behind the proxy) this
    is never reachable from the internet, and on the Mac it's available at
    localhost:PORT/walker_health with no token or env var required.
    Severity (green/yellow/red) is computed per-row from the walker's own
    declared cadence_seconds, not from page-wide hardcoded thresholds."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(404)
    rows = db.read_walker_health_all()
    enriched = []
    for r in rows:
        severity, rank = _walker_severity(r)
        enriched.append({
            **r,
            "severity": severity,
            "_rank": rank,
            "cadence_human": _humanize_cadence(r.get("cadence_seconds")),
            "age_human": _humanize_age(r.get("last_success_age_seconds")),
        })
    enriched.sort(key=lambda x: (-x["_rank"], x["walker_name"]))
    counts = {
        "red": sum(1 for r in enriched if r["severity"] == "red"),
        "yellow": sum(1 for r in enriched if r["severity"] == "yellow"),
        "green": sum(1 for r in enriched if r["severity"] == "green"),
        "idle": sum(1 for r in enriched if r["severity"] == "idle"),
        "total": len(enriched),
    }
    resp = make_response(render_template(
        "walker_health.html",
        rows=enriched,
        counts=counts,
        warn_multiple=WALKER_SEVERITY_WARN_MULTIPLE,
        crit_multiple=WALKER_SEVERITY_CRIT_MULTIPLE,
    ))
    resp.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
    return resp


def _group_credentials_for_display(examples):
    """Collapse identical (issuer, type, URI) attestations to one card so the
    examples list reads as N distinct claims rather than N near-duplicates.
    Order-preserving: first occurrence's position sets the group's slot."""
    groups = []
    index_by_key = {}
    for c in examples or []:
        key = (
            c.get("issuer"),
            c.get("credential_type_hex") or c.get("credential_type_label"),
            c.get("uri_label") or c.get("uri_hex"),
        )
        if key in index_by_key:
            g = groups[index_by_key[key]]
            g["members"].append(c)
            if not c.get("accepted"):
                g["all_accepted"] = False
            if not c.get("self_issued"):
                g["all_self_issued"] = False
        else:
            index_by_key[key] = len(groups)
            groups.append({
                "head": c,
                "members": [c],
                "all_accepted": bool(c.get("accepted")),
                "all_self_issued": bool(c.get("self_issued")),
            })
    return groups


def _decode_credential_type(hex_str):
    """XLS-70 CredentialType is hex-encoded ASCII per the spec. '4B5943' → 'KYC'.
    Falls back to the raw hex when bytes don't decode as printable ASCII."""
    if not hex_str:
        return None
    try:
        decoded = bytes.fromhex(hex_str).decode("ascii")
        if decoded.isprintable():
            return decoded
    except (ValueError, UnicodeDecodeError):
        pass
    return None


def _shape_permissioned_domains_for_display(rows):
    """Shape PermissionedDomain rows for the credentials.html embed.
    Splits each domain's accept-list into self-issued vs external entries so
    the editorial framing can distinguish coalition-trust from self-trust.
    A domain that accepts its own owner as an issuer is structurally different
    from one that only accepts external attestations — surfacing that split
    explicitly is the precision the page is built on."""
    shaped = []
    for r in rows or []:
        owner = r.get("owner_account")
        accept_list = r.get("accepted_credentials") or []
        entries = []
        self_count = 0
        external_count = 0
        types_seen = set()
        for entry in accept_list:
            cred = entry.get("Credential") if isinstance(entry, dict) else None
            if not cred:
                continue
            issuer = cred.get("Issuer")
            ct_hex = cred.get("CredentialType")
            ct_label = _decode_credential_type(ct_hex)
            is_self = (issuer == owner)
            if is_self:
                self_count += 1
            else:
                external_count += 1
            if ct_label:
                types_seen.add(ct_label)
            elif ct_hex:
                types_seen.add(ct_hex)
            entries.append({
                "issuer": issuer,
                "credential_type_hex": ct_hex,
                "credential_type_label": ct_label,
                "is_self_issued": is_self,
            })

        # Editorial shape classification. Explicit precision over "N-issuer
        # consortium" simplification — owner-as-its-own-issuer is structurally
        # distinct from coalition trust.
        if external_count >= 2 and self_count >= 1:
            shape_label = "multi_party_with_self"
        elif external_count >= 2 and self_count == 0:
            shape_label = "pure_consortium"
        elif external_count == 1 and self_count >= 1:
            shape_label = "mixed_single_plus_self"
        elif external_count == 1 and self_count == 0:
            shape_label = "single_external"
        elif external_count == 0 and self_count >= 1:
            shape_label = "self_only"
        else:
            shape_label = "empty"

        # Unix epoch → ISO8601. The walker already converts ripple-epoch
        # close_time → Unix before persistence, so no offset adjustment
        # here. Template's ts-local JS expects ISO.
        ledger_close_time = r.get("ledger_close_time")
        ledger_close_iso = None
        if ledger_close_time is not None:
            try:
                ledger_close_iso = datetime.fromtimestamp(
                    int(ledger_close_time), tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, TypeError, OverflowError):
                pass

        shaped.append({
            "domain_id": r.get("domain_id"),
            "owner_account": owner,
            "sequence": r.get("sequence"),
            "cred_count": r.get("cred_count"),
            "previous_txn_id": r.get("previous_txn_id"),
            "ledger_close_time": ledger_close_time,
            "ledger_close_iso": ledger_close_iso,
            "fetched_at_iso": r.get("fetched_at_iso"),
            "snapshot_date": r.get("snapshot_date"),
            "entries": entries,
            "self_count": self_count,
            "external_count": external_count,
            "types_seen": sorted(types_seen),
            "shape_label": shape_label,
        })
    return shaped


@app.route("/credentials")
def credentials():
    """Live view of XRPL Credentials (XLS-70) plus the PermissionedDomains
    (XLS-80) that gate on those credentials. The credentials amendment is
    enabled on mainnet but adoption is sparse, so an in-request `ledger_data`
    walk is infeasible — credentials_walker.py runs every 30 min under launchd
    on Mac and writes a snapshot to Postgres. permissioned_domains_walker.py
    runs daily under launchd against a 14-account institutional seed; both
    paths read from PG so every gunicorn worker (Mac and Render) serves the
    same view. The two amendments describe one institutional surface, so
    they render on one page until adoption justifies a split."""
    state = get_credentials_state()
    examples = (state.get("cumulative") or {}).get("examples") if state else None
    cred_groups = _group_credentials_for_display(examples) if examples else []
    has_collapsed_groups = any(len(g["members"]) > 1 for g in cred_groups)

    perm_domains_raw = db.read_permissioned_domains_latest()
    perm_domains = _shape_permissioned_domains_for_display(perm_domains_raw)
    perm_walker_runs = db.read_permissioned_domain_walker_runs(limit=1)
    perm_walker_last = perm_walker_runs[0] if perm_walker_runs else None
    # Freshness truthfulness: PD walker cadence is 86400s (daily). Anything past
    # 48h without a walker row means the walker is genuinely broken/unloaded and
    # the domain list may have drifted. Surface a plain "stale" label so
    # visitors don't read a 3-week-old snapshot as current.
    perm_walker_age_hours = _iso_to_age_seconds(perm_walker_last.get("fetched_at_iso")) / 3600.0 \
        if perm_walker_last and perm_walker_last.get("fetched_at_iso") \
        and _iso_to_age_seconds(perm_walker_last.get("fetched_at_iso")) is not None else None
    perm_walker_is_stale = perm_walker_age_hours is not None and perm_walker_age_hours >= 48.0

    resp = make_response(render_template(
        "credentials.html",
        state=state,
        cred_groups=cred_groups,
        has_collapsed_groups=has_collapsed_groups,
        perm_domains=perm_domains,
        perm_walker_last=perm_walker_last,
        perm_walker_age_hours=perm_walker_age_hours,
        perm_walker_is_stale=perm_walker_is_stale,
    ))
    # Explicit: 60s browser cache + 60s CF edge cache. Visitors always
    # see fresh-within-a-minute data; without this header, CF returns
    # DYNAMIC but browsers heuristic-cache the response indefinitely.
    resp.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
    return resp


@app.route("/verify")
def verify():
    """Step-by-step guide for an XRPL issuer to publish a two-way TOML
    attestation: set the Domain field on the issuer wallet AND publish an
    xrp-ledger.toml at that domain that claims the wallet back. Public-good
    operator doc — linked from each /rwa pending entry and from outreach DMs."""
    return render_template("verify.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/security")
def security():
    return render_template("security.html")


@app.route("/glossary")
def glossary():
    with open(GLOSSARY_PATH, encoding="utf-8") as f:
        terms = json.load(f)
    terms_sorted = sorted(terms, key=lambda t: t["term"].upper())
    return render_template("glossary.html", terms=terms_sorted)


@app.route("/help/already-sent-money")
def help_already_sent_money():
    """Calm crisis-design page for someone who has already sent XRP or
    a token to a scammer. Linked from every /check result footer. The
    hardest tone in the codebase: plain, non-judgmental, actionable.
    The recovery-scam warning is the emotional centerpiece — never
    remove it and never soften the "we are NOT a recovery service"
    line. See D4 Gate 3 for the design rationale."""
    return render_template("help_already_sent_money.html")


@app.route("/subprocessors")
def subprocessors():
    return render_template("subprocessors.html")


# ---------------------------------------------------------------------------
# Signed integrity snapshots — the truth-first endpoint
#
# Every day at 00:05 UTC, signed_snapshot.py captures a small set of
# canonical numbers (ledger index, pool count + TVL, MPT count, watchlist
# count), hashes them into a Merkle leaf, extends the global chain, and
# signs the result with our Ed25519 key. The resulting JSON file is
# self-attesting: anyone with our public key can verify the value was
# attested at the recorded date and is included in the chain root.
#
# Files live in signed_snapshots/ (committed to repo for transparency).
# Routes below serve them at standard well-known paths so any verifier
# (a journalist, an institutional buyer, a contributor) can fetch them
# programmatically.
# ---------------------------------------------------------------------------


def _list_signed_snapshots():
    """Newest-first list of YYYY-MM-DD dates with a signed snapshot. Prefer
    Postgres (Render has no disk access to the Mac-written files); fall
    back to disk so local dev still works without DATABASE_URL."""
    pg_dates = db.read_signed_snapshot_dates()
    if pg_dates:
        return pg_dates
    try:
        files = sorted(
            (f for f in os.listdir(SIGNED_SNAPSHOTS_DIR)
             if f.endswith(".json") and f != "chain.json"),
            reverse=True,
        )
    except (FileNotFoundError, OSError):
        return []
    return [f.removesuffix(".json") for f in files]


def _read_chain_meta():
    """Live chain head. PG-first (so Render sees the same chain root the
    Mac wrote); disk fallback for local dev. Returns the dict matching
    disk's chain.json shape, or None when no data anywhere."""
    pg_chain = db.read_signed_snapshot_chain()
    if pg_chain:
        return pg_chain
    try:
        with open(os.path.join(SIGNED_SNAPSHOTS_DIR, "chain.json")) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        return None


def _read_signed_envelope(date_str):
    """Load one date's envelope: PG-first, disk fallback. Returns dict or
    None. Caller has already validated `date_str` via _safe_date_str."""
    pg_env = db.read_signed_snapshot(date_str)
    if pg_env:
        return pg_env
    path = os.path.join(SIGNED_SNAPSHOTS_DIR, f"{date_str}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        return None


@app.route("/.well-known/snapshots/<date>.json")
@limiter.limit(agent_tier_limit_rate)
def well_known_signed_snapshot(date):
    """Serve a signed snapshot JSON. PG-first so Render serves the same
    bytes the Mac wrote; disk fallback for local dev. Once written, a
    snapshot is mathematically immutable — its hash is anchored into the
    Merkle chain — so the cache header reflects that semantic."""
    if not _safe_date_str(date):
        abort(404)
    envelope = _read_signed_envelope(date)
    if envelope is None:
        abort(404)
    resp = jsonify(envelope)
    resp.headers["Cache-Control"] = (
        "public, max-age=31536000, s-maxage=31536000, immutable"
    )
    return resp


@app.route("/.well-known/snapshots/chain.json")
@limiter.limit(agent_tier_limit_rate)
def well_known_signed_chain():
    """Append-only Merkle chain head. PG-first; disk fallback. Shorter
    cache than per-date snapshots because the chain extends daily and we
    want verifiers to pick up new leaves promptly."""
    chain = _read_chain_meta()
    if chain is None:
        abort(404)
    resp = jsonify(chain)
    resp.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
    return resp


@app.route("/.well-known/snapshots/pubkey.pem")
@limiter.limit(agent_tier_limit_rate)
def well_known_signed_pubkey():
    """Public key, PEM-encoded. Pinned in three independent places:
    here, /about page, and a DNS TXT record. Verifiers triangulate."""
    resp = send_from_directory(
        HERE, "snapshot_pubkey.pem",
        mimetype="application/x-pem-file",
    )
    resp.headers["Cache-Control"] = "public, max-age=3600, s-maxage=3600"
    return resp


def _safe_date_str(s):
    """Strict YYYY-MM-DD validation. Defends the well-known route from
    directory-traversal attempts via crafted date param."""
    if not isinstance(s, str) or len(s) != 10:
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


@app.route("/snapshots/")
def signed_snapshots_index():
    """Human-readable index of all signed snapshots + verification doc."""
    dates = _list_signed_snapshots()
    fingerprint = None
    try:
        with open(SNAPSHOT_PUBKEY_FP_PATH) as f:
            fingerprint = f.read().strip()
    except (OSError, FileNotFoundError):
        pass

    chain = _read_chain_meta()
    chain_meta = None
    if chain:
        leaves = chain.get("leaves") or []
        chain_meta = {
            "current_root": chain.get("current_root"),
            "leaves_total": chain.get("leaves_total") or len(leaves),
            "first_date": chain.get("first_date")
                or (leaves[0].get("date") if leaves else None),
        }

    return render_template(
        "signed_snapshots_index.html",
        dates=dates,  # already newest-first
        fingerprint=fingerprint,
        chain_meta=chain_meta,
    )


@app.route("/snapshots/verify")
def signed_snapshots_verify():
    """Interactive verification UI. Visitor enters (date, metric, expected
    value); we fetch the corresponding signed file from disk, re-derive
    the leaf hash, verify the Ed25519 signature, verify the audit path
    against the chain root, and report each check independently."""
    date_str = (request.args.get("date") or "").strip()
    metric = (request.args.get("metric") or "").strip()
    expected = (request.args.get("value") or "").strip()

    result = None
    if date_str:
        if not _safe_date_str(date_str):
            result = {"ok": False, "issues": [f"invalid date format (need YYYY-MM-DD): {date_str}"]}
        else:
            result = _verify_snapshot(date_str, metric, expected)

    fingerprint = None
    try:
        with open(SNAPSHOT_PUBKEY_FP_PATH) as f:
            fingerprint = f.read().strip()
    except (OSError, FileNotFoundError):
        pass

    return render_template(
        "signed_snapshots_verify.html",
        date_str=date_str,
        metric=metric,
        expected=expected,
        result=result,
        fingerprint=fingerprint,
    )


def _verify_snapshot(date_str, metric, expected):
    """Run the same checks signed_snapshot.py --verify does, plus an
    optional metric-value lookup. PG-first; disk fallback. Pulls the
    envelope from whichever source has it and runs verify_envelope on
    the dict directly so a single code path covers both storage sources."""
    try:
        import signed_snapshot as ss
    except Exception as e:
        return {"ok": False, "issues": [f"verifier module unavailable: {type(e).__name__}"]}

    envelope = _read_signed_envelope(date_str)
    if envelope is None:
        return {"ok": False, "issues": [f"snapshot for {date_str}: not found"]}

    ok, issues = ss.verify_envelope(envelope)
    matched_metric = None
    if ok and metric:
        for m in envelope.get("metrics", []):
            if m.get("name") == metric:
                matched_metric = m
                break
        if matched_metric is None:
            issues.append(f"metric '{metric}' not present in snapshot")
            ok = False
        elif expected:
            # Best-effort string compare — handles ints and floats by
            # round-trip through str() of the file value.
            if str(matched_metric.get("value")) != expected:
                issues.append(
                    f"value mismatch (snapshot has {matched_metric.get('value')!r}, "
                    f"you provided expected value {expected!r})"
                )
                ok = False

    return {"ok": ok, "issues": issues, "matched_metric": matched_metric}


def _historical_snapshot_meta_from_disk():
    """Scan historical_snapshots/ for first-snapshot date, day count, and
    coverage from the latest snapshot. Returns None if nothing on disk yet —
    template hides the section in that case rather than rendering "0 days".
    Cheap enough to call per request (a directory listing + one file open)."""
    try:
        files = sorted(f for f in os.listdir(SNAPSHOT_DIR) if f.endswith(".json"))
    except (FileNotFoundError, OSError):
        return None
    if not files:
        return None
    first_date = files[0].replace(".json", "")
    latest_path = os.path.join(SNAPSHOT_DIR, files[-1])
    try:
        with open(latest_path) as f:
            latest = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return {
        "first_date": first_date,
        "days_collected": len(files),
        "accounts_tracked": len(latest.get("accounts") or []),
        "pools_tracked": len(latest.get("amm_pools") or []),
        "mpts_tracked": len(latest.get("mpts") or []),
    }


def _historical_snapshot_meta():
    """Prefer the Postgres-mirrored rollup so Render renders the strip even
    without local snapshot files. Fall back to the local directory when PG
    is unavailable or empty — keeps Mac-side dev paths working unchanged."""
    pg_meta = db.read_snapshot_meta()
    if pg_meta:
        return pg_meta
    return _historical_snapshot_meta_from_disk()


_COLLECTING_SINCE = date(2026, 5, 7)


@app.route("/institutional")
def institutional():
    """Pre-launch institutional positioning page. Contact-only (no published
    prices) until launch-partner conversations produce real pricing data.
    Linked from the top nav so the CTA has traffic to measure; server-side
    click logger at /click/institutional-contact writes one row per click."""
    days_collecting = max(1, (date.today() - _COLLECTING_SINCE).days + 1)
    return render_template(
        "institutional.html",
        snapshot_meta=_historical_snapshot_meta(),
        days_collecting=days_collecting,
    )


# Pre-encoded mailto target for the /institutional launch-partner CTA.
# Held server-side so the click endpoint can't be coerced into redirecting
# to an arbitrary URL — the destination is fixed regardless of input.
_INSTITUTIONAL_MAILTO = (
    "mailto:contact@xrpldashboard.com"
    "?subject=Launch%20partner%20interest"
    "&body=Hi%20Charlie%2C%0A%0A"
    "Company%3A%0A"
    "Use%20case%3A%0A"
    "Current%20XRPL%20data%20tooling%3A%0A"
    "Approximate%20budget%20range%3A%0A%0A"
)


@app.route("/click/institutional-contact")
def click_institutional_contact():
    """Server-side click logger for the /institutional launch-partner CTA.
    Logs the click, then 302-redirects to the on-site contact form. The
    previous target was a pre-filled mailto: URL, which silently failed on
    mobile devices without a configured mail app, corporate lockdowns, and
    webmail-only users (21 unique clicks, 0 emails received May–Jun 2026).
    The form path works on every device without requiring a mail client;
    the mailto: fallback stays visible inside the form for founders who
    prefer direct email."""
    try:
        ip = _client_ip()
        ua = (request.user_agent.string or "")[:300] or None
        ref_param = (request.args.get("ref") or "").strip()[:64] or None
        referrer = (request.referrer or "")[:300] or None
        country = request.headers.get("CF-IPCountry") \
            or request.headers.get("X-Vercel-IP-Country") \
            or request.headers.get("X-Country-Code")
        db.log_cta_click(
            cta_id="institutional-contact",
            ref_param=ref_param,
            referrer=referrer,
            visitor_hash=_visitor_hash(ip, ua),
            user_agent=ua,
            country=country,
        )
    except Exception:
        pass
    ref = (request.args.get("ref") or "").strip()[:64]
    dest = url_for("institutional_contact_form")
    if ref:
        dest = f"{dest}?ref={ref}"
    return redirect(dest, code=302)


def _send_institutional_alert(inquiry):
    """Best-effort Brevo SMTP alert to Charlie when a new inquiry lands.
    No-op if SMTP env vars aren't set — the DB row is authoritative, the
    alert is a convenience.

    Env vars (all required to enable): SMTP_HOST, SMTP_PORT, SMTP_USER,
    SMTP_PASS, SMTP_FROM, SMTP_TO. Squarespace-preset DMARC (p=reject) on
    xrpldashboard.com means SMTP_FROM must be a Brevo-verified sender."""
    host = os.environ.get("SMTP_HOST", "").strip()
    port = os.environ.get("SMTP_PORT", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    pw = os.environ.get("SMTP_PASS", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip()
    to = os.environ.get("SMTP_TO", "").strip()
    if not (host and port and user and pw and sender and to):
        return False
    try:
        import smtplib
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = (
            f"[xrpldashboard] Institutional inquiry: "
            f"{inquiry.get('org') or inquiry.get('name') or inquiry['email']}"
        )
        msg["From"] = sender
        msg["To"] = to
        msg["Reply-To"] = inquiry["email"]
        lines = [
            f"Name:      {inquiry.get('name') or '—'}",
            f"Email:     {inquiry['email']}",
            f"Org:       {inquiry.get('org') or '—'}",
            f"Best time: {inquiry.get('best_time') or '—'}",
            f"Country:   {inquiry.get('country') or '?'}",
            f"Ref:       {inquiry.get('ref_param') or '—'}",
            f"Referrer:  {inquiry.get('referrer') or '—'}",
            "",
            "Message:",
            inquiry["message"],
            "",
            f"— row id {inquiry.get('id')}",
        ]
        msg.set_content("\n".join(lines))
        with smtplib.SMTP(host, int(port), timeout=10) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception:
        return False


def _looks_like_email(s):
    """Cheap syntactic check — no MX lookup, no RFC parse. Rejects the
    obviously-broken inputs without over-blocking valid oddities. The DB
    is authoritative; false-positives waste at most one DB row."""
    if not s or "@" not in s or " " in s:
        return False
    local, _, domain = s.rpartition("@")
    return bool(local) and "." in domain and len(s) <= 254


@app.route("/institutional/contact", methods=["GET"])
@limiter.limit("60 per minute")
def institutional_contact_form():
    """On-site contact form for launch-partner inquiries. Replaces the
    prior mailto:-only path, which lost visitors on mobile / corporate /
    webmail (see /click/institutional-contact for context)."""
    ref = (request.args.get("ref") or "").strip()[:64] or None
    return render_template(
        "institutional_contact.html",
        ref_param=ref,
        submitted=False,
    )


@app.route("/institutional/contact", methods=["POST"])
@limiter.limit("6 per hour")
def institutional_contact_submit():
    """Validate and persist one inquiry, fire the optional Brevo alert,
    then render the same template in a 'submitted' state.

    Rate limit is aggressive (6/hour/IP) because this endpoint writes to
    a table Charlie reads by hand — a spam flood would drown legitimate
    inquiries. Honeypot field 'website' (invisible in the UI) catches the
    naive bot floor; the limiter catches the rest."""
    if (request.form.get("website") or "").strip():
        return render_template(
            "institutional_contact.html",
            submitted=True,
            ref_param=None,
        )

    name = (request.form.get("name") or "").strip()[:200] or None
    email = (request.form.get("email") or "").strip()[:254]
    org = (request.form.get("org") or "").strip()[:200] or None
    best_time = (request.form.get("best_time") or "").strip()[:120] or None
    message = (request.form.get("message") or "").strip()[:5000]
    ref_param = (request.form.get("ref") or "").strip()[:64] or None

    errors = []
    if not _looks_like_email(email):
        errors.append("Please enter a valid email address.")
    if len(message) < 10:
        errors.append("Please include a short message (10+ characters).")

    if errors:
        return render_template(
            "institutional_contact.html",
            submitted=False,
            ref_param=ref_param,
            errors=errors,
            form={"name": name, "email": email, "org": org,
                  "best_time": best_time, "message": message},
        ), 400

    ip = _client_ip()
    ua = (request.user_agent.string or "")[:300] or None
    referrer = (request.referrer or "")[:300] or None
    country = request.headers.get("CF-IPCountry") \
        or request.headers.get("X-Vercel-IP-Country") \
        or request.headers.get("X-Country-Code")

    try:
        row_id = db.insert_institutional_inquiry(
            name=name, email=email, org=org,
            best_time=best_time, message=message,
            ref_param=ref_param, referrer=referrer,
            visitor_hash=_visitor_hash(ip, ua),
            user_agent=ua, country=country,
        )
    except Exception:
        return render_template(
            "institutional_contact.html",
            submitted=False,
            ref_param=ref_param,
            errors=["Something went wrong saving your message. "
                    "Please email contact@xrpldashboard.com directly."],
            form={"name": name, "email": email, "org": org,
                  "best_time": best_time, "message": message},
        ), 500

    if row_id is not None:
        sent = _send_institutional_alert({
            "id": row_id, "name": name, "email": email, "org": org,
            "best_time": best_time, "message": message,
            "ref_param": ref_param, "referrer": referrer, "country": country,
        })
        if sent:
            db.mark_institutional_inquiry_alerted(row_id)

    return render_template(
        "institutional_contact.html",
        submitted=True,
        ref_param=ref_param,
    )


# ─────────────────────────────────────────────────────────────────────
# B1 — /click/contact + /contact form. Extends the institutional-contact
# pattern to every other on-site mailto: link (bug reports, feedback,
# corrections, RWA attestations, etc.). Same rationale: mailto: silently
# fails on mobile without a mail app, corporate-lockdown browsers, and
# webmail-only users; the form lands submissions server-side regardless.
# security / legal / privacy addresses stay plain-text — legal precedent
# for coordinated disclosure + GDPR/CCPA rights requests treats the
# posted address as the primary channel, not a redirect.
# ─────────────────────────────────────────────────────────────────────

CONTACT_PURPOSES = {
    "bug-report":              "Bug report or data issue",
    "donation":                "Donation inquiry",
    "general":                 "General contact",
    "learn-feedback":          "Feedback on the /learn page",
    "verify-attestation":      "Verified-issuer attestation submission",
    "rwa-attestation":         "RWA issuer TOML attestation submission",
    "subprocessor-404":        "Broken subprocessor link",
    "methodology-discrepancy": "Methodology discrepancy",
    "data-correction":         "Data or number correction",
    "attestation-dispute":     "Attestation or label dispute",
    "institutional-general":   "Institutional inquiry (form fallback)",
}


def _is_bot_contact_submission(ua: str, message: str) -> tuple[bool, str]:
    """Return (is_bot, signature) for a /contact form submission.

    Three signatures cover the two campaigns observed in contact_inquiries
    (2026-07-03 through 2026-08-10) plus the original backlog pattern:

      unclosed_paren_ua  — `(KHTML, like Gecko;` in the UA string (no closing
                           paren before the semicolon). Identified in /contact
                           page-view traffic 2026-08-10; hasn't appeared in
                           submissions yet but included as a future-proof guard.

      seo_spam_owner     — message body contains "hello xrpldashboard" (case-
                           insensitive). The exact template: "Hello Xrpldashboard
                           Com Owner, My name is X and I'm betting you'd like
                           your website..." — seen in 5 of the 19 submission rows.

      av_bundle_ua       — UA contains "ccleaner/" or "avast/" after the Chrome
                           version. CCleaner and Avast bundle a Chrome-based
                           browser that sends these suffixes; they appear in the
                           multilingual price-inquiry campaign (LT/RO/LV, rotating
                           Gmail, "Hi I wanted to know your price" in 12 languages).

    Soft-drop design: return value drives a fake-200 — the bot gets told
    "message received" while the payload goes nowhere. Never teach the bot
    which signal it tripped.

    Returns (False, "") for clean submissions so callers can short-circuit
    without checking the reason string."""
    ua_lower = (ua or "").lower()
    msg_lower = (message or "").lower()

    if "(khtml, like gecko;" in ua_lower:
        return True, "unclosed_paren_ua"
    if "hello xrpldashboard" in msg_lower:
        return True, "seo_spam_owner"
    if "ccleaner/" in ua_lower or "avast/" in ua_lower:
        return True, "av_bundle_ua"

    return False, ""


@app.route("/click/contact")
def click_contact():
    """Click logger + purpose-routed redirect to /contact. purpose is
    baked into cta_id so click analytics segment by intent."""
    purpose = (request.args.get("purpose") or "general").strip().lower()
    if purpose not in CONTACT_PURPOSES:
        purpose = "general"
    try:
        ip = _client_ip()
        ua = (request.user_agent.string or "")[:300] or None
        ref_param = (request.args.get("ref") or "").strip()[:64] or None
        referrer = (request.referrer or "")[:300] or None
        country = request.headers.get("CF-IPCountry") \
            or request.headers.get("X-Vercel-IP-Country") \
            or request.headers.get("X-Country-Code")
        db.log_cta_click(
            cta_id=f"contact:{purpose}",
            ref_param=ref_param,
            referrer=referrer,
            visitor_hash=_visitor_hash(ip, ua),
            user_agent=ua,
            country=country,
        )
    except Exception:
        pass
    dest = f"{url_for('contact_form')}?purpose={purpose}"
    ref = (request.args.get("ref") or "").strip()[:64]
    if ref:
        dest = f"{dest}&ref={ref}"
    return redirect(dest, code=302)


def _send_contact_alert(inquiry):
    """Best-effort Brevo SMTP alert for new /contact submissions. Same
    env-vars gate as the institutional alert; no-op if unset."""
    host = os.environ.get("SMTP_HOST", "").strip()
    port = os.environ.get("SMTP_PORT", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    pw = os.environ.get("SMTP_PASS", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip()
    to = os.environ.get("SMTP_TO", "").strip()
    if not (host and port and user and pw and sender and to):
        return False
    try:
        import smtplib
        from email.message import EmailMessage
        purpose_label = CONTACT_PURPOSES.get(
            inquiry["purpose"], inquiry["purpose"]
        )
        msg = EmailMessage()
        msg["Subject"] = (
            f"[xrpldashboard] {purpose_label}: "
            f"{inquiry.get('name') or inquiry['email']}"
        )
        msg["From"] = sender
        msg["To"] = to
        msg["Reply-To"] = inquiry["email"]
        lines = [
            f"Purpose:   {purpose_label} ({inquiry['purpose']})",
            f"Name:      {inquiry.get('name') or '—'}",
            f"Email:     {inquiry['email']}",
            f"Country:   {inquiry.get('country') or '?'}",
            f"Ref:       {inquiry.get('ref_param') or '—'}",
            f"Referrer:  {inquiry.get('referrer') or '—'}",
            "",
            "Message:",
            inquiry["message"],
            "",
            f"— row id {inquiry.get('id')}",
        ]
        msg.set_content("\n".join(lines))
        with smtplib.SMTP(host, int(port), timeout=10) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception:
        return False


@app.route("/contact", methods=["GET"])
@limiter.limit("60 per minute")
def contact_form():
    """Render the on-site general contact form. purpose enum drives the
    headline; unknown values fall back to 'general'."""
    purpose = (request.args.get("purpose") or "general").strip().lower()
    if purpose not in CONTACT_PURPOSES:
        purpose = "general"
    ref = (request.args.get("ref") or "").strip()[:64] or None
    return render_template(
        "contact.html",
        purpose=purpose,
        purpose_label=CONTACT_PURPOSES[purpose],
        purposes=CONTACT_PURPOSES,
        ref_param=ref,
        submitted=False,
    )


@app.route("/contact", methods=["POST"])
@limiter.limit("6 per hour")
def contact_submit():
    """Validate + persist + fire Brevo alert. Same aggressive rate limit
    as the institutional form because Charlie reads by hand. Honeypot
    field 'website' (invisible in the UI) catches naive bots."""
    if (request.form.get("website") or "").strip():
        return render_template(
            "contact.html",
            submitted=True,
            purpose="general",
            purpose_label=CONTACT_PURPOSES["general"],
            purposes=CONTACT_PURPOSES,
            ref_param=None,
        )

    purpose = (request.form.get("purpose") or "general").strip().lower()
    if purpose not in CONTACT_PURPOSES:
        purpose = "general"
    name = (request.form.get("name") or "").strip()[:200] or None
    email = (request.form.get("email") or "").strip()[:254]
    message = (request.form.get("message") or "").strip()[:5000]
    ref_param = (request.form.get("ref") or "").strip()[:64] or None

    errors = []
    if not _looks_like_email(email):
        errors.append("Please enter a valid email address.")
    if len(message) < 10:
        errors.append("Please include a short message (10+ characters).")

    if errors:
        return render_template(
            "contact.html",
            submitted=False,
            purpose=purpose,
            purpose_label=CONTACT_PURPOSES[purpose],
            purposes=CONTACT_PURPOSES,
            ref_param=ref_param,
            errors=errors,
            form={"name": name, "email": email, "message": message},
        ), 400

    ip = _client_ip()
    ua = (request.user_agent.string or "")[:300] or None
    referrer = (request.referrer or "")[:300] or None
    country = request.headers.get("CF-IPCountry") \
        or request.headers.get("X-Vercel-IP-Country") \
        or request.headers.get("X-Country-Code")

    # Bot filter — soft-drop: bot gets a fake-200 success page, payload
    # goes nowhere, bot never learns it was filtered. The drop is logged
    # (ts + UA + signature only, no payload) so campaign decay is auditable.
    is_bot, bot_sig = _is_bot_contact_submission(ua or "", message)
    if is_bot:
        db.log_contact_bot_drop(ua, bot_sig)
        return render_template(
            "contact.html",
            submitted=True,
            purpose=purpose,
            purpose_label=CONTACT_PURPOSES[purpose],
            purposes=CONTACT_PURPOSES,
            ref_param=ref_param,
        )

    try:
        row_id = db.insert_contact_inquiry(
            purpose=purpose, name=name, email=email, message=message,
            ref_param=ref_param, referrer=referrer,
            visitor_hash=_visitor_hash(ip, ua),
            user_agent=ua, country=country,
        )
    except Exception:
        return render_template(
            "contact.html",
            submitted=False,
            purpose=purpose,
            purpose_label=CONTACT_PURPOSES[purpose],
            purposes=CONTACT_PURPOSES,
            ref_param=ref_param,
            errors=["Something went wrong saving your message. "
                    "Please email contact@xrpldashboard.com directly."],
            form={"name": name, "email": email, "message": message},
        ), 500

    if row_id is not None:
        sent = _send_contact_alert({
            "id": row_id, "purpose": purpose, "name": name, "email": email,
            "message": message, "ref_param": ref_param,
            "referrer": referrer, "country": country,
        })
        if sent:
            db.mark_contact_inquiry_alerted(row_id)

    return render_template(
        "contact.html",
        submitted=True,
        purpose=purpose,
        purpose_label=CONTACT_PURPOSES[purpose],
        purposes=CONTACT_PURPOSES,
        ref_param=ref_param,
    )


def _rank_status_order(r):
    """Sort: exact USD-pegged > estimated XRP-paired > non-XRP > error.
    Within each group, descending TVL."""
    order = {"exact": 0, "estimated": 1, "non_xrp_pair": 2, "error": 3}.get(
        r.get("tvl_status"), 4)
    return (order, -(r.get("tvl_usd") or 0))


@app.route("/pools")
def pools():
    """Browse every indexed AMM, ranked by TVL.

    Reads amm_ranked.json (produced by rank_amms.py) and shows a tiered
    view: a hero strip of the top 10, then a paginated table that defaults
    to top 100 (?tier=100|500|all). The header reflects how many pools
    have been indexed by the bootstrap scan, not just how many are ranked
    so far — so users see "9,500+ indexed" even if ranking is mid-run."""
    _PAGE_SIZE = 500
    valid_tiers = {"100": 100, "500": 500}
    tier = (request.args.get("tier") or "").strip().lower()
    has_page = request.args.get("page") is not None

    ranked, meta = _ranked_amm_snapshot()

    # Strict TVL desc — header says "by TVL", so sort by TVL. Pools with no
    # USD value (non_xrp_pair, error) sink to the bottom. Status pill on
    # each row still surfaces exact-vs-estimated trust.
    ranked = sorted(
        ranked,
        key=lambda r: (
            0 if (r.get("tvl_usd") or 0) > 0 else 1,
            -(r.get("tvl_usd") or 0),
        ),
    )

    top10 = [r for r in ranked if (r.get("tvl_usd") or 0) > 0][:10]

    if has_page:
        pass  # fall through to browse-all branch below
    elif tier not in valid_tiers:
        tier = "100"  # unknown or missing tier → default top-100

    if not has_page and tier in valid_tiers:
        limit = valid_tiers[tier]
        rows = ranked[:limit]
        page = None
        total_pages = None
    else:
        # browse-all: paginated at PAGE_SIZE rows per page
        tier = "browse"
        try:
            page = max(1, int(request.args.get("page") or 1))
        except (TypeError, ValueError):
            page = 1
        total_pages = max(1, (len(ranked) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = min(page, total_pages)
        offset = (page - 1) * _PAGE_SIZE
        rows = ranked[offset : offset + _PAGE_SIZE]

    indexed_count = meta.get("indexed_count") or 0
    exact_count_all = sum(1 for r in ranked if r.get("tvl_status") == "exact")
    estimated_count_all = sum(1 for r in ranked if r.get("tvl_status") == "estimated")
    non_xrp_pair_count_all = sum(1 for r in ranked if r.get("tvl_status") == "non_xrp_pair")
    # ranked_count is the visible breakdown sum, not len(ranked). `error`
    # rows are operational (RPC flake, schema migration partial) — they
    # belong in worker logs, not the public stats card. Reconciles math
    # by construction: ranked_count == exact + estimated + non_xrp_pair
    # even if error rows are present in the ranked list.
    ranked_count = exact_count_all + estimated_count_all + non_xrp_pair_count_all
    spoof_count_all = sum(
        1
        for r in ranked
        for side in (r.get("asset_a"), r.get("asset_b"))
        if isinstance(side, dict) and side.get("unverified_brand")
    )
    rank_finished = meta.get("finished_at") is not None
    rank_started = meta.get("started_at") is not None
    rank_in_progress = rank_started and not rank_finished

    # Aggregate: total TVL across exact + estimated only (non_xrp_pair has
    # tvl_usd=None, error has no value either).
    total_tvl_usd = sum(
        (r.get("tvl_usd") or 0)
        for r in ranked
        if r.get("tvl_status") in ("exact", "estimated")
    )

    # Treemap-hero data: share-of-top-10 drives bar widths, share-of-all
    # gives the concentration headline. Mutates top10 in place since the
    # template iterates the same list.
    top10_total_tvl = sum((p.get("tvl_usd") or 0) for p in top10)
    for p in top10:
        tvl = p.get("tvl_usd") or 0
        p["share_of_top10"] = (tvl / top10_total_tvl * 100.0) if top10_total_tvl else 0
    top10_share_of_all = (
        (top10_total_tvl / total_tvl_usd * 100.0) if total_tvl_usd else 0
    )

    snapshot_age_label = None
    try:
        snap_ts = meta.get("snapshot_ts")
        if snap_ts:
            age = max(0, int(time.time()) - int(snap_ts))
            if age < 60:
                snapshot_age_label = f"{age}s ago"
            elif age < 3600:
                snapshot_age_label = f"{age // 60}m ago"
            elif age < 86400:
                snapshot_age_label = f"{age // 3600}h ago"
            else:
                snapshot_age_label = f"{age // 86400}d ago"
    except Exception:
        pass

    return render_template(
        "pools.html",
        top10=top10,
        rows=rows,
        tier=tier,
        indexed_count=indexed_count,
        ranked_count=ranked_count,
        exact_count_all=exact_count_all,
        estimated_count_all=estimated_count_all,
        non_xrp_pair_count_all=non_xrp_pair_count_all,
        spoof_count_all=spoof_count_all,
        rank_finished=rank_finished,
        rank_in_progress=rank_in_progress,
        total_tvl_usd=total_tvl_usd,
        top10_total_tvl=top10_total_tvl,
        top10_share_of_all=top10_share_of_all,
        snapshot_age_label=snapshot_age_label,
        page=page,
        total_pages=total_pages,
    )


_XRPL_ADDR_CHARS = set("rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz")


def _is_xrpl_address(s):
    """Loose XRPL address validation: starts with 'r', 25-35 base58 chars."""
    if not s or not isinstance(s, str):
        return False
    if not s.startswith("r"):
        return False
    if not (25 <= len(s) <= 35):
        return False
    return all(c in _XRPL_ADDR_CHARS for c in s)


@app.route("/wallet/<address>")
@limiter.limit("60 per minute")
def wallet(address):
    """Live wallet detail view: balance, reserve, network graph of
    counterparties, 30d activity pulse. Reads directly from XRPL.

    Cached 5min per (address, lookback_days). Designed to double as an
    integrity check — every metric is verifiable against XRPSCAN/Bithomp."""
    address = (address or "").strip()
    if not _is_xrpl_address(address):
        # Render the branded 404 instead of a zero-filled wallet view —
        # zeros silently misrepresent a malformed/nonexistent address as
        # an empty-but-valid wallet, which is misleading on a page that
        # doubles as an integrity check.
        return render_template("404.html"), 404

    data = fetch_wallet_data_cached(address)
    # Surface the live USD anchor on every wallet render so the user can
    # see where the dollar figures come from. Cheap (cached ~60s in oracle).
    try:
        from price_oracle import xrp_usd, xrp_usd_sources
        data["xrp_usd"] = xrp_usd()
        data["xrp_usd_sources"] = xrp_usd_sources()
    except Exception:
        data["xrp_usd"] = None
        data["xrp_usd_sources"] = []
    import wallet_data
    return render_template(
        "wallet.html",
        data=data,
        wallet_qr_svg=_wallet_qr_svg(address),
        cache_ttl_seconds=wallet_data.CACHE_TTL,
    )


_CHECK_JSON_METHODOLOGY_URL = "https://xrpldashboard.com/methodology#for-ai-agents"


def _check_prefers_json(req) -> bool:
    """Content-negotiation for /check. True when caller asked for JSON.

    Browsers send `text/html,…,application/json;q=0.9` → HTML wins.
    AI-agent fetchers sending `Accept: application/json` → JSON wins.
    A missing / `*/*` Accept header stays HTML (backwards-compat with
    the paste-box path and curl defaults).

    v0.9 (2026-08-30): the `/check.json` alias route and the `?format=json`
    query param both force JSON regardless of Accept header so machine
    callers don't have to construct headers.
    """
    if req.path == "/check.json":
        return True
    if (req.args.get("format") or "").lower() == "json":
        return True
    # Werkzeug's MIMEAccept is truthy for `*/*`, so `not accept` never
    # fires for a bare curl (which sends `Accept: */*`). Check the raw
    # header for the empty / wildcard case explicitly.
    raw_accept = (req.headers.get("Accept") or "").strip()
    if not raw_accept or raw_accept == "*/*":
        return False
    accept = req.accept_mimetypes
    # text/html listed first so a `*/*` fragment in a longer header ties
    # to HTML rather than JSON (audit finding 2026-08-30).
    best = accept.best_match(
        ["text/html", "application/json"], default="text/html"
    )
    return best == "application/json"


# ---------------------------------------------------------------------------
# /check.json v0.9 — per-verdict Ed25519 signing (DRAFT, 2026-08-30).
#
# Field names and envelope shape are subject to Charlie's Tue 2026-09-01 EOD
# ruling before v1.0 freezes them. Enabled only when CHECK_SIGNING_KEY_PATH
# env var points at a readable unencrypted Ed25519 PEM (hot key, separate
# from the snapshot cold key). Unset → sig_ed25519 = null in output, no
# crash. Fail-quiet-with-null-sig parallels x402_rails.py's pay_to empty
# default: a downstream that expects a signature will see null and know
# to fix the deploy rather than silently trust an absent sig.
#
# Key lifecycle owed as its own ruling; docs/BUSINESS_TRACK_CHECKLIST_2026-
# 08-30.md carries the pointer.
# ---------------------------------------------------------------------------

_CHECK_V09_SIGNER_ID = "check.xrpldashboard.com"
_CHECK_V09_KEY_CACHE = {"loaded": False, "key": None}


def _check_v09_load_signing_key():
    """Load the /check hot signing key lazily; None if unavailable."""
    cache = _CHECK_V09_KEY_CACHE
    if cache["loaded"]:
        return cache["key"]
    cache["loaded"] = True
    key_path = os.environ.get("CHECK_SIGNING_KEY_PATH", "").strip()
    if not key_path or not os.path.exists(key_path):
        return None
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        with open(key_path, "rb") as f:
            priv = serialization.load_pem_private_key(
                f.read(), password=None
            )
        if isinstance(priv, Ed25519PrivateKey):
            cache["key"] = priv
            return priv
        app.logger.error(
            "CHECK_SIGNING_KEY_PATH=%r is not an Ed25519 private key",
            key_path,
        )
    except Exception:
        app.logger.exception(
            "failed to load /check.json signing key from %r", key_path
        )
    return None


def _check_v09_sign(envelope: dict) -> dict:
    """Return envelope with a `proof.check_v09_signature` block added.

    DRAFT — field names subject to Charlie's Tue 2026-09-01 EOD ruling.
    Nested inside `proof` (rather than as a new top-level key) to keep
    the {data, proof, server} envelope contract locked by
    tests/test_check_json_negotiation.py.

    Canonical hash is SHA-256 of `signed_snapshot._canonical_json(...)` —
    same canonicalization the snapshot signer uses, so verifiers can
    reuse the existing verify path. Hash is computed over the envelope
    with the signature block absent (self-reference would be circular),
    so the signature covers exactly what a verifier reconstructs.
    """
    import base64
    import copy
    import hashlib
    from signed_snapshot import _canonical_json

    envelope_for_hash = copy.deepcopy(envelope)
    envelope_for_hash.get("proof", {}).pop("check_v09_signature", None)
    canonical = _canonical_json(envelope_for_hash)
    canonical_hash = hashlib.sha256(canonical).hexdigest()
    priv = _check_v09_load_signing_key()
    if priv is None:
        sig_b64 = None
        signer = None
    else:
        sig_b64 = base64.b64encode(priv.sign(canonical)).decode()
        signer = _CHECK_V09_SIGNER_ID
    signed = copy.deepcopy(envelope)
    signed.setdefault("proof", {})["check_v09_signature"] = {
        "signer": signer,
        "sig_ed25519": sig_b64,
        "canonical_hash_sha256": canonical_hash,
        "canonicalization": "sorted-keys-no-whitespace-utf8",
    }
    return signed


def _check_strip_top_null_keys(payload: dict) -> dict:
    """Remove top-level keys with None values before envelope wrap.

    /check result dicts carry rendering hints (`next_action=None` for
    the empty-message case and the URL safety-gate reject case) which
    are UI signals, not missing sub-sources. The envelope contract
    treats top-level nulls as missing sub-sources, so strip them before
    wrap_envelope validation runs.
    """
    return {k: v for k, v in payload.items() if v is not None}


def _check_json_response(result: dict, *, status: int = 200) -> Response:
    """Wrap a /check result in the proof-annotation envelope."""
    from mcp_server import wrap_envelope
    couldnt = result.get("couldnt_check") or []
    honest_partial = bool(couldnt)
    if honest_partial:
        labels = [
            c.get("label", "?") for c in couldnt if isinstance(c, dict)
        ]
        scope_note = (
            "One or more sub-sources didn't answer in time or returned "
            "no data (RDAP / crt.sh / on-chain lookup / OFAC / TOML "
            "fetch): "
            + ", ".join(labels)
            + ". Everything else in `data` is present."
        )
    else:
        scope_note = None

    payload = _check_strip_top_null_keys(result)
    envelope = wrap_envelope(
        payload,
        source="xrpldashboard/check-endpoint",
        as_of=result.get("checked_at_utc"),
        freshness_contract="≤ 5min",
        methodology_url=_CHECK_JSON_METHODOLOGY_URL,
        cross_check_status="not_applicable",
        honest_partial=honest_partial,
        scope_note=scope_note,
    )
    envelope = _check_v09_sign(envelope)
    response = jsonify(envelope)
    response.status_code = status
    return response


@app.route("/check", methods=["GET", "POST"])
@limiter.limit("60 per minute")
def check_page():
    """D1 + D2 + D3 + D4: paste an XRPL address, token, URL, or a whole
    message, get timestamped/sourced signals.

    Facts-not-verdicts by construction — every returned signal carries
    label + source + checked_at_utc, and status pill summarizes WHAT
    IDENTITY CLAIM EXISTS ON THE LEDGER, not whether the subject is
    safe to interact with.

    Two input modes:
      GET  ?q=…         — permalink form for address / token / URL.
      POST body m=…     — message triage (D4). NEVER a permalink.
                          Message contents are not stored anywhere;
                          _log_page_view skips POST so no page_view
                          row lands with the message in the URL. Back-
                          button hits the empty GET, not a rehydrated
                          message. This is a hard privacy contract
                          with /privacy §2b — do not change it without
                          updating that section.
    """
    # --- D4 message triage (POST) ------------------------------------
    if request.method == "POST":
        # Read message from form, clamp to a bounded size. NEVER store
        # this value beyond the request scope — no DB write, no cache,
        # no log line containing the full message. The 120-char preview
        # in the app request log is the only durable trace; a text
        # preview of an INPUT TYPE, not the input's content.
        raw_message = (request.form.get("m") or "").strip()
        if len(raw_message) > 8000:
            raw_message = raw_message[:8000]
        # Ephemeral request-log breadcrumb: type + short preview only.
        # Goes through gunicorn's stderr, not a queryable store. See
        # /privacy §2b: pasted content is NOT stored in the database.
        preview = raw_message[:120].replace("\n", " ")
        app.logger.info(
            "check_message input: len=%d preview=%r",
            len(raw_message), preview,
        )
        import check_data
        try:
            result = check_data.check_message(raw_message)
            input_error = None
        except Exception:
            app.logger.exception("check_page: message triage failed")
            result = None
            input_error = babel_gettext(
                "Something went wrong checking that message. "
                "Try again in a moment."
            )
        # Deliberately pass query="" so back-button rehydration does
        # not resurface the message. Empty paste box + "paste again"
        # is the graceful path (Charlie's D4a-Q1 answer).
        if result is not None and _check_prefers_json(request):
            return _check_json_response(result)
        return render_template(
            "check.html",
            query="",
            result=result,
            input_error=input_error,
        )

    # --- GET fallthrough: D1/D2/D3 permalink path --------------------
    q = (request.args.get("q") or "").strip()
    result = None
    input_error = None

    if q:
        try:
            import check_data
            # URL form has highest precedence when scheme is explicit.
            if "://" in q:
                host = check_data._extract_hostname(q)
                if host and check_data._domain_is_safe(host):
                    result = check_data.check_url(q)
                else:
                    input_error = babel_gettext(
                        "That URL fails a basic safety gate (private-network "
                        "IP, invalid shape, or off-standard characters)."
                    )
            elif "." in q:
                # Token form: SYMBOL.rIssuer — issuer must be a valid r-address.
                symbol, _, rest = q.partition(".")
                symbol = symbol.strip()
                rest = rest.strip()
                if symbol and _is_xrpl_address(rest):
                    result = check_data.check_token(symbol, rest)
                elif check_data._domain_is_safe(q):
                    # Bare domain (e.g. "bitstamp.com") — treat as URL form.
                    result = check_data.check_url(q)
                else:
                    input_error = babel_gettext(
                        "That doesn't look like an XRPL wallet address, "
                        "a token (SYMBOL.rIssuer), or a URL/domain."
                    )
            elif _is_xrpl_address(q):
                result = check_data.check_address(q)
            else:
                input_error = babel_gettext(
                    "That doesn't look like an XRPL wallet address "
                    "(starts with 'r'), a token (SYMBOL.rIssuer), "
                    "or a URL/domain."
                )
        except Exception:
            app.logger.exception("check_page: lookup failed")
            input_error = babel_gettext(
                "Something went wrong checking that. Try again in a moment."
            )

    if _check_prefers_json(request):
        if result is not None:
            return _check_json_response(result)
        # Empty query or user-input error: JSON-shaped 400 for machine
        # callers so they get a structured signal, not an HTML paste-box.
        return (
            jsonify({
                "error": input_error or "no query provided (use ?q=...)",
                "query": q,
            }),
            400,
        )

    return render_template(
        "check.html",
        query=q,
        result=result,
        input_error=input_error,
    )


@app.route("/check.json", methods=["GET", "POST"])
@limiter.limit("60 per hour")
def check_json_page():
    """v0.9 machine surface — alias route that forces JSON regardless of
    Accept header. Reuses check_page's full D1-D4 dispatch. Anon IP
    rate-limit 60/hour (Charlie 2026-08-30 spec). API-key-tier bypass
    lands in v1.0 when key middleware wires up."""
    return check_page()


_CURRENCY_HEX_CHARS = set("0123456789abcdefABCDEF")
_CURRENCY_ASCII_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "<>(){}[]|@#$%^&*?!"
)


def _is_valid_currency(s):
    """XRPL currency code: 3-char ASCII OR 40-char hex."""
    if not s:
        return False
    if len(s) == 3:
        return all(c in _CURRENCY_ASCII_CHARS for c in s)
    if len(s) == 40:
        return all(c in _CURRENCY_HEX_CHARS for c in s)
    return False


def _resolve_display_label_to_hex(label, issuer):
    """If `label` is a human display form (e.g. "RLUSD") for a verified
    token at `issuer`, return its canonical currency_hex. Otherwise None.
    Lets shareable URLs like /token/RLUSD/<issuer> redirect to the hex form."""
    if not label or not issuer:
        return None
    for entry in _load_token_names_dict().values():
        if entry.get("issuer") != issuer:
            continue
        if (entry.get("currency_display") or "").lower() == label.lower():
            return entry.get("currency_hex")
    return None


@app.route("/token/<currency>/<issuer>")
@limiter.limit("60 per minute")
def token_detail(currency, issuer):
    """Token detail view — drilldown from /tokens. Shows trade activity
    over time, AMM pools that hold this token, and links out to other
    explorers. Read-only artifacts only — no live RPC."""
    currency = (currency or "").strip()
    issuer = (issuer or "").strip()

    if not _is_xrpl_address(issuer):
        return render_template("404.html"), 404

    if not _is_valid_currency(currency):
        # Try resolving a human display label (e.g. "RLUSD") to its
        # canonical hex code so shareable URLs don't dead-end at 404.
        hex_match = _resolve_display_label_to_hex(currency, issuer)
        if hex_match:
            return redirect(
                url_for("token_detail", currency=hex_match, issuer=issuer),
                code=301,
            )
        # Render the branded 404 instead of a zero-filled token view.
        # The previous stub rendering echoed the raw `currency` / `issuer`
        # values into the page <title>, hero, and external explorer URLs —
        # so malformed (or attacker-supplied) input was visibly reflected.
        # Mirrors the /wallet/<address> handling.
        return render_template("404.html"), 404

    data = fetch_token_data_cached(currency, issuer)
    return render_template("token.html", data=data)


@app.route("/cold-storage")
def cold_storage():
    """Cold-storage tracker — currently scoped to Ripple monthly-release escrows.
    See cold_storage.py for the liquid-balance data layer, escrow_supply.py
    for the EscrowCreate-summed locked total, and escrow_snapshot.py for the
    per-escrow browser + upcoming-releases calendar (walker-populated)."""
    data = fetch_cold_storage_cached()
    try:
        locked = fetch_escrow_locked_cached()
    except Exception:
        locked = None
    try:
        from escrow_snapshot import fetch_escrow_snapshot_cached
        escrows = fetch_escrow_snapshot_cached()
    except Exception:
        escrows = None
    import cold_storage as cold_storage_module
    return render_template(
        "cold_storage.html",
        data=data,
        locked=locked,
        escrows=escrows,
        cache_ttl_seconds=cold_storage_module.CACHE_TTL,
    )


@app.route("/price-data")
def price_data():
    """XRPL price-data explainer — XLS-47 PriceOracle amendment.

    Framed as "here is the one production feed on mainnet, decoded and
    dated," not as a directory. XRPLWin runs the full raw directory
    (linked in the template) — we curate for signal quality. Walker
    (oracle_walker.py) populates oracles_snapshot; oracle_snapshot.py
    is the 5-min TTL cached read layer used here. Never touches the
    XRPL node from this route."""
    try:
        from oracle_snapshot import fetch_oracle_snapshot_cached
        data = fetch_oracle_snapshot_cached()
    except Exception:
        data = None
    return render_template("price_data.html", data=data)


@app.route("/lending")
def lending():
    """XLS-66 native lending — pre-built for activation day. While the
    LendingProtocol + SingleAssetVault amendments are in voting, the page
    shows amendment status + plain-English explainer. Once both light up,
    the broker table switches to live LoanBroker/Vault data.

    Data path on activation: a background worker (lending_snapshot.py)
    writes lending_snapshot.json with enriched broker rows. We read that
    snapshot if it's recent enough; otherwise we fall back to the in-
    process cached fetcher (5min TTL) so the page never breaks even if
    the snapshot worker is dead."""
    status = fetch_lending_status_cached()
    data = None
    if status and status.get("activated"):
        data = load_lending_snapshot()
        if data is None:
            data = fetch_lending_data_cached()
    return render_template(
        "lending.html",
        status=status,
        data=data,
        cache_ttl_seconds=LENDING_CACHE_TTL,
    )


# Tickers and names that obviously mark an issuance as test/demo. Conservative
# by design: a false-positive "test" badge on a real issuance is more harmful
# than a false-negative "prepared" badge on a sloppy test. Adjust when a real
# issuer collides with one of these patterns.
_MPT_TEST_TICKERS = {"TEST", "TMPT"}
_MPT_TEST_NAME_PREFIXES = ("test ", "test mpt")


def _normalize_outstanding(row):
    """Scale-aware outstanding amount used for sorting and "is this live".
    raw is a string of base units; asset_scale is the decimals. (None scale
    treated as 0 — same as the template's display logic.)"""
    raw = row.get("outstanding_amount") or 0
    try:
        raw = int(raw)
    except (TypeError, ValueError):
        return 0.0
    scale = row.get("asset_scale") or 0
    try:
        scale = int(scale)
    except (TypeError, ValueError):
        scale = 0
    if scale <= 0:
        return float(raw)
    return raw / (10 ** scale)


def _classify_mpt_status(row):
    """Three-state badge: live / prepared / test. The badge IS the editorial
    trust signal — readers shouldn't have to compute "real or staged" from
    a hex outstanding amount."""
    ticker = (row.get("ticker") or "").upper()
    name = (row.get("name") or "").strip().lower()
    if ticker in _MPT_TEST_TICKERS:
        return "test"
    if any(name.startswith(p) for p in _MPT_TEST_NAME_PREFIXES):
        return "test"
    if _normalize_outstanding(row) > 0:
        return "live"
    return "prepared"


def _normalize_holders(holders):
    """Return the v3 holders dict shape regardless of input. One-cycle
    compatibility shim: pre-worker snapshots had `holders` as int|None
    (positive-balance count, in practice always 0 due to a field-name bug);
    v3 snapshots have the full dict. After one hourly cycle confirms only
    v3 is on disk + in Postgres, the int/None branches can be deleted.

    Renderers (template + /api/mpts) only ever see this shape, so they
    don't need to know which schema produced the row."""
    if isinstance(holders, dict):
        return holders
    if isinstance(holders, int):
        return {
            "with_balance": holders,
            "authorized": None,
            "top": [],
            "walked_complete": True,
            "walked_at": None,
            "reason": "complete" if holders > 0 else "no_holders",
        }
    return {
        "with_balance": None,
        "authorized": None,
        "top": [],
        "walked_complete": False,
        "walked_at": None,
        "reason": "pending",
    }


def _enrich_mpt_rows(data):
    """Take a raw snapshot dict and add per-row {status, normalized_outstanding,
    issuer_label, holders (normalized)} fields, then re-sort: live first (by
    outstanding desc), then prepared, then test. Mutates a shallow copy so
    callers see the original snapshot dict untouched."""
    if not data or not data.get("issuances"):
        return data
    rows = list(data.get("issuances") or [])
    labels = db.read_account_labels({r.get("issuer") for r in rows if r.get("issuer")})

    status_count = {"live": 0, "prepared": 0, "test": 0}
    enriched = []
    for r in rows:
        copy = dict(r)
        copy["status"] = _classify_mpt_status(r)
        copy["normalized_outstanding"] = _normalize_outstanding(r)
        copy["holders"] = _normalize_holders(r.get("holders"))
        # Only set issuer_label from PG if the snapshot didn't already carry one.
        if not copy.get("issuer_name"):
            lbl = labels.get(copy.get("issuer"))
            if lbl and lbl.get("name"):
                copy["issuer_name"] = lbl["name"]
        status_count[copy["status"]] += 1
        enriched.append(copy)

    status_rank = {"live": 0, "prepared": 1, "test": 2}
    enriched.sort(key=lambda r: (status_rank.get(r["status"], 3), -r["normalized_outstanding"]))

    out = dict(data)
    out["issuances"] = enriched
    out["by_status"] = status_count
    return out


@app.route("/mpts")
def mpts():
    """MPT registry — every MPTokenIssuance on the ledger, with XLS-89
    metadata decoded and classified (RWA / Stablecoin / Utility / Other).

    Source order: local snapshot file (Mac), Postgres mirror (Render).
    NEVER falls through to an in-process walk inside a request handler:
    a full ledger walk takes hours and would blow gunicorn's 60s timeout,
    surfacing as a Render "server failure" alert (2026-05-12 incident).
    When both snapshots are missing, we render a warming-up placeholder
    so the page stays responsive — the Mac mpt_snapshot worker will fill
    PG on its next run and the page becomes real on the next refresh.

    Set MPT_ALLOW_LIVE_FETCH=1 to opt back in to the synchronous walk
    (local dev only — never on Render).

    ?outstanding_min=N is a presentational threshold for the "Has supply"
    long-tail filter chip. Default 0 hides only never-minted MPTs; power
    users (researchers, journalists) bump it for a tighter view."""
    data = load_mpt_snapshot()
    if data is None:
        data = _cached_db_mpt_snapshot()
    if data is None and os.environ.get("MPT_ALLOW_LIVE_FETCH") == "1":
        data = fetch_mpt_data_cached()
    if data is None:
        data = {
            "ok": False,
            "warming": True,
            "issuances": [],
            "total": 0,
            "by_class": {"rwa": 0, "stablecoin": 0, "utility": 0, "other": 0},
            "by_status": {"live": 0, "prepared": 0, "test": 0},
        }
    else:
        data = _enrich_mpt_rows(data)

    # Long-tail filter threshold. Default 0 = hide rows with no circulating
    # supply (the 77% prepared-but-never-minted tail). Negative or junk
    # input falls back to 0.
    try:
        outstanding_min = max(0.0, float(request.args.get("outstanding_min") or 0))
    except (TypeError, ValueError):
        outstanding_min = 0.0

    rows = data.get("issuances") or []
    unnamed_total = sum(1 for r in rows if not r.get("name"))
    # Hide predicate matches the chip's stated meaning:
    #   min == 0  → "Has supply"     → hide rows where outstanding == 0
    #   min  > 0  → "Outstanding ≥ N" → hide rows where outstanding < N
    # The split avoids the off-by-one where "≥ 100" hides a row at exactly 100.
    def _below_supply_floor(r):
        n = r.get("normalized_outstanding") or 0
        return n <= 0 if outstanding_min == 0 else n < outstanding_min

    low_supply_total = sum(1 for r in rows if _below_supply_floor(r))
    # Combined = |unnamed ∪ low_supply|, not the sum (some rows fail both).
    # Computed once here so the template can't accidentally render an
    # additive count that doesn't match reality.
    combined_hidden_total = sum(
        1 for r in rows
        if (not r.get("name")) or _below_supply_floor(r)
    )

    # Compose the chip label server-side. Whole numbers render without a
    # trailing .0 ("Outstanding ≥ 100", not "Outstanding ≥ 100.0").
    if outstanding_min > 0:
        if outstanding_min == int(outstanding_min):
            threshold_str = str(int(outstanding_min))
        else:
            threshold_str = f"{outstanding_min:g}"
        supply_chip_label = f"{babel_gettext('Outstanding')} ≥ {threshold_str}"
    else:
        supply_chip_label = babel_gettext("Has supply")

    # Most-held live MPT for the lede sentence. Only rows with a completed
    # holder walk contribute — warming/partial rows would inflate the count.
    top_holder_count = max(
        (
            (r.get("holders") or {}).get("with_balance") or 0
            for r in rows
            if r.get("status") == "live"
            and (r.get("holders") or {}).get("reason") in ("complete", "no_holders")
        ),
        default=0,
    )

    # Page thesis is "holders as a leading indicator," so default sort is
    # holders desc. ?sort=outstanding preserves the supply-ranked view that
    # the helper-default produced (procurement/journalism scrapers may want it).
    sort_mode = (request.args.get("sort") or "holders").lower()
    if sort_mode == "holders":
        status_rank = {"live": 0, "prepared": 1, "test": 2}
        rows.sort(key=lambda r: (
            status_rank.get(r.get("status"), 3),
            -((r.get("holders") or {}).get("with_balance") or 0),
            -(r.get("normalized_outstanding") or 0),
        ))

    # Walker health for the staleness banner — surfaces silent failures
    # of the launchd mpt_snapshot walker (caught 2026-05-27: 11 of last 13
    # runs failed silently while /mpts kept rendering last-good as fresh).
    walker_health = db.read_walker_health("mpt_snapshot")

    return render_template(
        "mpts.html",
        data=data,
        outstanding_min=outstanding_min,
        unnamed_total=unnamed_total,
        low_supply_total=low_supply_total,
        combined_hidden_total=combined_hidden_total,
        supply_chip_label=supply_chip_label,
        top_holder_count=top_holder_count,
        sort_mode=sort_mode,
        walker_health=walker_health,
    )


@app.route("/api/mpts")
@limiter.limit("60 per minute")
def api_mpts():
    """Public JSON mirror of /mpts — same rows, same status badges, same
    sort. Procurement teams, journalists, and downstream tools will scrape
    this so they don't have to parse the HTML. Documented field names are
    stable; adding new fields is safe, renaming or removing is not."""
    data = load_mpt_snapshot()
    if data is None:
        data = _cached_db_mpt_snapshot()
    if data is None:
        return ({"ok": False, "warming": True, "issuances": [], "total": 0}, 503,
                {"Cache-Control": "no-store"})
    enriched = _enrich_mpt_rows(data)
    payload = {
        "ok": True,
        "total": enriched.get("total"),
        "by_class": enriched.get("by_class"),
        "by_status": enriched.get("by_status"),
        "snapshot_age_seconds": enriched.get("snapshot_age_seconds"),
        "issuances": [
            {
                "issuance_id": r.get("issuance_id"),
                "name": r.get("name"),
                "ticker": r.get("ticker"),
                "issuer": r.get("issuer"),
                "issuer_name": r.get("issuer_name"),
                "classification": r.get("classification"),
                "asset_subclass": r.get("asset_subclass"),
                "outstanding_amount": r.get("outstanding_amount"),
                "asset_scale": r.get("asset_scale"),
                "holders": r.get("holders"),
                "flags": r.get("flags"),
                "status": r.get("status"),
                "detail_url": f"/mpt/{r.get('issuance_id')}" if r.get("issuance_id") else None,
            }
            for r in (enriched.get("issuances") or [])
        ],
    }
    return payload


@app.route("/mpt/<issuance_id>")
def mpt_detail(issuance_id):
    """Per-MPT detail page. Minimal at launch: name, ticker, asset_class,
    status badge, issuer + curated label, outstanding amount, peer MPTs
    from the same issuer. Holders / supply-over-time / mint-burn velocity
    arrive once the holders worker and history table land.

    Test-shaped MPTs render with a noindex meta so we don't pollute the
    sitemap with junk pages. Real and prepared MPTs are fully indexable —
    they're the institutional discovery surface."""
    data = load_mpt_snapshot()
    if data is None:
        data = _cached_db_mpt_snapshot()
    if data is None:
        abort(503)

    enriched = _enrich_mpt_rows(data)
    rows = enriched.get("issuances") or []
    # Match case-insensitively because XRPL hex IDs are sometimes lowercased
    # in URLs even though the canonical form is uppercase.
    target_id = issuance_id.upper()
    mpt = next((r for r in rows if (r.get("issuance_id") or "").upper() == target_id), None)
    if mpt is None:
        abort(404)

    peer_issuer = mpt.get("issuer")
    peers = [r for r in rows
             if r.get("issuer") == peer_issuer
             and (r.get("issuance_id") or "").upper() != target_id]

    # Decode MPTokenIssuance flags here — Jinja doesn't have bitwise ops,
    # so the template only renders the resulting (label, on) list.
    # Bits per XLS-33 / MPTokenIssuance ledger entry definitions.
    flags = int(mpt.get("flags") or 0)
    flag_decoded = [
        ("Locked", bool(flags & 0x01)),
        ("Can be locked", bool(flags & 0x02)),
        ("Requires authorization", bool(flags & 0x04)),
        ("Can be escrowed", bool(flags & 0x08)),
        ("Can be traded", bool(flags & 0x10)),
        ("Transferable", bool(flags & 0x20)),
        ("Clawback enabled", bool(flags & 0x40)),
    ]

    # Concentration is derived from the v3 holders.top array. We compute it
    # here (not in the worker) because it's a detail-page-only metric and
    # avoids a schema bump. Denominator is OutstandingAmount — per XRPL
    # semantics that equals the sum of all non-issuer balances, so we don't
    # need to sum the (capped-at-20) top array ourselves.
    concentration = None
    holders = mpt.get("holders") or {}
    if holders.get("reason") == "complete":
        outstanding = int(mpt.get("outstanding_amount") or 0)
        top = holders.get("top") or []
        positive = [int(h.get("mpt_amount") or 0) for h in top]
        positive = [a for a in positive if a > 0]
        if outstanding > 0 and positive:
            concentration = {
                "top1_pct": round(positive[0] / outstanding * 100, 1),
            }
            if len(positive) >= 3:
                concentration["top3_pct"] = round(sum(positive[:3]) / outstanding * 100, 1)

    # Supply history sparkline. Reader is a single indexed lookup (no
    # joins, oldest-first). We materialize as dicts so the template doesn't
    # have to index into tuples, and cast Decimal to int up-front so SVG
    # path arithmetic never sees scientific notation downstream. Dates are
    # pre-formatted UTC strings (the rest of the app does the same — no
    # custom jinja filter).
    raw_history = db.read_mpt_supply_history(mpt.get("issuance_id") or "")
    history = []
    for (ts, outstanding, with_balance, top1, top3) in raw_history:
        if outstanding is None:
            continue
        history.append({
            "snapshot_ts": ts,
            "outstanding": int(outstanding),
            "with_balance": with_balance,
            "top1_share": top1,
            "top3_share": top3,
            "date_label": datetime.fromtimestamp(int(ts), tz=timezone.utc)
                .strftime("%Y-%m-%d %H:%M UTC"),
        })

    # Archive-depth label for the supply-trend heading chip. Bucketed by
    # magnitude so early-stage MPTs read "11h" rather than misleading "0.46d".
    # max(0, ...) guards against a reversed-history schema regression.
    archive_depth_seconds = 0
    archive_depth_label = None
    if len(history) >= 2:
        archive_depth_seconds = max(0, int(history[-1]["snapshot_ts"] - history[0]["snapshot_ts"]))
        h = archive_depth_seconds / 3600
        if h < 1:
            archive_depth_label = f"{max(int(archive_depth_seconds / 60), 1)}m"
        elif h < 48:
            archive_depth_label = f"{int(h)}h"
        else:
            archive_depth_label = f"{int(h / 24)}d"

    resp = make_response(render_template(
        "mpt_detail.html",
        mpt=mpt,
        peers=peers,
        peer_count=len(peers),
        flag_decoded=flag_decoded,
        concentration=concentration,
        history=history,
        archive_depth_seconds=archive_depth_seconds,
        archive_depth_label=archive_depth_label,
    ))
    # Force revalidation on Safari — prevents post-deploy stale HTML from
    # pinning after template fixes (2026-08-18 sparkline-diet incident).
    # ETag is snapshot-derived (id + written_at), not body-hashed — the nav
    # liveness chip injects a per-request epoch that would otherwise churn
    # every render and defeat revalidation.
    resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    resp.set_etag(f"{target_id}-{data.get('written_at') or 0}", weak=True)
    return resp.make_conditional(request)


@app.route("/mpt/issuer/<address>")
def mpt_issuer(address):
    """Per-issuer MPT roll-up: every MPT this r-address has minted, with
    aggregated supply/holders/concentration. Same data path as /mpts and
    /mpt/<id> (snapshot file → PG mirror), no extra RPC.

    Always renders the full shell — even for issuers with a single MPT or
    zero MPTs — so the surface is consistent regardless of issuer scale.
    A 404 only fires for syntactically invalid addresses; a valid address
    with no MPTs renders an "no MPTs issued" stub.

    Indexable when at least one live (non-test, non-prepared) issuance
    exists. Issuers with only test-shaped MPTs get noindex so test fixtures
    don't compete with real institutional pages in search."""
    if not _is_xrpl_address(address):
        abort(404)

    data = load_mpt_snapshot()
    if data is None:
        data = _cached_db_mpt_snapshot()
    if data is None:
        abort(503)

    enriched = _enrich_mpt_rows(data)
    all_rows = enriched.get("issuances") or []
    rows = [r for r in all_rows if r.get("issuer") == address]

    # Curated label takes priority; fall back to any per-MPT issuer_name
    # the metadata set; final fallback is the truncated hex (rendered in
    # the template). Matches the priority used by /mpts and /mpt/<id>.
    pg_label = db.read_account_label(address)
    header_label = None
    if pg_label and pg_label.get("name"):
        header_label = pg_label["name"]
    else:
        for r in rows:
            if r.get("issuer_name"):
                header_label = r["issuer_name"]
                break

    # Hero stats. total_holders sums only rows whose walk completed cleanly
    # — a row with reason=incomplete has with_balance=None and can't be
    # summed honestly. We surface that gap via `holders_walk_clean_count`.
    total_holders = 0
    walks_clean = 0
    for r in rows:
        h = r.get("holders") or {}
        if h.get("reason") in ("complete", "no_holders") and h.get("with_balance") is not None:
            total_holders += int(h["with_balance"])
            walks_clean += 1

    # Combined outstanding only meaningful when all issuances share an
    # asset_scale. Mixed-scale issuers (e.g. one with scale=6, one with
    # scale=2) make a numeric sum lie about magnitude. In that case we
    # surface "mixed scales" instead of an arithmetic answer.
    scales = {int(r.get("asset_scale") or 0) for r in rows}
    if len(scales) <= 1 and rows:
        scale = next(iter(scales))
        combined_outstanding = sum(int(r.get("outstanding_amount") or 0) for r in rows)
        combined_display = (combined_outstanding / (10 ** scale)) if scale else combined_outstanding
        combined = {"value": combined_display, "scale": scale, "mixed": False}
    else:
        combined = {"value": None, "scale": None, "mixed": len(scales) > 1}

    # Classification + status breakdowns. Counter is fine but we sort by
    # count desc for a stable display order.
    from collections import Counter
    class_counts = Counter(r.get("classification") for r in rows)
    status_counts = Counter(r.get("status") for r in rows)
    class_breakdown = sorted(class_counts.items(), key=lambda kv: -kv[1])
    status_breakdown = sorted(status_counts.items(), key=lambda kv: -kv[1])

    # Indexability: any non-test row → indexable. "prepared" means real
    # on-ledger metadata with no holders yet (institutional staging, not
    # test fixture), so prepared issuers belong in the index. Only
    # all-test issuers + zero-MPT issuers are noindex'd.
    has_indexable = any(r.get("status") != "test" for r in rows)

    last_refresh_ts = (data.get("last_holders_refresh_at")
                       or data.get("written_at"))
    last_refresh_age_seconds = None
    if last_refresh_ts:
        last_refresh_age_seconds = max(0, int(time.time()) - int(last_refresh_ts))

    resp = make_response(render_template(
        "mpt_issuer.html",
        address=address,
        header_label=header_label,
        pg_label=pg_label,
        rows=rows,
        total_mpts=len(rows),
        total_holders=total_holders,
        walks_clean=walks_clean,
        combined=combined,
        class_breakdown=class_breakdown,
        status_breakdown=status_breakdown,
        has_indexable=has_indexable,
        last_refresh_ts=last_refresh_ts,
        last_refresh_age_seconds=last_refresh_age_seconds,
    ))
    # Force revalidation on Safari — prevents post-deploy stale HTML from
    # pinning after template fixes (2026-08-18 sparkline-diet incident).
    # ETag is snapshot-derived (address + last_refresh_ts), not body-hashed —
    # the nav liveness chip injects a per-request epoch that would otherwise
    # churn every render and defeat revalidation.
    resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    resp.set_etag(f"{address}-{last_refresh_ts or 0}", weak=True)
    return resp.make_conditional(request)


@app.route("/api/ledger-tip")
@limiter.limit("60 per minute")
def api_ledger_tip():
    """Lightweight JSON endpoint that the liveness chip in _nav.html polls
    every 30s. Same data the context_processor injects, but without a full
    page render."""
    p = fetch_pulse_cached()
    if not p or p.get("error"):
        return {"error": "unavailable"}, 503
    return {
        "ledger_index": p.get("ledger_index"),
        "last_close_age_seconds": max(0, p.get("last_close_age_seconds") or 0),
        "status": p.get("status"),
        "status_text": p.get("status_text"),
        "load_factor": p.get("load_factor"),
        "avg_close_seconds": p.get("avg_close_seconds"),
    }


@app.route("/api/heartbeat-age")
@limiter.limit("120 per minute")
def api_heartbeat_age():
    """Public liveness probe for the XRPL stream worker. Returns 200 only
    when the heartbeat row is fresh AND Postgres is reachable. Every
    failure mode (stale, missing, DB unreachable, env not set) returns 503
    so external monitors treat any break in the alarm chain as an outage —
    a silent 200 would mean we lost the ability to detect failure."""
    STALE_SECONDS = 600
    headers = {"Cache-Control": "no-store"}

    if not db.pg_available():
        return ({"status": "config_error",
                 "detail": "DATABASE_URL not set"}, 503, headers)

    try:
        with db.pg_connect() as conn:
            with conn.cursor() as cur:
                # Host-tagged keys ('xrpl_stream:mac', future ':render') —
                # endpoint stays green if any worker is fresh.
                cur.execute(
                    "SELECT ts FROM worker_heartbeat "
                    "WHERE worker LIKE 'xrpl_stream%' "
                    "ORDER BY ts DESC LIMIT 1"
                )
                row = cur.fetchone()
    except Exception as e:
        return ({"status": "db_error",
                 "detail": type(e).__name__}, 503, headers)

    if not row:
        return ({"status": "no_heartbeat"}, 503, headers)

    age = int(time.time()) - int(row[0])
    if age < 0:
        age = 0
    if age > STALE_SECONDS:
        return ({"status": "stale",
                 "age_seconds": age,
                 "threshold_seconds": STALE_SECONDS}, 503, headers)

    return ({"status": "ok",
             "age_seconds": age,
             "threshold_seconds": STALE_SECONDS}, 200, headers)


@app.route("/api/xrp-distribution")
@limiter.limit("60 per minute")
def api_xrp_distribution():
    """Public JSON of the three XRP supply buckets (escrowed / AMM /
    circulating) with per-bucket age_seconds and is_stale flags.
    Currently has NO in-app UI consumer — the homepage reservoir
    gauge that consumed this was reverted (8c2d8b9 → HEAD) after a
    visual review. Endpoint retained deliberately: cheap (reads the
    same in-process caches the homepage already uses, no walker is
    triggered by a poll), well-scoped, and a natural fit for any
    future viz or third-party dashboard that wants a small honest
    supply-distribution feed. Delete if it's still unused after the
    next content pass."""
    try:
        ranked_full, _meta = _ranked_amm_snapshot()
    except Exception:
        ranked_full = []
    payload = _build_xrp_distribution(ranked_full)
    return (payload, 200, {"Cache-Control": "no-store"})


@app.route("/api/xrp-price")
@limiter.limit("120 per minute")
def api_xrp_price():
    """Live XRP/USD price from the on-chain XRP/RLUSD AMM (xrplcluster.com).
    Single disclosed source — RLUSD only, not a median. 20s server cache."""
    p = fetch_xrp_price_cached()
    if p.get("error"):
        return {"error": p["error"]}, 503
    return (
        {
            "price": p["price"],
            "source": p["source"],
            "xrp_reserves": p.get("xrp_reserves"),
            "rlusd_reserves": p.get("rlusd_reserves"),
            "cached_age_seconds": p["cached_age_seconds"],
        },
        200,
        {"Cache-Control": "no-store"},
    )


@app.route("/api/xrp-usd")
@limiter.limit("60 per minute")
def api_xrp_usd():
    """USD-per-XRP, derived from the median of multiple XRP/stablecoin AMMs
    on the XRP Ledger itself. No external price APIs — the only source of
    truth is the ledger.

    Sources (each surfaces in `sources` so the methodology page and any
    consumer can audit which pools fed the median):
      • RLUSD (Ripple's USD stablecoin)
      • USD.GH (GateHub)
      • USD.Bitstamp (Bitstamp)
    Cached ~60s server-side."""
    from price_oracle import xrp_usd, xrp_usd_sources, PRICE_TTL
    rate = xrp_usd()
    if rate is None:
        return {"error": "no anchor pools resolved"}, 503
    return {
        "xrp_usd": round(rate, 6),
        "sources": [
            {"label": label, "xrp_usd": round(usd, 6)}
            for (label, usd) in xrp_usd_sources()
        ],
        "ttl_seconds": PRICE_TTL,
    }


@app.route("/api/pools/recent_events")
@limiter.limit("60 per minute")
def api_pools_recent_events():
    """Recent per-pool AMM activity (deposit/withdraw/swap), polled by the
    constellation on /pools to fire real comets at the matching star.

    Reads the worker-written amm_pool_events ring buffer in events.db. In
    production (no worker) this returns an empty list, so the constellation
    stays in standby — honest by construction."""
    try:
        seconds = int(request.args.get("seconds", "10"))
    except (TypeError, ValueError):
        seconds = 10
    seconds = max(1, min(seconds, 60))
    cutoff = int(time.time()) - seconds

    events = None

    # Prefer Postgres so /pools comets fire in prod where the worker is
    # remote; fall back to the local SQLite ring buffer otherwise.
    if db.pg_available():
        try:
            events = db.read_recent_amm_pool_events(seconds)
        except Exception:
            events = None

    if events is None and os.path.exists(EVENTS_DB_PATH):
        try:
            conn = sqlite3.connect(f"file:{EVENTS_DB_PATH}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT id, ts, amm_account, event_type, magnitude_xrp_drops "
                    "FROM amm_pool_events WHERE ts >= ? "
                    "ORDER BY id ASC LIMIT 200",
                    (cutoff,),
                ).fetchall()
                events = [
                    {"id": r[0], "ts": r[1], "amm_account": r[2],
                     "event_type": r[3], "magnitude_xrp_drops": r[4]}
                    for r in rows
                ]
            finally:
                conn.close()
        except sqlite3.OperationalError:
            events = []  # table not yet created
        except Exception:
            events = []

    if events is None:
        events = []

    return {"now": int(time.time()), "events": events}


@app.route("/api/whales/recent")
@limiter.limit("60 per minute")
def api_whales_recent():
    """Recent whale events as JSON. Polled by the homepage globe so each
    pulse is timed by a real on-ledger event (coordinates remain symbolic;
    see /methodology). Same source as the /whales feed and the mosaic card.

    Returns {"now": <unix>, "events": [...]} — empty list when the source
    is unavailable, so the consumer can fall silent rather than fake."""
    try:
        limit = int(request.args.get("limit", "10"))
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 50))

    events = _recent_whale_events(limit=limit)
    return (
        {"now": int(time.time()), "events": events},
        200,
        {"Cache-Control": "no-store"},
    )


@app.route("/api/whales/radar-stats")
@limiter.limit("120 per minute")
def api_whales_radar_stats():
    """Live 24h whale count and last whale amount for the /whales HUD.
    Polled every 60s by the client to keep the radar stats current."""
    radar_floor_drops = WHALE_XRP_THRESHOLD * 1_000_000
    stats = {"last_24h": 0, "last_amount_drops": None}
    if db.pg_available():
        try:
            stats = db.read_whale_radar_stats(radar_floor_drops)
        except Exception:
            pass
    last_drops = stats.get("last_amount_drops")
    if last_drops:
        xrp = last_drops / 1_000_000.0
        if xrp >= 1_000_000:
            last_label = f"{xrp / 1_000_000:.1f}M XRP"
        elif xrp >= 1_000:
            last_label = f"{xrp / 1_000:.0f}K XRP"
        else:
            last_label = f"{xrp:,.0f} XRP"
    else:
        last_label = None
    return (
        {"last_24h": stats["last_24h"], "last_label": last_label},
        200,
        {"Cache-Control": "no-store"},
    )


# ─────────────────────────────────────────────────────────────────────
# Phase 1b Coverage Register — internal-first surface.
#
# Charlie call at Gate 1: internal-first for one week of clean three-way
# diff before flipping to public /coverage. The register is the brand's
# boldest honesty claim; it should demonstrably not lie before it gets a
# public URL.
#
# Auth: HTTP Basic against INTERNAL_ADMIN_USER + INTERNAL_ADMIN_PASS.
# NOT a query-param secret — those leak into access logs, referrer
# headers, and pasted URLs, which is exactly the wrong look on the
# honesty page. When creds are unset OR wrong, return 404 (not 401) so
# the endpoint's existence isn't confirmed to unauthenticated pokes.
#
# Publishability: unknown-type samples show field NAMES only, never
# values. Memo/URI/Blob/Signature/TxnSignature explicitly excluded by
# name allowlist. Fails safe on novelty (unrecognized field name with
# unsafe-suffix pattern → blocked by name too).
# ─────────────────────────────────────────────────────────────────────

# Field names blocked from even name-level display for unknown-type
# rendering. Memos/URIs/Blobs can carry arbitrary user payload; even the
# field name is defensible to omit since the same content is available
# via public explorers if a genuine forensic need arises.
_UNKNOWN_TYPE_UNSAFE_FIELDS = frozenset({
    "Memos", "Memo", "URI", "Blob",
    "Signature", "TxnSignature", "Signers", "SigningPubKey",
})
_UNKNOWN_TYPE_UNSAFE_SUFFIXES = ("Memo", "URI", "Blob", "Signature", "Sig")


def _publishable_field_names(names):
    """Return the sanitized field-name list for an unknown-type sample.
    Blocks the explicit unsafe set and anything matching an unsafe
    suffix pattern (defense against a novel field name we haven't seen)."""
    safe = []
    for n in names or []:
        if n in _UNKNOWN_TYPE_UNSAFE_FIELDS:
            continue
        if any(n.endswith(suf) for suf in _UNKNOWN_TYPE_UNSAFE_SUFFIXES):
            continue
        safe.append(n)
    return sorted(safe)


def _internal_admin_ok():
    """HTTP Basic auth gate for /internal/*. Env-driven; when either var
    is unset the route acts as if it doesn't exist (returns 404 to the
    caller).

    Both hmac.compare_digest calls are evaluated unconditionally, then
    AND'd — Python's `and` short-circuits, so `check_user and check_pw`
    would run only ONE compare on wrong-user (fast) and TWO on
    valid-user+wrong-pass (slow), leaking username validity. Assigning
    both results first forces both calls every request.

    Note: hmac.compare_digest still leaks length via early exit on
    unequal-length inputs; mitigating that would require hashing both
    sides to a fixed width. For an env-configured credential shipped in
    Render's dashboard, treating the username length as semi-public is
    acceptable — the timing-parity fix here is what closes the
    actionable side-channel."""
    user = os.environ.get("INTERNAL_ADMIN_USER", "").strip()
    pw = os.environ.get("INTERNAL_ADMIN_PASS", "").strip()
    if not user or not pw:
        return False
    auth = request.authorization
    if not auth or not auth.username or not auth.password:
        return False
    user_ok = hmac.compare_digest(auth.username, user)
    pw_ok = hmac.compare_digest(auth.password, pw)
    return user_ok and pw_ok


@app.route("/coverage")
def coverage():
    """Coverage Register — three-way diff between the vocabulary the
    local rippled node advertises (Phase 0) and the types the live stream
    has actually observed (Phase 1a), cross-referenced against the
    curated coverage_labels and the walker_scope_declarations escrow-
    lesson inventory.

    Row states rendered:
      • defined-but-unseen — grey; awaiting first-ever XRPL sighting
      • seen-and-labeled — green; live count + last_seen + linked_page
      • seen-and-unlabeled — amber; curation debt (pressure to keep
        labels current)
      • seen-but-undefined — RED, persistent; the 1a SCREAM state
        finally gets its durable surface here
      • walker STALE — struck-through if walker_health.last_success_at
        is >2× cadence_seconds ago
      • walker UNDECLARED — RED; any walker in walker_health missing a
        walker_scope_declarations row (structural version of the escrow
        lesson: undeclared filter is undeclared coverage)

    Freshness: every row footprints its provenance chain and greys out
    when any hop is stale.

    Flipped public 2026-07-15 after 7d soak (all 5 evidence checks green).
    """
    state = db.read_coverage_register_state()
    if state is None:
        # Honest degrade — never claim awareness the sensors aren't
        # currently delivering.
        return render_template(
            "coverage_register.html",
            state=None,
            unavailable_reason=(
                "Postgres unreachable or Phase 0 singleton missing. "
                "Register cannot be computed. Check "
                "ledger_definitions_walker health first."
            ),
        )

    # Compose the render packet. Row-level provenance so the template
    # doesn't have to compute freshness inline.
    now = int(time.time())

    def _stale_for(walker_name):
        wh = state["walker_health"].get(walker_name)
        if wh is None:
            return {"stale": True, "reason": "no walker_health row",
                    "undeclared": walker_name not in state["walker_scopes"]}
        return {
            "stale": wh["is_stale"],
            "reason": None if not wh["is_stale"] else "past 2× cadence",
            "undeclared": wh["undeclared"],
        }

    # Enrich unknown-type rows (SCREAM state) with sanitized field-name
    # samples pulled from the seen_entry / seen_tx rows. First revision:
    # no per-object sample payload yet — Phase 1a doesn't archive
    # examples. When it does (backlog), the same _publishable_field_names
    # rule applies here.
    return render_template(
        "coverage_register.html",
        state=state,
        now=now,
        stale_for=_stale_for,
        publishable_field_names=_publishable_field_names,
        unavailable_reason=None,
    )


@app.route("/internal/coverage")
def internal_coverage_redirect():
    """Legacy internal path — kept as a 302 to preserve link continuity
    for anyone still pointing at the pre-flip URL."""
    return redirect(url_for("coverage"), code=302)


@app.route("/healthz")
@limiter.limit("120 per minute")
def healthz():
    """Render routing probe — PG-reachability only.

    Fires every 10s via render.yaml healthCheckPath. Returns 503 iff we
    cannot reach Postgres, since without PG the app cannot serve any
    dynamic page. Deliberately does NOT check walker freshness / stream
    liveness / mirror liveness: those are data-quality signals for
    /api/health and BetterStack, not routing signals. The 2026-08-07
    outage (project_healthz_outage_2026-08-07.md) proved that gating
    routing on walker liveness lets one Mac walker's silence take the
    whole public site down — the wrong tradeoff for users.

    Rate-limit: 120/min per client IP. Render's probe fires at 6/min
    (comfortable headroom) and comes from an internal source distinct
    from public CF-Connecting-IP buckets. Public curl-loop abusers get
    their own bucket and hit the limit at 2/sec sustained — probe stays
    green while attack surface shrinks."""
    try:
        db.ping()
        return {"status": "ok", "db": "reachable"}, 200
    except Exception as e:
        return {"status": "unhealthy", "db": "unreachable", "error": str(e)[:120]}, 503


@app.route("/api/health")
def api_health():
    """Rich degrade JSON — per-check freshness booleans + 503 on degrade.

    External monitors (BetterStack, uptime checkers) should poll THIS,
    not /healthz. Returns 503 when any of scan/stream/mirror is stale,
    matching the pre-2026-08-07 /healthz semantics — but on this
    endpoint the 503 is a data-freshness signal, not a routing signal."""
    state = _health_degrade_state()
    body = {
        "status": state["overall"],
        "checks": {
            "scan": state["scan_alive"],
            "stream": state["stream_alive"],
            "mirror": state["mirror_alive"],
        },
    }
    return body, state["status_code"]


@app.route("/robots.txt")
@limiter.limit(agent_tier_limit_rate)
def robots_txt():
    """Tell crawlers what's indexable. Allow the public surface, exclude
    operational endpoints and the unbounded detail-page space (every
    valid wallet address would otherwise be a crawlable URL).

    /mpt/<id> is allow-listed: the universe of MPT issuance IDs is finite
    and curated, and indexing them is the point — issuers Google their
    own MPT name and find themselves here."""
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /mpt/\n"
        "Allow: /coverage\n"
        "Disallow: /healthz\n"
        "Disallow: /api/\n"
        "Disallow: /lookup\n"
        "Disallow: /v2\n"
        "Disallow: /wallet/\n"
        "Disallow: /token/\n"
        f"\nSitemap: {SITE_URL}/sitemap.xml\n"
    )
    return Response(body, mimetype="text/plain")


@app.route("/.well-known/security.txt")
@limiter.limit(agent_tier_limit_rate)
def security_txt():
    body = (
        "Contact: mailto:contact@xrpldashboard.com\n"
        "Expires: 2027-05-12T00:00:00.000Z\n"
        "Preferred-Languages: en\n"
        f"Canonical: {SITE_URL}/.well-known/security.txt\n"
    )
    return Response(body, mimetype="text/plain")


# Machine-readable site directory for AI crawlers and agents, following
# the llmstxt.org convention (markdown, root-served). Title-is-contract
# with extra teeth: every URL listed here is a promise the file is
# executed literally by a machine reader. If a route is renamed or
# removed, this string moves in the same commit. Freshness stamp is
# interpolated from LAST_VERIFIED_AGENT_TIER_METHODOLOGY — one constant
# feeds three surfaces (llms.txt, agents.json, /methodology "For AI
# agents"). Bump the constant, all three refresh.
_LLMS_TXT = f"""# xrpldashboard

> Public read-only data for the XRP Ledger, computed directly from XRPL and Ethereum nodes. Every page discloses its data source, cache TTL, and known limitations. No third-party analytics APIs feed any metric — price, volume, TVL, balances are all computed from on-chain state. Free for humans and identified crawlers.

Independent project — not affiliated with Ripple, the XRP Ledger Foundation, any exchange, or with xrpdashboard.com (note: missing 'L' — that's a separate XRP portfolio product).

Every public claim is catalogued in [CLAIMS.yaml](https://github.com/Enkryptedx/xrpldashboard/blob/main/CLAIMS.yaml) (Layer 4 of the four-layer truth audit — see [/methodology]({SITE_URL}/methodology) and [docs/TRUTH_AUDIT_DESIGN.md](https://github.com/Enkryptedx/xrpldashboard/blob/main/docs/TRUTH_AUDIT_DESIGN.md)). Each claim carries a permanent URI at [/claims/xrpl.<domain>.<series>]({SITE_URL}/claims) with a traffic-light sovereignty tier (green = own infrastructure / signable, yellow = public XRPL RPC or unverified, red = third-party derived). Machine-readable index: [{SITE_URL}/claims/index.json]({SITE_URL}/claims/index.json). Signed integrity snapshots are published daily.

## Data pages
- [/rlusd]({SITE_URL}/rlusd): RLUSD supply history — cross-chain supply, mint/burn events (XRPL + Ethereum), computed live from both ledgers.
- [/whales]({SITE_URL}/whales): XRPL whale transfers live — every XRP payment above 100,000 XRP, streamed as it validates.
- [/rwa]({SITE_URL}/rwa): real-world-asset tokens on XRPL with issuer attestation.
- [/tokens]({SITE_URL}/tokens): verified XRPL token supply — full token registry with domain-attested labels and on-ledger activity.
- [/mpts]({SITE_URL}/mpts): MPT (Multi-Purpose Token) registry and issuer roll-ups.
- [/nfts]({SITE_URL}/nfts): XLS-20 NFT activity on XRPL — mints, burns, offers, and sales, with per-source labels (live: own rippled; historical backfill: Ripple's public Clio archive, disclosed and free-tier only under SELLABLE_REQUIRES_SOVEREIGN_SOURCE).
- [/pools]({SITE_URL}/pools): AMM pools ranked by TVL and volume.
- [/amendments]({SITE_URL}/amendments): current XRPL amendment status — enabled, voting, and vetoed amendments with validator support tallies.
- [/analytics]({SITE_URL}/analytics): first-party page-view analytics, bot-filtered.
- [/coverage]({SITE_URL}/coverage): what this site covers versus the XRPL's canonical object-type inventory.
- [/lending]({SITE_URL}/lending): LendingProtocol amendment status.
- [/regulation]({SITE_URL}/regulation): plain-English CLARITY Act (H.R. 3633) status tracker.
- [/check]({SITE_URL}/check): typed triage for XRPL addresses, tokens, URLs, and pasted messages. Address/token inputs return OFAC SDN + identity + on-chain signals; URL inputs return domain-age + earliest-SSL-cert; message inputs extract and triage every subject inside. Facts-not-verdicts — every signal carries source + timestamp.
- [/cold-storage]({SITE_URL}/cold-storage): known cold-wallet balances.

## How this is computed
- [/methodology]({SITE_URL}/methodology): per-surface freshness contracts, cache TTLs, data sources, known limitations. See especially the "For AI agents" section.
- [/glossary]({SITE_URL}/glossary): plain-English definitions for XRPL terms and xrpldashboard methodology concepts (AMM, amendment, trust line, signed snapshot, sovereignty tier, and more).
- [/about]({SITE_URL}/about): mission, funding, principles.
- [/health]({SITE_URL}/health): live infrastructure status endpoint.
- [/terms]({SITE_URL}/terms): terms of use.
- [/privacy]({SITE_URL}/privacy): privacy policy.

## Integrity and verification
- Signed snapshot chain: [{SITE_URL}/.well-known/snapshots/chain.json]({SITE_URL}/.well-known/snapshots/chain.json) — daily Ed25519-signed database snapshots, chain-linked.
- Snapshot public key: [{SITE_URL}/.well-known/snapshots/pubkey.pem]({SITE_URL}/.well-known/snapshots/pubkey.pem) — pin this for verification.
- On-ledger anchor of the snapshot chain (since 2026-08-07): each daily `chain_root` is additionally committed inside an XRPL Payment memo from anchor account `rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ` to ops account `rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd`. First anchor tx: `01D0BB9D230955F43DB35703E2EB7F5DFA43CEB69CCBBF57FBC8F17407E50DF8` at ledger 106140698 (2026-08-07 21:49:32 UTC). Cadence is weekly, manual today; language upgrades when automation lands. Memo format v1 (namespace-in-MemoData) and verifier rules (including the `.strip()` rule for wallet-appended newlines) documented at [{SITE_URL}/methodology#signed-snapshots-xrpl-anchor]({SITE_URL}/methodology#signed-snapshots-xrpl-anchor).
- Public claims manifest: [{SITE_URL}/claims]({SITE_URL}/claims) — every claim on the site has a permanent URI + traffic-light sovereignty tier; content-negotiated JSON via `Accept: application/json` or `.json` suffix on the URI.
- Copy-pasteable client snippets (curl, Python, JavaScript for fetching a claim envelope + a Python end-to-end verifier for the daily signed snapshots): [{SITE_URL}/claims#use-this-data]({SITE_URL}/claims#use-this-data). Every snippet on that page was executed against live prod before shipping.
- Security contact: [{SITE_URL}/.well-known/security.txt]({SITE_URL}/.well-known/security.txt).
- Source code: [github.com/Enkryptedx/xrpldashboard](https://github.com/Enkryptedx/xrpldashboard) (MIT-licensed Flask app).

## For agent authors
- Agent identification, rate limits, and preferred crawl behavior: [{SITE_URL}/.well-known/agents.json]({SITE_URL}/.well-known/agents.json).
- OpenAPI spec (machine-readable index of the LIVE free surface + envelope schema + MCP tool inventory): [{SITE_URL}/openapi.json]({SITE_URL}/openapi.json). Swagger UI: [{SITE_URL}/docs]({SITE_URL}/docs).
- Freshness contract for this file and the agent-tier surfaces (llms.txt, agents.json, openapi.json, /methodology#for-ai-agents): last verified {LAST_VERIFIED_AGENT_TIER_METHODOLOGY}. Bumped whenever the agent-tier surface changes.
- MCP server (public beta through 2026-09): `https://mcp.xrpldashboard.com/mcp` — streamable-http transport, MCP protocol version 2025-06-18, no auth. Backed by our own rippled node on the Lenovo box; source at `mcp_server.py` + `mcp_tools_*.py` in the repo. Tool inventory is machine-readable at `info.x-mcp-tools` in the OpenAPI spec above. Session rate limit: 600 tool calls/hour/session, enforced live (see `mcp_session_rate_limit.py`; 429 with Retry-After on breach). No payment rails; free for identified agents at reasonable volume.
- Connect an MCP client in 60 seconds — copy-paste config for Claude Desktop or the mcp-remote bridge, plus three sample prompts (primitive / aggregation / verify-signed-snapshot): [{SITE_URL}/connect#connect-in-60-seconds]({SITE_URL}/connect#connect-in-60-seconds). Dogfooded against the public URL on 2026-08-05 before publishing.
- Every response from the MCP server is wrapped in a proof-annotation envelope. Shape: `{{data, proof:{{source, as_of, freshness_contract, methodology_url, claims_ref?, cross_check_status, honest_partial, scope_note?}}, server:{{name, version, public_key_fingerprint, docs}}}}` — verify locally against the signed snapshot chain rather than trusting the score. Full JSON schema at `#/components/schemas/ProofAnnotationEnvelope` in the OpenAPI spec. The read-only HTTP API will emit the same envelope when it ships.
- Directory listings for this MCP server (same endpoint + tool inventory as above; the directories are discovery aids, not different endpoints):
  - Anthropic MCP Registry: [registry.modelcontextprotocol.io/v0/servers?search=xrpldashboard](https://registry.modelcontextprotocol.io/v0/servers?search=xrpldashboard) — server id `com.xrpldashboard/xrpldashboard-mcp`, DNS-verified namespace, listed 2026-08-05.
  - Smithery: [smithery.ai/servers/xrpldashboard/xrpldashboard](https://smithery.ai/servers/xrpldashboard/xrpldashboard) — Smithery gateway URL `https://xrpldashboard--xrpldashboard.run.tools`, listed 2026-08-05.
"""


@app.route("/llms.txt")
@limiter.limit(agent_tier_limit_rate)
def llms_txt():
    """Machine-readable site directory for AI crawlers and agents,
    following the llmstxt.org convention. Every URL listed resolves to
    a live public surface — this is a title-is-contract file with extra
    teeth (first artifact written primarily for machine readers)."""
    resp = Response(_LLMS_TXT, mimetype="text/plain")
    resp.headers["Cache-Control"] = "public, max-age=3600, s-maxage=3600"
    return resp


# Agent-discovery manifest (Wildcard-AI flavor). Served at the
# .well-known standard path. Declares site identity, rate limits,
# trust surfaces, and the proof-annotation envelope agents should
# expect. Status block is deliberately honest — each boolean flips
# to true only after the corresponding surface actually responds:
# openapi_ready=True (spec live at /openapi.json); mcp_ready=True
# (public daemon live at mcp.xrpldashboard.com/mcp since 2026-08-05,
# streamable HTTP protocol 2025-06-18, 15 read-only tools, no auth);
# flows_ready stays False until Wildcard-AI flows land. See
# docs/AGENT_TIER_DESIGN.md.
_AGENTS_JSON = {
    "name": "xrpldashboard",
    "description": (
        "Public read-only data for the XRP Ledger, computed directly from "
        "XRPL and Ethereum nodes. Every response is proof-annotated with "
        "source, freshness stamp, and CLAIMS reference. Free for humans "
        "and identified agents."
    ),
    "disambiguation": (
        "Independent project — not affiliated with Ripple, the XRP Ledger "
        "Foundation, any exchange, or with xrpdashboard.com (note: missing "
        "'L' — that's a separate XRP portfolio product)."
    ),
    "site_url": SITE_URL,
    "documentation": f"{SITE_URL}/methodology#for-ai-agents",
    "contact": "contact@xrpldashboard.com",
    "source_code": "https://github.com/Enkryptedx/xrpldashboard",
    "license": "MIT (source); data derived from public XRPL and Ethereum ledgers",
    "last_verified": LAST_VERIFIED_AGENT_TIER_METHODOLOGY,
    "policies": {
        "auth": "none",
        "cost": "free at v1 (no accounts, no API keys, no payment rails)",
        "retention": "no query retention",
        "backoff": "429 with Retry-After header; no silent throttling",
    },
    "rate_limits": {
        "anonymous": "60 requests/minute/IP",
        "identified_ai_crawler": (
            "300 requests/minute (by UA: GPTBot, ClaudeBot, PerplexityBot, "
            "Google-Extended, and others on Cloudflare's verified-bot list)"
        ),
        "mcp_session": "600 tool calls/hour/session, enforced live at https://mcp.xrpldashboard.com/mcp (see mcp_session_rate_limit.py; 429 with Retry-After on breach, walker_health surfaces block frequency)",
        "signed_snapshot_verify": "unlimited (stateless, cryptographic-only)",
    },
    "trust_surfaces": {
        "methodology": f"{SITE_URL}/methodology",
        "claims_manifest_repo": "https://github.com/Enkryptedx/xrpldashboard/blob/main/CLAIMS.yaml",
        "claims_index": f"{SITE_URL}/claims",
        "claims_index_json": f"{SITE_URL}/claims/index.json",
        "claims_uri_scheme": "/claims/xrpl.<domain>.<series> — permanent, additive-only. Fetch any URI with Accept: application/json (or append .json) for status JSON.",
        "signed_snapshot_chain": f"{SITE_URL}/.well-known/snapshots/chain.json",
        "signed_snapshot_pubkey": f"{SITE_URL}/.well-known/snapshots/pubkey.pem",
        "signed_snapshot_xrpl_anchor": {
            "anchor_account": "rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ",
            "ops_account": "rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd",
            "first_anchor_tx": "01D0BB9D230955F43DB35703E2EB7F5DFA43CEB69CCBBF57FBC8F17407E50DF8",
            "first_anchor_ledger": 106140698,
            "first_anchor_close_time_utc": "2026-08-07T21:49:32Z",
            "memo_format_v1": "xrpldashboard/anchor/v1|<ISO date>|<chain_root_hex>",
            "cadence": "weekly, manual (2026-08 through automation cutover)",
            "spec_url": f"{SITE_URL}/methodology#signed-snapshots-xrpl-anchor",
            "note": "Each daily chain_root is additionally committed inside an XRPL Payment memo from anchor→ops. Verifiers must .strip() MemoData before splitting/comparing (wallet UIs may append trailing newlines).",
        },
        "security_contact": f"{SITE_URL}/.well-known/security.txt",
        "llms_txt": f"{SITE_URL}/llms.txt",
    },
    "receipts_envelope": {
        "shape": {
            "source": "string — data source identifier (e.g., 'local_rippled', 'neon_postgres', 'ethereum_1rpc')",
            "as_of": "ISO 8601 timestamp",
            "methodology_url": "string — deep link to the /methodology section describing this source",
            "claims_ref": "string? — CLAIMS.yaml claim id where one exists",
            "snapshot_signature": "string? — Ed25519 signature reference where the datum is snapshot-derived",
        },
        "spec_url": f"{SITE_URL}/methodology#for-ai-agents",
        "note": (
            "Envelope is normative for the MCP server (public at "
            "https://mcp.xrpldashboard.com/mcp — see mcp_servers below — "
            "backed by our own rippled node on the Lenovo box) and "
            "applies to the read-only HTTP API when it ships. HTML "
            "surfaces expose the same source metadata inline via "
            "per-page methodology chips and the /methodology page."
        ),
    },
    "mcp_servers": [
        {
            "url": "https://mcp.xrpldashboard.com/mcp",
            "transport": "streamable-http",
            "protocol_version": "2025-06-18",
            "auth": "none",
            "tool_count": len(AGENT_TIER_MCP_INVENTORY),
            "tool_inventory_url": f"{SITE_URL}/openapi.json",  # tools listed at info.x-mcp-tools
            "session_rate_limit": "600 tool calls/hour/session (enforced)",
            "connect_docs": f"{SITE_URL}/connect#connect-in-60-seconds",
        },
    ],
    "discovery_backlinks": [
        {
            "registry": "anthropic_mcp_registry",
            "listing_url": "https://registry.modelcontextprotocol.io/v0/servers?search=xrpldashboard",
            "server_id": "com.xrpldashboard/xrpldashboard-mcp",
            "listed_at": "2026-08-05",
        },
        {
            "registry": "smithery",
            "listing_url": "https://smithery.ai/servers/xrpldashboard/xrpldashboard",
            "gateway_url": "https://xrpldashboard--xrpldashboard.run.tools",
            "listed_at": "2026-08-05",
        },
    ],
    "openapi": f"{SITE_URL}/openapi.json",
    "flows": [],
    "status": {
        "phase": f"Agent Tier live: discovery + OpenAPI + public MCP endpoint at mcp.xrpldashboard.com (public beta through 2026-09, running against our own rippled node on the Lenovo box). Signed snapshots + CLAIMS + envelope contract on every response. Since 2026-08-07, each daily chain_root is additionally anchored on the XRP Ledger from account rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ (first anchor tx 01D0BB9D230955F43DB35703E2EB7F5DFA43CEB69CCBBF57FBC8F17407E50DF8, ledger 106140698); cadence is weekly, manual today. Public-daemonization freshness: {LAST_VERIFIED_AGENT_TIER_METHODOLOGY}.",
        "discovery_layer_ready": True,
        "mcp_ready": True,
        "mcp_stability": "public_beta_through_2026-09",
        "openapi_ready": True,
        "flows_ready": False,
        "reference": "docs/AGENT_TIER_DESIGN.md in the source repo",
    },
}


@app.route("/.well-known/agents.json")
@limiter.limit(agent_tier_limit_rate)
def agents_json():
    """Agent-discovery manifest at the standard well-known path.
    Wildcard-AI flavor. Every field is verifiable — status booleans
    stay false until the corresponding surface actually responds."""
    resp = make_response(jsonify(_AGENTS_JSON))
    resp.headers["Cache-Control"] = "public, max-age=3600, s-maxage=3600"
    return resp


# ---------------------------------------------------------------------------
# /.well-known/x402 — free-tier discovery manifest (2026-08-30, v0.9-draft).
#
# Schema follows the x402 Bazaar discovery-resource shape documented at
# https://docs.x402.org/extensions/bazaar (accepts / extensions.bazaar.info /
# resource / type / x402Version). Bazaar listings normally get published by
# facilitators when a real payment settles with the `bazaar` extension echoed
# in the PaymentPayload — no submission form or PR exists as of 2026-08-30.
# We publish this static manifest as a discovery hint for crawler-based
# directories (t54, xrpl-utilities.io, etc.) and as source-of-truth for
# downstream tooling; when we flip x402 rails from mode=off to mode=live
# (Fence-#8 unblocking, target 2026-09-25), the same `accepts` array will
# populate with real pricing entries.
#
# Field parity with /.well-known/agents.json is deliberate — anything that
# names identity, trust surfaces, or the last_verified date is sourced from
# the same constants so a single edit keeps both manifests in sync. Fields
# that only make sense in the bazaar shape (resource, type, extensions,
# accepts) live only here.
#
# Free-tier scope (Charlie 2026-08-30 spec): /check.json v0.9 is the only
# resource listed. `accepts: []` communicates "no charge required" — a
# machine caller reads that as an unpriced endpoint gated only by IP-bucket
# rate limits. Pricing lands in a follow-up commit after Charlie's ruling.
# ---------------------------------------------------------------------------


_X402_CATALOG = {
    "x402Version": 1,
    # Bazaar-shape core resource entry — a directory ingesting this file
    # can read `resource`, `type`, `accepts`, `extensions.bazaar.info`
    # exactly as documented at docs.x402.org/extensions/bazaar.
    "resource": f"{SITE_URL}/check.json",
    "type": "http",
    "serviceName": "xrpldashboard",
    "iconUrl": f"{SITE_URL}/static/favicon.ico",
    "tags": [
        "xrpl", "verification", "provenance", "attestation", "identity",
        "credentials", "kyc", "signed-snapshot", "free-tier",
    ],
    "lastUpdated": f"{LAST_VERIFIED_AGENT_TIER_METHODOLOGY}T00:00:00Z",
    # Empty accepts = free tier, no payment required. IP-bucket rate limit
    # only. Populated with pricing entries when x402 rails flip live.
    "accepts": [],
    "extensions": {
        "bazaar": {
            "info": {
                "input": {
                    "type": "http",
                    "method": "GET",
                    "queryParams": {
                        "q": (
                            "string — XRPL wallet address (r...), "
                            "token (SYMBOL.rIssuer), URL, or bare domain"
                        ),
                    },
                    "description": (
                        "Free-tier identity + provenance verification for "
                        "XRPL wallets, tokens, URLs, and domains. Returns a "
                        "proof-annotation envelope with source, freshness, "
                        "methodology_url, cross_check_status, and (when the "
                        "/check hot signing key is configured) an Ed25519 "
                        "signature block for third-party verification. Anon "
                        "IP rate limit 60 requests/hour."
                    ),
                    "example": {
                        "url": f"{SITE_URL}/check.json?q=rEXAMPLE...",
                        "headers": {"Accept": "application/json"},
                    },
                },
                "output": {
                    "type": "application/json",
                    "schema": {
                        "$ref": (
                            f"{SITE_URL}/openapi.json"
                            "#/components/schemas/ProofAnnotationEnvelope"
                        ),
                    },
                    "example": {
                        "data": {
                            "kind": "wallet",
                            "address": "rEXAMPLE...",
                            "tier": "verified",
                            "signals": ["identity-credential", "on-chain-activity"],
                        },
                        "proof": {
                            "source": "xrpldashboard/check-endpoint",
                            "as_of": "2026-08-30T22:15:00Z",
                            "freshness_contract": "≤ 5min",
                            "methodology_url": f"{SITE_URL}/methodology#for-ai-agents",
                            "cross_check_status": "verified",
                            "honest_partial": False,
                        },
                        "server": {"signer_id": "check.xrpldashboard.com"},
                        "sig_ed25519": None,
                    },
                },
            },
        },
    },
    # --- Fields below are xrpldashboard-specific (non-bazaar) but useful
    # to any crawler that indexes /.well-known/x402 as a general discovery
    # hint. Kept in sync with /.well-known/agents.json for identity/trust
    # parity — see the shared LAST_VERIFIED / SITE_URL / _AGENTS_JSON
    # constants above.
    "identity": {
        "name": "xrpldashboard",
        "disambiguation": (
            "Independent project — not affiliated with Ripple, the XRP "
            "Ledger Foundation, any exchange, or with xrpdashboard.com "
            "(note: missing 'L' — that's a separate XRP portfolio product)."
        ),
        "site_url": SITE_URL,
        "contact": "contact@xrpldashboard.com",
        "source_code": "https://github.com/Enkryptedx/xrpldashboard",
        "license": "MIT (source); data derived from public XRPL and Ethereum ledgers",
        "agents_manifest": f"{SITE_URL}/.well-known/agents.json",
        "documentation": f"{SITE_URL}/methodology#for-ai-agents",
        "openapi": f"{SITE_URL}/openapi.json",
        "last_verified": LAST_VERIFIED_AGENT_TIER_METHODOLOGY,
    },
    "trust_surfaces": {
        "methodology": f"{SITE_URL}/methodology",
        "claims_index": f"{SITE_URL}/claims",
        "claims_index_json": f"{SITE_URL}/claims/index.json",
        "signed_snapshot_chain": f"{SITE_URL}/.well-known/snapshots/chain.json",
        "signed_snapshot_pubkey": f"{SITE_URL}/.well-known/snapshots/pubkey.pem",
        "security_contact": f"{SITE_URL}/.well-known/security.txt",
    },
    "policies": {
        "cost": "free at v0.9 (no accounts, no API keys, no payment rails wired)",
        "rate_limit_anonymous": "60 requests/hour/IP for /check.json v0.9",
        "auth": "none",
        "retention": "no query retention — POST bodies and query params are NOT stored",
    },
    "disclaimer": (
        "Attestation, not safety. Every /check response carries per-signal "
        "source + freshness. The endpoint reports what identity claims exist "
        "on the ledger; it does NOT tell you whether a subject is safe to "
        "interact with. Signals are facts (KYC credential present, XRPL "
        "amendments voted, snapshot in chain), not verdicts. Human judgment "
        "is required."
    ),
    "status": {
        "phase": (
            "Free-tier verification live. x402 rails currently mode=off "
            "(middleware shipped in commit b406233, 2026-08-30, not "
            "settling payments). Fence-#8 sovereignty items (see "
            "docs/SOVEREIGNTY_COVENANT_VIOLATIONS_2026-08-30.md) must "
            "close before mode=live flip; target 2026-09-25."
        ),
        "x402_rails_ready": False,
        "free_tier_ready": True,
        "signing_key_wired": False,
    },
}


@app.route("/.well-known/x402")
@limiter.limit(agent_tier_limit_rate)
def x402_catalog():
    """x402 Bazaar-shape free-tier discovery manifest. Schema per
    https://docs.x402.org/extensions/bazaar as of 2026-08-30. Cross-check
    with /.well-known/agents.json is enforced by
    tests/test_x402_catalog_parity.py."""
    resp = make_response(jsonify(_X402_CATALOG))
    resp.headers["Cache-Control"] = "public, max-age=3600, s-maxage=3600"
    return resp


@app.route("/sitemap.xml")
@limiter.limit(agent_tier_limit_rate)
def sitemap_xml():
    """Static + dynamic sitemap. Curated public pages always included.
    Per-MPT detail pages (live/prepared only — test issuances carry a
    noindex meta and are excluded here too) extend the sitemap so issuers
    find their own MPT page via Google when they search the token name."""
    today = datetime.now(timezone.utc).date().isoformat()
    urls = []
    for path in PUBLIC_ROUTES:
        # Homepage gets daily/high; secondary pages weekly/medium.
        is_home = path == "/"
        urls.append(
            f"  <url>\n"
            f"    <loc>{SITE_URL}{path}</loc>\n"
            f"    <lastmod>{today}</lastmod>\n"
            f"    <changefreq>{'daily' if is_home else 'weekly'}</changefreq>\n"
            f"    <priority>{'1.0' if is_home else '0.7'}</priority>\n"
            f"  </url>"
        )

    # MPT detail pages — gated on having a snapshot. Failure to load is
    # silent: we just emit a sitemap without MPT entries that day rather
    # than 500 on a transient snapshot read.
    try:
        data = load_mpt_snapshot()
        if data is None:
            data = _cached_db_mpt_snapshot()
        if data:
            enriched = _enrich_mpt_rows(data)
            issuer_indexable = {}
            for r in (enriched.get("issuances") or []):
                if r.get("status") == "test":
                    continue
                iid = r.get("issuance_id")
                if not iid:
                    continue
                urls.append(
                    f"  <url>\n"
                    f"    <loc>{SITE_URL}/mpt/{iid}</loc>\n"
                    f"    <lastmod>{today}</lastmod>\n"
                    f"    <changefreq>daily</changefreq>\n"
                    f"    <priority>0.5</priority>\n"
                    f"  </url>"
                )
                issuer = r.get("issuer")
                if issuer:
                    issuer_indexable[issuer] = True
            # Per-issuer roll-up pages: one URL per distinct issuer with
            # at least one non-test issuance (mirrors the page's own
            # noindex rule — test-only issuers stay out of the sitemap).
            for issuer in sorted(issuer_indexable.keys()):
                urls.append(
                    f"  <url>\n"
                    f"    <loc>{SITE_URL}/mpt/issuer/{issuer}</loc>\n"
                    f"    <lastmod>{today}</lastmod>\n"
                    f"    <changefreq>daily</changefreq>\n"
                    f"    <priority>0.5</priority>\n"
                    f"  </url>"
                )
    except Exception:
        pass

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    )
    return Response(body, mimetype="application/xml")


@app.route("/analytics")
@limiter.limit("60 per minute")
def analytics():
    """Public visitor analytics dashboard. Shows page-view counts, top
    pages, country breakdown, and the last 100 visits (path + country code +
    browser/OS label — no IPs, no referrers rendered, no session data).

    PII posture: recent-visits table shows relative age, path, two-letter
    country code, and 'Browser · OS' label only. The combination is generic
    enough (comparable to Plausible.io public stats) that no individual
    visit is identifiable to third parties. Referrer is stored in the DB
    but intentionally not rendered here."""
    # 60s cache — heavy view (28 queries, 22 heavy _bot_filter_sql, 4
    # all-time scans) was blowing past Cloudflare's 25s origin timeout
    # under load 2026-07-21. Right-now counts move via /analytics/live
    # instead of a full re-render.
    _analytics_cache_key = "full"
    _analytics_now = time.time()
    _analytics_serve_stale = False
    _cached_body = None
    if not getattr(_CACHE_REBUILD_LOCAL, "bypass", False):
        with _ANALYTICS_CACHE_LOCK:
            _cached_entry = _ANALYTICS_CACHE.get(_analytics_cache_key)
            if _cached_entry:
                _cached_body = _cached_entry[1]
                if _cached_entry[0] > _analytics_now:
                    _ANALYTICS_CACHE_STATS["hits"] += 1
                else:
                    # SWR: expired but body exists — serve it now, rebuild
                    # in bg. Live-updating panels (right-now, last-hour,
                    # recent visits) get overwritten by /analytics/live JS
                    # every 15s, so a visitor only sees stale aggregates
                    # (24h / 7d / all-time) — inherently slow-moving.
                    _ANALYTICS_CACHE_STATS["stale_serves"] += 1
                    _analytics_serve_stale = True
    if _cached_body is not None:
        if _analytics_serve_stale:
            _trigger_analytics_rebuild()
        _maybe_flush_analytics_receipts(force=False)
        return _cached_body
    _analytics_render_start = time.perf_counter()

    # Bot classification tiers (fastest first):
    # 1. Table path (db._bot_hash_table_ready=True): _bot_filter_sql uses
    #    indexed subqueries against page_view_bot_hashes + scanner_combos.
    #    No Python params bound. Warmer refreshes tables every ~30s.
    # 2. Precomputed literals (fallback during first warmer cycle): bind
    #    visitor/ip_day hash sets as literal IN params — 2.5x faster than
    #    legacy subqueries but still ~13s cold.
    # 3. Legacy subquery form: if pg unavailable or precompute fails.
    _precomputed_bots = None
    # Skip precompute when the is_bot column is ready — _bot_filter_sql
    # short-circuits on it before checking `precomputed`, so the two
    # full-table HashAggregates here (5-15s/render) would be discarded.
    # Fossil: this path predated the is_bot column and survived the
    # migration silently until 2026-08-29.
    if (db.pg_available()
            and not db._bot_hash_table_ready
            and not db._is_bot_column_ready):
        try:
            with db.pg_connect() as _bot_conn:
                _precomputed_bots = db.compute_bot_hash_sets(_bot_conn)
        except Exception:
            _precomputed_bots = None  # falls back to legacy subquery form

    rollups = db.read_page_view_stats(kind="human",
                                      precomputed_bots=_precomputed_bots)
    top_24h = db.read_top_pages(24 * 60 * 60, limit=15, kind="human",
                                precomputed_bots=_precomputed_bots)
    top_7d = db.read_top_pages(7 * 24 * 60 * 60, limit=15, kind="human",
                               precomputed_bots=_precomputed_bots)
    # Fetch limit=500 for the 24h list ONCE; the top-10 table just slices
    # from it. Previously two separate queries hit the same window twice.
    countries_24h_all = db.read_country_breakdown(24 * 60 * 60, limit=500,
                                                  kind="human",
                                                  precomputed_bots=_precomputed_bots)
    countries_24h = countries_24h_all[:10]
    countries_24h_count = db.read_country_count(24 * 60 * 60, kind="human",
                                                precomputed_bots=_precomputed_bots)

    # All-time origin list. limit=500 is a no-op ceiling vs the ~250
    # ISO 3166-1 codes plus a handful of Cloudflare special codes
    # (T1 = Tor, ? = no header) — sized to never truncate in practice.
    countries_all = db.read_country_breakdown(None, limit=500, kind="human",
                                              precomputed_bots=_precomputed_bots)
    countries_all_count = db.read_country_count(None, kind="human",
                                                precomputed_bots=_precomputed_bots)

    # Reach panel — same bot filter, same population as the tables.
    # Continent counts exclude 'Unknown' (Tor + no-CF-header rows) because
    # their real continent is unknowable.
    continent_24h = _continent_aggregate(countries_24h_all)
    continent_all = _continent_aggregate(countries_all)
    continent_24h_count = sum(1 for c, _, _ in continent_24h if c != "Unknown")
    continent_all_count = sum(1 for c, _, _ in continent_all if c != "Unknown")

    external_refs_7d = db.read_external_referrers(7 * 24 * 60 * 60, limit=15)
    utm_landings_7d = db.read_utm_landings(7 * 24 * 60 * 60, limit=15)

    cta_stats = db.read_cta_click_stats(cta_id="institutional-contact")
    cta_recent_raw = db.read_recent_cta_clicks(limit=10,
                                               cta_id="institutional-contact")

    bot_rollups = db.read_page_view_stats(kind="bot",
                                          precomputed_bots=_precomputed_bots)
    bot_top_24h = db.read_top_pages(24 * 60 * 60, limit=15, kind="bot",
                                    precomputed_bots=_precomputed_bots)
    bot_countries_24h = db.read_country_breakdown(24 * 60 * 60, limit=10,
                                                  kind="bot",
                                                  precomputed_bots=_precomputed_bots)

    recent = db.read_recent_page_views(limit=100)

    now = int(time.time())
    recent_view = []
    for r in recent:
        recent_view.append({
            "age": _humanize_seconds(now - r["ts"]),
            "path": r["path"],
            "country": r["country"] or "?",
            "ua_short": _short_ua(r.get("user_agent")),
        })

    cta_recent = [
        {
            "age": _humanize_seconds(now - r["ts"]),
            "ref_param": r.get("ref_param") or "—",
            "country": r.get("country") or "?",
            "ua_short": _short_ua(r.get("user_agent")),
        }
        for r in cta_recent_raw
    ]

    _analytics_body = render_template(
        "admin_stats.html",
        rollups=rollups,
        top_24h=top_24h,
        top_7d=top_7d,
        countries_24h=countries_24h,
        countries_24h_count=countries_24h_count,
        countries_all=countries_all,
        countries_all_count=countries_all_count,
        continent_24h=continent_24h,
        continent_all=continent_all,
        continent_24h_count=continent_24h_count,
        continent_all_count=continent_all_count,
        external_refs_7d=external_refs_7d,
        utm_landings_7d=utm_landings_7d,
        cta_stats=cta_stats,
        cta_recent=cta_recent,
        bot_rollups=bot_rollups,
        bot_top_24h=bot_top_24h,
        bot_countries_24h=bot_countries_24h,
        recent=recent_view,
        pg_ok=db.pg_available(),
    )
    _analytics_gen_ms = int(
        (time.perf_counter() - _analytics_render_start) * 1000
    )
    with _ANALYTICS_CACHE_LOCK:
        _ANALYTICS_CACHE[_analytics_cache_key] = (
            _analytics_now + _ANALYTICS_CACHE_TTL_S,
            _analytics_body,
            _analytics_gen_ms,
        )
        _ANALYTICS_CACHE_STATS["misses"] += 1
        _hits_total = _ANALYTICS_CACHE_STATS["hits"]
        _misses_total = _ANALYTICS_CACHE_STATS["misses"]
    app.logger.info(
        "analytics_cache: hit=%d miss=%d gen_ms=%d key=%s",
        _hits_total, _misses_total, _analytics_gen_ms, _analytics_cache_key,
    )
    _maybe_flush_analytics_receipts(force=True)
    return _analytics_body


@app.route("/analytics/live")
@limiter.limit("120 per minute")
def analytics_live():
    """Small JSON endpoint for the /analytics page's JS refresh interval.
    Returns only the truly-live sections: right-now counts (5min + 1h) and
    the last N recent visits. NOT cached — the whole point is delta polling
    that dodges the 60s /analytics cache. Uses _bot_filter_sql_lite (path
    LIKE + UA ILIKE only, no session subqueries) so it stays sub-100ms."""
    try:
        stats = db.read_page_view_stats_live(kind="human")
        recent = db.read_recent_page_views(limit=25)
        now = int(time.time())
        recent_view = [
            {
                "age": _humanize_seconds(now - r["ts"]),
                "path": r["path"],
                "country": r["country"] or "?",
                "ua_short": _short_ua(r.get("user_agent")),
            }
            for r in recent
        ]
        return jsonify({
            "ok": True,
            "as_of": now,
            "now": stats.get("now", {"views": 0, "uniques": 0}),
            "hour": stats.get("hour", {"views": 0, "uniques": 0}),
            "recent": recent_view,
        })
    except Exception:
        return jsonify({"ok": False}), 503


@app.route("/admin/stats")
def admin_stats():
    """Legacy path — redirect to the now-public /analytics page."""
    return redirect(url_for("analytics"), code=301)


def _short_ua(ua):
    """Best-effort browser/OS label from a User-Agent string. We don't ship
    a UA-parser dep; this just pattern-matches the common shapes so the
    recent-visits feed reads at a glance instead of a 200-char blob."""
    if not ua:
        return "?"
    s = ua
    browser = "?"
    for token in ("Edg/", "OPR/", "Chrome/", "Firefox/", "Safari/"):
        if token in s:
            browser = token.rstrip("/")
            if browser == "Safari" and "Chrome" in s:
                continue
            break
    os_ = "?"
    for needle, label in (
        ("iPhone", "iPhone"), ("iPad", "iPad"),
        ("Android", "Android"), ("Macintosh", "Mac"),
        ("Windows", "Windows"), ("Linux", "Linux"),
    ):
        if needle in s:
            os_ = label
            break
    return f"{browser} · {os_}"


@app.errorhandler(429)
def _rate_limit_429(e):
    """Day 6: guarantee Retry-After on every 429, per the design doc
    'never silent throttling' rule. flask-limiter's default response
    omits the header unless headers_enabled=True, which would decorate
    ALL responses. Targeted handler leaves non-429 responses untouched.

    flask-limiter raises RateLimitExceeded with a .description like
    '2 per 1 minute' and a `.limit.limit.get_expiry()` giving seconds
    until reset. Fall back to 60s if the shape changes upstream so
    the header is never absent."""
    retry_after = None
    try:
        limit = getattr(e, "limit", None)
        if limit is not None:
            expiry = getattr(getattr(limit, "limit", None), "get_expiry", None)
            if callable(expiry):
                retry_after = int(expiry())
    except Exception:
        retry_after = None
    if not retry_after or retry_after < 1:
        retry_after = 60
    resp = jsonify({"error": "rate_limit_exceeded", "detail": str(e.description)})
    resp.status_code = 429
    resp.headers["Retry-After"] = str(retry_after)
    return resp


@app.errorhandler(404)
def not_found(e):
    """Branded 404 instead of Flask's default. Sends visitors back into
    the public navigation rather than into a dead end."""
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(e):
    """Branded 500. The committed SQLite snapshots make most pages
    survive partial outages, so a 500 means a real bug — point users
    to /health so they can see if workers are paused."""
    return render_template("500.html"), 500


# Register OpenAPI path items for the LIVE discovery + well-known
# surfaces. Runs after every @app.route decorator so url_map is
# fully populated and _rule_for() can find each rule. See the
# Agent Tier / OpenAPI decoration block near the top of this file
# for the fence list and the design behind this decoration.
_register_agent_tier_openapi_paths(app, api.spec)


if __name__ == "__main__":
    # Local dev only. Production uses gunicorn (see Procfile / render.yaml).
    port = int(os.environ.get("PORT", "5001"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=port, debug=debug)

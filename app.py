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
import time
from collections import Counter
from datetime import date, datetime, timezone

from flask import Flask, Response, abort, jsonify, make_response, redirect, render_template, request, send_from_directory, url_for
from flask_limiter import Limiter
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
from xrp_price import fetch_xrp_price_cached
from cold_storage import fetch_cold_storage_cached
from escrow_supply import fetch_escrow_locked_cached
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


_SNAPSHOT_FP_CACHE = {"path_mtime": None, "value": None}


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

    - Cross-Origin-Resource-Policy: same-origin
        Stops cross-origin pages from embedding our resources as no-cors
        loads. Same-origin only — we don't host shared assets.

    - Content-Security-Policy-Report-Only (Trusted Types)
        Report-only enforcement of Trusted Types for sinks like innerHTML.
        Violations log to the browser console without breaking rendering.
        Promote to enforced (drop "-Report-Only") after a week of clean logs.
    """
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


_BLOCKED_UA_FRAGMENTS = ("meta-externalagent",)


@app.before_request
def _block_ai_crawlers():
    """Fast-path 403 for AI training crawlers. Their UA is unique enough that
    a substring match is safe; legitimate browsers never include these tokens.
    Runs before page-view logging so denied requests aren't counted."""
    ua = (request.headers.get("User-Agent") or "").lower()
    for fragment in _BLOCKED_UA_FRAGMENTS:
        if fragment in ua:
            return "Forbidden", 403


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
        ip = request.remote_addr or ""
        if ip and ip in _ANALYTICS_EXCLUDED_IPS:
            return
        ua = (request.user_agent.string or "")[:300] or None
        ref = (request.referrer or "")[:300] or None
        country = request.headers.get("CF-IPCountry") \
            or request.headers.get("X-Vercel-IP-Country") \
            or request.headers.get("X-Country-Code")
        utm = request.args.get("utm_source")
        utm = utm[:100] if utm else None
        db.log_page_view(
            path=path[:300],
            visitor_hash=_visitor_hash(ip, ua),
            referrer=ref,
            user_agent=ua,
            country=country,
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
            rows = db.read_recent_events(limit)
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
                    "ORDER BY ts DESC LIMIT ?",
                    (limit,),
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
        out.append({
            "display": display,
            "issuer": iss,
            "issuer_short": _short_addr(iss),
            "issuer_label": lbl.get("name"),
            "issuer_attested_domain": attested_domain,
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

    # "Where is XRP?" supply constellation — three live on-ledger buckets:
    #   - escrowed:  sum of EscrowCreate objects owned by Ripple's 20
    #                monthly-release accounts (escrow_supply.py)
    #   - amm:       XRP-side of every AMM pool's reserves
    #   - wallets:   100B design supply − the two locked buckets above
    # The design supply is a known constant; the live total drifts down
    # by ~0.02%/year via transaction-fee burns, well below display rounding.
    try:
        esc = fetch_escrow_locked_cached()
    except Exception:
        esc = None
    escrowed_xrp = float(esc.get("total_xrp") or 0) if esc else 0.0
    amm_xrp = 0.0
    try:
        for p in ranked_full:
            a = p.get("asset_a") or {}
            b = p.get("asset_b") or {}
            if a.get("currency") == "XRP":
                amm_xrp += float(p.get("amount_a") or 0)
            elif b.get("currency") == "XRP":
                amm_xrp += float(p.get("amount_b") or 0)
    except Exception:
        amm_xrp = 0.0
    XRP_DESIGN_SUPPLY = 100_000_000_000.0
    locked = escrowed_xrp + amm_xrp
    wallets_xrp = max(0.0, XRP_DESIGN_SUPPLY - locked)
    xrp_distribution = {
        "total_xrp": XRP_DESIGN_SUPPLY,
        "escrowed_xrp": escrowed_xrp,
        "amm_xrp": amm_xrp,
        "wallets_xrp": wallets_xrp,
        "escrowed_pct": (escrowed_xrp / XRP_DESIGN_SUPPLY) * 100,
        "amm_pct": (amm_xrp / XRP_DESIGN_SUPPLY) * 100,
        "wallets_pct": (wallets_xrp / XRP_DESIGN_SUPPLY) * 100,
        "escrow_object_count": (esc.get("object_count") if esc else 0) or 0,
    }

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
    # runs every 14400s (verified via `launchctl print`).
    scan_mode = "scanner" if scan_state else "ranker"
    ranker_next_in = (
        max(0, 14400 - ranker_hb_age) if ranker_hb_age is not None else None
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

    return render_template(
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

    return render_template(
        "rlusd.html",
        initial=initial,
        cached_at=cached_at,
        age_seconds=age_seconds,
        fresh=fresh,
        refresh_interval_minutes=refresh_interval_minutes,
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
         "reason": "Archax is a real institutional broker (archax.com) but no "
                   "evidence of trust-line token issuance on XRPL. Promote "
                   "pending TOML check."},
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
    resp.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
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
    resp = make_response(render_template(
        "network.html",
        state=state,
        diffs=diffs,
        cache_ttl_seconds=network_state.CACHE_TTL,
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
# /walker_health — public view of every instrumented background walker.
# Reads the walker_health table (populated by each walker's try/finally
# instrumentation) and renders a single sortable severity table.
# Truth-first stance: this page exists so visitors can verify the data
# they see on other pages is actually fresh, not last-good.
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

    resp = make_response(render_template(
        "credentials.html",
        state=state,
        cred_groups=cred_groups,
        has_collapsed_groups=has_collapsed_groups,
        perm_domains=perm_domains,
        perm_walker_last=perm_walker_last,
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
        ip = request.remote_addr or ""
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

    ip = request.remote_addr or ""
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
    "institutional-general":   "Institutional inquiry (form fallback)",
}


@app.route("/click/contact")
def click_contact():
    """Click logger + purpose-routed redirect to /contact. purpose is
    baked into cta_id so click analytics segment by intent."""
    purpose = (request.args.get("purpose") or "general").strip().lower()
    if purpose not in CONTACT_PURPOSES:
        purpose = "general"
    try:
        ip = request.remote_addr or ""
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

    ip = request.remote_addr or ""
    ua = (request.user_agent.string or "")[:300] or None
    referrer = (request.referrer or "")[:300] or None
    country = request.headers.get("CF-IPCountry") \
        or request.headers.get("X-Vercel-IP-Country") \
        or request.headers.get("X-Country-Code")

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


@app.route("/check")
@limiter.limit("60 per minute")
def check_page():
    """D1 + D2: paste an XRPL address or a token (SYMBOL.rIssuer), get
    timestamped/sourced signals.

    Facts-not-verdicts by construction — every returned signal carries
    label + source + checked_at_utc, and status pill summarizes WHAT
    IDENTITY CLAIM EXISTS ON THE LEDGER, not whether the subject is
    safe to interact with. Query-string permalink (`?q=…`) is the
    shareable form; POST body deliberately unused so URLs are the
    only surface (pasted messages never enter the URL, D4 concern)."""
    q = (request.args.get("q") or "").strip()
    result = None
    input_error = None

    if q:
        # Token form takes precedence: split on the first "." only.
        # SYMBOL.rIssuer — both sides must be non-empty and issuer must
        # pass strict r-address validation.
        symbol = issuer = None
        if "." in q:
            symbol, _, issuer = q.partition(".")
            symbol = symbol.strip()
            issuer = issuer.strip()

        try:
            import check_data
            if symbol is not None and issuer is not None:
                if not symbol or not _is_xrpl_address(issuer):
                    input_error = babel_gettext(
                        "Token form is SYMBOL.rIssuer \u2014 e.g. "
                        "USD.rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B. "
                        "The issuer must be a valid r-address."
                    )
                else:
                    result = check_data.check_token(symbol, issuer)
            elif _is_xrpl_address(q):
                result = check_data.check_address(q)
            else:
                input_error = babel_gettext(
                    "That doesn't look like an XRPL wallet address "
                    "(starts with 'r') or a token (SYMBOL.rIssuer)."
                )
        except Exception:
            app.logger.exception("check_page: lookup failed")
            input_error = babel_gettext(
                "Something went wrong checking that. Try again in a moment."
            )

    return render_template(
        "check.html",
        query=q,
        result=result,
        input_error=input_error,
    )


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

    return render_template(
        "mpt_detail.html",
        mpt=mpt,
        peers=peers,
        peer_count=len(peers),
        flag_decoded=flag_decoded,
        concentration=concentration,
        history=history,
        archive_depth_seconds=archive_depth_seconds,
        archive_depth_label=archive_depth_label,
    )


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

    return render_template(
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
    )


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


@app.route("/healthz")
@app.route("/api/health")
def healthz():
    """Machine-readable health endpoint for uptime monitors.

    Shares `_health_degrade_state()` with `/health` so the JSON verdict here
    and the human page can never disagree for the same request. PG + local
    file reads only — no XRPL RPC — so polling at 30s cadence stays cheap
    even at high monitor fanout. The per-check breakdown lets monitors
    surface what degraded, not just that something did.

    /api/health is an alias matching the /api/ prefix convention for
    programmatic clients.
    """
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
def security_txt():
    body = (
        "Contact: mailto:contact@xrpldashboard.com\n"
        "Expires: 2027-05-12T00:00:00.000Z\n"
        "Preferred-Languages: en\n"
        f"Canonical: {SITE_URL}/.well-known/security.txt\n"
    )
    return Response(body, mimetype="text/plain")


@app.route("/sitemap.xml")
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
    rollups = db.read_page_view_stats(kind="human")
    top_24h = db.read_top_pages(24 * 60 * 60, limit=15, kind="human")
    top_7d = db.read_top_pages(7 * 24 * 60 * 60, limit=15, kind="human")
    countries_24h = db.read_country_breakdown(24 * 60 * 60, limit=10,
                                              kind="human")
    countries_24h_count = db.read_country_count(24 * 60 * 60, kind="human")

    # All-time origin list. limit=500 is a no-op ceiling vs the ~250
    # ISO 3166-1 codes plus a handful of Cloudflare special codes
    # (T1 = Tor, ? = no header) — sized to never truncate in practice.
    countries_all = db.read_country_breakdown(None, limit=500, kind="human")
    countries_all_count = db.read_country_count(None, kind="human")

    # Reach panel — same bot filter, same population as the tables. Pulls
    # ALL 24h countries (not just the top 10 the table renders) so continent
    # aggregation covers every origin. Continent counts exclude 'Unknown'
    # (Tor + no-CF-header rows) because their real continent is unknowable.
    countries_24h_all = db.read_country_breakdown(24 * 60 * 60, limit=500,
                                                  kind="human")
    continent_24h = _continent_aggregate(countries_24h_all)
    continent_all = _continent_aggregate(countries_all)
    continent_24h_count = sum(1 for c, _, _ in continent_24h if c != "Unknown")
    continent_all_count = sum(1 for c, _, _ in continent_all if c != "Unknown")

    external_refs_7d = db.read_external_referrers(7 * 24 * 60 * 60, limit=15)
    utm_landings_7d = db.read_utm_landings(7 * 24 * 60 * 60, limit=15)

    cta_stats = db.read_cta_click_stats(cta_id="institutional-contact")
    cta_recent_raw = db.read_recent_cta_clicks(limit=10,
                                               cta_id="institutional-contact")

    bot_rollups = db.read_page_view_stats(kind="bot")
    bot_top_24h = db.read_top_pages(24 * 60 * 60, limit=15, kind="bot")
    bot_countries_24h = db.read_country_breakdown(24 * 60 * 60, limit=10,
                                                  kind="bot")

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

    return render_template(
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


if __name__ == "__main__":
    # Local dev only. Production uses gunicorn (see Procfile / render.yaml).
    port = int(os.environ.get("PORT", "5001"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=port, debug=debug)

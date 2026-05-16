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
from datetime import date, datetime, timezone

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_from_directory, url_for
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
from lending_amendment import fetch_lending_status_cached
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
SNAPSHOT_DIR = os.path.join(HERE, "historical_snapshots")
SIGNED_SNAPSHOTS_DIR = os.path.join(HERE, "signed_snapshots")
SNAPSHOT_PUBKEY_PEM_PATH = os.path.join(HERE, "snapshot_pubkey.pem")
SNAPSHOT_PUBKEY_FP_PATH = os.path.join(HERE, "snapshot_pubkey_fingerprint.txt")

WHALE_XRP_THRESHOLD = 100_000  # mirror of WHALE_XRP_THRESHOLD_DROPS / 1e6

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
    "/health",
    "/about",
    "/institutional",
    "/security",
    "/subprocessors",
]

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
    "lending":       ("XRPL Lending",          "Loan brokers, vaults, and TVL — XLS-66.",             "#f59e0b"),
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
# every entry is a trust decision. Plausible is allowlisted ahead of time
# so uncommenting the analytics tag in templates needs no header change.
_CSP_SCRIPT_SRC = "'self' 'unsafe-inline' https://cdn.jsdelivr.net https://plausible.io"
_CSP_STYLE_SRC = "'self' 'unsafe-inline' https://cdn.jsdelivr.net"
_CSP_FONT_SRC = "'self' https://cdn.jsdelivr.net data:"
_CSP_IMG_SRC = "'self' data:"
_CSP_CONNECT_SRC = (
    # Browsers connect to wss://xrplcluster.com (primary). s2 and s1 are
    # kept in the allowlist as automatic fallbacks so a cluster outage
    # can be mitigated without also pushing a CSP header change.
    "'self' https://plausible.io "
    "wss://xrplcluster.com wss://s2.ripple.com wss://s1.ripple.com"
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
        Allowlists the exact external origins we load (jsdelivr, plausible).
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
        ua = (request.user_agent.string or "")[:300] or None
        ref = (request.referrer or "")[:300] or None
        country = request.headers.get("CF-IPCountry") \
            or request.headers.get("X-Vercel-IP-Country") \
            or request.headers.get("X-Country-Code")
        db.log_page_view(
            path=path[:300],
            visitor_hash=_visitor_hash(ip, ua),
            referrer=ref,
            user_agent=ua,
            country=country,
        )
    except Exception:
        # Logging must never break a page render.
        pass


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
    data = scan_all_pools_cached()
    pulse = fetch_pulse_cached()
    timestamp_str = data["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC")
    timestamp_iso = data["timestamp"].strftime("%Y-%m-%dT%H:%M:%SZ")
    cached_age = data.get("cached_age_seconds", 0.0)
    _featured, _top_tier, _other, enriched = _tier_pools(data["pools"])
    tvl_shares = _compute_tvl_shares(enriched, top_n=5)

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
        top_pools=enriched[:5],
        tvl_shares=tvl_shares,
        ranked_top5=ranked_top5,
        ranked_pool_count=ranked_pool_count,
        ranked_total_tvl_usd=ranked_total_tvl_usd,
        recent_whales=_recent_whale_events(limit=3),
        whales_snapshot_at=_whales_snapshot_label(),
        top_tokens=_top_tokens_recent(limit=5),
        cold_storage=cold,
        xrp_distribution=xrp_distribution,
        **data,
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


@app.route("/health")
def health():
    """Operational status page for the background workers.

    Public on purpose — the project's trust positioning says we publish
    real metrics about real infrastructure. Anyone can see whether the
    bootstrap scan is making progress, whether the live watcher is
    catching transactions, and how much data has accumulated.
    """
    scan_state = _safe_load_json(SCAN_STATE_PATH) or {}
    stream_state = _safe_load_json(STREAM_STATE_PATH) or {}

    # Cross-machine truth: workers on the Mac dual-write heartbeats + ranked
    # snapshots to Neon, so prod (Render) — which has none of the Mac's JSON
    # files — can still report real state. Local file reads stay authoritative
    # on the Mac itself; PG reads only fill gaps.
    # Clamp heartbeat ages to 0 — a worker on a host whose clock is ahead of
    # this Flask process produces a "future" timestamp, which is a clock-skew
    # artifact, not a freshness problem. Negative ages would otherwise leak
    # to the UI as "-10668s ago" and read as broken.
    # Host-tagged keys ('xrpl_stream:mac', future ':render') — match the
    # prefix and take the freshest row. Reading the bare 'xrpl_stream' key
    # would return the orphan from before 148c712 (host-tag rollout), which
    # hasn't been updated since the writer rename and looks dead forever.
    pg_hb = db.read_heartbeat_prefix("xrpl_stream")
    pg_hb_age = max(0, int(time.time()) - pg_hb["ts"]) if pg_hb else None
    pg_hb_extra = (pg_hb.get("extra") if isinstance(pg_hb, dict) else None) or {}

    # Latest event ts is a better "Last update" signal than the 5-min worker
    # heartbeat: every whale move / token trade stamps an events row, so on a
    # live network this ticks every 30–90s and matches the user-facing copy
    # ("watcher saw a new transaction").
    try:
        last_event_ts = db.read_max_event_ts()
    except Exception:
        last_event_ts = None
    pg_events_age = max(0, int(time.time()) - last_event_ts) if last_event_ts else None

    ranker_hb = db.read_heartbeat("amm_ranker")
    ranker_hb_age = max(0, int(time.time()) - ranker_hb["ts"]) if ranker_hb else None
    ranker_hb_extra = (ranker_hb.get("extra") if isinstance(ranker_hb, dict) else None) or {}

    scan_started = scan_state.get("started_at") or ranker_hb_extra.get("started_at")
    scan_finished = scan_state.get("finished_at") or ranker_hb_extra.get("finished_at")
    scan_uptime = _iso_to_age_seconds(scan_started)
    scan_pages = scan_state.get("pages", 0)
    scan_rate = round(scan_pages / scan_uptime, 2) if scan_uptime else None
    scan_log_age = _file_age_seconds(SCAN_LOG_PATH)

    stream_started = stream_state.get("started_at") or pg_hb_extra.get("started_at")
    stream_uptime = _iso_to_age_seconds(stream_started)
    stream_log_age = _file_age_seconds(STREAM_LOG_PATH)

    # Liveness: scanner log should tick every ~30s, watcher every ~60s.
    # Conservative thresholds: 5 min and 10 min before flagging stale.
    scan_alive_local = scan_finished is None and (scan_log_age or 999) < 300
    # Ranker cron is every 4h; treat the catalogue as "up to date" whenever
    # the ranker has stamped a heartbeat in the last 6h.
    ranker_alive_remote = ranker_hb_age is not None and ranker_hb_age < 21600
    scan_alive = scan_alive_local or ranker_alive_remote

    stream_alive_local = (stream_log_age or 999) < 600
    # Trust the freshest signal we have. If the local log is missing/stale
    # but Neon shows a recent heartbeat, the worker IS alive — just on a
    # different host. Threshold a bit looser (900s) since heartbeat cadence
    # is 5min and a single skipped tick shouldn't flip prod to "degraded".
    stream_alive_remote = pg_hb_age is not None and pg_hb_age < 900
    stream_alive = stream_alive_local or stream_alive_remote

    amm_index = _safe_load_json(AMM_INDEX_PATH) or []
    amms_in_index = len(amm_index) if isinstance(amm_index, list) and amm_index \
        else ranker_hb_extra.get("indexed_count")

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

    # Banner downgrades when any subsystem is stale, not just when one
    # disappears outright. Without this, a 40h-old pool tracker still
    # rolled up to "ok" because `pool_finished` was true.
    overall = "ok" if scan_alive and stream_alive else "degraded"

    # Status code is the contract for uptime monitors (UptimeRobot etc.) —
    # keyword-matching the HTML body is fragile. Body stays informative for
    # humans hitting /health in a browser; only the code flips on degrade.
    status_code = 503 if overall == "degraded" else 200

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
        },
        recent_log=_tail_lines(STREAM_LOG_PATH, n=8),
    ), status_code


def _load_named_accounts_dict():
    return _safe_load_json(NAMED_ACCOUNTS_PATH) or {}


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

    return {
        "tx_hash": tx_hash,
        "tx_hash_short": (tx_hash[:10] + "…") if tx_hash else "?",
        "ledger": ledger_index,
        "age": _humanize_seconds(age_seconds),
        "type": etype,
        "type_display": type_labels.get(etype, etype),
        "from_addr": from_addr,
        "from_addr_short": _short_addr(from_addr),
        "from_label": _label(from_addr),
        "from_attested_domain": _attested_domain(from_addr),
        "to_addr": to_addr,
        "to_addr_short": _short_addr(to_addr),
        "to_label": _label(to_addr),
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
    # `trustset` events have no Amount at all and bypass entirely because
    # the existence of a new trustline is the signal.
    tier_map = {
        "1m":   ("≥1M XRP",   1_000_000 * 1_000_000),
        "100k": ("≥100K XRP",   100_000 * 1_000_000),
        "50k":  ("≥50K XRP",     50_000 * 1_000_000),
    }
    tier = (request.args.get("tier") or "1m").strip().lower()
    if tier not in tier_map:
        tier = "1m"
    tier_label, tier_drops = tier_map[tier]

    # XRP-denominated tagged events have amount_drops populated and can be
    # gated cheaply in SQL alongside large_xfer. Token-denominated tagged
    # events have amount_drops=NULL and slip through to be priced in Python.
    # trustset events have no amount and always pass.
    clauses = [
        "(type = 'trustset' "
        "OR type = 'tagged' AND amount_drops IS NULL "
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
                    "WHERE (type = 'trustset' "
                    "       OR (type = 'tagged' AND amount_drops IS NULL) "
                    "       OR amount_drops >= ?) "
                    "GROUP BY type",
                    (tier_drops,),
                ):
                    if r[0] in type_counts:
                        type_counts[r[0]] = r[1]
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
        enriched.append({
            "rank": len(enriched) + 1,
            "currency_raw": cur,
            "issuer": iss,
            "issuer_short": _short_addr(iss),
            "display": display,
            "category": category,
            "labeled": labeled,
            "trades": trades,
            "hours_active": hours_active,
        })

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
    cat_order = [
        ("stablecoin",     "stablecoins",  "34,197,94"),
        ("fiat",           "fiat tokens",  "34,197,94"),
        ("wrapped_major",  "wrapped",      "245,158,11"),
        ("native_utility", "utility",      "34,211,238"),
        ("memecoin",       "memecoins",    "236,72,153"),
    ]
    cat_groups = {key: [] for key, _, _ in cat_order}
    for t in enriched:
        if t["category"] in cat_groups:
            cat_groups[t["category"]].append(t)
    labeled_total = sum(
        t["trades"] or 0 for t in enriched
        if t["category"] in cat_groups
    ) or 1
    category_bars = []
    for key, label, rgb in cat_order:
        members = cat_groups[key]
        cat_total = sum(t["trades"] or 0 for t in members)
        if cat_total == 0:
            continue
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
        category_bars.append({
            "key": key,
            "label": label,
            "rgb": rgb,
            "trades": cat_total,
            "share_pct": round((cat_total / labeled_total) * 100, 1),
            "token_count": len(members),
            "segments": segments,
        })

    # A single 100%-tall bar is misleading — it implies all token activity
    # is one category when really it's "all *labeled* activity". Suppress
    # the hero until at least two categories have data.
    if len(category_bars) < 2:
        category_bars = []

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
    return render_template("about.html")


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

    return render_template(
        "rlusd.html",
        initial=initial,
        cached_at=cached_at,
        age_seconds=age_seconds,
        fresh=fresh,
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
        except Exception:
            pass

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
    )


@app.route("/methodology")
def methodology():
    """Per-surface freshness, cache TTLs, data sources, known limitations.
    The differentiator page — no other XRPL dashboard discloses its
    caching/source dependencies in one public document."""
    return render_template("methodology.html")


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
    Linked from /about, intentionally not in top nav."""
    days_collecting = max(1, (date.today() - _COLLECTING_SINCE).days + 1)
    return render_template(
        "institutional.html",
        snapshot_meta=_historical_snapshot_meta(),
        days_collecting=days_collecting,
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
    return render_template("wallet.html", data=data, wallet_qr_svg=_wallet_qr_svg(address))


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
    See cold_storage.py for the data layer + future-scope notes."""
    data = fetch_cold_storage_cached()
    return render_template("cold_storage.html", data=data)


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
    return render_template("lending.html", status=status, data=data)


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
        data = db.read_mpt_snapshot()
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
        data = db.read_mpt_snapshot()
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
        data = db.read_mpt_snapshot()
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
        data = db.read_mpt_snapshot()
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
                    "SELECT id, ts, amm_account, event_type "
                    "FROM amm_pool_events WHERE ts >= ? "
                    "ORDER BY id ASC LIMIT 200",
                    (cutoff,),
                ).fetchall()
                events = [
                    {"id": r[0], "ts": r[1], "amm_account": r[2], "event_type": r[3]}
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
def healthz():
    """Lightweight health endpoint for uptime monitors. No XRPL call, no scan."""
    return {"status": "ok"}, 200


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
            data = db.read_mpt_snapshot()
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

    return render_template(
        "admin_stats.html",
        rollups=rollups,
        top_24h=top_24h,
        top_7d=top_7d,
        countries_24h=countries_24h,
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

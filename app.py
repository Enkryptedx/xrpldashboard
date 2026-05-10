"""Flask app: render live XRPL AMM scan results.

Local dev:    python app.py  (binds 127.0.0.1:5001)
Production:   gunicorn app:app  (PORT from env, set by host)
"""

import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone

from flask import Flask, Response, redirect, render_template, request, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
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
from cold_storage import fetch_cold_storage_cached
from token_data import fetch_token_data_cached
from wallet_data import fetch_wallet_data_cached
import db

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

WHALE_XRP_THRESHOLD = 50_000  # mirror of WHALE_XRP_THRESHOLD_DROPS / 1e6

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
    "/cold-storage",
    "/health",
    "/about",
    "/institutional",
]

app = Flask(__name__)
# Render's edge terminates TLS and forwards via X-Forwarded-For. Without
# ProxyFix, every request appears to come from Render's edge IP and the
# rate limiter would lump all visitors into one bucket. x_for=1 trusts
# exactly one upstream proxy hop (Render's), which is correct for our
# topology. x_proto=1 lets url_for build https URLs behind the proxy.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.jinja_env.globals.update(fmt_money=fmt_money, fmt_num=fmt_num)

# In-memory limiter — Render free tier is single-process / single-replica,
# so memory storage is correct. Counts reset on deploy (intentional: we're
# stopping curl loops, not enforcing daily quotas). If we ever scale to
# multiple replicas, swap storage_uri to Redis or a Neon-backed store.
# No global default — limits are applied explicitly per-route so health
# checks and HTML pages stay unthrottled.
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    storage_uri="memory://",
)


@app.context_processor
def inject_site_url():
    """Make {{ site_url }} available to every template, primarily for the
    shared _head_meta.html partial that builds canonical / og:url."""
    return {"site_url": SITE_URL}


# Allowlist of external origins our pages legitimately load. Keep narrow —
# every entry is a trust decision. Plausible is allowlisted ahead of time
# so uncommenting the analytics tag in templates needs no header change.
_CSP_SCRIPT_SRC = "'self' 'unsafe-inline' https://cdn.jsdelivr.net https://plausible.io"
_CSP_STYLE_SRC = "'self' 'unsafe-inline' https://cdn.jsdelivr.net"
_CSP_FONT_SRC = "'self' https://cdn.jsdelivr.net data:"
_CSP_IMG_SRC = "'self' data:"
_CSP_CONNECT_SRC = (
    # Browsers currently only connect to wss://s2.ripple.com. s1 and
    # xrplcluster are kept in the allowlist as documented fallbacks so a
    # node outage can be mitigated by editing the WS_URL constants in
    # the templates without also pushing a CSP header change. When this
    # tightens to s2 only, the only operational cost is one extra deploy
    # on the day s2 has a bad hour. Worth it.
    "'self' https://plausible.io "
    "wss://s2.ripple.com wss://s1.ripple.com wss://xrplcluster.com"
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
)
_PAGEVIEW_SKIP_EXACT = {
    "/favicon.ico", "/robots.txt", "/sitemap.xml",
    "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
}


def _visitor_hash(ip, ua):
    """Stable per-day fingerprint for unique-visitor counting. We hash so
    /admin/stats never shows raw IPs (the page is private to Charlie, but
    not storing PII in the DB at all is the cleaner default). Day-bucketed
    so the same person counts as one unique per day."""
    import hashlib
    day = time.strftime("%Y-%m-%d", time.gmtime())
    raw = f"{ip or '?'}|{ua or '?'}|{day}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:16]


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
        ua = (request.user_agent.string or "")[:300] if request.user_agent else None
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
        "last_close_age_seconds": p.get("last_close_age_seconds"),
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
            return rows, {
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
    return rows, {
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
    out = []
    for cur, iss, trades in rows:
        meta = tokens_meta.get((cur, iss)) or {}
        if meta:
            display = meta.get("currency_display") or cur
        else:
            decoded = _decode_currency_hex(cur)
            display = decoded or (cur[:8] + "…" if cur and len(cur) > 8 else (cur or "?"))
        out.append({
            "display": display,
            "issuer_short": _short_addr(iss),
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
    return render_template(
        "index.html",
        timestamp_str=timestamp_str,
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
        return int((datetime.now(timezone.utc) - dt).total_seconds())
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
    pg_hb = db.read_heartbeat("xrpl_stream")
    pg_hb_age = (int(time.time()) - pg_hb["ts"]) if pg_hb else None
    pg_hb_extra = (pg_hb.get("extra") if isinstance(pg_hb, dict) else None) or {}

    ranker_hb = db.read_heartbeat("amm_ranker")
    ranker_hb_age = (int(time.time()) - ranker_hb["ts"]) if ranker_hb else None
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

    # Pool tracker is "finished" (catalogue available) whenever the ranker
    # has produced a snapshot — even if this host has no local scan state.
    pool_finished = scan_finished is not None or (
        ranker_hb is not None and (amms_in_index or 0) > 0
    )

    overall = "ok" if (scan_alive or pool_finished) and stream_alive else "degraded"

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
            "uptime": _humanize_seconds(scan_uptime),
            "pages": scan_pages,
            "objects_scanned": scan_state.get("raw_objects_scanned", 0),
            "rate": scan_rate,
            "ledger_index": scan_state.get("ledger_index"),
            "log_age": _humanize_seconds(scan_log_age if scan_log_age is not None else ranker_hb_age),
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
            "log_age": _humanize_seconds(stream_log_age if stream_log_age is not None else pg_hb_age),
            "seen_tokens_count": len(stream_state.get("seen_tokens", []) or []),
        },
        substrate={
            "events_rows": _safe_count_table(EVENTS_DB_PATH, "events"),
            "volumes_rows": _safe_count_table(VOLUMES_DB_PATH, "token_volume"),
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

    age_seconds = max(0, int(time.time() - ts))
    type_labels = {
        "large_xfer": "large XRP",
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
        "to_addr": to_addr,
        "to_addr_short": _short_addr(to_addr),
        "to_label": _label(to_addr),
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

    # Tier filter — applies only to large_xfer events; tagged/trustset bypass
    # because the "who" makes them signal regardless of size.
    tier_map = {
        "1m":   ("≥1M XRP",   1_000_000 * 1_000_000),
        "100k": ("≥100K XRP",   100_000 * 1_000_000),
        "50k":  ("≥50K XRP",     50_000 * 1_000_000),
    }
    tier = (request.args.get("tier") or "1m").strip().lower()
    if tier not in tier_map:
        tier = "1m"
    tier_label, tier_drops = tier_map[tier]

    clauses = ["(type != 'large_xfer' OR amount_drops >= ?)"]
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
                    "WHERE (type != 'large_xfer' OR amount_drops >= ?) "
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

    # Real readings for the two HUD corners (replace the old fake
    # bearing/range/contacts text). Always reflect the canonical 50K-XRP
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
    max_trades = max((t["trades"] or 0) for t in labeled_pool) if labeled_pool else 1
    for idx, t in enumerate(labeled_pool[: HEX_COLS * HEX_ROWS]):
        row = idx // HEX_COLS
        col = idx % HEX_COLS
        cx = pad_x + col * col_w + (col_w / 2 if row % 2 else 0)
        cy = pad_y + row * row_h
        baseline = (t["trades"] or 0) / max_trades if max_trades else 0
        hex_cells.append({
            "key": f"{t['currency_raw']}|{t['issuer']}",
            "display": t["display"],
            "category": t["category"] or "other",
            "trades": t["trades"] or 0,
            "currency_raw": t["currency_raw"],
            "issuer": t["issuer"],
            "cx": round(cx, 2),
            "cy": round(cy, 2),
            "baseline": round(baseline, 3),
        })
    hex_view_w = round(pad_x * 2 + (HEX_COLS - 1) * col_w + col_w / 2, 2)
    hex_view_h = round(pad_y * 2 + (HEX_ROWS - 1) * row_h, 2)

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
        data_age_label=_format_age_seconds(_volumes_db_age_seconds()),
    )


@app.route("/about")
def about():
    """Public-facing 'what is this' page. Mission, principles, methodology,
    funding model. Copy lives in the template — review before launch."""
    return render_template("about.html")


@app.route("/methodology")
def methodology():
    """Per-surface freshness, cache TTLs, data sources, known limitations.
    The differentiator page — no other XRPL dashboard discloses its
    caching/source dependencies in one public document."""
    return render_template("methodology.html")


def _historical_snapshot_meta():
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
    }


@app.route("/institutional")
def institutional():
    """Pre-launch institutional positioning page. Contact-only (no published
    prices) until launch-partner conversations produce real pricing data.
    Linked from /about, intentionally not in top nav."""
    return render_template(
        "institutional.html",
        snapshot_meta=_historical_snapshot_meta(),
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
    valid_tiers = {"100": 100, "500": 500, "all": None}
    tier = (request.args.get("tier") or "100").strip().lower()
    if tier not in valid_tiers:
        tier = "100"
    limit = valid_tiers[tier]

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
    rows = ranked if limit is None else ranked[:limit]

    indexed_count = meta.get("indexed_count") or 0
    ranked_count = len(ranked)
    exact_count_all = sum(1 for r in ranked if r.get("tvl_status") == "exact")
    estimated_count_all = sum(1 for r in ranked if r.get("tvl_status") == "estimated")
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
        rank_finished=rank_finished,
        rank_in_progress=rank_in_progress,
        total_tvl_usd=total_tvl_usd,
        top10_total_tvl=top10_total_tvl,
        top10_share_of_all=top10_share_of_all,
        snapshot_age_label=snapshot_age_label,
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
    return render_template("wallet.html", data=data)


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
        "last_close_age_seconds": p.get("last_close_age_seconds"),
        "status": p.get("status"),
        "status_text": p.get("status_text"),
        "load_factor": p.get("load_factor"),
        "avg_close_seconds": p.get("avg_close_seconds"),
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


@app.route("/healthz")
def healthz():
    """Lightweight health endpoint for uptime monitors. No XRPL call, no scan."""
    return {"status": "ok"}, 200


@app.route("/robots.txt")
def robots_txt():
    """Tell crawlers what's indexable. Allow the public surface, exclude
    operational endpoints and the unbounded detail-page space (every
    valid wallet address would otherwise be a crawlable URL)."""
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /healthz\n"
        "Disallow: /api/\n"
        "Disallow: /lookup\n"
        "Disallow: /v2\n"
        "Disallow: /wallet/\n"
        "Disallow: /token/\n"
        f"\nSitemap: {SITE_URL}/sitemap.xml\n"
    )
    return Response(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    """Static sitemap covering the curated public pages. Detail pages
    (wallet/token) are intentionally excluded — they're discoverable
    through the listing pages, and the URL space is unbounded."""
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
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    )
    return Response(body, mimetype="application/xml")


@app.route("/admin/stats")
def admin_stats():
    """Private real-time visitor analytics, gated by ADMIN_STATS_KEY.
    The token comes via ?key= query param. Wrong/missing key returns 404
    (not 401) so the route's existence stays unadvertised to scanners."""
    expected = (os.environ.get("ADMIN_STATS_KEY") or "").strip()
    provided = (request.args.get("key") or "").strip()
    if not expected or not provided or provided != expected:
        return render_template("404.html"), 404

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
            "referrer": r["referrer"],
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
        key=provided,
        pg_ok=db.pg_available(),
    )


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
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="127.0.0.1", port=port, debug=debug)

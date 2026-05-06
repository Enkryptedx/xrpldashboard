"""Flask app: render live XRPL AMM scan results.

Local dev:    python app.py  (binds 127.0.0.1:5001)
Production:   gunicorn app:app  (PORT from env, set by host)
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone

from flask import Flask, Response, redirect, render_template, request, url_for

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

WHALE_XRP_THRESHOLD = 500_000  # mirror of WHALE_XRP_THRESHOLD_DROPS / 1e6

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
app.jinja_env.globals.update(fmt_money=fmt_money, fmt_num=fmt_num)


@app.context_processor
def inject_site_url():
    """Make {{ site_url }} available to every template, primarily for the
    shared _head_meta.html partial that builds canonical / og:url."""
    return {"site_url": SITE_URL}


@app.after_request
def apply_security_headers(response):
    """Minimal security headers on every response.

    Three headers chosen for real protection without breaking the site's
    current inline-script + inline-style pattern:

    - X-Content-Type-Options: nosniff
        Prevents browsers from MIME-sniffing a response away from its
        declared Content-Type. Cheap, no compatibility risk.

    - X-Frame-Options: DENY
        Disallows the site being framed by any other origin. Mitigates
        clickjacking. We never embed our own pages in frames.

    - Referrer-Policy: strict-origin-when-cross-origin
        On same-origin nav, send the full referrer; on cross-origin,
        only the origin (no path). Standard sensible default.

    Deferred:

    - Content-Security-Policy
        A real CSP would forbid inline <script> and inline <style>, but
        the templates rely on both (liveness chip polling, table sorts,
        large embedded <style> blocks). Doing CSP correctly means moving
        every inline script into /static/js/ and every inline style into
        /static/css/, which is a pre-launch refactor we explicitly chose
        to defer. Add CSP after the inline-asset move lands.

    - Strict-Transport-Security (HSTS)
        Only meaningful once the site is served over HTTPS in production.
        Add it in app.py once the Render deploy + custom domain are live.
    """
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Referrer-Policy", "strict-origin-when-cross-origin"
    )
    return response


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


def _pools_snapshot_label():
    """Friendly date the AMM ranking last completed (from amm_rank_state.json).
    Used to label the pool tracker honestly when workers aren't live in prod.
    Falls back to amm_scan_state started_at, then None."""
    for path, key in (
        (AMM_RANK_STATE_PATH, "finished_at"),
        (AMM_RANK_STATE_PATH, "started_at"),
        (SCAN_STATE_PATH, "finished_at"),
        (SCAN_STATE_PATH, "started_at"),
    ):
        try:
            d = _safe_load_json(path) or {}
            iso = d.get(key)
            if not iso:
                continue
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return f"{dt.strftime('%B')} {dt.day}, {dt.year}"
        except Exception:
            continue
    return None


def _top_tokens_recent(limit=5, hours_back=24 * 7):
    """Top N tokens by trade count over the last `hours_back` hours.
    Mirrors the /tokens route but trimmed for the homepage preview."""
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

    scan_started = scan_state.get("started_at")
    scan_finished = scan_state.get("finished_at")
    scan_uptime = _iso_to_age_seconds(scan_started)
    scan_pages = scan_state.get("pages", 0)
    scan_rate = round(scan_pages / scan_uptime, 2) if scan_uptime else None
    scan_log_age = _file_age_seconds(SCAN_LOG_PATH)

    stream_started = stream_state.get("started_at")
    stream_uptime = _iso_to_age_seconds(stream_started)
    stream_log_age = _file_age_seconds(STREAM_LOG_PATH)

    # Liveness: scanner log should tick every ~30s, watcher every ~60s.
    # Conservative thresholds: 5 min and 10 min before flagging stale.
    scan_alive = scan_finished is None and (scan_log_age or 999) < 300
    stream_alive = (stream_log_age or 999) < 600

    amm_index = _safe_load_json(AMM_INDEX_PATH) or []

    overall = "ok" if (scan_alive or scan_finished) and stream_alive else "degraded"

    pulse = fetch_pulse_cached()

    return render_template(
        "health.html",
        overall=overall,
        pulse=pulse,
        scan={
            "alive": scan_alive,
            "finished": scan_finished is not None,
            "uptime": _humanize_seconds(scan_uptime),
            "pages": scan_pages,
            "objects_scanned": scan_state.get("raw_objects_scanned", 0),
            "rate": scan_rate,
            "ledger_index": scan_state.get("ledger_index"),
            "log_age": _humanize_seconds(scan_log_age),
            "amms_in_index": len(amm_index) if isinstance(amm_index, list) else None,
            "snapshot_at": _pools_snapshot_label(),
        },
        stream={
            "alive": stream_alive,
            "uptime": _humanize_seconds(stream_uptime),
            "txns_seen": stream_state.get("txns_seen", 0),
            "amm_creates": stream_state.get("amm_creates_seen", 0),
            "whale_events": stream_state.get("whale_events_seen", 0),
            "token_events": stream_state.get("token_events_seen", 0),
            "new_tokens": stream_state.get("new_tokens_seen", 0),
            "last_ledger": stream_state.get("last_ledger_index"),
            "log_age": _humanize_seconds(stream_log_age),
            "seen_tokens_count": len(stream_state.get("seen_tokens", []) or []),
        },
        substrate={
            "events_rows": _safe_count_table(EVENTS_DB_PATH, "events"),
            "volumes_rows": _safe_count_table(VOLUMES_DB_PATH, "token_volume"),
        },
        recent_log=_tail_lines(STREAM_LOG_PATH, n=8),
    )


def _load_named_accounts_dict():
    return _safe_load_json(NAMED_ACCOUNTS_PATH) or {}


def _load_token_names_dict():
    """Build {(currency_hex, issuer): entry} for fast lookup."""
    raw = _safe_load_json(TOKEN_NAMES_PATH) or {}
    out = {}
    for entry in raw.values():
        if isinstance(entry, dict):
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
    where_clause = ""
    params = []
    if filter_type in valid_types:
        where_clause = "WHERE type = ?"
        params.append(filter_type)
    else:
        filter_type = ""

    events = []
    type_counts = {"large_xfer": 0, "tagged": 0, "trustset": 0, "_total": 0}

    if os.path.exists(EVENTS_DB_PATH):
        try:
            conn = sqlite3.connect(f"file:{EVENTS_DB_PATH}?mode=ro", uri=True)
            try:
                for r in conn.execute(
                    "SELECT type, COUNT(*) FROM events GROUP BY type"
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
            named = _load_named_accounts_dict()
            tokens = _load_token_names_dict()
            events = [_resolve_event(r, named, tokens) for r in rows]
        except Exception:
            events = []

    return render_template(
        "whales.html",
        events=events,
        filter_type=filter_type,
        type_counts=type_counts,
        threshold_xrp=WHALE_XRP_THRESHOLD,
        named_accounts_count=len(_load_named_accounts_dict()),
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
    range_key = (request.args.get("range") or "all").strip().lower()
    if range_key not in valid_ranges:
        range_key = "all"
    hours_back = valid_ranges[range_key]

    rows = []
    earliest_bucket = None
    total_buckets = 0

    if os.path.exists(VOLUMES_DB_PATH):
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
                    "SELECT MIN(hour_bucket), COUNT(DISTINCT hour_bucket) "
                    "FROM token_volume"
                ).fetchone()
                earliest_bucket, total_buckets = stats[0], stats[1]
            finally:
                conn.close()
        except Exception:
            pass

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

    return render_template(
        "tokens.html",
        tokens=enriched,
        earliest_iso=earliest_iso,
        total_buckets=total_buckets,
        labeled_count=sum(1 for t in enriched if t["labeled"]),
        range_key=range_key,
    )


@app.route("/about")
def about():
    """Public-facing 'what is this' page. Mission, principles, methodology,
    funding model. Copy lives in the template — review before launch."""
    return render_template("about.html")


@app.route("/institutional")
def institutional():
    """Pre-launch institutional positioning page. Contact-only (no published
    prices) until launch-partner conversations produce real pricing data.
    Linked from /about, intentionally not in top nav."""
    return render_template("institutional.html")


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

    ranked = _safe_load_json(AMM_RANKED_PATH) or []
    index = _safe_load_json(AMM_INDEX_PATH) or []
    state = _safe_load_json(AMM_RANK_STATE_PATH) or {}

    # Defensive sort: rank_amms.py finalizes sort at the end of a run, but
    # while ranking is in progress the file is in append order.
    ranked = sorted(ranked, key=_rank_status_order)

    top10 = ranked[:10]
    rows = ranked if limit is None else ranked[:limit]

    indexed_count = len(index) if isinstance(index, list) else 0
    ranked_count = len(ranked)
    rank_finished = state.get("finished_at") is not None
    rank_started = state.get("started_at") is not None
    rank_in_progress = rank_started and not rank_finished

    # Aggregate: total TVL across exact + estimated only (non_xrp_pair has
    # tvl_usd=None, error has no value either).
    total_tvl_usd = sum(
        (r.get("tvl_usd") or 0)
        for r in ranked
        if r.get("tvl_status") in ("exact", "estimated")
    )

    return render_template(
        "pools.html",
        top10=top10,
        rows=rows,
        tier=tier,
        indexed_count=indexed_count,
        ranked_count=ranked_count,
        rank_finished=rank_finished,
        rank_in_progress=rank_in_progress,
        total_tvl_usd=total_tvl_usd,
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
        return render_template(
            "wallet.html",
            data={
                "error": "Not a valid XRPL address.",
                "address": address,
                "address_short": address[:12] + "…" if len(address) > 12 else address,
                "balance_xrp": 0, "available_xrp": 0, "reserved_xrp": 0,
                "pct_locked": 0, "owner_count": 0, "trustline_count": 0,
                "tx_count_30d": 0, "active_days_30d": 0, "lookback_days": 30,
                "last_seen": "—", "top_counterparty_label": "—",
                "pulse": [0] * 30, "nodes": [], "tx_sample_size": 0,
                "holdings": [], "holdings_lp": [],
            },
        ), 400

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


@app.route("/token/<currency>/<issuer>")
def token_detail(currency, issuer):
    """Token detail view — drilldown from /tokens. Shows trade activity
    over time, AMM pools that hold this token, and links out to other
    explorers. Read-only artifacts only — no live RPC."""
    currency = (currency or "").strip()
    issuer = (issuer or "").strip()

    if not _is_valid_currency(currency) or not _is_xrpl_address(issuer):
        return render_template(
            "token.html",
            data={
                "error": "Invalid token currency or issuer address.",
                "currency_raw": currency, "issuer": issuer,
                "issuer_short": issuer[:10] + "…" if len(issuer) > 10 else issuer,
                "display": currency or "?", "category": None, "labeled": False,
                "currency_decoded": None, "source_url": None,
                "trades_all": 0, "trades_24h": 0, "trades_7d": 0,
                "volume_all_xrp": 0.0, "hours_active": 0,
                "first_seen_iso": None, "last_seen_iso": None, "last_seen_age": "—",
                "sparkline": [0] * (24 * 7), "sparkline_hours": 24 * 7,
                "pools": [], "pool_count": 0,
            },
        ), 400

    data = fetch_token_data_cached(currency, issuer)
    return render_template("token.html", data=data)


@app.route("/cold-storage")
def cold_storage():
    """Cold-storage tracker — currently scoped to Ripple monthly-release escrows.
    See cold_storage.py for the data layer + future-scope notes."""
    data = fetch_cold_storage_cached()
    return render_template("cold_storage.html", data=data)


@app.route("/api/ledger-tip")
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

"""Flask app: render live XRPL AMM scan results.

Local dev:    python app.py  (binds 127.0.0.1:5001)
Production:   gunicorn app:app  (PORT from env, set by host)
"""

import os

from flask import Flask, render_template, request

from amm_scan_pools import (
    JsonRpcClient,
    XRPL_NODE,
    fetch_pool,
    fmt_money,
    fmt_num,
    scan_all_pools_cached,
)

app = Flask(__name__)
app.jinja_env.globals.update(fmt_money=fmt_money, fmt_num=fmt_num)


def _render_dashboard(lookup_result=None, lookup_error=None,
                      lookup_currency="", lookup_issuer=""):
    data = scan_all_pools_cached()
    timestamp_str = data["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC")
    cached_age = data.get("cached_age_seconds", 0.0)
    return render_template(
        "index.html",
        timestamp_str=timestamp_str,
        cached_age=cached_age,
        lookup_result=lookup_result,
        lookup_error=lookup_error,
        lookup_currency=lookup_currency,
        lookup_issuer=lookup_issuer,
        **data,
    )


@app.route("/")
def index():
    return _render_dashboard()


@app.route("/lookup")
def lookup():
    currency = (request.args.get("currency") or "").strip()
    issuer = (request.args.get("issuer") or "").strip()

    if not currency or not issuer:
        return _render_dashboard(
            lookup_error="Both currency and issuer are required.",
            lookup_currency=currency,
            lookup_issuer=issuer,
        )

    token_info = {
        "name": currency if len(currency) <= 6 else currency[:6] + "…",
        "currency": currency,
        "issuer": issuer,
        "usd_peg": None,
    }

    client = JsonRpcClient(XRPL_NODE)
    pool = fetch_pool(client, token_info)

    if "error" in pool:
        return _render_dashboard(
            lookup_error=f"No AMM pool found for XRP/{token_info['name']} @ {issuer} ({pool['error']}).",
            lookup_currency=currency,
            lookup_issuer=issuer,
        )

    return _render_dashboard(
        lookup_result=pool,
        lookup_currency=currency,
        lookup_issuer=issuer,
    )


@app.route("/healthz")
def healthz():
    """Lightweight health endpoint for uptime monitors. No XRPL call, no scan."""
    return {"status": "ok"}, 200


if __name__ == "__main__":
    # Local dev only. Production uses gunicorn (see Procfile / render.yaml).
    port = int(os.environ.get("PORT", "5001"))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="127.0.0.1", port=port, debug=debug)

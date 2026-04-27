"""Flask app: render live XRPL AMM scan results at localhost:5001."""

from flask import Flask, render_template, request

from amm_scan_pools import (
    JsonRpcClient,
    XRPL_NODE,
    fetch_pool,
    fmt_money,
    fmt_num,
    scan_all_pools,
)

app = Flask(__name__)
app.jinja_env.globals.update(fmt_money=fmt_money, fmt_num=fmt_num)


def _render_dashboard(lookup_result=None, lookup_error=None,
                      lookup_currency="", lookup_issuer=""):
    data = scan_all_pools()
    timestamp_str = data["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC")
    return render_template(
        "index.html",
        timestamp_str=timestamp_str,
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)

# XRPL AMM Scanner — Web App Notes

Minimal Flask wrapper around `amm_scan_pools.py`. Renders live XRPL AMM pool
data in the browser.

## Run

```sh
./venv/bin/python app.py
```

Then open: **http://localhost:5001**

(http://127.0.0.1:5001 also works.)

To stop: Ctrl+C in the terminal running the server.

## Why port 5001 (not 5000)

macOS ships with the **AirPlay Receiver** service bound to port 5000 on all
interfaces, including IPv6 `[::1]`. Browsers resolving `localhost` prefer
IPv6, so `http://localhost:5000` hits AirPlay (HTTP 403 / blank page) instead
of Flask, even when Flask is running on `127.0.0.1:5000`.

Port 5001 is unused by macOS, so `localhost:5001` resolves cleanly to Flask
on both IPv4 and IPv6 lookups.

(Alternative fix: System Settings → General → AirDrop & Handoff → turn off
"AirPlay Receiver". We chose the port change instead — no system-level
toggling required.)

## How it works

- Flask route `/` calls `scan_all_pools()` from `amm_scan_pools.py`.
- Flask route `/lookup` accepts `?currency=…&issuer=…`, runs the same scan,
  AND queries the requested AMM pool, rendering both on the same page.
- **Each page load triggers a fresh live query to the XRP Ledger** (~1–2s).
  No caching, no scheduler, no database. Reload the page and the ledger
  index advances — that's how you confirm it's real data, not a snapshot.
- The yellow "Last scanned: …" banner at the top shows the UTC timestamp
  of the current scan.

## Search (two elements)

The dashboard has two distinct ways to find a pool:

- **Filter** — input above the table; client-side JS hides rows whose name
  doesn't substring-match the query. Instant, no server hit. For finding a
  pool that's already in the curated list of 19.
- **Lookup form** — below the table; takes a currency code (3-letter or
  40-char hex) and an issuer r-address, queries XRPL for that exact
  XRP/<token> AMM pool, and renders the result in a blue detail card at
  the top of the page. Use this for any pool not in the curated list.
  Graceful "No AMM pool found" warning if the pool doesn't exist.

## Files

- `app.py` — Flask app (`/` and `/lookup` routes; shared `_render_dashboard`)
- `templates/index.html` — page template (table, filter, lookup form, detail card)
- `amm_scan_pools.py` — scan logic + 19-token `KNOWN_TOKENS` list; also CLI
- `amm_test.py` — original single-pool proof-of-concept (untouched)

## Dependencies

`flask` and `xrpl-py`, both installed in `./venv/`.

## Changelog

### 2026-04-26 — Curated pool expansion + search

- Expanded `KNOWN_TOKENS` from 4 to 19 pools, all verified against live XRPL
  (`amm_info` returned a real pool with TVL on both sides for every entry).
  Coverage: 4 USD-pegged stables (RLUSD, USDC, USD.Bitstamp, USD.Gatehub),
  EUR.Gatehub, wrapped majors (BTC.Bitstamp, BTC.Gatehub, ETH.Gatehub),
  utility tokens (SOLO, CSC, CORE, ELS, XPM, EQ, RPR), and four memecoins
  with real AMM TVL (XRdoge, scrap, PHNIX, BERT).
- Skipped USDT — no verifiable XRP/USDT AMM was found at any plausible
  issuer. Better than guessing.
- Added two-element search: client-side filter + server-side `/lookup` route.
- Total TVL visible on the dashboard jumped from ~$5.69M (4 pools) to
  ~$9.20M (19 pools).

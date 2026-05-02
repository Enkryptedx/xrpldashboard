# xrpldashboard

Public-good analytics for the XRP Ledger.
Live at **[xrpldashboard.com](https://xrpldashboard.com)** *(deployment in progress)*.

> Closer in spirit to [mempool.space](https://mempool.space) (Bitcoin) than to
> Nansen (Ethereum): opinionated about clarity, no token, no DEX, no upsells.
> A piece of infrastructure the ecosystem can rely on.

## What it does

Renders live AMM pool data from the XRP Ledger as a single dashboard page.
19 curated pools (stablecoins, wrapped majors, native utility tokens,
memecoins with real TVL), sorted by total value locked, with a search box
to look up any XRP/token pool by issuer.

Every number is pulled directly from a public XRPL node — no third-party
data source, no aggregator middle-layer. Server-side cache (60s TTL by
default) keeps load light without making the page feel stale.

## Running locally

```sh
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open <http://localhost:5001>.

(Port 5001 not 5000 — see [NOTES.md](./NOTES.md) for why.)

## Deploying

See [DEPLOY.md](./DEPLOY.md) for the launch checklist.
TL;DR: Render + Cloudflare DNS + Plausible analytics + UptimeRobot.
~$13/month all-in for a real, monitored, observably-up service.

## Repo layout

| File | What it does |
|---|---|
| `app.py` | Flask app — `/`, `/lookup`, `/healthz` |
| `amm_scan_pools.py` | XRPL scan logic + 19-token curated list + cache |
| `templates/index.html` | Dashboard page (table, filter, lookup form, detail card) |
| `requirements.txt` | Pinned production deps (Flask, xrpl-py, gunicorn) |
| `render.yaml` | Render.com infrastructure-as-code config |
| `Procfile` | Fallback process definition (Heroku-style hosts) |
| `ROADMAP.md` | Where this is going (network pulse, whale watch, institutional layer) |
| `NOTES.md` | How the dev setup works, port choice, search behavior |
| `DEPLOY.md` | Step-by-step launch guide |
| `amm_test.py` | Original single-pool proof-of-concept (kept for reference) |

## Mission

Help XRP holders understand on-chain activity. Not get rich. Not sell
liquidity. Not push tokens. Just data, explained well, for people who
want to understand what they own.

Free for retail users, forever.

## Funding model

1. Build the free retail product first.
2. Apply for grants once there's something real to show
   (Ripple's XRPL Grants program, XRPL Foundation grants).
3. Add an institutional tier (API access, historical depth, alerts)
   when retail is proven and demand pulls. Never gate retail basics.

## License

*(TBD — likely MIT or Apache 2.0 once the project goes public. For now,
all rights reserved by the author.)*

## Disclaimer

Data shown is for educational and informational purposes only. Not
financial advice. Verify against [XRPSCAN](https://xrpscan.com) or
[Bithomp](https://bithomp.com) for additional information.

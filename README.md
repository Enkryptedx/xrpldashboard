# xrpldashboard

Public-good analytics for the XRP Ledger.
Live at **[xrpldashboard.com](https://xrpldashboard.com)**.

> Closer in spirit to [mempool.space](https://mempool.space) (Bitcoin) than to
> Nansen (Ethereum): opinionated about clarity, no token, no DEX, no upsells.
> A piece of infrastructure the ecosystem can rely on.

## What it does

A multi-page public dashboard for the XRP Ledger:

- **Homepage** — network status, cold-storage total, AMM TVL, recent
  whale moves, top pools, most-traded tokens. Live panels swap in
  every 30s without a full page reload.
- **/whales** — large XRP transfers (≥100,000 XRP), labeled when known.
- **/pools** — every XRPL AMM pool (10,000+), ranked by TVL, with
  search by issuer or token.
- **/tokens** — issued tokens by trading activity.
- **/cold-storage** — Ripple escrow accounts and balances.
- **/wallet** — paste any XRPL address for balance, counterparties,
  and 30-day activity.
- **/health**, **/methodology**, **/about**, **/institutional**.

Every number is computed directly from a public XRPL node — no
aggregators, no third-party API wrappers. Hand-curated token and
account labels live in [`TOKEN_NAMES.md`](./TOKEN_NAMES.md) and
[`KNOWN_ACCOUNTS.md`](./KNOWN_ACCOUNTS.md), contributed via PR (same
pattern [mempool.space](https://mempool.space) uses for its mining-pool
list).

## Architecture

A long-running ingest worker (`xrpl_stream.py` + `rank_amms.py`)
subscribes to a public XRPL node and writes ledger snapshots to
Postgres (Neon). The Flask web service (`app.py`) reads those
snapshots and renders pages. Per-surface cache TTLs are documented on
[/methodology](https://xrpldashboard.com/methodology).

```
┌─────────────────┐    ws    ┌──────────────┐    write    ┌──────────┐
│ public XRPL node│ ───────▶ │ ingest worker│ ──────────▶ │ Postgres │
└─────────────────┘          └──────────────┘             └─────┬────┘
                                                                │ read
                                                                ▼
                                              ┌────────────────────────┐
                                              │ Flask web (Render)     │
                                              │ → xrpldashboard.com    │
                                              └────────────────────────┘
```

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
TL;DR: Render (web + worker) + Neon Postgres + Cloudflare DNS +
Plausible analytics + UptimeRobot.

## Repo layout

| File | What it does |
|---|---|
| `app.py` | Flask app — every public route, snapshot reads from PG |
| `xrpl_stream.py` | Long-running ingest worker (whales, AMM events) |
| `rank_amms.py` | Periodic AMM TVL snapshot for `/pools` |
| `db.py` | Postgres bridge — read/write helpers shared by web + worker |
| `wallet_data.py` | `/wallet` lookup pipeline (live read from XRPL) |
| `templates/` | Jinja templates for every page |
| `render.yaml` | Render infrastructure-as-code (web service + worker) |
| `TOKEN_NAMES.md` / `KNOWN_ACCOUNTS.md` | Curated label sources |
| `DEPLOY.md`, `NOTES.md`, `ROADMAP.md` | Docs |

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

MIT — see [LICENSE](./LICENSE). Free to fork, modify, and self-host.

## Disclaimer

Data shown is for educational and informational purposes only. Not
financial advice. Verify against [XRPSCAN](https://xrpscan.com) or
[Bithomp](https://bithomp.com) for additional information.

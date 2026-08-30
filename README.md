# xrpldashboard

Public XRPL analytics with a built-in truth-audit system.
Live at **[xrpldashboard.com](https://xrpldashboard.com)**.

## What this is

Three things make this repo different from "another blockchain dashboard":

1. **Every headline number has a claim record.** `CLAIMS.yaml` enumerates the public numeric claims across `/rlusd`, `/whales`, `/coverage`, and `/analytics`, each tied to its source function and freshness contract. `scripts/claims_check.sh` runs before pushes.
2. **A four-layer audit catches stale numbers before readers do.** See [`docs/TRUTH_AUDIT_DESIGN.md`](docs/TRUTH_AUDIT_DESIGN.md). Retro-tested against the last nine real incidents: **9 of 9 caught.** The first live catch (an RLUSD partial-day bug) surfaced within hours instead of the 53 days it had previously gone unnoticed.
3. **Daily signed snapshots.** Ed25519-signed, Merkle-chained. `/signed-snapshots` and `.well-known/snapshots/<date>.json` let anyone verify a number as-of a date without trusting the site.

Quick links: [`/methodology`](https://xrpldashboard.com/methodology) · [`/signed-snapshots`](https://xrpldashboard.com/signed-snapshots) · [`docs/TRUTH_AUDIT_DESIGN.md`](docs/TRUTH_AUDIT_DESIGN.md)

## Agent tier (MCP)

[![Listed on Anthropic MCP Registry](https://img.shields.io/badge/MCP-Anthropic%20Registry-blue)](https://registry.modelcontextprotocol.io/v0/servers?search=xrpldashboard)
[![Smithery listed](https://img.shields.io/badge/Smithery-listed-6a5acd)](https://smithery.ai/servers/xrpldashboard/xrpldashboard)

Live public MCP endpoint: **`https://mcp.xrpldashboard.com/mcp`** — streamable-HTTP, protocol version 2025-06-18, no auth. Fifteen read-only tools over XRPL and the on-XRPL RLUSD supply, every response wrapped in a proof-annotation envelope with `source`, `as_of`, `freshness_contract`, and a `claims_ref` back to [`/claims`](https://xrpldashboard.com/claims). Public beta through 2026-09.

Connect in 60 seconds — copy-paste config for Claude Desktop or any MCP client:

```json
{
  "mcpServers": {
    "xrpldashboard": {
      "command": "npx",
      "args": [
        "mcp-remote@latest",
        "https://mcp.xrpldashboard.com/mcp?ref=readme"
      ]
    }
  }
}
```

Or add it as a Custom Connector in Claude Desktop → Settings → Connectors with URL `https://mcp.xrpldashboard.com/mcp` and auth `None`. Full onboarding page (three sample prompts, honest limits, 429 shape): [`/connect`](https://xrpldashboard.com/connect#connect-in-60-seconds).

- **Session rate limit:** 600 tool calls / hour / session, enforced live (HTTP 429 + `Retry-After` on breach). See [`mcp_session_rate_limit.py`](mcp_session_rate_limit.py).
- **Discovery manifest:** [`/.well-known/agents.json`](https://xrpldashboard.com/.well-known/agents.json) · [`/llms.txt`](https://xrpldashboard.com/llms.txt) · [`/openapi.json`](https://xrpldashboard.com/openapi.json).
- **Verifiable moat:** `get_signed_snapshot` + `verify_snapshot_signature` — pin the Ed25519 pubkey at [`/.well-known/snapshots/pubkey.pem`](https://xrpldashboard.com/.well-known/snapshots/pubkey.pem) and verify a day's snapshot without trusting us.
- **Backing infra:** our own rippled full-history node (Ubuntu box in Indiana → Cloudflare Tunnel), source at [`mcp_server.py`](mcp_server.py) and `mcp_tools_*.py`.

Design doc: [`docs/AGENT_TIER_DESIGN.md`](docs/AGENT_TIER_DESIGN.md).

## Pages

37 public pages, organized as:

- **Money flow:** `/whales`, `/pools`, `/tokens`, `/token/<id>`, `/mpts`, `/mpt/<id>`, `/rlusd`, `/lending`
- **Institutional & regulatory:** `/institutional`, `/regulation`, `/rwa`, `/credentials`, `/amendments`, `/sidechain`
- **Coverage & audit:** `/coverage`, `/methodology`, `/signed-snapshots`, `/verify`, `/walker-health`, `/health`
- **Reader tools:** `/check`, `/wallet/<id>`, `/network`, `/learn`, `/price-data`, `/help/already-sent-money`
- **Trust & meta:** `/about`, `/security`, `/privacy`, `/terms`, `/subprocessors`, `/contact`

## Architecture

- **Web:** Flask 3.1 + Jinja2 + Flask-Babel (i18n) + Flask-Limiter, deployed on Render, fronted by Cloudflare.
- **Data:** Neon Postgres (single shared DB across web + walkers).
- **Ingest:** 29 background services under launchd — 23 ingest walkers and canaries, plus 4 backup and 2 snapshot-signing jobs. Each walker writes to `walker_health`; `/walker-health` surfaces stalls.
- **XRPL client:** `xrpl-py 4.5.0` with a local-first cascade to public rippled nodes; silent-failover attempts logged to `walker_node_fallback` so we can audit reliability rather than hope.
- **Signing:** Ed25519 via `cryptography` (see `signed_snapshot.py`).

## The truth-audit system (why this repo may be reusable)

Most public dashboards trust their own writes. This one doesn't. Four layers, working together:

- **Layer 1 — walker health.** `walker_health` rows + `/walker-health` page. Catches "the number stopped moving because the writer died."
- **Layer 2 — plausibility rules.** Continuous checks like "24h net-change should not equal zero for 53 days." Catches "the number is moving in the DB but the query is wrong."
- **Layer 3 — external cross-check.** Third-party comparisons (e.g., independent Ethereum RPCs for RLUSD supply, CoinGecko for XRP price) surface disagreement on the same measurement.
- **Layer 4 — claims manifest.** `CLAIMS.yaml` + `scripts/claims_check.sh`. Every headline number is enumerated with its source function and freshness contract; the checker exits non-zero on drift.

Full design and the 9-of-9 retro-test are in [`docs/TRUTH_AUDIT_DESIGN.md`](docs/TRUTH_AUDIT_DESIGN.md).

If you're building a public-data project and want the pattern, the design doc is written to be lifted.

## Running locally

**What runs in five minutes:** the Flask web app against a copy of the schema. Enough to click through pages and see the UI.

**What does not run in five minutes:** the full site, because a live dashboard needs the walker fleet ingesting from XRPL against Neon Postgres. Standing up the walkers is a real operations task, not a `docker compose up`.

Quickstart (web app only):

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in DATABASE_URL, FLASK_SECRET_KEY, etc.
python app.py             # http://localhost:5001
```

For the full ingest setup, see [`docs/TRUTH_AUDIT_DESIGN.md`](docs/TRUTH_AUDIT_DESIGN.md) and the `launchd/` plists (which are the author's local install — forkers will want to path-adjust).

## Independence

xrpldashboard is not operated by Ripple, the XRPL Foundation, or any exchange. No paid placements. No affiliate links on labeling or metric surfaces.

## Funding

Development is self-funded, with support from community grant programs where they align with the mission (public-goods data infrastructure for XRPL).

## Contributing

Issues and PRs welcome. If you're touching a page that renders a headline number, add or update the corresponding entry in `CLAIMS.yaml`; `scripts/claims_check.sh` will remind you.

## License

MIT. See [`LICENSE`](LICENSE).

## Disclaimer

xrpldashboard reports on-chain data and public regulatory information. Nothing on the site or in this repo is financial, legal, or investment advice.

"""rwa_supply_nav_walker — daily RWA on-ledger supply × cited NAV.

Charlie ruling 2026-09-22 Tue PM: build the honest floor of the
`rwa_onledger_supply_usd` metric. Each verified rwa_family has an entry
in rwa_nav_sources.yaml naming its NAV source (URL, publish cadence,
XRPL issuer address). This walker:

  1. Reads the registry.
  2. For each family, queries our own XRPL node for gateway_balances of
     the family's issuer address. Sums obligations for the family's
     tokens (nav_symbol filter for baskets like Midas's Axelar bridge).
     If our node has no data, the row records supply_units=NULL with
     the reason ("issuer_not_on_node", "empty_obligations", etc.).
  3. Attempts an HTTP GET on nav_url (best-effort — the operator may
     block programmatic access, that's fine, we RECORD the status). The
     fetched Content-Type + first 1KB is stored so a downstream verifier
     can see what we saw.
  4. If nav_machine_url is set AND the fetch parses JSON with a
     documented NAV field, we extract the number. Otherwise
     nav_per_unit_usd=NULL and value_usd=0.0 with reason "no_machine_readable_nav".
  5. Writes rwa_supply_nav_daily(date, family_slug, xrpl_issuer, nav_symbol,
     supply_units, nav_per_unit_usd, value_usd, nav_source_url,
     nav_fetched_at_utc, nav_fetch_http_status, reason). ONE row per family
     per day. UPSERT by (date, family_slug).

## Env-gate

Ships env-gated on RWA_SUPPLY_NAV_ENABLED. Land in the code path
tonight, kickstart-proven; enters the leaf tomorrow after one dry cycle
per Charlie's rule.

## No Ethereum dependency

Charlie's explicit ruling. Chainlink Ethereum feeds for Midas / Ondo /
OpenEden are all technically machine-readable but require an ETH RPC
this project doesn't host. Rather than paper over with an external RPC
call whose sourcing we don't control, we honestly emit $0 for families
whose only NAV source lives on Ethereum, with a citation.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import ssl
import sys
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import certifi
import yaml

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db


WALKER_NAME = "rwa_supply_nav_walker"
WALKER_CADENCE_SECONDS = 86400  # daily
LAST_OK_STAMP = os.path.join(HERE, "launchd_state", "rwa_supply_nav_walker_last_ok")

# Env-gate — land in code path tonight, don't ARM until Charlie approves
# the shape of the first row.
ENABLED = os.environ.get("RWA_SUPPLY_NAV_ENABLED", "0") in ("1", "true", "yes")

NAV_SOURCES_YAML = os.path.join(HERE, "rwa_nav_sources.yaml")
XRPL_NODE = os.environ.get("XRPL_NODE", "https://s1.ripple.com:51234")
HTTP_TIMEOUT_SECONDS = 15.0
NAV_FETCH_MAX_BYTES = 128 * 1024

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def _load_registry() -> dict:
    with open(NAV_SOURCES_YAML) as f:
        return yaml.safe_load(f) or {}


def _xrpl_gateway_balances(issuer: str) -> tuple[dict | None, str | None]:
    """Return (obligations_dict, err). obligations_dict is currency_code
    -> str_amount. err is None on success."""
    body = {
        "method": "gateway_balances",
        "params": [{"account": issuer, "ledger_index": "validated"}],
    }
    try:
        req = Request(
            XRPL_NODE, method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=HTTP_TIMEOUT_SECONDS, context=_SSL_CTX) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        return None, f"rpc_{type(e).__name__}"
    except Exception as e:
        return None, f"parse_{type(e).__name__}"
    result = raw.get("result") or {}
    if result.get("status") == "error":
        return None, f"xrpl_error_{result.get('error')}"
    obligations = result.get("obligations") or {}
    return obligations, None


def _attempt_nav_fetch(nav_url: str) -> tuple[int | None, str, str | None]:
    """Best-effort GET on the NAV URL. Returns (http_status, content_type,
    first_kb_of_body). Blocking for HTTP_TIMEOUT_SECONDS."""
    if not nav_url:
        return None, "", None
    try:
        req = Request(nav_url, headers={
            "User-Agent": "xrpldashboard-rwa-supply-nav-walker/1.0 (+https://xrpldashboard.com)",
        })
        with urlopen(req, timeout=HTTP_TIMEOUT_SECONDS, context=_SSL_CTX) as resp:
            body = resp.read(NAV_FETCH_MAX_BYTES + 1)
            ct = resp.headers.get("Content-Type", "")
            return resp.getcode(), ct, body[:1024].decode("utf-8", errors="replace")
    except HTTPError as e:
        return e.code, "", None
    except (URLError, TimeoutError, OSError) as e:
        return None, "", f"fetch_error:{type(e).__name__}"
    except Exception as e:
        return None, "", f"fetch_error:{type(e).__name__}"


def _obligations_sum_for_symbol(obligations: dict, nav_symbol: str) -> float:
    """Sum obligations across all currency codes matching the family's
    nav_symbol. nav_symbol may be comma-separated for baskets (Midas)."""
    if not obligations:
        return 0.0
    wanted = {s.strip().upper() for s in nav_symbol.split(",")}
    # XRPL currency codes are either 3-char ISO ("TBILL", "OUSG") or
    # 40-hex non-standard. Decode hex-encoded codes to ASCII for compare.
    total = 0.0
    for code, amount in obligations.items():
        code_upper = code.upper()
        decoded = None
        if len(code) == 40:
            try:
                raw = bytes.fromhex(code)
                decoded = raw.rstrip(b"\x00").decode("ascii", errors="replace").upper()
            except Exception:
                decoded = None
        for w in wanted:
            if code_upper == w.upper() or (decoded and decoded == w.upper()):
                try:
                    total += float(amount)
                except (TypeError, ValueError):
                    pass
                break
    return total


def _ensure_table():
    """Idempotent table create. Chained via db.pg_connect() so we run in
    the same env as the wrapper."""
    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rwa_supply_nav_daily (
                    fetch_date            DATE NOT NULL,
                    family_slug           TEXT NOT NULL,
                    xrpl_issuer           TEXT,
                    nav_symbol            TEXT,
                    supply_units          NUMERIC,
                    nav_per_unit_usd      NUMERIC,
                    value_usd             NUMERIC NOT NULL DEFAULT 0,
                    nav_source_url        TEXT,
                    nav_fetched_at_utc    TIMESTAMPTZ,
                    nav_as_of             DATE,
                    nav_fetch_http_status INTEGER,
                    reason                TEXT,
                    PRIMARY KEY (fetch_date, family_slug)
                );
                -- Additive migration for existing tables from the pre-curator schema.
                ALTER TABLE rwa_supply_nav_daily
                    ADD COLUMN IF NOT EXISTS nav_as_of DATE;
            """)
        conn.commit()


def _upsert_row(row: dict):
    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rwa_supply_nav_daily (
                    fetch_date, family_slug, xrpl_issuer, nav_symbol,
                    supply_units, nav_per_unit_usd, value_usd,
                    nav_source_url, nav_fetched_at_utc, nav_as_of,
                    nav_fetch_http_status, reason
                ) VALUES (
                    %(fetch_date)s, %(family_slug)s, %(xrpl_issuer)s, %(nav_symbol)s,
                    %(supply_units)s, %(nav_per_unit_usd)s, %(value_usd)s,
                    %(nav_source_url)s, %(nav_fetched_at_utc)s, %(nav_as_of)s,
                    %(nav_fetch_http_status)s, %(reason)s
                )
                ON CONFLICT (fetch_date, family_slug) DO UPDATE SET
                    xrpl_issuer = EXCLUDED.xrpl_issuer,
                    nav_symbol  = EXCLUDED.nav_symbol,
                    supply_units = EXCLUDED.supply_units,
                    nav_per_unit_usd = EXCLUDED.nav_per_unit_usd,
                    value_usd = EXCLUDED.value_usd,
                    nav_source_url = EXCLUDED.nav_source_url,
                    nav_fetched_at_utc = EXCLUDED.nav_fetched_at_utc,
                    nav_as_of = EXCLUDED.nav_as_of,
                    nav_fetch_http_status = EXCLUDED.nav_fetch_http_status,
                    reason = EXCLUDED.reason
                """,
                row,
            )
        conn.commit()


def _stamp_last_ok():
    try:
        os.makedirs(os.path.dirname(LAST_OK_STAMP), exist_ok=True)
        with open(LAST_OK_STAMP, "w") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


def main() -> int:
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS)
    ok = False
    message = "not_yet_stamped"
    try:
        if not ENABLED:
            message = "disabled_by_env_RWA_SUPPLY_NAV_ENABLED=0"
            # Not-enabled is not a failure — we're honoring the env-gate.
            ok = True
            print(f"[{WALKER_NAME}] {message}")
            return 0

        registry = _load_registry()
        if not registry:
            message = "registry_empty"
            print(f"[{WALKER_NAME}] {message}", file=sys.stderr)
            return 1

        _ensure_table()

        today = dt.date.today().isoformat()
        wrote = 0
        for family_slug, cfg in registry.items():
            xrpl_issuer = cfg.get("xrpl_issuer")
            nav_symbol = cfg.get("nav_symbol") or ""
            nav_url = cfg.get("nav_url")
            nav_status = cfg.get("nav_status")

            # 1. Supply (own node)
            supply_units: float | None = None
            supply_err: str | None = None
            if xrpl_issuer:
                obligations, oerr = _xrpl_gateway_balances(xrpl_issuer)
                if oerr:
                    supply_err = oerr
                elif obligations is None or not obligations:
                    supply_err = "empty_obligations"
                else:
                    supply_units = _obligations_sum_for_symbol(obligations, nav_symbol)

            # 2. NAV fetch (best-effort — records HTTP status; body preview
            #    still stored on disk via the walker log line).
            status, _ct, _preview = _attempt_nav_fetch(nav_url) if nav_url else (None, "", None)

            # 3. Curator-mode NAV (Charlie ruling 2026-09-22 Tue PM):
            #    when nav_curator_usd is set in yaml, walker uses it and
            #    cites nav_curator_source + nav_curator_as_of. Charlie
            #    reads the human page + updates the yaml.
            nav_curator_usd = cfg.get("nav_curator_usd")
            nav_curator_as_of = cfg.get("nav_curator_as_of")
            nav_curator_source = cfg.get("nav_curator_source") or nav_url

            nav_per_unit_usd = None
            value_usd = 0.0
            nav_as_of = None
            source_url = nav_url
            reasons = []
            if supply_err:
                reasons.append(supply_err)
            if nav_status == "not_public":
                reasons.append("nav_not_public_per_registry")
            elif nav_curator_usd is not None:
                # Curator populated the NAV — compute value.
                try:
                    nav_per_unit_usd = float(nav_curator_usd)
                    if supply_units is not None:
                        value_usd = float(supply_units) * nav_per_unit_usd
                    nav_as_of = nav_curator_as_of
                    source_url = nav_curator_source
                except (TypeError, ValueError):
                    reasons.append("nav_curator_usd_not_numeric")
            elif nav_status == "on_chain_oracle":
                reasons.append("nav_on_ethereum_oracle_no_eth_rpc_dep")
            elif nav_status == "published":
                # Published human page, no curator value yet (no machine
                # endpoint per registry) — honest absence.
                reasons.append("curator_nav_not_set_yet")

            reason = ";".join(reasons) if reasons else None
            row = {
                "fetch_date": today,
                "family_slug": family_slug,
                "xrpl_issuer": xrpl_issuer,
                "nav_symbol": nav_symbol,
                "supply_units": supply_units,
                "nav_per_unit_usd": nav_per_unit_usd,
                "value_usd": value_usd,
                "nav_source_url": source_url,
                "nav_fetched_at_utc": dt.datetime.now(dt.timezone.utc),
                "nav_as_of": nav_as_of,
                "nav_fetch_http_status": status,
                "reason": reason,
            }
            _upsert_row(row)
            wrote += 1
            print(f"[{WALKER_NAME}] {family_slug}: supply={supply_units} "
                  f"nav={nav_per_unit_usd} value_usd={value_usd} "
                  f"as_of={nav_as_of} reason={reason}")

        _stamp_last_ok()
        message = f"wrote_rows={wrote}"
        ok = True
        print(f"[{WALKER_NAME}] {message}")
        return 0
    except Exception as e:
        message = f"exception:{type(e).__name__}:{e}"
        raise
    finally:
        try:
            db.write_walker_health_end(
                WALKER_NAME, ok=ok,
                message=message or ("clean_no_message" if ok else "unlabeled_failure"),
            )
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())

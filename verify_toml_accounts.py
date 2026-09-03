"""verify_toml_accounts.py — first-party XRPL identity scanner.

Promotes XRPL accounts to `named_accounts.json` only when ownership is
provable via the XRPL community standard: an `xrp-ledger.toml` file
served from a domain the entity controls, listing the address in its
[[ACCOUNTS]] section.

Two complementary discovery modes run in one pass:

    1) ORG MODE — primary
       For every domain in `xrpl_org_domains.json`, fetch its toml and
       promote every address listed in [[ACCOUNTS]]. This is how Ripple,
       XRPL Labs, etc. publish their accounts. The toml URL is the proof,
       so we use it as `verified_via`.

    2) ON-CHAIN MODE — secondary
       For active accounts in events.db (and any --extra), read the
       on-chain `Domain` field. If it decodes to a hostname AND that
       hostname's toml lists the address back, promote it. This is the
       symmetric proof and catches new orgs without needing them in the
       seed list.

Both modes write to `named_accounts.json` with the toml URL as
`verified_via` — the same provenance discipline as existing entries.

Usage:
    python verify_toml_accounts.py            # full scan + auto-promote
    python verify_toml_accounts.py --dry-run  # report only, no writes
    python verify_toml_accounts.py --extra rABC,rDEF
    python verify_toml_accounts.py --org-only --account-only  # mode toggles
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import tomllib
from urllib.parse import urlparse

import ssl
import urllib.request
import urllib.error

import certifi

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountInfo

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())


HERE = os.path.abspath(os.path.dirname(__file__))
EVENTS_DB_PATH = os.path.join(HERE, "events.db")
NAMED_ACCOUNTS_PATH = os.path.join(HERE, "named_accounts.json")
ORG_DOMAINS_PATH = os.path.join(HERE, "xrpl_org_domains.json")
# Cursor for on-chain mode: last address processed in the previous run.
# Next run resumes from the address alphabetically after this one.
# When the whole active-address set has been walked, cursor resets to
# empty and the next run starts over. Wired 2026-09-04 so a --limit N
# bounded run at a weekly cadence still visits the full set eventually
# instead of restarting from 'r...' every week.
CURSOR_PATH = os.path.join(HERE, "launchd_state", "verify_toml_cursor.json")
LOG_PATH = os.path.join(HERE, "verify_toml.log")

# Prefer LAN Lenovo rippled (XRPL_LOCAL_NODE, matches xrpl_client.py
# convention) so this walker doesn't spam Ripple's public s1/s2 with
# ~thousands of account_info calls per run. Fall through to the older
# XRPL_RPC env var for backwards compatibility, then public s1 as a
# last resort — but the wrapper sources ~/.config/xrpldashboard/env
# which sets XRPL_LOCAL_NODE, so the public URL should never be hit
# in practice. Wired 2026-09-04 with the cursor + bounded-run rework.
XRPL_RPC = (
    os.environ.get("XRPL_LOCAL_NODE")
    or os.environ.get("XRPL_RPC")
    or "https://s1.ripple.com:51234"
)
HTTP_TIMEOUT = 10
USER_AGENT = "xrpldashboard-toml-verifier/1.0 (+https://xrpldashboard.com)"
WALKER_CADENCE_SECONDS = 604800  # StartInterval in com.xrpldashboard.verify_toml.plist (weekly)

# Strict hostname gate applied before any HTTP fetch. RFC 1035 label shape:
# lowercase alnum, internal hyphens allowed but no leading/trailing hyphen,
# each label 1-63 chars, at least one dot, total length 4-253. Rejects
# IP addresses, paths, schemes, ports, IDN punycode-as-input, whitespace,
# and the on-chain `Domain` field accidents (control bytes, mojibake) that
# slip past minimal ASCII decoding.
DOMAIN_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)


def _is_safe_domain(d) -> bool:
    if not isinstance(d, str):
        return False
    d = d.strip().lower()
    if not (4 <= len(d) <= 253 and DOMAIN_RE.match(d)):
        return False
    # Reject IPv4 literals (last label all-digits is never a valid TLD
    # under IANA policy). Without this, the regex accepts "192.168.1.1"
    # and an attacker-controlled on-chain Domain field could SSRF to
    # internal infra via https://<rfc1918>/.well-known/xrp-ledger.toml.
    if d.rsplit(".", 1)[-1].isdigit():
        return False
    return True


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save_named(data: dict) -> None:
    with open(NAMED_ACCOUNTS_PATH, "w") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")


def candidates_mpt_issuers() -> list[str]:
    """MPT issuance issuer wallets — read from `mpt_snapshot` JSONB
    (mirror of mpt_snapshot.json). Returns dedup'd issuer addresses in
    snapshot order so output is reproducible run-to-run."""
    try:
        import db
    except Exception as e:
        log(f"WARN candidates_mpt_issuers: db import failed: {e}")
        return []
    try:
        snap = db.read_mpt_snapshot()
    except Exception as e:
        log(f"WARN candidates_mpt_issuers: read_mpt_snapshot failed: {e}")
        return []
    if not snap:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for r in (snap.get("issuances") or []):
        addr = r.get("issuer")
        if addr and addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def candidates_token_issuers(top_n: int) -> list[str]:
    """Top-N trust-line token issuers by recent trade count — read from
    `token_volume` aggregates (Postgres-first, what /tokens already
    ranks). Returns issuer addresses, deduped, in trade-count DESC order."""
    try:
        import db
    except Exception as e:
        log(f"WARN candidates_token_issuers: db import failed: {e}")
        return []
    try:
        rows = db.read_token_volume_aggregates(hours_back=None, limit=top_n)
    except Exception as e:
        log(f"WARN candidates_token_issuers: read_token_volume_aggregates "
            f"failed: {e}")
        return []
    out: list[str] = []
    seen: set[str] = set()
    for cur, iss, _trades, _hours in rows:
        if iss and iss not in seen:
            seen.add(iss)
            out.append(iss)
    return out


def filter_curated(addrs: list[str]) -> list[str]:
    """Skip addresses that already have a non-derived `account_labels`
    row (manual / xrpscan / toml — curated layers we don't want to
    overwrite or waste RPC re-checking). Pass-through if db is
    unavailable so org/on-chain runs aren't blocked offline."""
    if not addrs:
        return addrs
    try:
        import db
    except Exception as e:
        log(f"WARN filter_curated: db import failed ({e}); "
            f"pass-through with {len(addrs)} candidate(s)")
        return addrs
    try:
        labeled = db.read_account_labels(addrs)
    except Exception as e:
        log(f"WARN filter_curated: read_account_labels failed ({e}); "
            f"pass-through with {len(addrs)} candidate(s)")
        return addrs
    kept = [
        a for a in addrs
        if not (labeled.get(a) and not (
            labeled[a].get("source", "").startswith("derived:")
        ))
    ]
    skipped = len(addrs) - len(kept)
    if skipped:
        log(f"  curated-skip: {skipped}/{len(addrs)} candidate(s) already "
            f"labeled (non-derived); use --force-recheck to override")
    return kept


def _install_timeout_handler(walker_name: str) -> None:
    """Install SIGALRM handler so a wrapper-alarm timeout writes a clean
    walker_health_end row (ok=false, message=timeout) instead of the
    Python process dying mid-address and leaving walker_health showing
    the started-but-never-ended state the pre-2026-09-04 stuck runs
    exhibited. Best-effort DB write; cursor persistence still happens
    every 500 addresses inside scan_account_mode."""
    import signal
    try:
        import db as _wh_db
    except Exception:
        _wh_db = None

    def _handler(signum, frame):
        try:
            log(f"SIGALRM (signum={signum}) — wrapper timeout, writing walker_health_end and exiting")
            if _wh_db is not None:
                _wh_db.write_walker_health_end(
                    walker_name, ok=False,
                    message="wrapper SIGALRM timeout — partial run; cursor persisted",
                )
        finally:
            os._exit(124)  # POSIX 124 = timeout convention

    try:
        signal.signal(signal.SIGALRM, _handler)
    except (ValueError, OSError) as e:
        log(f"WARN could not install SIGALRM handler: {e}")


def _load_cursor_dict() -> dict:
    """Return the full cursor dict (last_address, wrapped, last_new_scan_ts,
    updated_at) or {} when unreadable. Never raises."""
    if not os.path.exists(CURSOR_PATH):
        return {}
    try:
        with open(CURSOR_PATH) as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_cursor() -> str:
    """Back-compat wrapper — return just last_address."""
    return str(_load_cursor_dict().get("last_address") or "")


def _save_cursor(
    last_address: str,
    wrapped: bool = False,
    new_scan_ts: int | None = None,
) -> None:
    """Persist the cursor to disk atomically. `wrapped=True` when the
    walker finished the tail of the address set and reset. If
    `new_scan_ts` is None the previous last_new_scan_ts value is
    preserved (Phase B mid-run saves must not clobber Phase A's HWM);
    pass an explicit int to advance the high-water mark after a Phase A
    completes within budget."""
    existing = _load_cursor_dict()
    payload = {
        "last_address": last_address,
        "wrapped": wrapped,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if new_scan_ts is not None:
        payload["last_new_scan_ts"] = int(new_scan_ts)
    elif "last_new_scan_ts" in existing:
        payload["last_new_scan_ts"] = existing["last_new_scan_ts"]
    try:
        os.makedirs(os.path.dirname(CURSOR_PATH), exist_ok=True)
        tmp = CURSOR_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, CURSOR_PATH)
    except OSError as e:
        log(f"WARN cursor save failed: {e}")


def active_addresses() -> set[str]:
    """Unique from_addr/to_addr seen in events.db."""
    seen: set[str] = set()
    if not os.path.exists(EVENTS_DB_PATH):
        return seen
    try:
        conn = sqlite3.connect(f"file:{EVENTS_DB_PATH}?mode=ro", uri=True)
        for from_a, to_a in conn.execute(
            "SELECT DISTINCT from_addr, to_addr FROM events"
        ):
            if from_a:
                seen.add(from_a)
            if to_a:
                seen.add(to_a)
        conn.close()
    except Exception as e:
        log(f"WARN events.db read failed: {e}")
    return seen


# Phase A cap: addresses first-seen since last scan are the ONLY set with
# any real chance of having gained a fresh Domain field. Cap at 5000 so a
# busy week's overflow rolls into the next weekly run instead of starving
# Phase B (cursor walk through the old set). Wired 2026-09-04 rework.
PHASE_A_CAP = 5000


def _events_db_max_ts() -> int | None:
    """Max(ts) across the events table, or None if empty/unreadable."""
    if not os.path.exists(EVENTS_DB_PATH):
        return None
    try:
        conn = sqlite3.connect(f"file:{EVENTS_DB_PATH}?mode=ro", uri=True)
        row = conn.execute("SELECT MAX(ts) FROM events").fetchone()
        conn.close()
        return int(row[0]) if row and row[0] is not None else None
    except Exception as e:
        log(f"WARN events.db max(ts) read failed: {e}")
        return None


def _phase_a_first_seen_since(hwm_ts: int) -> tuple[list[str], int]:
    """Return (sorted new addresses whose earliest events.db appearance
    is strictly after `hwm_ts`, max_ts observed).

    Single full-table scan of events.db is O(rows) but avoids a nested
    NOT-EXISTS query; events.db is capped by retention and stays in
    the low-hundreds-of-MB range in practice.
    """
    if not os.path.exists(EVENTS_DB_PATH):
        return [], hwm_ts
    prior: set[str] = set()
    recent: set[str] = set()
    max_ts = hwm_ts
    try:
        conn = sqlite3.connect(f"file:{EVENTS_DB_PATH}?mode=ro", uri=True)
        # Index events_ts_idx exists — use it to split the scan.
        # Old set first (bounds recent-set exclusion).
        for from_a, to_a in conn.execute(
            "SELECT DISTINCT from_addr, to_addr FROM events WHERE ts <= ?",
            (hwm_ts,),
        ):
            if from_a:
                prior.add(from_a)
            if to_a:
                prior.add(to_a)
        for from_a, to_a, ts in conn.execute(
            "SELECT from_addr, to_addr, ts FROM events WHERE ts > ?",
            (hwm_ts,),
        ):
            if ts and ts > max_ts:
                max_ts = int(ts)
            if from_a:
                recent.add(from_a)
            if to_a:
                recent.add(to_a)
        conn.close()
    except Exception as e:
        log(f"WARN events.db Phase A read failed: {e}")
        return [], hwm_ts
    new_addrs = sorted(recent - prior)
    return new_addrs, max_ts


def fetch_domain_field(client: JsonRpcClient, address: str) -> str | None:
    """Read on-chain `Domain` field, return decoded ASCII or None."""
    try:
        r = client.request(AccountInfo(account=address))
        domain_hex = (r.result.get("account_data") or {}).get("Domain")
        if not domain_hex:
            return None
        try:
            decoded = bytes.fromhex(domain_hex).decode("ascii", errors="strict")
        except (ValueError, UnicodeDecodeError):
            return None
        decoded = decoded.strip().lower()
        if not _is_safe_domain(decoded):
            return None
        return decoded
    except Exception:
        return None


def fetch_toml(domain: str) -> tuple[str, dict | None, str | None]:
    """Fetch domain's xrp-ledger.toml. Returns (final_url, parsed, error_reason)."""
    if not _is_safe_domain(domain):
        return f"https://{domain}/.well-known/xrp-ledger.toml", None, (
            "rejected by domain regex gate"
        )
    url = f"https://{domain}/.well-known/xrp-ledger.toml"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            body = resp.read(512_000)
            final_url = resp.geturl()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        return url, None, f"fetch failed: {e}"
    except Exception as e:
        return url, None, f"unexpected fetch error: {e}"

    # Refuse if the redirect chain leaves the original host family
    final_host = urlparse(final_url).netloc.split(":")[0].lower()
    if not (final_host == domain or final_host.endswith("." + domain)
            or domain.endswith("." + final_host) or final_host == "www." + domain):
        return final_url, None, f"redirected off-host to {final_host}"

    # Refuse HTML masquerading as toml (200 OK with login page or app shell)
    head = body[:512].lstrip().lower()
    if (head.startswith(b"<!doctype") or head.startswith(b"<html")
            or "text/html" in ctype):
        return final_url, None, "served HTML, not toml"

    try:
        parsed = tomllib.loads(body.decode("utf-8", errors="replace"))
    except tomllib.TOMLDecodeError as e:
        return final_url, None, f"toml parse error: {e}"
    return final_url, parsed, None


def address_in_toml(toml_data: dict, address: str) -> bool:
    accounts = toml_data.get("ACCOUNTS")
    if not isinstance(accounts, list):
        return False
    return any(
        isinstance(b, dict) and b.get("address") == address for b in accounts
    )


def derive_name(toml_data: dict, domain: str, account_block: dict | None) -> str:
    if account_block:
        for key in ("name", "desc"):
            v = (account_block.get(key) or "").strip()
            if v:
                return v
    org_name = ((toml_data.get("ORGANIZATION") or {}).get("name") or "").strip()
    return org_name or domain


def categorize(toml_data: dict, default: str | None) -> str:
    if default:
        return default
    name = ((toml_data.get("ORGANIZATION") or {}).get("name") or "").lower()
    if "exchange" in name or "gateway" in name:
        return "exchange"
    if "validator" in name:
        return "validator"
    return "other"


def _write_account_label(
    addr: str, entry: dict, mode_tag: str, dry_run: bool,
) -> None:
    """Dual-write: mirror a successfully TOML-attested entry into the
    Postgres account_labels table so it surfaces on /pools brand-warn,
    /mpt attestation badges, /rwa promotion logic, etc. Idempotent via
    the db.py ON CONFLICT path; failure here logs and continues so the
    named_accounts.json write path isn't blocked by a DB hiccup."""
    if dry_run:
        return
    try:
        import db
    except Exception as e:
        log(f"  [{mode_tag}] {addr}: account_labels dual-write skipped "
            f"(db import failed: {e})")
        return
    # Pick a category that downstream queries can target. app.py:1967
    # uses `source='toml' AND category='mpt_issuer'` for the /rwa MPT
    # attestation panel — match that for the mpt-issuers mode.
    category = entry.get("category") or "other"
    if mode_tag == "mpt-issuers":
        category = "mpt_issuer"
    elif mode_tag == "token-issuers":
        category = "token_issuer"
    via = entry.get("verified_via", "")
    domain = None
    try:
        netloc = urlparse(via).netloc
        if netloc:
            domain = netloc.split(":")[0].lower()
    except Exception:
        pass
    extra = {
        "domain": domain,
        "verified_via": via,
        "verified_at_unix": int(time.time()),
        "mode": mode_tag,
    }
    ok = db.upsert_account_label(
        address=addr,
        name=entry.get("name") or addr,
        source="toml",
        category=category,
        confidence=0.95,
        extra=extra,
    )
    if ok:
        log(f"  [{mode_tag}] {addr}: account_labels dual-write OK "
            f"(source=toml category={category})")
    else:
        log(f"  [{mode_tag}] {addr}: account_labels dual-write FAILED")


def upsert(named: dict, address: str, entry_new: dict, dry_run: bool) -> str:
    """Insert or refresh. Return one of {'new', 'refreshed', 'noop'}."""
    existing = named.get(address)
    if not existing:
        if not dry_run:
            named[address] = entry_new
        return "new"
    # Refresh URL only if it changed
    if existing.get("verified_via") != entry_new["verified_via"]:
        if not dry_run:
            existing["verified_via"] = entry_new["verified_via"]
            named[address] = existing
        return "refreshed"
    return "noop"


# ─── ORG MODE ─────────────────────────────────────────────────────────────

def scan_org_mode(named: dict, dry_run: bool) -> tuple[int, int]:
    cfg = load_json(ORG_DOMAINS_PATH, {"domains": {}})
    domains = cfg.get("domains", {})
    if not domains:
        log("org mode: no domains in xrpl_org_domains.json — skipping")
        return 0, 0

    log(f"org mode: scanning {len(domains)} domain(s)")
    new_count = refreshed_count = 0

    for domain, meta in domains.items():
        final_url, toml_data, err = fetch_toml(domain)
        if toml_data is None:
            log(f"  {domain}: SKIP — {err}")
            continue

        # Two address-bearing toml sections per the XRPL standard:
        #   [[ACCOUNTS]] — first-party operational accounts
        #   [[TOKENS]]   — token issuer accounts (`issuer` field)
        accounts = toml_data.get("ACCOUNTS") or []
        tokens = toml_data.get("TOKENS") or []
        log(f"  {domain}: toml ok — {len(accounts)} account(s), "
            f"{len(tokens)} token(s)")

        # Process ACCOUNTS
        for block in accounts:
            if not isinstance(block, dict):
                continue
            addr = block.get("address")
            if not addr:
                continue
            entry = {
                "name": derive_name(toml_data, domain, block),
                "category": categorize(toml_data, meta.get("default_category")),
                "verified_via": final_url,
                "_note": (
                    f"Auto-discovered via {final_url} — domain owner "
                    f"({domain}) lists this address in its xrp-ledger.toml "
                    f"[[ACCOUNTS]] section."
                ),
            }
            outcome = upsert(named, addr, entry, dry_run)
            if outcome == "new":
                new_count += 1
                log(f"    NEW account {addr} → {entry['name']}")
            elif outcome == "refreshed":
                refreshed_count += 1
                log(f"    refreshed source for {addr}")
            _write_account_label(addr, entry, "org", dry_run)

        # Process TOKENS (issuer addresses)
        for block in tokens:
            if not isinstance(block, dict):
                continue
            addr = block.get("issuer")
            if not addr:
                continue
            tk_name = block.get("name") or block.get("currency") or "?"
            entry = {
                "name": f"{((toml_data.get('ORGANIZATION') or {}).get('name') or domain)} ({tk_name} issuer)",
                "category": "issuer",
                "verified_via": final_url,
                "_note": (
                    f"Auto-discovered via {final_url} — domain owner "
                    f"({domain}) lists this address in its xrp-ledger.toml "
                    f"[[TOKENS]] section as the {tk_name} issuer."
                ),
            }
            outcome = upsert(named, addr, entry, dry_run)
            if outcome == "new":
                new_count += 1
                log(f"    NEW issuer {addr} → {entry['name']}")
            elif outcome == "refreshed":
                refreshed_count += 1
                log(f"    refreshed source for issuer {addr}")
            _write_account_label(addr, entry, "org", dry_run)

    return new_count, refreshed_count


# ─── ON-CHAIN MODE ────────────────────────────────────────────────────────

def scan_account_mode(
    candidates: list[str], named: dict, dry_run: bool,
    mode_tag: str = "on-chain",
) -> tuple[int, int]:
    """Walk each candidate via on-chain `Domain` field → TOML two-way
    proof. Same verification path for every candidate source; the only
    difference is the `mode_tag` printed in log lines so a multi-mode
    --all run is grep-able."""
    log(f"[{mode_tag}] probing {len(candidates)} account(s) for Domain field "
        f"(XRPL_RPC={XRPL_RPC})")
    if not candidates:
        return 0, 0
    client = JsonRpcClient(XRPL_RPC)
    new_count = refreshed_count = 0
    no_domain_count = 0
    error_count = 0
    unverifiable_count = 0
    last_processed = ""
    # Slimmed logging (2026-09-04): NO_DOMAIN/UNVERIFIABLE/ERROR were the
    # bulk of the previous 250MB .out.log noise (~99.9% of iterations).
    # Track them as counters and emit periodic progress lines instead.
    PROGRESS_EVERY = 500
    # Throttle: 0.02s per address (was 0.15s) since we're on LAN rippled
    # now, not polite-pace public. LAN can absorb 50 req/s from one client.
    THROTTLE_S = 0.02

    for i, addr in enumerate(candidates, 1):
        last_processed = addr
        domain = fetch_domain_field(client, addr)
        if not domain:
            no_domain_count += 1
        elif True:
            final_url, toml_data, err = fetch_toml(domain)
            if toml_data is None:
                error_count += 1
                # Domain-fetch errors are rare enough to keep logging
                log(f"  [{mode_tag}] {addr}: ERROR domain={domain} → toml unusable ({err})")
            elif not address_in_toml(toml_data, addr):
                unverifiable_count += 1
                # Unverifiable domains are typically parked/unrelated; count-only
            else:
                block = next(
                    (b for b in toml_data.get("ACCOUNTS", [])
                     if isinstance(b, dict) and b.get("address") == addr),
                    None,
                )
                entry = {
                    "name": derive_name(toml_data, domain, block),
                    "category": categorize(toml_data, None),
                    "verified_via": final_url,
                    "_note": (
                        f"Auto-verified via two-way ownership proof: on-chain "
                        f"`Domain` field decodes to {domain}, and {final_url} lists "
                        f"this address in its [[ACCOUNTS]] section."
                    ),
                }
                outcome = upsert(named, addr, entry, dry_run)
                if outcome == "new":
                    new_count += 1
                    log(f"  [{mode_tag}] {addr}: VERIFIED [{i}/{len(candidates)}] → "
                        f"{entry['name']} ({final_url})")
                elif outcome == "refreshed":
                    refreshed_count += 1
                    log(f"  [{mode_tag}] {addr}: REFRESHED [{i}/{len(candidates)}] "
                        f"({final_url})")
                _write_account_label(addr, entry, mode_tag, dry_run)
        # Persist cursor every PROGRESS_EVERY addresses so a mid-run
        # SIGALRM kill doesn't lose most of the walk.
        if i % PROGRESS_EVERY == 0:
            log(f"  [{mode_tag}] progress: {i}/{len(candidates)} · "
                f"NEW={new_count} REFRESHED={refreshed_count} "
                f"NO_DOMAIN={no_domain_count} UNVERIFIABLE={unverifiable_count} "
                f"ERROR={error_count} · cursor={last_processed}")
            if mode_tag in ("on-chain", "on-chain-cursor"):
                _save_cursor(last_processed)
        time.sleep(THROTTLE_S)

    # Final cursor save + summary line
    if mode_tag in ("on-chain", "on-chain-cursor") and last_processed:
        _save_cursor(last_processed)
    log(f"[{mode_tag}] SUMMARY: probed={len(candidates)} NEW={new_count} "
        f"REFRESHED={refreshed_count} NO_DOMAIN={no_domain_count} "
        f"UNVERIFIABLE={unverifiable_count} ERROR={error_count} · "
        f"cursor_end={last_processed}")
    return new_count, refreshed_count


# ─── ENTRYPOINT ───────────────────────────────────────────────────────────

MODE_CHOICES = ("org", "on-chain", "mpt-issuers", "token-issuers")

# Order matters for --all: walk the small candidate pools first so any
# overlap with the much larger on-chain set surfaces as cross-mode
# dedup-skips rather than getting silently included. mpt-issuers (~36)
# and token-issuers (~50) before on-chain (~1k+) accomplishes that.
ALL_RUN_ORDER = ("org", "mpt-issuers", "token-issuers", "on-chain")


def _resolve_modes(args) -> list[str]:
    """Pick which modes to run, honouring legacy flags for backwards-compat.
    Precedence: --mode > --all > legacy --org-only/--account-only > default."""
    if args.mode:
        return [args.mode]
    if args.all:
        return list(ALL_RUN_ORDER)
    if args.org_only:
        return ["org"]
    if args.account_only:
        return ["on-chain"]
    # Default = pre-existing behaviour (ORG + ON-CHAIN). MPT and token
    # modes are opt-in until the population pass has been reviewed.
    return ["org", "on-chain"]


def _gather_candidates(
    mode: str, args, seen: set[str], remaining_budget: int | None,
) -> list[str]:
    """Source the candidate list for `mode`, drop wallets already walked
    in this --all run, then apply the curated-skip filter unless
    --force-recheck is set. `remaining_budget` is a shared total cap
    across the whole run so --all --limit N walks N wallets total, not
    N per mode (which would silently bypass cross-mode dedup)."""
    if mode == "org":
        return []  # org mode walks domains, not pre-listed wallets
    if mode == "on-chain":
        cands = sorted(active_addresses())
        extras = [s.strip() for s in (args.extra or "").split(",") if s.strip()]
        cands.extend(a for a in extras if a not in cands)
        # Cursor: skip addresses <= last-processed from previous run.
        # If cursor is past the last candidate (wrapped), reset it so
        # this run starts over from the beginning of the sorted set.
        # Wired 2026-09-04: weekly walker + --limit N per run + cursor
        # persistence = full active-set gets walked over N weeks
        # (previously each week restarted from 'r...', never reaching
        # 'z...' before the walker was killed).
        cursor = _load_cursor()
        if cursor:
            before = len(cands)
            cands = [a for a in cands if a > cursor]
            if not cands:
                log(f"  [on-chain] cursor at {cursor!r} past end of "
                    f"{before} active addresses — resetting and walking from top")
                _save_cursor("", wrapped=True)
                cands = sorted(active_addresses())
                cands.extend(a for a in extras if a not in cands)
            else:
                log(f"  [on-chain] cursor {cursor!r} → resuming with "
                    f"{len(cands)}/{before} candidates remaining in this cycle")
    elif mode == "mpt-issuers":
        cands = candidates_mpt_issuers()
    elif mode == "token-issuers":
        cands = candidates_token_issuers(args.top_n)
    else:
        return []
    before_dedup = len(cands)
    cands = [a for a in cands if a not in seen]
    dedup_dropped = before_dedup - len(cands)
    if dedup_dropped:
        log(f"  [{mode}] cross-mode dedup: {dedup_dropped}/{before_dedup} "
            f"candidate(s) already walked in an earlier mode — skipping")
    if not args.force_recheck:
        cands = filter_curated(cands)
    # --limit is a TOTAL walked-wallets budget shared across modes — not
    # per-mode — so the dedup logic gets exercised when modes overlap.
    if remaining_budget is not None:
        cands = cands[:max(0, remaining_budget)]
    return cands


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report only, do not write to named_accounts.json")
    ap.add_argument("--extra", default="",
                    help="comma-separated extra XRPL addresses for on-chain mode")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap addresses scanned in on-chain mode (0 = no cap)")
    ap.add_argument("--mode", choices=MODE_CHOICES, default=None,
                    help="run a single candidate-source mode "
                         "(default: org + on-chain)")
    ap.add_argument("--all", action="store_true",
                    help=f"run every mode sequentially: "
                         f"{', '.join(MODE_CHOICES)}")
    ap.add_argument("--top-n", type=int, default=50,
                    help="N for --mode token-issuers (default 50)")
    ap.add_argument("--force-recheck", action="store_true",
                    help="walk candidates even if account_labels has a "
                         "non-derived row for them (default: skip curated)")
    # Legacy aliases — kept so existing launchd jobs and ops scripts keep
    # working without a flag flip.
    ap.add_argument("--org-only", action="store_true",
                    help="legacy alias for --mode org")
    ap.add_argument("--account-only", action="store_true",
                    help="legacy alias for --mode on-chain")
    args = ap.parse_args()

    _wh_db = None
    if not args.dry_run:
        try:
            import db as _wh_db_mod
            _wh_db = _wh_db_mod
            _wh_db.write_walker_health_start("verify_toml", cadence_seconds=WALKER_CADENCE_SECONDS)
            # 2026-09-04: install SIGALRM handler so wrapper timeout
            # writes clean walker_health_end + preserves the cursor
            # that scan_account_mode has been saving every 500 addrs.
            _install_timeout_handler("verify_toml")
        except Exception:
            _wh_db = None

    _ok = False
    _msg = "init"
    try:
        modes = _resolve_modes(args)
        log("=" * 60)
        log(f"verify_toml_accounts start (dry_run={args.dry_run} "
            f"modes={','.join(modes)} top_n={args.top_n} "
            f"force_recheck={args.force_recheck})")
        named = load_json(NAMED_ACCOUNTS_PATH, {})
        initial_size = len(named)

        seen: set[str] = set()
        per_mode_new: dict[str, int] = {}
        per_mode_ref: dict[str, int] = {}
        # Total walked-wallets budget (None = uncapped). Depletes as each
        # mode consumes from it; later modes see a smaller cap.
        remaining_budget: int | None = args.limit if args.limit else None

        for mode in modes:
            try:
                if mode == "org":
                    new, ref = scan_org_mode(named, args.dry_run)
                elif mode == "on-chain":
                    # Two-phase (2026-09-04 rework):
                    #   Phase A = addresses first-seen in events.db since
                    #     the previous run's high-water mark (capped at
                    #     PHASE_A_CAP). These are the only set with any
                    #     real chance of gaining a fresh Domain field.
                    #   Phase B = existing cursor walk through the old
                    #     active-address set, spending whatever budget
                    #     Phase A left over. HWM only advances if Phase A
                    #     completed fully within budget so surplus new
                    #     addresses roll into the next run.
                    cursor_dict = _load_cursor_dict()
                    last_hwm = cursor_dict.get("last_new_scan_ts")

                    phase_a_budget = PHASE_A_CAP
                    if remaining_budget is not None:
                        phase_a_budget = min(PHASE_A_CAP, remaining_budget)

                    if last_hwm is None:
                        # First run under this scheme: no baseline, so
                        # skip Phase A and anchor HWM at current max(ts)
                        # so next weekly run has a "since" to key on.
                        phase_a_cands: list[str] = []
                        anchor_ts = _events_db_max_ts()
                        if anchor_ts is not None:
                            _save_cursor(
                                cursor_dict.get("last_address", ""),
                                wrapped=cursor_dict.get("wrapped", False),
                                new_scan_ts=anchor_ts,
                            )
                        log(f"  [on-chain-new] first run under two-phase "
                            f"scheme — no last_new_scan_ts baseline; "
                            f"Phase A skipped; HWM anchored at {anchor_ts}")
                        phase_a_max_ts = anchor_ts
                        phase_a_capped = False
                    else:
                        new_addrs, phase_a_max_ts = _phase_a_first_seen_since(int(last_hwm))
                        available = len(new_addrs)
                        if available > phase_a_budget:
                            phase_a_cands = new_addrs[:phase_a_budget]
                            phase_a_capped = True
                            log(f"  [on-chain-new] {available} newly-active "
                                f"addresses since ts>{last_hwm}; capping at "
                                f"budget={phase_a_budget}; HWM held "
                                f"(surplus rolls into next run)")
                        else:
                            phase_a_cands = new_addrs
                            phase_a_capped = False
                            log(f"  [on-chain-new] {available} newly-active "
                                f"addresses since ts>{last_hwm}; all fit in "
                                f"budget={phase_a_budget}; HWM advances to "
                                f"{phase_a_max_ts} after Phase A")

                    # Cross-mode dedup + curated filter (same rules as
                    # _gather_candidates so Phase A + earlier modes and
                    # Phase A + Phase B never re-walk the same address).
                    if phase_a_cands:
                        before_dedup = len(phase_a_cands)
                        phase_a_cands = [a for a in phase_a_cands if a not in seen]
                        dropped = before_dedup - len(phase_a_cands)
                        if dropped:
                            log(f"  [on-chain-new] cross-mode dedup: "
                                f"{dropped}/{before_dedup} candidate(s) "
                                f"already walked in an earlier mode — skipping")
                        if not args.force_recheck:
                            phase_a_cands = filter_curated(phase_a_cands)
                    seen.update(phase_a_cands)

                    pa_new, pa_ref = scan_account_mode(
                        phase_a_cands, named, args.dry_run,
                        mode_tag="on-chain-new",
                    )
                    per_mode_new["on-chain-new"] = pa_new
                    per_mode_ref["on-chain-new"] = pa_ref

                    # Advance HWM iff Phase A completed within budget
                    # (surplus? keep old HWM so next run picks it up).
                    if last_hwm is not None and not phase_a_capped and phase_a_max_ts is not None:
                        _save_cursor(
                            _load_cursor_dict().get("last_address", ""),
                            wrapped=_load_cursor_dict().get("wrapped", False),
                            new_scan_ts=int(phase_a_max_ts),
                        )
                        log(f"  [on-chain-new] HWM advanced to {phase_a_max_ts}")

                    if remaining_budget is not None:
                        remaining_budget = max(0, remaining_budget - len(phase_a_cands))

                    # Phase B — cursor walk with whatever budget is left.
                    if remaining_budget is None or remaining_budget > 0:
                        pb_cands = _gather_candidates(
                            "on-chain", args, seen, remaining_budget,
                        )
                        seen.update(pb_cands)
                        pb_new, pb_ref = scan_account_mode(
                            pb_cands, named, args.dry_run,
                            mode_tag="on-chain-cursor",
                        )
                        if remaining_budget is not None:
                            remaining_budget = max(0, remaining_budget - len(pb_cands))
                    else:
                        pb_new, pb_ref = 0, 0
                        log("  [on-chain-cursor] SKIP — budget exhausted by Phase A")
                    per_mode_new["on-chain-cursor"] = pb_new
                    per_mode_ref["on-chain-cursor"] = pb_ref

                    # Aggregate into the mode-name key too, for the
                    # top-line summary readers that still expect "on-chain".
                    new = pa_new + pb_new
                    ref = pa_ref + pb_ref
                else:
                    cands = _gather_candidates(
                        mode, args, seen, remaining_budget,
                    )
                    seen.update(cands)
                    new, ref = scan_account_mode(
                        cands, named, args.dry_run, mode_tag=mode,
                    )
                    if remaining_budget is not None:
                        remaining_budget = max(0, remaining_budget - len(cands))
            except Exception as e:
                # Continue-on-error: one mode's failure must not cascade.
                log(f"ERROR mode={mode} crashed: {e!r} — continuing")
                new, ref = 0, 0
            per_mode_new[mode] = new
            per_mode_ref[mode] = ref

        new_total = sum(per_mode_new.values())
        refreshed_total = sum(per_mode_ref.values())

        breakdown = " ".join(
            f"{m}={per_mode_new.get(m, 0)}/{per_mode_ref.get(m, 0)}"
            for m in modes
        )
        log(f"summary: new={new_total} refreshed={refreshed_total} "
            f"[{breakdown}] watchlist {initial_size}→{len(named)}")

        if (new_total or refreshed_total) and not args.dry_run:
            save_named(named)
            log(f"wrote {NAMED_ACCOUNTS_PATH}")

        _ok = True
        _msg = f"new={new_total} refreshed={refreshed_total} [{breakdown}]"
        return 0
    except Exception as e:
        _msg = f"{type(e).__name__}: {e}"
        raise
    finally:
        if _wh_db is not None:
            _wh_db.write_walker_health_end("verify_toml", ok=_ok, message=_msg)


if __name__ == "__main__":
    sys.exit(main())

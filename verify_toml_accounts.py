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
LOG_PATH = os.path.join(HERE, "verify_toml.log")

XRPL_RPC = os.environ.get("XRPL_RPC", "https://s1.ripple.com:51234")
HTTP_TIMEOUT = 10
USER_AGENT = "xrpldashboard-toml-verifier/1.0 (+https://xrpldashboard.com)"

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

    return new_count, refreshed_count


# ─── ON-CHAIN MODE ────────────────────────────────────────────────────────

def scan_account_mode(
    candidates: list[str], named: dict, dry_run: bool
) -> tuple[int, int]:
    log(f"on-chain mode: probing {len(candidates)} account(s) for Domain field")
    client = JsonRpcClient(XRPL_RPC)
    new_count = refreshed_count = 0

    for i, addr in enumerate(candidates, 1):
        domain = fetch_domain_field(client, addr)
        if not domain:
            time.sleep(0.1)  # throttle even on misses
            continue
        final_url, toml_data, err = fetch_toml(domain)
        if toml_data is None:
            log(f"  {addr}: domain={domain} → toml unusable ({err})")
            time.sleep(0.1)
            continue
        if not address_in_toml(toml_data, addr):
            log(f"  {addr}: domain={domain} → toml does NOT list this address")
            time.sleep(0.1)
            continue
        # Find the ACCOUNTS block matching this address (for name/desc)
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
            log(f"  NEW [{i}/{len(candidates)}] {addr} → {entry['name']}")
        elif outcome == "refreshed":
            refreshed_count += 1
            log(f"  refreshed [{i}/{len(candidates)}] {addr}")
        time.sleep(0.15)  # be polite to public RPC

    return new_count, refreshed_count


# ─── ENTRYPOINT ───────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report only, do not write to named_accounts.json")
    ap.add_argument("--extra", default="",
                    help="comma-separated extra XRPL addresses for on-chain mode")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap addresses scanned in on-chain mode (0 = no cap)")
    ap.add_argument("--org-only", action="store_true",
                    help="run only the ORG (domain seed) mode")
    ap.add_argument("--account-only", action="store_true",
                    help="run only the ON-CHAIN (Domain field) mode")
    args = ap.parse_args()

    log("=" * 60)
    log(f"verify_toml_accounts start (dry_run={args.dry_run})")
    named = load_json(NAMED_ACCOUNTS_PATH, {})
    initial_size = len(named)

    org_new = org_refreshed = acct_new = acct_refreshed = 0

    if not args.account_only:
        org_new, org_refreshed = scan_org_mode(named, args.dry_run)

    if not args.org_only:
        candidates = sorted(active_addresses())
        extras = [s.strip() for s in args.extra.split(",") if s.strip()]
        candidates.extend(a for a in extras if a not in candidates)
        # Skip addresses already promoted via org mode this run
        if args.limit:
            candidates = candidates[: args.limit]
        acct_new, acct_refreshed = scan_account_mode(
            candidates, named, args.dry_run
        )

    new_total = org_new + acct_new
    refreshed_total = org_refreshed + acct_refreshed

    log(f"summary: new={new_total} (org={org_new} onchain={acct_new}) "
        f"refreshed={refreshed_total} (org={org_refreshed} onchain={acct_refreshed}) "
        f"watchlist {initial_size}→{len(named)}")

    if (new_total or refreshed_total) and not args.dry_run:
        save_named(named)
        log(f"wrote {NAMED_ACCOUNTS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

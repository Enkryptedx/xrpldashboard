"""check_data.py — address / token → timestamped, sourced signals for /check.

Every signal returned carries: fact + source + checked_at_utc. Facts, not
verdicts. If a source can't be reached, it goes in `couldnt_check`, never
silently dropped — that's the VirusTotal "0/94 security vendors flagged
this" pattern for XRPL.

The status pill (`verified` / `self` / `bare`) summarizes WHAT IDENTITY
CLAIM EXISTS ON THE LEDGER, not whether the account is safe to interact
with. Green pill never renders alone — always paired with the signals
list and the "does not mean safe to send money" tooltip.

D1: wallet r-address. D2 (this pass): token = (currency, issuer).
Amber (self-described) carries its own anti-reassurance tooltip:
"Self-reported details are not a safety signal. A scammer can fill in a
website and a name just as easily as a legitimate project can." The trap
that tooltip actively kills is the "they have a website and a name, so
they're probably legit" inference — the specific amber trap for tokens.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import db
import re
import ssl
import tomllib
import urllib.error
import urllib.request
from urllib.parse import quote as urlquote, urlparse

import certifi
from publicsuffix2 import get_sld  # Mozilla Public Suffix List, for correct eTLD+1
from xrpl.models.requests import AccountInfo

import xrpl_client
from xrpl_client import get_client
from sovereign_tunnel_client import (
    SOURCING_SOVEREIGN,
    SOURCING_FALLBACK,
    SOURCING_NO_TUNNEL,
    SovereignFetcher,
    worse_sourcing,
)

# D3 URL/domain check: reuse the battle-tested SSRF gate from the
# walker. _is_safe_domain rejects IPs, control chars, and off-shape
# names BEFORE any HTTP fetch, so a hostile on-chain Domain or paste
# can't SSRF via https://<rfc1918>/.well-known/xrp-ledger.toml. Do not
# roll our own gate here — drift on this is a footgun.
from verify_toml_accounts import _is_safe_domain as _domain_is_safe

# D3 URL check: keep the request-path HTTP fetch quick. 2s is enough
# for any well-configured TOML endpoint; anything longer belongs in
# couldnt_check, not blocking the paste-box. Tightened 2026-08-27 so
# machine fetchers (Grok/ChatGPT) don't time out on the URL form.
_D3_HTTP_TIMEOUT = 2
_D3_USER_AGENT = "xrpldashboard-check-page/1.0 (+https://xrpldashboard.com/check)"
_D3_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

_HERE = os.path.dirname(os.path.abspath(__file__))
_NAMED_PATH = os.path.join(_HERE, "named_accounts.json")
_TOKEN_NAMES_PATH = os.path.join(_HERE, "token_names.json")

# Phase 1 of the /check expansion (docs/CHECK_IT_DESIGN.md). All three
# additions below are STRUCTURAL signals — facts we surface with a source
# label and a checked_at_utc stamp. No verdicts, no scores. The design
# doc's Fence 1 and Fence 2 govern every field returned from these
# functions.
_OFAC_SDN_PATH = os.path.join(_HERE, "ofac_sdn_addresses.json")

# Public services used by Phase 1 signals. Both are free, no-key, and
# have well-established uptime — but we still budget short timeouts and
# fall back to couldnt_check on failure (same discipline as _fetch_toml_fast).
_RDAP_URL_TEMPLATE = "https://rdap.org/domain/{}"
_CRTSH_URL_TEMPLATE = "https://crt.sh/?q={}&output=json"
_RDAP_TIMEOUT = 3
_CRTSH_TIMEOUT = 4

# XRPL ripple-epoch offset: seconds between 1970-01-01 and 2000-01-01 UTC.
_RIPPLE_EPOCH = 946_684_800

# Placeholder marker used in token_names.json for entries pending manual
# curation. Curator-set: means "an entry exists but nobody has verified
# the URL". Must render amber (self-described), never green (verified).
_TOML_TODO_MARKER = "TODO_curation_pass"

# Loose XRPL r-address gate; kept minimal to avoid drift from app.py's
# _XRPL_ADDR_CHARS. The route already validates before we get here.
_XRPL_ADDR_PREFIX = "r"


def _load_named() -> dict:
    try:
        with open(_NAMED_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_token_names() -> dict:
    """token_names.json shape: {"<currency>:<issuer>": {...}} where currency
    is either 3-char ASCII (e.g. "USD") or 40-char hex-padded (e.g. RLUSD's
    "524C555344…"). Verified entries carry a `verified_via` URL; entries
    pending curation carry the sentinel _TOML_TODO_MARKER and MUST NOT be
    treated as verified — that's the amber trap the /check page exists
    to prevent."""
    try:
        with open(_TOKEN_NAMES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _normalize_currency(cur: str) -> str:
    """XRPL currency codes are either 3-char ASCII or 40-char hex. Users
    typically paste "USD" or "RLUSD" — normalize the >3-char form to the
    hex-padded key used in token_names.json. Idempotent on already-hex input.
    Returns uppercase for consistency with the JSON keys."""
    if not cur:
        return ""
    cur = cur.strip()
    if len(cur) == 40 and all(c in "0123456789abcdefABCDEF" for c in cur):
        return cur.upper()
    if len(cur) <= 3:
        return cur.upper()
    # >3 chars, non-hex → hex-encode ASCII, right-pad zero to 40 chars.
    try:
        return (cur.encode("ascii").hex() + "0" * 40)[:40].upper()
    except UnicodeEncodeError:
        return cur.upper()


def _display_currency(cur_normalized: str) -> str:
    """Reverse of _normalize_currency for display. Delegates to
    token_naming.decode_currency (single source of truth across
    /tokens, /token, /whales, /check, /wallet). 3-char passes through;
    hex-40 with clean bytes unpacks back to ASCII; junk falls back to
    the "0x…" short-hex form so downstream copy still has a scannable
    handle to the raw code."""
    if not cur_normalized:
        return cur_normalized
    from token_naming import decode_currency
    d = decode_currency(cur_normalized)
    if d["kind"] == "decoded" or d["kind"] == "short" or d["kind"] == "xrp":
        return d["display"]
    # kind == "junk"
    return f"0x{cur_normalized[:6]}…{cur_normalized[-3:]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _short_addr(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}" if a and len(a) > 12 else a


def _decode_domain_hex(hex_str: str | None) -> str | None:
    if not hex_str:
        return None
    try:
        raw = bytes.fromhex(hex_str).decode("ascii")
        raw = raw.strip().lower()
        return raw or None
    except Exception:
        return None


def _account_age_days(account_data: dict) -> int | None:
    """AccountRoot doesn't expose a direct 'created_at'. First
    PreviousTxnLgrSeq gets us a bound, but for D1 we use the parent
    ledger's close_time_iso if the caller supplies it. Return None
    otherwise — never fabricate an age."""
    ct = account_data.get("_first_seen_close_time_ripple")
    if ct is None:
        return None
    try:
        first_unix = _RIPPLE_EPOCH + int(ct)
        return max(0, int((time.time() - first_unix) // 86_400))
    except Exception:
        return None


def _signal(label: str, value: str, source_label: str,
            source_url: str | None = None) -> dict:
    return {
        "label": label,
        "value": value,
        "source_label": source_label,
        "source_url": source_url,
        "checked_at_utc": _now_iso(),
    }


def _couldnt(label: str, reason: str) -> dict:
    return {
        "label": label,
        "reason": reason,
        "checked_at_utc": _now_iso(),
    }


# ─────────────────────────────────────────────────────────────────────
# Phase 1 signals — /check expansion (docs/CHECK_IT_DESIGN.md).
#
# These three producers (_signal_domain_age, _signal_earliest_ssl_cert,
# _signal_ofac_sdn_match) surface structural facts from named external
# sources. Same shape as every other signal on the page: (label, value,
# source_label, checked_at_utc). No verdicts — Fence 2.
# ─────────────────────────────────────────────────────────────────────


def _registered_domain(host: str) -> str:
    """Mozilla PSL-backed eTLD+1 extraction. Correct for co.uk, co.jp,
    and every other multi-label public suffix. Falls back to the
    last-two-labels approximation only if PSL cannot classify (very rare).
    """
    if not host:
        return ""
    try:
        sld = get_sld(host.lower())
        if sld:
            return sld
    except Exception:
        pass
    parts = host.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


_OFAC_SNAPSHOT_CACHE: dict[str, Any] | None = None


def _load_ofac_snapshot() -> dict[str, Any]:
    """Load ofac_sdn_addresses.json once per process. Returns an empty
    stub if the file is missing so the signal downgrades to couldnt_check
    rather than crashing — a fresh clone without a snapshot must still
    render /check."""
    global _OFAC_SNAPSHOT_CACHE
    if _OFAC_SNAPSHOT_CACHE is not None:
        return _OFAC_SNAPSHOT_CACHE
    try:
        with open(_OFAC_SDN_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "addresses" not in data:
            raise ValueError("snapshot missing 'addresses' key")
    except Exception:
        data = {
            "source_url": None,
            "fetched_at_utc": None,
            "publication_date": None,
            "count": 0,
            "addresses": {},
            "_load_error": True,
        }
    _OFAC_SNAPSHOT_CACHE = data
    return data


def _signal_ofac_sdn_match(address: str, chain: str) -> dict:
    """OFAC SDN check for a wallet address on any chain the snapshot
    covers (XRP, ETH, XBT, TRX, USDT, ...). Returns a `_signal(...)`
    dict either way — "not present" is a real signal, not a null result.
    If the local snapshot cannot be loaded, returns a `_couldnt(...)` dict.
    Caller decides where to bucket it (signals vs couldnt_check).
    """
    snap = _load_ofac_snapshot()
    if snap.get("_load_error"):
        return _couldnt(
            label="OFAC SDN cross-check",
            reason="local snapshot unavailable (run scripts/refresh_ofac_sdn.py)",
        )

    addresses: dict[str, dict] = snap.get("addresses") or {}
    pub = snap.get("publication_date") or "unknown"
    fetched = snap.get("fetched_at_utc") or "unknown"
    count = snap.get("count") or 0

    # ETH-family addresses are case-insensitive; check both original and
    # lowercased. XRP/BTC are case-sensitive base58 — exact match only.
    hit = addresses.get(address)
    if hit is None and chain.upper() in {"ETH", "USDC", "USDT", "ARB", "BSC"}:
        hit = addresses.get(address.lower())
        if hit is None:
            # Snapshot may store checksummed form; scan for a case-insensitive match.
            lower_addr = address.lower()
            for k, v in addresses.items():
                if k.lower() == lower_addr:
                    hit = v
                    break

    source_label = (
        f"OFAC Specially Designated Nationals list "
        f"(local snapshot from OFAC publication {pub}; fetched {fetched}; "
        f"{count} digital-currency addresses)"
    )
    source_url = snap.get("source_url")

    if hit:
        entity = hit.get("entity_name") or "(unnamed entry)"
        programs = ", ".join(hit.get("programs") or []) or "unspecified"
        return _signal(
            label="OFAC SDN cross-check",
            value=(
                f"PRESENT on the OFAC Specially Designated Nationals list "
                f"as a “{hit.get('chain')}” digital-currency address for "
                f"entity “{entity}” (sanctions program(s): {programs})."
            ),
            source_label=source_label,
            source_url=source_url,
        )

    return _signal(
        label="OFAC SDN cross-check",
        value=(
            f"Not present on the U.S. Treasury OFAC Specially "
            f"Designated Nationals list (checked against local snapshot "
            f"of {count} digital-currency addresses from OFAC "
            f"publication {pub})."
        ),
        source_label=source_label,
        source_url=source_url,
    )


def _signal_domain_age(host: str) -> dict:
    """RDAP domain-age lookup. Signal: 'Domain registered N days ago
    (YYYY-MM-DD)' if the registration event is present, else couldnt_check.

    RDAP is the modern replacement for WHOIS and is free, keyless, and
    well-supported. rdap.org proxies to the correct registry endpoint
    per TLD.

    IMPORTANT: this is a structural signal, not a verdict. A three-day
    old domain is not necessarily malicious; a fifteen-year old domain
    is not necessarily safe. The reader interprets.
    """
    if not host or not _domain_is_safe(host):
        return _couldnt(
            label="Domain registration age",
            reason="host failed the SSRF gate before an RDAP lookup",
        )

    reg_root = _registered_domain(host)
    if not reg_root:
        return _couldnt(
            label="Domain registration age",
            reason="could not derive a registered-domain root from this host",
        )

    url = _RDAP_URL_TEMPLATE.format(reg_root)
    req = urllib.request.Request(url, headers={"User-Agent": _D3_USER_AGENT})
    try:
        with urllib.request.urlopen(
            req, timeout=_RDAP_TIMEOUT, context=_D3_SSL_CTX,
        ) as resp:
            body = resp.read(256_000)
    except urllib.error.HTTPError as e:
        return _couldnt(
            label="Domain registration age",
            reason=f"RDAP responded HTTP {e.code} for {reg_root}",
        )
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return _couldnt(
            label="Domain registration age",
            reason=f"RDAP fetch failed ({type(e).__name__})",
        )
    except Exception as e:
        return _couldnt(
            label="Domain registration age",
            reason=f"RDAP unexpected error ({type(e).__name__})",
        )

    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return _couldnt(
            label="Domain registration age",
            reason="RDAP returned non-JSON",
        )

    events = payload.get("events") or []
    reg_date: str | None = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if (ev.get("eventAction") or "").lower() == "registration":
            reg_date = ev.get("eventDate")
            break

    if not reg_date:
        return _couldnt(
            label="Domain registration age",
            reason="RDAP record has no registration event",
        )

    # Parse RFC 3339 — RDAP dates are ISO 8601. Strip a trailing 'Z'.
    iso = reg_date.rstrip("Z").split(".", 1)[0]
    try:
        reg_dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    except ValueError:
        return _couldnt(
            label="Domain registration age",
            reason=f"RDAP returned unparseable date '{reg_date}'",
        )

    age_days = max(0, int((datetime.now(timezone.utc) - reg_dt).total_seconds() // 86_400))
    date_short = reg_dt.strftime("%Y-%m-%d")
    return _signal(
        label="Domain registration age",
        value=(
            f"{reg_root} was registered {age_days:,} days ago "
            f"(first registration event: {date_short}). Structural "
            f"fact only — very-new domains are common in fraud, but "
            f"age alone is not a verdict."
        ),
        source_label=f"RDAP (rdap.org proxy → authoritative registry) for {reg_root}",
        source_url=url,
    )


def _signal_earliest_ssl_cert(host: str) -> dict:
    """Earliest SSL certificate for the host per Certificate Transparency
    (crt.sh). Signal: 'Earliest CT cert seen for this host: YYYY-MM-DD
    (N certs on record).' If crt.sh is unreachable, degrades to couldnt.

    CT logs are the truest "how long has this host actually been serving
    HTTPS" measurement — they can't be back-dated because they're
    append-only Merkle logs run by browser vendors. Complements RDAP
    (domain age) with hostname-scoped age; a fresh subdomain on an old
    parent domain is a common scam shape.
    """
    if not host or not _domain_is_safe(host):
        return _couldnt(
            label="Earliest HTTPS certificate",
            reason="host failed the SSRF gate before a crt.sh lookup",
        )

    url = _CRTSH_URL_TEMPLATE.format(urlquote(host, safe=""))
    req = urllib.request.Request(url, headers={"User-Agent": _D3_USER_AGENT})
    try:
        with urllib.request.urlopen(
            req, timeout=_CRTSH_TIMEOUT, context=_D3_SSL_CTX,
        ) as resp:
            body = resp.read(2_000_000)
    except urllib.error.HTTPError as e:
        return _couldnt(
            label="Earliest HTTPS certificate",
            reason=f"crt.sh responded HTTP {e.code}",
        )
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return _couldnt(
            label="Earliest HTTPS certificate",
            reason=f"crt.sh fetch failed ({type(e).__name__})",
        )
    except Exception as e:
        return _couldnt(
            label="Earliest HTTPS certificate",
            reason=f"crt.sh unexpected error ({type(e).__name__})",
        )

    try:
        entries = json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return _couldnt(
            label="Earliest HTTPS certificate",
            reason="crt.sh returned non-JSON",
        )

    if not isinstance(entries, list) or not entries:
        return _signal(
            label="Earliest HTTPS certificate",
            value=(
                f"No public HTTPS certificates found in Certificate "
                f"Transparency logs for “{host}”. Either the host has "
                f"never served HTTPS or its certs are unusually recent "
                f"and not yet indexed."
            ),
            source_label="Certificate Transparency search (crt.sh)",
            source_url=url,
        )

    earliest: datetime | None = None
    for entry in entries:
        raw = (entry or {}).get("not_before")
        if not raw:
            continue
        # crt.sh returns "2023-04-15T00:00:00" — no timezone suffix.
        try:
            dt = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if earliest is None or dt < earliest:
            earliest = dt

    if earliest is None:
        return _couldnt(
            label="Earliest HTTPS certificate",
            reason="crt.sh entries had no parseable not_before date",
        )

    age_days = max(0, int((datetime.now(timezone.utc) - earliest).total_seconds() // 86_400))
    return _signal(
        label="Earliest HTTPS certificate",
        value=(
            f"Earliest certificate covering “{host}” in Certificate "
            f"Transparency logs was issued {age_days:,} days ago "
            f"({earliest.strftime('%Y-%m-%d')}); {len(entries):,} "
            f"cert record(s) total. Structural fact — a fresh cert on "
            f"an old parent domain is a common scam shape but not a "
            f"verdict."
        ),
        source_label="Certificate Transparency search (crt.sh)",
        source_url=url,
    )


def _make_fetcher() -> SovereignFetcher:
    """Build a per-request SovereignFetcher for /check.

    walker_name='check_page' so any cascade shows up in
    walker_node_fallback under the same name as the pre-tunnel-wire
    logger (see xrpl_client.get_client(walker_name='check_page')),
    which keeps the ~287/day cascade count comparable before/after.
    Public fallback URL comes from xrpl_client.PUBLIC_NODES[0] so env
    overrides flow through consistently.
    """
    public_url = xrpl_client.PUBLIC_NODES[0]
    return SovereignFetcher(public_url=public_url, walker_name="check_page")


def _query_ledger(address: str, fetcher: SovereignFetcher | None = None) -> tuple[dict | None, str | None, str]:
    """Return (account_data, error_reason, sourcing).

    account_data is None if the account doesn't exist on-chain or if
    all endpoints failed. sourcing is one of the SOURCING_* constants
    from sovereign_tunnel_client, reflecting how the RPC was served.

    Requests signer_lists=True so multi-sig detection piggybacks on the
    same RPC call — no extra round-trip for the capabilities section.

    Tunnel-first via SovereignFetcher (rpc.xrpldashboard.com with
    CF-Access headers); public s1/s2 fallback if the tunnel fails or
    is unconfigured. Fail-open if env vars missing (fetcher's sourcing
    reports 'public-no-tunnel-configured' instead of crashing).
    """
    if fetcher is None:
        fetcher = _make_fetcher()

    params = {
        "account": address,
        "ledger_index": "validated",
        "signer_lists": True,
    }
    result = fetcher.call("account_info", params)
    if result is None:
        return None, "XRPL endpoints unreachable", fetcher.sourcing

    err = result.get("error")
    if err == "actNotFound":
        return None, "not_found", fetcher.sourcing
    if err:
        return None, f"XRPL returned '{err}'", fetcher.sourcing

    account_data = dict(result.get("account_data") or {})
    # signer_lists is returned as a top-level field in the RPC result,
    # not inside account_data — fold it in so capability builders can
    # read it off the same object.
    if "signer_lists" not in account_data:
        sl = result.get("signer_lists")
        if sl:
            account_data["signer_lists"] = sl
    return account_data, None, fetcher.sourcing


# ─── Ledger-level capabilities (Phase 3 issuer flags + account security) ─
#
# Bit values from xrpl.org/docs/references/protocol/ledger-data/ledger-entry-types/accountroot.
# The section renders as neutral-facts-only per feedback_check_lookalike_neutral_pill_only:
# no color coding, no verdicts, no banned safety words. Notes explicitly
# defuse both directions (freeze-capable ≠ danger; no-freeze ≠ all-clear;
# absence of a capability ≠ reassurance) — the subtitle carries that too.
_FLAG_LSF_DEFAULT_RIPPLE            = 0x00800000
_FLAG_LSF_DEPOSIT_AUTH              = 0x01000000
_FLAG_LSF_DISABLE_MASTER            = 0x00100000
_FLAG_LSF_GLOBAL_FREEZE             = 0x00400000
_FLAG_LSF_NO_FREEZE                 = 0x00200000
_FLAG_LSF_ALLOW_TRUSTLINE_CLAWBACK  = 0x02000000

_CAPABILITY_SOURCE_LABEL = "XRPL AccountRoot.Flags (via AccountInfo)"


def _capability_signals(account_data: dict) -> list[dict]:
    """Return the ledger-level capabilities list for an account.

    One dict per capability with: label, value, note, source_label,
    checked_at_utc. Freeze always renders (default, no-freeze, or
    currently-freezing). Clawback / TransferRate / DisableMaster /
    RegularKey / SignerList / DepositAuth render only when set. Copy
    is verbatim from Phase 3 Gate 1 approval — do not edit without
    a follow-up gate.
    """
    if not account_data:
        return []
    flags = int(account_data.get("Flags") or 0)
    now = _now_iso()

    def pill(label: str, value: str, note: str) -> dict:
        return {
            "label": label,
            "value": value,
            "note": note,
            "source_label": _CAPABILITY_SOURCE_LABEL,
            "checked_at_utc": now,
        }

    out: list[dict] = []

    # --- Freeze family (always renders; mutually exclusive states) ---
    if flags & _FLAG_LSF_NO_FREEZE:
        out.append(pill(
            label="Freeze capability",
            value=(
                "This issuer has permanently disabled the freeze "
                "capability. This flag cannot be reversed by the "
                "account itself."
            ),
            note=(
                "A technical commitment about what the account can do, "
                "not a safety signal. Scam tokens can lack freeze too."
            ),
        ))
    elif flags & _FLAG_LSF_GLOBAL_FREEZE:
        out.append(pill(
            label="Freeze capability",
            value=(
                "This issuer is currently freezing all trustlines for "
                "this token. Holders cannot send it to each other while "
                "the flag is set."
            ),
            note=(
                "An active freeze means holders cannot currently "
                "transfer this token. Issuers freeze for many reasons — "
                "this flag shows that it is happening, not why. If you "
                "hold this token, this directly affects you right now."
            ),
        ))
    else:
        out.append(pill(
            label="Freeze capability",
            value=(
                "This issuer retains the ability to freeze trustlines "
                "but is not currently doing so."
            ),
            note=(
                "Freeze is a standard AccountRoot capability. Both "
                "regulated stablecoins (e.g. RLUSD) and bad actors have "
                "used it — the capability alone is not a warning."
            ),
        ))

    # --- Clawback (only when set) ---
    if flags & _FLAG_LSF_ALLOW_TRUSTLINE_CLAWBACK:
        out.append(pill(
            label="Clawback capability",
            value=(
                "This issuer can pull tokens back from holders' wallets "
                "under the ledger's clawback rules."
            ),
            note=(
                "Clawback lets an issuer reverse token holdings after "
                "the fact. It exists for regulatory and recovery use, "
                "and can also be misused. The capability is a fact; it "
                "does not tell you the issuer's intent."
            ),
        ))

    # --- TransferRate (only when > 0). Ledger encoding: 1e9 = 0 %,
    #     1.005e9 = 0.5 %, up to 2e9 = 100 %. See AccountSet reference.
    tr = account_data.get("TransferRate")
    if tr:
        try:
            tr_int = int(tr)
            if tr_int > 1_000_000_000:
                rate_pct = (tr_int / 1_000_000_000 - 1) * 100
                out.append(pill(
                    label="Transfer fee",
                    value=(
                        f"Every non-issuer transfer of this token pays "
                        f"a {rate_pct:.3g}% fee to the issuer."
                    ),
                    note=(
                        "This is a real cost, not a subjective signal. "
                        "Verify the rate against what you expect before "
                        "transacting."
                    ),
                ))
        except (TypeError, ValueError):
            pass

    # --- Account security (each conditional) ---
    if flags & _FLAG_LSF_DISABLE_MASTER:
        out.append(pill(
            label="Master key",
            value=(
                "The account's master private key has been disabled. "
                "Transactions must be signed by an alternative key "
                "(RegularKey or SignerList)."
            ),
            note=(
                "Common on production custody setups; also seen on "
                "some compromised accounts. Presence alone is not a "
                "signal."
            ),
        ))
    reg_key = account_data.get("RegularKey")
    if reg_key:
        reg_short = _short_addr(reg_key)
        out.append(pill(
            label="RegularKey",
            value=(
                f"A RegularKey ({reg_short}) is configured, providing "
                f"an alternative signing key."
            ),
            note=(
                "Standard ledger pattern for key rotation and hot/cold "
                "separation."
            ),
        ))
    signer_lists = account_data.get("signer_lists") or []
    if signer_lists:
        out.append(pill(
            label="Multi-signature",
            value=(
                "This account uses multi-signature authentication. "
                "Transactions require a quorum of the listed signer "
                "keys."
            ),
            note=(
                "Multi-signature is a standard ledger feature and does "
                "not by itself indicate custody type."
            ),
        ))
    if flags & _FLAG_LSF_DEPOSIT_AUTH:
        out.append(pill(
            label="DepositAuth",
            value=(
                "This account only accepts incoming payments from "
                "pre-authorized senders."
            ),
            note=(
                "Common on issuer accounts expecting only trust-line "
                "traffic. Presence does not by itself indicate purpose."
            ),
        ))

    return out


def check_address(address: str) -> dict:
    """Build the /check D1 result for an r-address.

    Returns a dict shaped for direct render into templates/check.html.
    Structure is stable across tiers so the template renders one path.
    """
    named = _load_named()
    named_entry = named.get(address) or {}

    # Postgres-side labels (curator/walker-set); may be empty when
    # Postgres isn't configured (dev, first-run, degraded).
    pg_label = None
    pg_error = None
    if db.pg_available():
        try:
            labels = db.read_account_labels([address]) or {}
            pg_label = labels.get(address)
        except Exception as e:
            pg_error = f"database read failed ({type(e).__name__})"

    _fetcher = _make_fetcher()
    account_data, ledger_error, page_sourcing = _query_ledger(address, _fetcher)

    signals: list[dict] = []
    couldnt_check: list[dict] = []

    # --- Signal 1: curated identity claim -----------------------------
    if named_entry.get("verified_via"):
        signals.append(_signal(
            label="Identity attestation",
            value=(
                f"Listed as “{named_entry.get('name')}” "
                f"in a first-party disclosure file."
            ),
            source_label="curator: named_accounts.json (attested via "
                         f"{named_entry.get('verified_via')})",
            source_url=named_entry.get("verified_via"),
        ))
        tier = "verified"
    elif named_entry.get("name"):
        signals.append(_signal(
            label="Identity claim",
            value=(
                f"Labeled “{named_entry.get('name')}” by the site "
                f"curator; no independent attestation on file."
            ),
            source_label="curator: named_accounts.json",
            source_url=None,
        ))
        tier = "self"
    elif pg_label and pg_label.get("name"):
        signals.append(_signal(
            label="Identity claim",
            value=(
                f"Labeled “{pg_label.get('name')}” "
                f"(source: {pg_label.get('source') or 'unspecified'})."
            ),
            source_label=f"account_labels table · {pg_label.get('source') or 'n/a'}",
            source_url=None,
        ))
        tier = "self"
    else:
        signals.append(_signal(
            label="Identity claim",
            value="No identity claim on file for this address.",
            source_label="curator: named_accounts.json · account_labels table",
            source_url=None,
        ))
        tier = "bare"

    # --- Signal 2: on-chain existence + AccountRoot.Domain ------------
    if account_data:
        # Domain field (hex-encoded ASCII). Report presence + decoded
        # value; do NOT interpret it as a positive/negative signal.
        domain = _decode_domain_hex(account_data.get("Domain"))
        if domain:
            signals.append(_signal(
                label="Account Domain field",
                value=f"Set to “{domain}” on the AccountRoot object.",
                source_label="XRPL AccountRoot.Domain (via AccountInfo)",
                source_url=None,
            ))
        else:
            signals.append(_signal(
                label="Account Domain field",
                value="Empty (no self-declared domain on the AccountRoot).",
                source_label="XRPL AccountRoot.Domain (via AccountInfo)",
                source_url=None,
            ))

        seq = account_data.get("Sequence")
        if seq is not None:
            signals.append(_signal(
                label="On-chain activity",
                value=(
                    f"Account exists on the validated ledger; "
                    f"Sequence = {seq}."
                ),
                source_label="XRPL AccountInfo (validated ledger)",
                source_url=None,
            ))
    elif ledger_error == "not_found":
        signals.append(_signal(
            label="On-chain existence",
            value="No such account on the validated XRPL ledger.",
            source_label="XRPL AccountInfo (validated ledger)",
            source_url=None,
        ))
    else:
        couldnt_check.append(_couldnt(
            label="On-chain existence + Domain field",
            reason=ledger_error or "XRPL endpoints didn't answer in time",
        ))

    if pg_error:
        couldnt_check.append(_couldnt(
            label="account_labels lookup",
            reason=pg_error,
        ))

    # --- Phase 1 signal: OFAC SDN cross-check ------------------------
    ofac = _signal_ofac_sdn_match(address, "XRP")
    if "value" in ofac:
        signals.append(ofac)
    else:
        couldnt_check.append(ofac)

    # --- Ledger-level capabilities (Phase 3) -------------------------
    capabilities = _capability_signals(account_data) if account_data else []

    # --- Status line — routing, not verdict --------------------------
    if tier == "verified":
        status_line = (
            f"This XRPL address has a first-party identity attestation "
            f"on file. Verified identity does NOT mean it is safe to "
            f"send money to."
        )
    elif tier == "self":
        status_line = (
            "This XRPL address has an identity label on file, but no "
            "first-party attestation we can point at."
        )
    else:
        status_line = (
            "This XRPL address has no identity claim on file. No "
            "negative signals found in the sources we checked."
        )

    return {
        "kind": "wallet",
        "address": address,
        "address_short": _short_addr(address),
        "subject": _short_addr(address),
        "ref": address,
        "tier": tier,
        "status_line": status_line,
        "signals": signals,
        "capabilities": capabilities,
        "couldnt_check": couldnt_check,
        "checked_at_utc": _now_iso(),
        # Sourcing flag from the SovereignFetcher used for the on-chain
        # AccountInfo call. One of SOURCING_SOVEREIGN / SOURCING_FALLBACK
        # / SOURCING_NO_TUNNEL. Consumed by the /check human template to
        # render the fallback/no-tunnel banner AND by /check.json machine
        # callers so agents can see the sovereignty state per query.
        "sourcing": page_sourcing,
        # Next-action link for Fold 1 — always /wallet/<addr>, the
        # visitor's route into the fuller detail view.
        "next_action": {
            "label": "View this wallet's activity",
            "href": f"/wallet/{address}",
        },
    }


# ─────────────────────────────────────────────────────────────────────
# Token check (D2). Input = (currency, issuer). Tier logic mirrors the
# wallet path but the amber trap is louder here — "issuer has a website
# and a name" reads as legit by default, and it must not.
# ─────────────────────────────────────────────────────────────────────

def check_token(currency: str, issuer: str) -> dict:
    """Build the /check D2 result for a (currency, issuer) pair.

    Tiers:
      verified — token_names.json entry with a real `verified_via` URL
                 (not the TODO_curation_pass placeholder). First-party
                 attestation vetted at curation time. Green pill still
                 carries the "does not mean safe" tooltip.
      self     — any of: (a) token_names.json entry pending curation,
                 (b) issuer has an on-chain Domain set, (c) issuer in
                 named_accounts.json (identity claim but not for this
                 specific currency code). AMBER trap: user reads
                 "they have a website" as "kind of legit" — the amber
                 tooltip must actively kill that inference.
      bare     — no token_names entry, no issuer Domain, no named entry.
                 Just an issuer address plus a currency code.
    """
    currency_norm = _normalize_currency(currency)
    currency_disp = _display_currency(currency_norm)
    issuer = (issuer or "").strip()

    token_names = _load_token_names()
    named = _load_named()

    key = f"{currency_norm}:{issuer}"
    # token_names.json historically used the ASCII form for 3-char codes
    # (e.g. "USD:rvYAf…"). Try both keys to be robust.
    tn_entry = token_names.get(key)
    if tn_entry is None and len(currency_norm) <= 3:
        tn_entry = token_names.get(f"{currency.strip().upper()}:{issuer}")
    tn_entry = tn_entry or {}
    named_entry = named.get(issuer) or {}

    # On-chain Domain read from the issuer's AccountRoot.
    _fetcher = _make_fetcher()
    account_data, ledger_error, page_sourcing = _query_ledger(issuer, _fetcher)
    domain = _decode_domain_hex((account_data or {}).get("Domain")) \
        if account_data else None

    signals: list[dict] = []
    couldnt_check: list[dict] = []

    # NEW-1 (2026-09-06): ticker-collision signal — pushed to the FRONT of
    # the signals list so a reader checking a "USDT" token from an issuer
    # that isn't Tether sees the collision before any other signal. Uses
    # the AccountRoot Domain field (if we already have it via _query_ledger)
    # as the collision hint. Silent when no collision applies.
    from token_naming import resolve_display as _rd_check
    _dom_hint = domain if 'domain' in dir() else None
    _col = _rd_check(currency, issuer, issuer_domain=_dom_hint)
    if _col.get("collision"):
        signals.append(_signal(
            label="Ticker collision",
            value=(
                f"This token uses the ticker “{_col['collision']['ticker']}” "
                f"but the issuer is not the canonical XRPL issuer for that name — "
                f"this is {_col['collision']['note']}. Issuer: {_col['collision']['issuer_hint']}."
            ),
            source_label="curator: ticker_canonical_issuers.json",
            source_url=None,
        ))
    if _col.get("non_standard"):
        signals.append(_signal(
            label="Non-standard currency code",
            value=(
                "This token's on-chain currency code does not decode to a "
                "printable name. Raw hex is shown for reference; there is "
                "no reader-friendly ticker to compare against."
            ),
            source_label="XRPL currency code (raw)",
            source_url=None,
        ))

    # --- Tier decision ------------------------------------------------
    verified_url = tn_entry.get("verified_via")
    has_real_verify = bool(
        verified_url and verified_url != _TOML_TODO_MARKER
    )
    display_name = tn_entry.get("currency_display") or currency_disp

    if has_real_verify:
        tier = "verified"
        signals.append(_signal(
            label="Token attestation",
            value=(
                f"Listed as “{display_name}” issued by this address "
                f"in a first-party disclosure that was verified when "
                f"we curated this entry."
            ),
            source_label=f"curator: token_names.json (attested via {verified_url})",
            source_url=verified_url,
        ))
    elif tn_entry.get("currency_display") \
            or domain \
            or named_entry.get("name"):
        tier = "self"
        # The Bitstamp trap: if the issuer address is in named_accounts,
        # naming the entity in the amber card risks lending its credibility
        # to a token that entity never actually vouched for. Split the
        # recognition out into its own signal with explicit decoupling,
        # and never fold it into "self-reported identity" (the entity
        # didn't self-report — we labeled the address).
        recognized_name = named_entry.get("name")
        if recognized_name:
            signals.append(_signal(
                label="Known address, unverified token",
                value=(
                    f"This address is associated with {recognized_name} — "
                    f"but {recognized_name} has NOT confirmed this specific "
                    f"token. Anyone can create a token with any name from "
                    f"any address. The name shown here is not verified by "
                    f"{recognized_name}."
                ),
                source_label="named_accounts.json (address label only)",
                source_url=None,
            ))
        # Genuine self-reported bits: token_names entry + on-chain Domain.
        # named_accounts is deliberately excluded here — it's our label,
        # not something the issuer self-declared.
        parts = []
        if tn_entry.get("currency_display"):
            parts.append(
                f"our token registry has an unverified entry naming "
                f"this “{tn_entry['currency_display']}”"
            )
        if domain:
            parts.append(f"the issuer's on-chain Domain reads “{domain}”")
        if parts:
            reason = "; ".join(parts)
            signals.append(_signal(
                label="Self-reported identity",
                value=(
                    f"The issuer has filled in some details about itself "
                    f"(specifically: {reason}), but nothing independent "
                    f"confirms who's really behind this."
                ),
                source_label=(
                    "curator: token_names.json (unverified) · "
                    "XRPL AccountRoot.Domain"
                ),
                source_url=None,
            ))
    else:
        tier = "bare"
        signals.append(_signal(
            label="Identity claim",
            value=(
                f"No self-reported identity for currency "
                f"{currency_disp} issued by this address."
            ),
            source_label=(
                "curator: token_names.json · "
                "XRPL AccountRoot.Domain · named_accounts.json"
            ),
            source_url=None,
        ))

    # --- Issuer on-chain existence signal (same shape as wallet) ------
    if account_data:
        seq = account_data.get("Sequence")
        if seq is not None:
            signals.append(_signal(
                label="Issuer on-chain",
                value=(
                    f"Issuer account exists on the validated ledger; "
                    f"Sequence = {seq}."
                ),
                source_label="XRPL AccountInfo (validated ledger)",
                source_url=None,
            ))
        if domain:
            signals.append(_signal(
                label="Issuer Domain field",
                value=f"Set to “{domain}” on the AccountRoot object.",
                source_label="XRPL AccountRoot.Domain (via AccountInfo)",
                source_url=None,
            ))
        else:
            signals.append(_signal(
                label="Issuer Domain field",
                value="Empty (no self-declared domain on the AccountRoot).",
                source_label="XRPL AccountRoot.Domain (via AccountInfo)",
                source_url=None,
            ))
    elif ledger_error == "not_found":
        signals.append(_signal(
            label="Issuer on-chain",
            value="No such issuer account on the validated XRPL ledger.",
            source_label="XRPL AccountInfo (validated ledger)",
            source_url=None,
        ))
    else:
        couldnt_check.append(_couldnt(
            label="Issuer on-chain existence + Domain field",
            reason=ledger_error or "XRPL endpoints didn't answer in time",
        ))

    # --- Phase 1 signal: OFAC SDN cross-check on the issuer address ---
    ofac = _signal_ofac_sdn_match(issuer, "XRP")
    if "value" in ofac:
        signals.append(ofac)
    else:
        couldnt_check.append(ofac)

    # --- Ledger-level capabilities (Phase 3) — issuer flags ----------
    capabilities = _capability_signals(account_data) if account_data else []

    # --- Status line — plain language per Charlie's D2 rewrite --------
    if tier == "verified":
        status_line = (
            "This token has a first-party attestation on file. Verified "
            "identity does NOT mean it is safe to send money to."
        )
    elif tier == "self":
        # Bitstamp-trap override: when the issuer address is in
        # named_accounts, the status line MUST actively decouple our
        # recognition of the address from any endorsement of this
        # specific token. Otherwise the user reads "Bitstamp" and
        # infers legitimacy the exchange never granted.
        if named_entry.get("name"):
            _n = named_entry["name"]
            status_line = (
                f"This address is associated with {_n} — but {_n} has "
                f"NOT confirmed this specific token. Anyone can create "
                f"a token with any name from any address. The name "
                f"shown here is not verified by {_n}."
            )
        else:
            status_line = (
                "The issuer has filled in some details about itself "
                "(like a website or a token name), but nothing "
                "independent confirms who's really behind this. These "
                "details are self-reported — anyone can enter them. "
                "Not confirmed by any outside source."
            )
    else:
        status_line = (
            "No identity claim on file for this token. No negative "
            "signals found in the sources we checked."
        )

    subject = f"{currency_disp} · {_short_addr(issuer)}"

    return {
        "kind": "token",
        "currency": currency_disp,
        "currency_normalized": currency_norm,
        "issuer": issuer,
        "issuer_short": _short_addr(issuer),
        "subject": subject,
        "ref": f"{currency_norm}.{issuer}",
        "tier": tier,
        "status_line": status_line,
        "signals": signals,
        "capabilities": capabilities,
        "couldnt_check": couldnt_check,
        "checked_at_utc": _now_iso(),
        "sourcing": page_sourcing,
        "next_action": {
            "label": "View this token's activity",
            "href": f"/token/{currency_norm}/{issuer}",
        },
    }


# ─────────────────────────────────────────────────────────────────────
# URL / domain check (D3). Input = a domain or URL. Same tier
# discipline as D1/D2. The traps:
#   - TOML present ≠ safe: anyone can publish an xrp-ledger.toml.
#     Verified requires the TWO-WAY proof — declared account's
#     on-chain Domain field points back to this domain.
#   - Lookalike (URL-version of the Bitstamp trap): a domain that
#     contains a well-known brand name but ISN'T that brand's
#     published domain. Surface it honestly, without accusing.
# ─────────────────────────────────────────────────────────────────────

def _extract_hostname(raw: str) -> str | None:
    """Turn a paste like `https://Bitstamp.NET/foo?a=1` into
    `bitstamp.net`. Returns None if the input isn't a plausibly
    parseable URL/host. Rejects userinfo (`user@host`) and non-ASCII."""
    if not raw:
        return None
    s = raw.strip()
    if not s or "@" in s:
        return None
    if "://" in s:
        try:
            parsed = urlparse(s)
        except Exception:
            return None
        host = (parsed.hostname or "").strip().lower()
    else:
        # Bare host — strip any accidental trailing /path or :port
        host = s.split("/", 1)[0].split(":", 1)[0].strip().lower()
    if not host or not host.isascii():
        return None
    return host


# Category allowlist for brand map — descriptions like "User Withdraw
# Account" / "Evan Wyn Evans" / "EBIT Token Issuer" aren't brands. Only
# categories that actually name a well-known brand get in.
_BRAND_CATEGORY_ALLOW = {"exchange", "issuer", "ripple", "bridge", "yield", "oracle"}

# Name patterns that mark an entry as a per-account instance of a
# larger brand ("Ripple Escrow #04") — strip these before deriving the
# brand's first-word probe.
_BRAND_NAME_STRIP_RE = re.compile(
    r"\s*(?:—|-|#\d+|escrow|deposit|withdrawal|treasury|account|issuer)\b.*$",
    re.IGNORECASE,
)


def _registered_root(host: str) -> str:
    """Approximate registered domain: last two labels of the host.
    Wrong for two-label public suffixes like `co.uk`, but good enough
    for the brand-family match used by the lookalike check."""
    parts = (host or "").lower().split(".")
    if len(parts) < 2:
        return host or ""
    return ".".join(parts[-2:])


def _load_brand_domains() -> dict[str, set[str]]:
    """Build brand-name → {registered-domain roots} map from
    named_accounts.json entries with a `verified_via` URL. Filters:
      - category must be in _BRAND_CATEGORY_ALLOW
      - name-first-word (post-strip) must be ≥4 chars, alphanumeric
      - brand's first-word must appear in one of its verified_via roots
        (kills mismatches where verified_via points at a Medium article
        or a xrpl.org doc rather than the brand's own domain)
    Values are registered-domain roots (blog.bitstamp.net → bitstamp.net)
    so a lookalike check treats `bitstamp.net` and `www.bitstamp.net`
    as legitimate family members, not typosquats."""
    brands: dict[str, set[str]] = {}

    for entry in _load_named().values():
        via = (entry or {}).get("verified_via")
        name = (entry or {}).get("name")
        category = (entry or {}).get("category") or (entry or {}).get("role")
        if not (via and name):
            continue
        if category not in _BRAND_CATEGORY_ALLOW:
            continue
        cleaned = _BRAND_NAME_STRIP_RE.sub("", name).strip().lower()
        if not cleaned:
            continue
        first_word = cleaned.split()[0]
        if len(first_word) < 4 or not first_word.isalnum():
            continue
        try:
            host = urlparse(via).netloc.split(":")[0].lower()
        except Exception:
            host = ""
        if not host:
            continue
        root = _registered_root(host)
        # Sanity: brand's first-word should appear in its own root; else
        # verified_via points somewhere else (article, docs, etc.) and
        # that root would produce nonsense lookalike calls.
        if first_word not in root:
            continue
        brands.setdefault(first_word, set()).add(root)

    return brands


def _find_lookalike(domain: str, brands: dict[str, set[str]]) -> tuple[str, set[str]] | None:
    """Return (brand_first_word, known_roots) if `domain` contains a
    brand's first-word substring AND is NOT within any of that brand's
    known registered-domain families. Legit if `domain == root` or
    `domain.endswith("." + root)` for any root.

    Multi-brand safety: if the input is legit for one brand it won't
    fire for another (early return covers that)."""
    d = domain.lower()
    for probe, roots in brands.items():
        # Legitimate family member for this brand? Not a lookalike.
        if any(d == r or d.endswith("." + r) for r in roots):
            return None
        if probe in d:
            return probe, roots
    return None


def _fetch_toml_fast(domain: str) -> tuple[str, dict | None, str | None]:
    """Request-path TOML fetch. Mirrors verify_toml_accounts.fetch_toml
    but with a shorter timeout (5s vs 10s) so the paste-box doesn't
    hang. Returns (final_url, parsed_toml_or_None, error_reason_or_None).
    Same SSRF, off-host-redirect, and HTML-masquerade defenses."""
    if not _domain_is_safe(domain):
        return (
            f"https://{domain}/.well-known/xrp-ledger.toml",
            None,
            "rejected by domain regex gate",
        )
    url = f"https://{domain}/.well-known/xrp-ledger.toml"
    req = urllib.request.Request(url, headers={"User-Agent": _D3_USER_AGENT})
    try:
        with urllib.request.urlopen(
            req, timeout=_D3_HTTP_TIMEOUT, context=_D3_SSL_CTX,
        ) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            body = resp.read(512_000)
            final_url = resp.geturl()
    except urllib.error.HTTPError as e:
        return url, None, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return url, None, f"fetch failed ({type(e).__name__})"
    except Exception as e:
        return url, None, f"unexpected fetch error ({type(e).__name__})"

    final_host = urlparse(final_url).netloc.split(":")[0].lower()
    if not (
        final_host == domain
        or final_host.endswith("." + domain)
        or domain.endswith("." + final_host)
        or final_host == "www." + domain
    ):
        return final_url, None, f"redirected off-host to {final_host}"

    head = body[:512].lstrip().lower()
    if (
        head.startswith(b"<!doctype")
        or head.startswith(b"<html")
        or "text/html" in ctype
    ):
        return final_url, None, "served HTML, not a TOML file"

    try:
        parsed = tomllib.loads(body.decode("utf-8", errors="replace"))
    except tomllib.TOMLDecodeError as e:
        return final_url, None, f"TOML parse error ({e})"
    return final_url, parsed, None


def _matchback_for_account(address: str, target_domain: str) -> tuple[bool, str | None]:
    """For a declared account, read its on-chain Domain and check
    whether it points back to `target_domain`. Returns (matches, decoded_domain_or_None).
    (False, None) covers both "no Domain set" and "account not found";
    (False, "otherdomain.com") means Domain is set but points elsewhere.

    A match is any within-family relation: exact, subdomain either way,
    or same registered root. So target `xrpl-labs.com` matches on-chain
    `validator.xrpl-labs.com` and vice versa."""
    account_data, err, _sourcing = _query_ledger(address)
    if err or not account_data:
        return False, None
    domain = _decode_domain_hex(account_data.get("Domain"))
    if not domain:
        return False, None
    d = domain.lower()
    t = target_domain.lower()
    if d == t:
        return True, domain
    if d.endswith("." + t) or t.endswith("." + d):
        return True, domain
    if _registered_root(d) == _registered_root(t):
        return True, domain
    return False, domain


def check_url(raw_input: str) -> dict:
    """Build the /check D3 result for a URL or bare domain.

    Tiers:
      verified — TOML present with [[ACCOUNTS]] block, and at least one
                 declared account's on-chain Domain field points back
                 to this domain. Two-way proof — the only tier that
                 justifies a green pill for a URL.
      self     — TOML present but no two-way match, OR domain matches
                 an issuer's on-chain Domain but hosts no TOML we can
                 verify. Half of the two-way proof only.
      bare     — no TOML, no on-chain Domain pointing here, no
                 lookalike issue. No identity claim on file.

    Lookalike override (applies to any tier): if the input domain
    contains a known brand name AND is not one of that brand's known
    domains, the top signal + status line actively decouple.
    """
    host = _extract_hostname(raw_input)
    if not host:
        return {
            "kind": "url",
            "subject": raw_input,
            "ref": raw_input,
            "tier": "bare",
            "status_line": "That does not look like a domain or URL we can check.",
            "signals": [],
            "couldnt_check": [],
            "checked_at_utc": _now_iso(),
            "next_action": None,
        }
    if not _domain_is_safe(host):
        return {
            "kind": "url",
            "subject": host,
            "ref": host,
            "tier": "bare",
            "status_line": (
                "That domain fails a basic safety gate (private-network "
                "IP, invalid shape, or off-standard characters). "
                "Nothing to check."
            ),
            "signals": [],
            "couldnt_check": [],
            "checked_at_utc": _now_iso(),
            "next_action": None,
        }

    signals: list[dict] = []
    couldnt_check: list[dict] = []

    # --- TOML fetch ---------------------------------------------------
    toml_url, toml_data, toml_err = _fetch_toml_fast(host)
    accounts_declared: list[dict] = []
    if toml_data is not None:
        raw_accounts = toml_data.get("ACCOUNTS") or []
        accounts_declared = [b for b in raw_accounts if isinstance(b, dict)]
        signals.append(_signal(
            label="XRPL identity file",
            value=(
                f"{host} publishes an xrp-ledger.toml with "
                f"{len(accounts_declared)} declared account(s)."
            ),
            source_label=toml_url,
            source_url=toml_url,
        ))
    else:
        couldnt_check.append(_couldnt(
            label="XRPL identity file (xrp-ledger.toml)",
            reason=toml_err or "unreachable",
        ))

    # --- Two-way check: up to 10 declared accounts. Cheap when local
    # rippled is up, still bounded by request-latency budget. Short-circuit
    # once we've found a match — one confirmed pair proves ownership.
    matched_count = 0
    checked_pairs: list[tuple[str, bool, str | None]] = []
    _CHECK_CAP = 10
    for block in accounts_declared[:_CHECK_CAP]:
        addr = (block or {}).get("address")
        if not addr:
            continue
        matches, decoded = _matchback_for_account(addr, host)
        checked_pairs.append((addr, matches, decoded))
        if matches:
            matched_count += 1
            break  # one is enough

    if checked_pairs:
        lines = []
        for addr, matches, decoded in checked_pairs:
            if matches:
                if decoded and decoded.lower() != host:
                    lines.append(
                        f"{_short_addr(addr)}: on-chain Domain = {decoded} "
                        f"(same domain family as {host}) ✓"
                    )
                else:
                    lines.append(f"{_short_addr(addr)}: on-chain Domain = {host} ✓")
            elif decoded:
                lines.append(
                    f"{_short_addr(addr)}: on-chain Domain = {decoded} "
                    f"(does not match {host})"
                )
            else:
                lines.append(
                    f"{_short_addr(addr)}: no on-chain Domain field set"
                )
        checked_n = len(checked_pairs)
        declared_n = len(accounts_declared)
        overflow = ""
        if matched_count > 0 and declared_n > checked_n:
            overflow = (
                f" (stopped at first match — {checked_n} of "
                f"{declared_n} declared checked)"
            )
        elif declared_n > checked_n:
            overflow = f" (checked first {checked_n} of {declared_n} declared)"
        signals.append(_signal(
            label="Two-way match on the ledger",
            value=" · ".join(lines) + overflow,
            source_label="XRPL AccountInfo (validated ledger) per declared account",
            source_url=None,
        ))

    # --- Lookalike detection -----------------------------------------
    brands = _load_brand_domains()
    lookalike = _find_lookalike(host, brands)

    # --- Tier decision -----------------------------------------------
    if matched_count > 0:
        tier = "verified"
    elif toml_data is not None or _is_domain_on_chain_for_some_issuer(host):
        tier = "self"
    else:
        tier = "bare"

    # Lookalike trap: prepend a dedicated top signal + override status.
    # The tier itself stays honest (green stays green if the two-way
    # match exists), but a lookalike SHOULD never be verified — that
    # would only happen if brand-substring matched AND two-way matched,
    # which by construction can't happen (the two-way match implies
    # the domain IS the brand's real domain, so it'd be in known set).
    if lookalike:
        probe, known_roots = lookalike
        brand_display = probe.capitalize()
        known_list = ", ".join(sorted(known_roots)) or "not published"
        signals.insert(0, _signal(
            label="Known-brand lookalike",
            value=(
                f"This domain contains \u201C{probe}\u201D but is NOT "
                f"within the domain family {brand_display} publishes its "
                f"XRPL identity from ({known_list}). Anyone can register "
                f"a domain containing a well-known name."
            ),
            source_label="named_accounts.json (brand → registered-domain map)",
            source_url=None,
        ))

    # --- Status line -------------------------------------------------
    if lookalike:
        probe, known_roots = lookalike
        brand_display = probe.capitalize()
        known_list = ", ".join(sorted(known_roots)) or "not published"
        status_line = (
            f"This domain is NOT within the domain family {brand_display} "
            f"publishes its XRPL identity from ({known_list}). Anyone can "
            f"register a domain that contains a well-known name — this "
            f"is not the domain {brand_display} publishes its identity from."
        )
    elif tier == "verified":
        status_line = (
            "This domain publishes an XRPL identity file, and at least "
            "one declared account confirms this domain on the ledger. "
            "Two-way match on file. Verified identity does NOT mean "
            "the domain is safe to interact with."
        )
    elif tier == "self":
        if toml_data is not None:
            status_line = (
                "This domain publishes an XRPL identity file, but none "
                "of the declared accounts confirm this domain on the "
                "ledger. The declaration is self-reported — anyone can "
                "publish an xrp-ledger.toml. Not confirmed by any "
                "outside source."
            )
        else:
            status_line = (
                "An XRPL issuer's on-chain Domain field points to this "
                "domain, but the domain itself does not publish an "
                "xrp-ledger.toml we can read. Only one side of the "
                "two-way check is present."
            )
    else:
        status_line = (
            "No XRPL identity file at this domain, and no on-chain "
            "issuer points here. No negative signals found in the "
            "sources we checked."
        )

    # Phase 1 structural signals: domain age (RDAP) + earliest HTTPS
    # cert (Certificate Transparency). Each degrades to couldnt_check on
    # network failure — never blocks the paste-box result.
    age_signal = _signal_domain_age(host)
    if "value" in age_signal:
        signals.append(age_signal)
    else:
        couldnt_check.append(age_signal)

    cert_signal = _signal_earliest_ssl_cert(host)
    if "value" in cert_signal:
        signals.append(cert_signal)
    else:
        couldnt_check.append(cert_signal)

    return {
        "kind": "url",
        "subject": host,
        "ref": host,
        "tier": tier,
        "status_line": status_line,
        "signals": signals,
        "couldnt_check": couldnt_check,
        "checked_at_utc": _now_iso(),
        "next_action": (
            {"label": "View the TOML file", "href": toml_url}
            if toml_data is not None else None
        ),
    }


def _is_domain_on_chain_for_some_issuer(host: str) -> bool:
    """Cheap approximate check: does any named_accounts entry with a
    verified_via URL point at this host? This is the "reverse" side of
    the two-way check for the case where THIS domain hosts no TOML but
    an issuer's on-chain Domain still points here."""
    host = (host or "").lower()
    if not host:
        return False
    for entry in _load_named().values():
        via = (entry or {}).get("verified_via")
        if not via:
            continue
        try:
            netloc = urlparse(via).netloc.split(":")[0].lower()
        except Exception:
            continue
        if netloc == host:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────
# Unified paste-box dispatcher. The /check route calls this with the
# raw user input; it returns either a check_address / check_token result
# or an {"error": ...} sentinel so the route can render the error banner.
# ─────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────
# Message triage (D4). Input = a whole pasted message (SMS, DM, email
# body) that a scared user is trying to sanity-check. Extract every
# address / token / URL that looks real, run each through D1/D2/D3.
#
# HARD PRIVACY (contract with /privacy §2b):
#   - Nothing about the pasted content lands in the DB. Not in
#     page_views (POST is skipped by _log_page_view), not in any new
#     table (we don't create one), not in curator files.
#   - No permalink. Results render in-place on POST. Back button hits
#     the empty paste-box GET, not a rehydrated message.
#   - The only durable trace of a paste is an input_type='message'
#     count in the app request log (no content, no preview) — see
#     app.check_page POST branch. The 120-char preview described in
#     the design lives ONLY in the transient in-request logger line;
#     never anywhere queryable.
#
# EXTRACTION (conservative, per Gate D4-Q3):
#   - XRPL address: base58 gate + length 25-35 + 'r' prefix. Strict.
#   - Token: "SYMBOL.rIssuer" where issuer passes the address gate.
#   - URL/domain: domain-shaped strings that pass _domain_is_safe.
#     Bare domains OK (catches "bitstamp-verify.com" without scheme)
#     but only real domain shapes — no every-word-with-a-dot noise.
# False positives here aren't just noise, they're MISLEADING signals.
# ─────────────────────────────────────────────────────────────────────

# XRPL base58 alphabet — Ripple flavour (excludes 0, O, I, l as in
# Bitcoin's base58 but with the Ripple-specific dictionary permutation).
_XRPL_BASE58 = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
_XRPL_ADDR_RE = re.compile(rf"\br[{_XRPL_BASE58}]{{24,34}}\b")

# Token pattern: SYMBOL then '.' then r-address. SYMBOL is 3-40 chars,
# ASCII letters/digits (matches _CURRENCY_ASCII_CHARS space in app.py
# less the punctuation forms which are extremely rare in-message).
_TOKEN_RE = re.compile(
    rf"\b([A-Za-z0-9]{{3,40}})\.(r[{_XRPL_BASE58}]{{24,34}})\b"
)

# Domain/URL pattern: scheme-optional. Requires ≥2 dot-separated labels
# with a TLD-shaped last label (2-24 alpha chars). Rejects IP-shaped
# things (all-numeric labels) at extraction; _domain_is_safe re-checks.
# The (?:https?://)? scheme is captured but the group also matches bare
# domains — the actual scheme-vs-bare test happens in check_url.
_URL_RE = re.compile(
    r"(?<![\w.])"                            # not preceded by word char / dot
    r"(?:https?://)?"                        # optional scheme
    r"([a-zA-Z0-9][-a-zA-Z0-9]{0,62}"        # first label
    r"(?:\.[a-zA-Z0-9][-a-zA-Z0-9]{0,62})*"  # middle labels
    r"\.[a-zA-Z]{2,24})"                     # TLD (alpha only, 2-24)
    r"(?::\d{1,5})?"                         # optional port
    r"(?:/[^\s<>\"']*)?"                     # optional path
    r"(?![\w.])"                             # not followed by word / dot
)

# TLD stop-list — extremely common false-positive TLDs from filenames
# and sentence fragments. If a "domain" hits one of these, drop it.
# Legit .txt/.py/.md-style file extensions get filtered even though
# they don't appear in the ICANN TLD list (the alpha-only TLD gate
# already handles most, but explicit skip is cheap insurance).
_URL_TLD_STOPLIST = {
    "txt", "py", "md", "js", "css", "html", "htm", "json", "yaml",
    "yml", "toml", "csv", "tsv", "log", "gz", "zip", "tar", "jpg",
    "jpeg", "png", "gif", "svg", "pdf", "doc", "docx", "xls", "xlsx",
    "mp3", "mp4", "mov", "avi",
}


def _extract_addresses(text: str) -> list[str]:
    """Yield unique XRPL addresses in appearance order. Strict base58
    gate — an r-prefix word that isn't valid base58 length/charset is
    silently skipped."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _XRPL_ADDR_RE.finditer(text):
        addr = m.group(0)
        if 25 <= len(addr) <= 35 and addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def _extract_tokens(text: str) -> list[tuple[str, str]]:
    """Yield unique (symbol, issuer) tokens in appearance order. Uses
    _TOKEN_RE, then re-checks issuer against the base58 gate. The
    address-list is deduped against these tokens by the caller — an
    address that appears INSIDE a token form shouldn't ALSO be listed
    as a bare wallet address."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(text):
        symbol, issuer = m.group(1), m.group(2)
        if not (25 <= len(issuer) <= 35):
            continue
        key = (symbol.upper(), issuer)
        if key not in seen:
            seen.add(key)
            out.append((symbol, issuer))
    return out


def _extract_urls(text: str) -> list[str]:
    """Yield unique domain-shaped strings in appearance order. Returns
    hostnames (lowercased, path/scheme stripped) so the render is
    de-noised — pasting "https://Bitstamp-Verify.com/path" and
    "bitstamp-verify.com" collapses to one card. _domain_is_safe
    re-gates each candidate so private-IP shapes and control chars
    can't reach the URL check."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        candidate = m.group(1).lower().strip(".")
        # Drop obvious file-extension false positives.
        tld = candidate.rsplit(".", 1)[-1]
        if tld in _URL_TLD_STOPLIST:
            continue
        if not _domain_is_safe(candidate):
            continue
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


# Cap displayed cards per type. Total = 5 shown + "view all N" bumper.
_MESSAGE_DISPLAY_CAP = 5


def check_message(text: str) -> dict:
    """Build the /check D4 message-triage result.

    Extract every plausible address / token / URL from the pasted
    message and run each through the same D1/D2/D3 pipeline. Group
    the results by type, cap at 5 per group, and lead with a plain
    one-line summary orienting the (often scared) user.

    Privacy: neither the text nor any derived preview is written into
    the returned dict for storage. The caller must NOT persist the
    incoming `text` to any store. All extracted subjects (addresses,
    domains, symbols) become part of the rendered page but are not
    logged beyond that single response.
    """
    if not text or not text.strip():
        return {
            "kind": "message",
            "subject": "message",
            "ref": "message",
            "tier": "bare",
            "status_line": (
                "That looks empty. Paste the message you want to check."
            ),
            "summary": "",
            "groups": {"addresses": [], "tokens": [], "urls": []},
            "counts": {"addresses": 0, "tokens": 0, "urls": 0},
            "checked_at_utc": _now_iso(),
            "next_action": None,
        }

    # --- extract, dedupe address ⇢ token overlap ---------------------
    tokens = _extract_tokens(text)
    token_issuers = {issuer for _, issuer in tokens}
    addresses = [a for a in _extract_addresses(text) if a not in token_issuers]
    urls = _extract_urls(text)

    counts = {
        "addresses": len(addresses),
        "tokens": len(tokens),
        "urls": len(urls),
    }

    # --- per-target lookups (capped) --------------------------------
    address_results = [
        check_address(a) for a in addresses[:_MESSAGE_DISPLAY_CAP]
    ]
    token_results = [
        check_token(sym, iss) for sym, iss in tokens[:_MESSAGE_DISPLAY_CAP]
    ]
    url_results = [
        check_url(u) for u in urls[:_MESSAGE_DISPLAY_CAP]
    ]

    # --- summary line -----------------------------------------------
    parts = []
    if counts["addresses"]:
        parts.append(
            f"{counts['addresses']} address"
            + ("es" if counts["addresses"] != 1 else "")
        )
    if counts["tokens"]:
        parts.append(
            f"{counts['tokens']} token"
            + ("s" if counts["tokens"] != 1 else "")
        )
    if counts["urls"]:
        parts.append(
            f"{counts['urls']} link"
            + ("s" if counts["urls"] != 1 else "")
        )

    if not parts:
        summary = (
            "We didn't find any XRPL addresses, tokens, or domains in "
            "that message. There is nothing for us to check on."
        )
        status_line = (
            "Nothing checkable in this message. If it contains a "
            "wallet address, token, or link that we missed, paste "
            "that specific thing on its own."
        )
    else:
        if len(parts) == 1:
            joined = parts[0]
        elif len(parts) == 2:
            joined = f"{parts[0]} and {parts[1]}"
        else:
            joined = f"{parts[0]}, {parts[1]}, and {parts[2]}"
        summary = (
            f"This message contains {joined}. "
            f"Here's what we found on each."
        )
        status_line = (
            "We checked each XRPL address, token, and domain we could "
            "find in the message. Each has its own signals below. "
            "This is not a scam verdict — read the individual findings."
        )

    # --- tier: use the "worst" of the per-target tiers as the group
    # tier, but never verdict. verified > self > bare in the sense
    # that self/bare gets a neutral pill; we don't downgrade to a
    # warning color because a lookalike appeared. See feedback memory
    # feedback_check_lookalike_neutral_pill_only.
    all_results = address_results + token_results + url_results
    if not all_results:
        tier = "bare"
    elif all(r.get("tier") == "verified" for r in all_results):
        tier = "verified"
    elif any(r.get("tier") == "verified" for r in all_results):
        tier = "self"  # mixed; use neutral pill
    else:
        tier = "self" if any(r.get("tier") == "self" for r in all_results) else "bare"

    return {
        "kind": "message",
        "subject": "pasted message",
        "ref": "message",
        "tier": tier,
        "status_line": status_line,
        "summary": summary,
        "groups": {
            "addresses": address_results,
            "tokens": token_results,
            "urls": url_results,
        },
        "counts": counts,
        "display_cap": _MESSAGE_DISPLAY_CAP,
        "checked_at_utc": _now_iso(),
        # NO permalink — messages never get a shareable link (privacy).
        # NO next_action — the individual sub-cards each carry their own.
        "next_action": None,
    }


def check(raw_input: str) -> dict:
    """Parse the paste-box input and dispatch to the right lookup.

    Accepted forms:
      r…                     → wallet lookup (D1)
      SYMBOL.r…              → token lookup (D2), e.g. "USD.rvYAf…"
      HEX40.r…               → token lookup (D2), raw hex currency code

    Anything else returns {"error": "<message>"} so the route surfaces
    the input-error banner. Real address/currency validity lives in
    app.py; this function only splits on the delimiter."""
    s = (raw_input or "").strip()
    if not s:
        return {"error": "Enter an XRPL wallet address or a token as SYMBOL.rIssuer…"}

    # Token form: "SYMBOL.rIssuerAddress" — split on the FIRST "." only.
    if "." in s:
        symbol, _, issuer = s.partition(".")
        symbol = symbol.strip()
        issuer = issuer.strip()
        if not symbol or not issuer.startswith(_XRPL_ADDR_PREFIX):
            return {"error": (
                "Token form is SYMBOL.rIssuer — e.g. USD.rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B."
            )}
        return check_token(symbol, issuer)

    # Wallet form: bare r-address.
    if s.startswith(_XRPL_ADDR_PREFIX):
        return check_address(s)

    return {"error": (
        "That doesn't look like an XRPL wallet address (starts with 'r') "
        "or a token (SYMBOL.rIssuer)."
    )}

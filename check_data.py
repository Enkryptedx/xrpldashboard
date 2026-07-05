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
from xrpl.models.requests import AccountInfo

from xrpl_client import get_client

_HERE = os.path.dirname(os.path.abspath(__file__))
_NAMED_PATH = os.path.join(_HERE, "named_accounts.json")
_TOKEN_NAMES_PATH = os.path.join(_HERE, "token_names.json")

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
    """Reverse of _normalize_currency for display. 3-char passes through;
    hex-40 with trailing zeros unpacks back to ASCII when the bytes decode
    cleanly, otherwise renders as short-hex (e.g. "0x534F4C…000")."""
    if not cur_normalized:
        return cur_normalized
    if len(cur_normalized) <= 3:
        return cur_normalized
    try:
        raw = bytes.fromhex(cur_normalized).rstrip(b"\x00")
        if raw and all(32 <= b < 127 for b in raw):
            return raw.decode("ascii")
    except ValueError:
        pass
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


def _query_ledger(address: str) -> tuple[dict | None, str | None]:
    """Return (account_data, error_reason). account_data is None if the
    account doesn't exist on-chain or if all endpoints failed."""
    try:
        client = get_client(walker_name="check_page")
        resp = client.request(
            AccountInfo(account=address, ledger_index="validated")
        )
    except Exception as e:
        return None, f"XRPL endpoints unreachable ({type(e).__name__})"

    result = resp.result or {}
    err = result.get("error")
    if err == "actNotFound":
        return None, "not_found"
    if err:
        return None, f"XRPL returned '{err}'"

    return result.get("account_data") or {}, None


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

    account_data, ledger_error = _query_ledger(address)

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
        "couldnt_check": couldnt_check,
        "checked_at_utc": _now_iso(),
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
    account_data, ledger_error = _query_ledger(issuer)
    domain = _decode_domain_hex((account_data or {}).get("Domain")) \
        if account_data else None

    signals: list[dict] = []
    couldnt_check: list[dict] = []

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
        "couldnt_check": couldnt_check,
        "checked_at_utc": _now_iso(),
        "next_action": {
            "label": "View this token's activity",
            "href": f"/token/{currency_norm}/{issuer}",
        },
    }


# ─────────────────────────────────────────────────────────────────────
# Unified paste-box dispatcher. The /check route calls this with the
# raw user input; it returns either a check_address / check_token result
# or an {"error": ...} sentinel so the route can render the error banner.
# ─────────────────────────────────────────────────────────────────────

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

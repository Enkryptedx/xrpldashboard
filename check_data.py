"""check_data.py — address → timestamped, sourced signals for /check D1.

Every signal returned carries: fact + source + checked_at_utc. Facts, not
verdicts. If a source can't be reached, it goes in `couldnt_check`, never
silently dropped — that's the VirusTotal "0/94 security vendors flagged
this" pattern for XRPL.

The status pill (`verified` / `self` / `bare`) summarizes WHAT IDENTITY
CLAIM EXISTS ON THE LEDGER, not whether the account is safe to interact
with. Green pill never renders alone — always paired with the signals
list and the "does not mean safe to send money" tooltip.

D1 scope: wallet r-address only.
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

# XRPL ripple-epoch offset: seconds between 1970-01-01 and 2000-01-01 UTC.
_RIPPLE_EPOCH = 946_684_800


def _load_named() -> dict:
    try:
        with open(_NAMED_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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
        "address": address,
        "address_short": _short_addr(address),
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

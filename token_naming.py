"""token_naming — single source of truth for how a token (currency, issuer)
is presented to a human across every surface: /tokens, /token detail,
/whales, /check, JSON twins.

Three responsibilities:

  1. Decode. 40-char hex currency codes → their ASCII names when the
     bytes are printable; junk bytes → the raw hex + a "non-standard"
     label. Short 3-char codes (USD, EUR, etc.) pass through unchanged.

  2. Ticker-collision guard. When a decoded name matches a well-known
     off-chain ticker (USDT, USDC, BTC, RLUSD, …) but the issuer is
     NOT one of that ticker's canonical XRPL issuers, render it as
     "USDT (usdxrp.net — not Tether)" instead of bare "USDT". Curated
     via ticker_canonical_issuers.json — issuer-qualified, never bare.

  3. Auditability. The raw hex is always returned alongside the display
     name so a JSON twin can carry both (`currency_raw`, `currency_display`)
     and the caller can render tooltips / secondary lines from the raw
     value without re-encoding logic.

Wired 2026-09-06 per Charlie's NEW-1 ask (gates the /token/ robots.txt
unblock). Existing per-file `_decode_currency_hex` implementations in
token_data.py / wallet_data.py / app.py delegate through here so all
future decode changes ripple from one file.

Non-goals: this module has no opinion on Charlie's editorial safety
copy. It ONLY resolves what to *show*; the ticker-collision note is
factual, not editorial ("not Tether" is not a scam judgment, it's a
statement of on-chain provenance).
"""
from __future__ import annotations

import json
import os
import threading
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
TICKER_CANONICAL_PATH = os.path.join(HERE, "ticker_canonical_issuers.json")

_lock = threading.Lock()
_ticker_map: Optional[dict] = None


def _load_ticker_map() -> dict:
    """Lazy-load the curated ticker-canonical-issuers map. Empty dict
    on missing/malformed file — better to fail-open (no collision
    detection) than to fail-closed (render nothing) if the file gets
    corrupted."""
    global _ticker_map
    with _lock:
        if _ticker_map is not None:
            return _ticker_map
        try:
            with open(TICKER_CANONICAL_PATH) as f:
                raw = json.load(f)
            _ticker_map = {
                k.upper(): {
                    "canonical_issuers": set(v.get("canonical_issuers") or []),
                    "note": v.get("note") or f"not {k}",
                    "brand": v.get("brand"),
                }
                for k, v in raw.items()
                if isinstance(v, dict)
            }
        except (OSError, json.JSONDecodeError, TypeError):
            _ticker_map = {}
        return _ticker_map


def decode_currency(currency: str) -> dict:
    """Decode one currency code to its display parts. Pure function
    (no issuer lookup — that's resolve_display's job).

    Returns dict:
      {
        "raw": original string,
        "display": human string ("USD", "RLUSD", or first 8 hex chars
                    of a non-standard 40-hex code),
        "kind": "xrp" | "short" | "decoded" | "junk",
        "is_hex_40": bool,
      }
    """
    if not currency:
        return {"raw": "", "display": "?", "kind": "short", "is_hex_40": False}
    if currency == "XRP":
        return {"raw": "XRP", "display": "XRP", "kind": "xrp", "is_hex_40": False}
    if len(currency) != 40:
        return {"raw": currency, "display": currency, "kind": "short", "is_hex_40": False}
    # 40-char hex path
    try:
        raw_bytes = bytes.fromhex(currency).rstrip(b"\x00")
    except ValueError:
        return {
            "raw": currency,
            "display": currency[:8].upper(),
            "kind": "junk",
            "is_hex_40": True,
        }
    if not raw_bytes or not all(32 <= c < 127 for c in raw_bytes):
        return {
            "raw": currency,
            "display": currency[:8].upper(),
            "kind": "junk",
            "is_hex_40": True,
        }
    try:
        ascii_name = raw_bytes.decode("ascii").strip()
    except UnicodeDecodeError:
        return {
            "raw": currency,
            "display": currency[:8].upper(),
            "kind": "junk",
            "is_hex_40": True,
        }
    return {
        "raw": currency,
        "display": ascii_name,
        "kind": "decoded",
        "is_hex_40": True,
    }


def resolve_display(
    currency: str,
    issuer: Optional[str] = None,
    issuer_domain: Optional[str] = None,
    issuer_hint: Optional[str] = None,
) -> dict:
    """Full display resolution including ticker-collision guard.

    Returns dict:
      {
        "raw":          original currency code,
        "decoded":      pure-decode display (no collision guard),
        "display":      final render-ready string (collision-qualified
                        if applicable, else just decoded),
        "kind":         "xrp"|"short"|"decoded"|"junk",
        "collision":    None | {
                           "ticker": "USDT",
                           "note":   "not Tether",
                           "issuer_hint": "usdxrp.net" | "rXYZ…",
                        },
        "non_standard": True when kind == "junk" (caller renders a
                        "non-standard code" label + raw hex),
      }

    issuer_domain: the AccountRoot.Domain field of the issuer, hex-decoded
      to ASCII (e.g. "usdxrp.net"). When present, used as the "issuer_hint"
      in a collision string. When absent, a short-address form of `issuer`
      is used instead so the collision label ALWAYS names something.
    issuer_hint: optional pre-formatted hint (curator override — the
      /token detail page can compute a nicer form once and pass it in).
    """
    d = decode_currency(currency)
    result = {
        "raw": d["raw"],
        "decoded": d["display"],
        "display": d["display"],
        "kind": d["kind"],
        "collision": None,
        "non_standard": d["kind"] == "junk",
    }

    # Only decoded (ASCII-clean) codes can collide with a known ticker.
    # Junk-hex, short (3-char), and XRP paths never trip collision logic.
    if d["kind"] != "decoded" and d["kind"] != "short":
        return result

    tmap = _load_ticker_map()
    upper = d["display"].upper()
    entry = tmap.get(upper)
    if not entry:
        return result

    canonical = entry["canonical_issuers"]
    if issuer and issuer in canonical:
        # Canonical issuance — display bare, no collision label.
        return result

    # Collision: matches a well-known ticker but issuer isn't canonical.
    hint = issuer_hint
    if hint is None:
        if issuer_domain:
            hint = issuer_domain
        elif issuer:
            hint = f"{issuer[:5]}…{issuer[-4:]}" if len(issuer) > 12 else issuer
        else:
            hint = "unknown issuer"
    result["collision"] = {
        "ticker": upper,
        "note": entry["note"],
        "issuer_hint": hint,
    }
    result["display"] = f"{d['display']} ({hint} — {entry['note']})"
    return result


# ─── Legacy compatibility shims — delegate here from other modules ──────
def decode_currency_hex_legacy(hex_str: str) -> Optional[str]:
    """Historical shape used by token_data / wallet_data. Returns the
    ASCII string on clean decode, else None. Kept so existing callers
    that fall back to their own display logic don't need rewriting."""
    if not hex_str or len(hex_str) != 40:
        return None
    d = decode_currency(hex_str)
    if d["kind"] == "decoded":
        return d["display"]
    return None

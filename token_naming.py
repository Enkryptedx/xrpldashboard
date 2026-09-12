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
NAMED_ACCOUNTS_PATH = os.path.join(HERE, "named_accounts.json")

_lock = threading.Lock()
_ticker_map: Optional[dict] = None
_bridge_index: Optional[dict] = None   # issuer → set of tickers it bridges
_named_lookup: Optional[dict] = None   # issuer address → named_accounts entry


def _load_ticker_map() -> dict:
    """Lazy-load the curated ticker-canonical-issuers map. Empty dict
    on missing/malformed file — better to fail-open (no collision
    detection) than to fail-closed (render nothing) if the file gets
    corrupted."""
    global _ticker_map, _bridge_index
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
                    # 2026-09-11 Circle-USDC ruling: track whether the
                    # entry has been audited (any canonical_issuers, or
                    # explicit no_official_xrpl_issuer=true). Empty list
                    # alone means UNAUDITED, not "positive no-legit-issuer
                    # knowledge" — the three-state wording branches on
                    # this in resolve_display below.
                    "audited": (bool(v.get("canonical_issuers"))
                                or bool(v.get("no_official_xrpl_issuer"))),
                    "no_official_xrpl_issuer": bool(v.get("no_official_xrpl_issuer")),
                    # 2026-09-12 memecoin narrowing (Charlie ruling):
                    # meme-name entries (PEPE, DOGE, SHIB, BONK, …) are
                    # NOT identity-bearing assets. Name reuse is expected
                    # practice for XRPL memecoins and deceives no one
                    # about backing. resolve_display below skips the
                    # collision path entirely for meme_name=true entries.
                    "meme_name": bool(v.get("meme_name")),
                }
                for k, v in raw.items()
                if isinstance(v, dict) and k != "bridges" and k != "gateways" and not k.startswith("_")
            }
            # Bridge + gateway whitelist (2026-09-08 + 2026-09-08 gateways):
            # issuer → set of tickers this bridge/gateway is authorized to
            # mint. Whitelisted (issuer, ticker) pairs are treated as
            # canonical (no collision label). Bridge entries typically map
            # to category=wrapped_bridge; gateway entries to
            # category=stablecoin_gateway. Both override the collision
            # path here — the category assignment is written elsewhere
            # (token_category_history via curator flip).
            index: dict[str, set[str]] = {}
            for section_name in ("bridges", "gateways"):
                section = raw.get(section_name) or {}
                for _bname, bdef in section.items():
                    if not isinstance(bdef, dict):
                        continue
                    issuers = bdef.get("issuers") or []
                    tickers = {t.upper() for t in (bdef.get("tickers") or [])}
                    for iss in issuers:
                        index.setdefault(iss, set()).update(tickers)
            _bridge_index = index
        except (OSError, json.JSONDecodeError, TypeError):
            _ticker_map = {}
            _bridge_index = {}
        return _ticker_map


def _load_named_accounts() -> dict:
    """Lazy-load named_accounts.json as issuer_address → entry dict.
    Used by the conflict rule in resolve_display: if named_accounts
    labels an issuer as brand X and ticker_canonical_issuers doesn't
    list that issuer, we render 'unverified — conflicting records'
    (curator queue) instead of a warning. Filed 2026-09-11 Circle-
    USDC ruling. Fail-open on missing/malformed file."""
    global _named_lookup
    with _lock:
        if _named_lookup is not None:
            return _named_lookup
        try:
            with open(NAMED_ACCOUNTS_PATH) as f:
                raw = json.load(f)
            _named_lookup = {
                addr: entry
                for addr, entry in raw.items()
                if isinstance(entry, dict)
            }
        except (OSError, json.JSONDecodeError, TypeError):
            _named_lookup = {}
        return _named_lookup


def _named_brand_match(issuer: Optional[str], ticker_upper: str,
                       brand: Optional[str]) -> bool:
    """Return True when named_accounts.json labels `issuer` in a way
    that plausibly matches this ticker's brand — i.e. the two data
    files are in CONFLICT (canonical list omits the issuer; named
    labels it as the brand). Comparison is case-insensitive substring
    of ticker OR brand vs. the named entry's name / _note / domain /
    verified_via."""
    if not issuer:
        return False
    named = _load_named_accounts()
    entry = named.get(issuer)
    if not entry:
        return False
    name = str(entry.get("name") or "")
    note = str(entry.get("_note") or "")
    dom = str(entry.get("domain") or "")
    vv = str(entry.get("verified_via") or "")
    blob = (name + " " + note + " " + dom + " " + vv).lower()
    if ticker_upper.lower() in blob:
        return True
    if brand and brand.lower() in blob:
        return True
    return False


def _is_bridge_issued(issuer: Optional[str], ticker_upper: str) -> bool:
    """Return True when (issuer, ticker) matches a bridge whitelist —
    means the bridge is authorized to mint this ticker on XRPL, so the
    ticker match is the bridged form, not a collision."""
    if not issuer:
        return False
    _load_ticker_map()  # ensures _bridge_index is populated
    tickers = _bridge_index.get(issuer)
    return bool(tickers and ticker_upper in tickers)


_canonical_neighbor_cache: Optional[list[tuple[str, str, str]]] = None
_LOOKALIKE_CAP_DEFAULT = 3   # 1-3 char swap is the vanity-generator scam shape


def _load_canonical_neighbors() -> list[tuple[str, str, str]]:
    """Return [(address, name, source), ...] over the union of canonical
    issuers, bridge issuers, and gateway issuers — the same three sets
    resolve_display() treats as canonical. Used by
    lookalike_canonical_issuer() below and by /admin/token-review to
    flag look-alike vanity impostors.

    Cached at module scope; call `_reset_canonical_neighbor_cache()` if
    ticker_canonical_issuers.json is edited at runtime."""
    global _canonical_neighbor_cache
    if _canonical_neighbor_cache is not None:
        return _canonical_neighbor_cache
    _load_ticker_map()  # populates _bridge_index too
    try:
        with open(TICKER_CANONICAL_PATH) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        _canonical_neighbor_cache = []
        return _canonical_neighbor_cache
    out: list[tuple[str, str, str]] = []
    # Top-level ticker entries — canonical_issuers list (e.g., RLUSD has one).
    for tk, td in raw.items():
        if tk.startswith("_") or tk in ("bridges", "gateways"):
            continue
        if not isinstance(td, dict):
            continue
        brand = td.get("brand") or tk
        for iss in (td.get("canonical_issuers") or []):
            out.append((iss, brand, f"canonical:{tk}"))
    # Bridges section
    for bname, bdef in (raw.get("bridges") or {}).items():
        if not isinstance(bdef, dict):
            continue
        name = bdef.get("name") or bname
        for iss in (bdef.get("issuers") or []):
            out.append((iss, name, f"bridge:{bname}"))
    # Gateways section
    for gname, gdef in (raw.get("gateways") or {}).items():
        if not isinstance(gdef, dict):
            continue
        name = gdef.get("name") or gname
        for iss in (gdef.get("issuers") or []):
            out.append((iss, name, f"gateway:{gname}"))
    _canonical_neighbor_cache = out
    return out


def _reset_canonical_neighbor_cache() -> None:
    """Clear the neighbor cache. Call after editing
    ticker_canonical_issuers.json at runtime (rarely — cache is fine
    for the process lifetime of the render loop)."""
    global _canonical_neighbor_cache
    _canonical_neighbor_cache = None


def _levenshtein(a: str, b: str, cap: int = 5) -> int:
    """Bounded Levenshtein distance with early exit above `cap`. Same
    shape as check_data._levenshtein — we duplicate the tiny function
    here rather than cross-import to keep token_naming import-free of
    check_data (avoids circular). Iterative two-row implementation;
    returns cap+1 if the true distance exceeds cap."""
    la, lb = len(a), len(b)
    if abs(la - lb) > cap:
        return cap + 1
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        row_min = i
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > cap:
            return cap + 1
        prev = cur
    return prev[lb]


_LOOKALIKE_PREFIX_MIN = 8   # 8-char shared prefix is the vanity-generator scam shape


def _shared_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def lookalike_canonical_issuer(address: str,
                                cap: int = _LOOKALIKE_CAP_DEFAULT,
                                prefix_min: int = _LOOKALIKE_PREFIX_MIN) -> Optional[dict]:
    """If `address` looks like a vanity-impostor of a canonical / bridge /
    gateway issuer, return {name, source, kind, distance|prefix_len,
    near_address}; else None. Two overlapping shapes flagged:

      1. Edit-distance ≤ `cap` (default 3): 1-3 char swap within a
         mostly-identical address. Attack shape: brute-force wallet
         until you land ≤3 chars off the target — computationally
         cheap for the attacker.

      2. Shared prefix ≥ `prefix_min` (default 8): first N chars
         identical to a canonical issuer's address. Attack shape:
         vanity-generator (each additional prefix char is 58× more
         work), so ≥8-char shared prefix with a canonical issuer is
         genuinely rare — and expensive to produce accidentally.

    Returns the STRONGER of the two matches when both fire (edit-distance
    wins on tie). `address` NOT considered a look-alike of itself —
    exact matches return None.

    `source` is one of:
      - "canonical:<ticker>" — matches a top-level canonical_issuers list
      - "bridge:<name>"      — matches an issuer in bridges section
      - "gateway:<name>"     — matches an issuer in gateways section
    `kind` is "edit-distance" or "prefix".

    Used by /admin/token-review to catch shapes like
    `rfmS3ZFBRe8W…wMy1` (SOL row #5, prefix `rfmS3` — actually only
    5-char shared, below default threshold, won't flag) vs a plausible
    stronger vanity impostor that shares 8+ chars of a canonical prefix."""
    if not address or len(address) < 25:
        return None
    neighbors = _load_canonical_neighbors()
    # Two candidate winners (one per kind) — return the stronger
    ed_winner: Optional[tuple[str, str, str, int]] = None
    ed_best_dist = cap + 1
    pfx_winner: Optional[tuple[str, str, str, int]] = None
    pfx_best_len = prefix_min - 1
    for cand_addr, cand_name, cand_source in neighbors:
        if cand_addr == address:
            return None  # exact match — canonical, not an impostor
        d = _levenshtein(address, cand_addr, cap=cap)
        if d <= cap and d < ed_best_dist:
            ed_winner = (cand_addr, cand_name, cand_source, d)
            ed_best_dist = d
        pl = _shared_prefix_len(address, cand_addr)
        if pl >= prefix_min and pl > pfx_best_len:
            pfx_winner = (cand_addr, cand_name, cand_source, pl)
            pfx_best_len = pl
    # Edit-distance wins on tie (more specific match).
    if ed_winner:
        return {
            "name": ed_winner[1], "source": ed_winner[2],
            "kind": "edit-distance", "distance": ed_winner[3],
            "near_address": ed_winner[0],
        }
    if pfx_winner:
        return {
            "name": pfx_winner[1], "source": pfx_winner[2],
            "kind": "prefix", "prefix_len": pfx_winner[3],
            "near_address": pfx_winner[0],
        }
    return None


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

    # Bridge/gateway-issued tickers: treat as canonical (issuer is
    # authorized to mint this XRPL representation of the ticker).
    # Overrides collision path — see ticker_canonical_issuers.json
    # § bridges and § gateways.
    if _is_bridge_issued(issuer, upper):
        return result

    # 2026-09-12 memecoin narrowing (Charlie ruling): meme-name
    # entries (PEPE, DOGE, SHIB, BONK, …) are NOT identity-bearing
    # assets. Any XRPL token with this ticker is a memecoin reusing
    # the name, which memecoins do constantly and which deceives no
    # one about backing. Skip the collision path entirely — the
    # curator queue keeps a "meme-name-reuse" tag internally for
    # awareness, but no public warning renders.
    if entry.get("meme_name"):
        return result

    # 2026-09-11 Circle-USDC ruling — three-state wording.
    # Prior code emitted a positive "not <brand>" claim for ANY ticker
    # match without a canonical-list entry. Empty canonical_issuers was
    # treated as knowledge — but empty means UNAUDITED. That misfired
    # on Circle's real USDC issuer (rGm7W…uWhE) from Sep 6 → Sep 11:
    # our USDC entry had canonical_issuers=[], so the real Circle
    # address rendered as "not Circle USDC" on /tokens, /whales, /check.
    #
    # New wording:
    #   1. canonical entries exist AND issuer isn't one    → "not <brand>"
    #      (positive knowledge, cited in the JSON)
    #   2. no canonical entries AND no_official_xrpl_issuer=true
    #      → also positive: "not <brand>" (we know none exist)
    #   3. empty canonical_issuers, no explicit no-issuer marker
    #      → NEUTRAL: "issuer not on our verified list for <brand>"
    #      A statement about OUR list, not about the issuer.
    #
    # PLUS conflict rule: if named_accounts.json labels this issuer as
    # the brand AND ticker canonical list is silent on it → mark
    # "unverified — conflicting records" (curator queue), NEVER warn.
    audited = entry.get("audited", bool(canonical))
    hint = issuer_hint
    if hint is None:
        if issuer_domain:
            hint = issuer_domain
        elif issuer:
            hint = f"{issuer[:5]}…{issuer[-4:]}" if len(issuer) > 12 else issuer
        else:
            hint = "unknown issuer"

    # Conflict path (highest priority): named_accounts thinks this
    # issuer IS the brand, but canonical list is silent. Do NOT warn.
    if not canonical and _named_brand_match(issuer, upper, entry.get("brand")):
        conflict_note = "unverified — conflicting records"
        result["collision"] = {
            "ticker": upper,
            "note": conflict_note,
            "issuer_hint": hint,
            "conflict": True,
        }
        result["display"] = f"{d['display']} ({hint} — {conflict_note})"
        return result

    # Choose wording per three-state rule.
    if audited:
        # Positive knowledge — either canonical exists (and this isn't
        # one) or no_official_xrpl_issuer flag is set.
        note = entry["note"]
    else:
        brand_label = entry.get("brand") or upper
        note = f"issuer not on our verified list for {brand_label}"

    result["collision"] = {
        "ticker": upper,
        "note": note,
        "issuer_hint": hint,
        "audited": audited,
    }
    result["display"] = f"{d['display']} ({hint} — {note})"
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

"""Shared tier/attestation resolver — one vocabulary, one source.

Consumed by /tokens label_lookup, /token detail, /whales badges, and /check.
Design ruled by Charlie 2026-09-10 morning (Part C):

- `token_category_current` view (db.py) is the FIRST source of truth.
- `d1_hero_snapshot.json` is FALLBACK ONLY — used only when DB is
  unreachable, or when a (currency, issuer) pair has no live row.
- Hero VERIFIED/SELF_DESCRIBED/DOMAIN_ONLY/ANONYMOUS counts come from
  `live_tier_counts()` which reads the same live view (falling back to
  the frozen snapshot only when DB is out).
- Auto-elevation via `two_way_toml_verifier_walker` (extracted core):
  invoked only when tier is SELF_DESCRIBED with a well-formed https
  citation URL AND our process-local 24h TTL cache hasn't attempted
  the pair recently. Fail-open — any verification error returns the
  current tier unchanged, never downgrades. On success, insert a new
  `token_category_history` row with `source='two_way_toml'`. Never a
  silent flag flip; `verified_via` citations alone never elevate.
  Elevation is opt-in (`elevate=True`); web routes default to False so
  requests never stall on toml fetches / XRPL RPC.

TIER VOCABULARY (display / external — what consumers see):
  VERIFIED       — two-way verified (curator, form-verified, toml-verified)
  SELF_DESCRIBED — issuer claims + citation; no two-way proof yet
  DOMAIN_ONLY    — mechanical / ledger-Domain-only signal
  ANONYMOUS      — bare: no self-description AND no ledger Domain

DB vocabulary (internal) is lowercase-with-hyphen:
  verified | self-described | curator-inferred | mechanical | bare
Translation is applied at read time; DB rows are never mutated to match
the display form.
"""
from __future__ import annotations
import json
import os
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

import db


HERE = os.path.dirname(os.path.abspath(__file__))
HERO_SNAPSHOT_PATH = os.path.join(HERE, "d1_hero_snapshot.json")

# Canonical tier vocabulary — lowercase-hyphen in-DB and in-code, Title
# Case on screen. Charlie ruling 2026-09-10 14:20 EDT: five tier values.
# `mechanical` and `sanctioned` are ROW-LEVEL FLAGS on separate columns,
# NOT tier values — do NOT map mechanical to labeled. `unknown` is
# reserved for lookup failure (row wasn't found or DB unreachable).
TIER_VERIFIED = "verified"
TIER_SELF_DESCRIBED = "self-described"
TIER_LABELED = "labeled"
TIER_BARE = "bare"
TIER_UNKNOWN = "unknown"
KNOWN_TIERS = (TIER_VERIFIED, TIER_SELF_DESCRIBED, TIER_LABELED, TIER_BARE, TIER_UNKNOWN)

# DB→canonical tier mapping. Legacy `curator-inferred` rows map to
# `labeled` (curator-inferred = third-party curator labeled it, distinct
# from self-described which is issuer's own claim). Rows with unknown /
# unmapped DB values return `unknown`.
_DB_TIER_TO_CANONICAL = {
    "verified": TIER_VERIFIED,
    "self-described": TIER_SELF_DESCRIBED,
    "curator-inferred": TIER_LABELED,
    "labeled": TIER_LABELED,
    "bare": TIER_BARE,
    "unknown": TIER_UNKNOWN,
    # NOTE: `mechanical` deliberately NOT mapped — Charlie 09-10: it's a
    # row-level flag on a separate column, not a tier value. Any row that
    # somehow has tier='mechanical' falls through to `unknown` below.
}

# Title Case display helper — consumer-facing labels. Consumers that
# render the tier in HTML/text should call this instead of hardcoding.
_TITLE_CASE = {
    TIER_VERIFIED: "Verified",
    TIER_SELF_DESCRIBED: "Self-Described",
    TIER_LABELED: "Labeled",
    TIER_BARE: "Bare",
    TIER_UNKNOWN: "Unknown",
}


def title_case_tier(canonical_tier: str) -> str:
    """Return the Title Case display label for a canonical (lowercase-hyphen)
    tier value. Falls back to the raw string for anything unrecognized."""
    return _TITLE_CASE.get(canonical_tier, canonical_tier.title() if canonical_tier else "Unknown")


# Legacy hero snapshot vocabulary (July 2026) → canonical (5-tier). Only
# used on the fallback path when DB is unreachable and we're reading
# `d1_hero_snapshot.json`. DOMAIN_ONLY → labeled (ledger-Domain-derived
# = machine-labeled), ANONYMOUS → bare (no attestation).
_SNAPSHOT_TIER_TO_CANONICAL = {
    "VERIFIED": TIER_VERIFIED,
    "SELF_DESCRIBED": TIER_SELF_DESCRIBED,
    "DOMAIN_ONLY": TIER_LABELED,
    "ANONYMOUS": TIER_BARE,
}


def _normalize_snapshot_tier(snapshot_tier: Optional[str]) -> str:
    if not snapshot_tier:
        return TIER_UNKNOWN
    return _SNAPSHOT_TIER_TO_CANONICAL.get(
        snapshot_tier.strip().upper(), TIER_UNKNOWN
    )


def canonicalize_currency(cx: Optional[str]) -> str:
    """Canonicalize a currency identifier to its DB storage form.

    Charlie ruling 2026-09-10 (Part C item 1): after the length-3→length-40
    migration of token_category_history, every row's currency_hex is either
    40-hex (standard XRPL 20-byte currency code) or 48-hex (MPT ID). This
    helper normalizes an input value BEFORE the DB compare so a caller
    passing 3-char ASCII ('USD', 'TXT') gets a match on the padded row.

    Rules:
      - Empty / None → returned as-is.
      - 3-char ASCII printable → hex-encode + upper + right-pad with '0' to 40.
      - 40-hex or 48-hex → uppercase (as-stored).
      - Anything else → uppercase as-is (best-effort match).

    Public: also used by app.py callers that build lookup keys against
    resolve_all_map's dict.
    """
    if not cx:
        return cx or ""
    cx = cx.strip()
    if len(cx) == 3 and cx.isprintable():
        try:
            return cx.encode("ascii").hex().upper().ljust(40, "0")
        except UnicodeEncodeError:
            return cx.upper()
    return cx.upper()

# TTL cache: (currency_hex, issuer) → last_attempt_unix. Prevents repeated
# toml fetches for the same pair. 24h = one attempt/day. Cache is
# process-local; a fleet of gunicorn workers will each check independently
# but the TTL still bounds pair-per-worker per day. Acceptable for the
# rate we care about.
_ELEVATION_TTL_SECONDS = 24 * 3600
_elevation_cache: dict[tuple[str, str], float] = {}
_elevation_cache_lock = threading.Lock()

# Hero snapshot fallback cache.
_hero_snapshot: Optional[dict] = None
_hero_snapshot_mtime: float = 0.0
_hero_snapshot_lock = threading.Lock()


@dataclass(frozen=True)
class TierRecord:
    """One tier/attestation record. `tier` is always in display vocabulary."""
    currency_hex: str
    issuer: str
    tier: str                     # display vocab: VERIFIED / SELF_DESCRIBED / DOMAIN_ONLY / ANONYMOUS
    category: Optional[str]       # taxonomy v1 category, or None
    source: str                   # 'db-registry:<db_source>' | 'hero-snapshot' | 'db-registry+two_way_toml' | 'unknown'
    citation_url: Optional[str]
    observed_at: Optional[str]    # ISO8601 UTC or None
    db_tier_raw: Optional[str] = None  # raw DB value (self-described / mechanical / etc); None if from snapshot

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_tier(db_tier: Optional[str]) -> str:
    """Translate a raw DB tier value to canonical (lowercase-hyphen). Unmapped
    values (including tier='mechanical', now a flag not a tier) return
    'unknown' — the correct signal for "we don't recognize this"."""
    if not db_tier:
        return TIER_UNKNOWN
    return _DB_TIER_TO_CANONICAL.get(db_tier.strip().lower(), TIER_UNKNOWN)


def _load_hero_snapshot() -> dict:
    """Load + memoize d1_hero_snapshot.json. Reloads on mtime change so an
    updated snapshot file takes effect without a process restart."""
    global _hero_snapshot, _hero_snapshot_mtime
    try:
        st = os.stat(HERO_SNAPSHOT_PATH)
    except OSError:
        return {}
    with _hero_snapshot_lock:
        if _hero_snapshot is None or st.st_mtime != _hero_snapshot_mtime:
            try:
                with open(HERO_SNAPSHOT_PATH) as f:
                    _hero_snapshot = json.load(f)
                _hero_snapshot_mtime = st.st_mtime
            except (OSError, json.JSONDecodeError):
                _hero_snapshot = {}
    return _hero_snapshot or {}


def _hero_snapshot_lookup(currency_hex: str, issuer: str) -> Optional[TierRecord]:
    """Hero-snapshot fallback for one (currency, issuer) pair.

    Snapshot's `tiers.lookup` may store entries in either shape:
      - bare string: "DOMAIN_ONLY" | "ANONYMOUS" | "VERIFIED" | "SELF_DESCRIBED"
      - dict: {"tier": "...", "category": "...", "citation_url": "...", ...}
    Both are accepted. Prior code assumed dict shape and called
    `entry.get(...)`, which raised AttributeError on the bare-string
    form and 500'd the /token page for any pair only present in the
    snapshot (e.g., XLM on rKiCet…; caught 2026-09-11 via warnings-
    feed click-through). The DB-registry path is preferred when
    available and returns before this fallback.
    """
    snap = _load_hero_snapshot()
    lookup = (snap.get("tiers") or {}).get("lookup") or {}
    for k in (f"{currency_hex}|{issuer}", f"{currency_hex.upper()}|{issuer}",
              f"{currency_hex.lower()}|{issuer}"):
        entry = lookup.get(k)
        if entry is None:
            continue
        if isinstance(entry, str):
            tier_val = entry
            category = None
            citation_url = None
            observed_at = None
        elif isinstance(entry, dict):
            tier_val = entry.get("tier")
            category = entry.get("category")
            citation_url = entry.get("citation_url")
            observed_at = entry.get("observed_at")
        else:
            continue
        return TierRecord(
            currency_hex=currency_hex.upper(),
            issuer=issuer,
            tier=_normalize_snapshot_tier(tier_val),
            category=category,
            source="hero-snapshot",
            citation_url=citation_url,
            observed_at=observed_at,
            db_tier_raw=None,
        )
    return None


def _db_current_lookup(currency_hex: str, issuer: str) -> Optional[TierRecord]:
    """Query `token_category_current` for one pair. Returns a TierRecord
    with source='db-registry:<db_source>' or None if not found / DB out.
    Input currency_hex is canonicalized to 40-hex (or 48-hex MPT) form
    before the exact-string compare — accepts 3-char ASCII inputs too."""
    if not db.pg_available():
        return None
    canon = canonicalize_currency(currency_hex)
    try:
        with db.pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tier, category, source, citation_url, observed_at "
                    "FROM token_category_current "
                    "WHERE currency_hex = %s AND issuer = %s",
                    (canon, issuer),
                )
                row = cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    raw_tier, category, src, cit, obs = row
    return TierRecord(
        currency_hex=canon,
        issuer=issuer,
        tier=normalize_tier(raw_tier),
        category=category,
        source=f"db-registry:{src}" if src else "db-registry",
        citation_url=cit,
        observed_at=obs.isoformat() if obs else None,
        db_tier_raw=raw_tier,
    )


def _try_elevate(rec: TierRecord) -> TierRecord:
    """If current tier is SELF_DESCRIBED and a well-formed https citation URL
    exists, attempt two_way_toml verification (extracted core). Fail-open.

    Elevation writes a NEW `token_category_history` row with source='two_way_toml'
    and tier='verified'. It does NOT flip flags on any existing row — the
    linked-list superseded_by pointer is what makes the new row 'current'
    for future reads (see db.py:1344 view definition).
    """
    # 2026-09-10 Charlie ruling (Part C): elevation gate accepts both
    # SELF_DESCRIBED and LABELED. `labeled` covers the audit-demoted rows
    # (was verified, dropped to labeled pending real proof) — those are
    # exactly the ones we WANT auto-elevation to attempt.
    if rec.tier not in (TIER_SELF_DESCRIBED, TIER_LABELED):
        return rec
    if not rec.citation_url or not rec.citation_url.startswith("https://"):
        return rec

    key = (rec.currency_hex, rec.issuer)
    now = time.time()
    with _elevation_cache_lock:
        last = _elevation_cache.get(key, 0.0)
        if now - last < _ELEVATION_TTL_SECONDS:
            return rec
        _elevation_cache[key] = now  # record attempt regardless of outcome

    try:
        from two_way_toml_verifier_walker import verify_submission
    except Exception:
        return rec

    synthesized_row = {
        "toml_url": rec.citation_url,
        "currency_hex": rec.currency_hex,
        "issuer": rec.issuer,
        # verify_submission compares toml `category` against `claimed_category`;
        # a mismatch fails the check. If we don't know the category, pass
        # empty string so the toml's declaration wins on match-any.
        "claimed_category": (rec.category or "").lower(),
    }
    try:
        ok, err, _ = verify_submission(synthesized_row)
    except Exception:
        return rec
    if not ok:
        return rec

    # Success — write an elevated history row. Use 'verified' (DB vocab).
    try:
        with db.pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO token_category_history (
                        currency_hex, issuer, category, tier, source,
                        citation_url, observed_at, taxonomy_version, note
                    ) VALUES (
                        %s, %s, %s, 'verified', 'two_way_toml',
                        %s, NOW(), '1.0.0',
                        'auto-elevation via shared_tier_verifier'
                    )
                    """,
                    (rec.currency_hex, rec.issuer,
                     rec.category or "unlabeled", rec.citation_url),
                )
                conn.commit()
    except Exception:
        # Elevation logic succeeded, DB write failed. Return current tier —
        # the next call after TTL will retry (write is idempotent-safe:
        # multiple rows with source='two_way_toml' merely re-affirm).
        return rec

    return TierRecord(
        currency_hex=rec.currency_hex,
        issuer=rec.issuer,
        tier=TIER_VERIFIED,
        category=rec.category,
        source="db-registry+two_way_toml",
        citation_url=rec.citation_url,
        observed_at=datetime.now(timezone.utc).isoformat(),
        db_tier_raw="verified",
    )


def resolve_tier(currency_hex: str, issuer: str, elevate: bool = False) -> TierRecord:
    """Return current tier/attestation for (currency_hex, issuer).

    Args:
      currency_hex: 40-char hex or 3-char ASCII (will be upper-cased for lookup)
      issuer: r-prefixed XRPL address
      elevate: if True and pair is SELF_DESCRIBED, attempt two_way_toml
               elevation. Default False — web routes should not stall on
               toml fetches; a background walker is the intended caller
               for elevate=True.
    """
    currency_hex = (currency_hex or "").strip()
    issuer = (issuer or "").strip()

    rec = _db_current_lookup(currency_hex, issuer)
    if rec is None:
        rec = _hero_snapshot_lookup(currency_hex, issuer)
    if rec is None:
        rec = TierRecord(
            currency_hex=currency_hex.upper(), issuer=issuer,
            tier=TIER_UNKNOWN, category=None,
            source="unknown", citation_url=None, observed_at=None,
            db_tier_raw=None,
        )
    if elevate:
        rec = _try_elevate(rec)
    return rec


def live_tier_counts() -> tuple[dict[str, int], str]:
    """Return (counts, source) where counts is a dict keyed by display tier
    (VERIFIED/SELF_DESCRIBED/DOMAIN_ONLY/ANONYMOUS) and source is
    'db-registry' or 'hero-snapshot-fallback'.

    Counts sum over the live `token_category_current` view. Falls back to
    the hero snapshot's `tiers.counts` block only when DB is unavailable.
    """
    if db.pg_available():
        try:
            with db.pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT tier, COUNT(*) FROM token_category_current "
                        "GROUP BY tier"
                    )
                    raw = cur.fetchall()
            counts = {t: 0 for t in KNOWN_TIERS}
            for raw_tier, n in raw:
                counts[normalize_tier(raw_tier)] = counts.get(
                    normalize_tier(raw_tier), 0
                ) + int(n)
            return counts, "db-registry"
        except Exception:
            pass
    snap = _load_hero_snapshot()
    snap_counts = (snap.get("tiers") or {}).get("counts") or {}
    return (
        {t: int(snap_counts.get(t, 0)) for t in KNOWN_TIERS},
        "hero-snapshot-fallback",
    )


def resolve_all_map() -> tuple[dict[str, str], str]:
    """Return (lookup, source) where lookup is a dict keyed by
    'CURRENCY_HEX|ISSUER' with UPPERCASE display-tier value covering the
    union of `token_category_current` (LIVE) + the hero snapshot's
    tiers.lookup (fallback). DB values overlay snapshot values on
    key conflict — live wins.

    Source is 'db-registry-overlay' when DB was reached, 'hero-snapshot-only'
    when DB was out. Compat shape for callers that were using
    `hero_snapshot.tiers.lookup` directly (/tokens label_lookup + the
    falling-visual JSON).
    """
    out: dict[str, str] = {}
    # Snapshot fallback layer
    snap = _load_hero_snapshot()
    lookup = (snap.get("tiers") or {}).get("lookup") or {}
    for k, entry in lookup.items():
        if not isinstance(entry, dict):
            continue
        tier = _normalize_snapshot_tier(entry.get("tier"))
        out[k.upper()] = tier

    src = "hero-snapshot-only"
    if db.pg_available():
        try:
            with db.pg_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT currency_hex, issuer, tier "
                        "FROM token_category_current"
                    )
                    for cx, iss, raw_tier in cur.fetchall():
                        canonical_tier = normalize_tier(raw_tier)
                        cx_up = cx.upper()
                        out[f"{cx_up}|{iss}"] = canonical_tier
                        # 2026-09-10 Part C item 1: expose 3-char ASCII alias
                        # for standard 40-hex currency codes so downstream
                        # callers (app.py:3402) that build lookup keys with
                        # a short-form `cur` variable still match. MPT IDs
                        # (48-hex) don't have an ASCII alias — skip.
                        if len(cx_up) == 40:
                            try:
                                b = bytes.fromhex(cx_up)
                                trimmed = b.rstrip(b"\x00").decode(
                                    "ascii", errors="strict"
                                )
                                if trimmed and len(trimmed) == 3 and trimmed.isprintable():
                                    out[f"{trimmed}|{iss}"] = canonical_tier
                            except (ValueError, UnicodeDecodeError):
                                pass
            src = "db-registry-overlay"
        except Exception:
            pass  # keep snapshot-only fallback
    return out, src


def resolve_many(pairs: list[tuple[str, str]], elevate: bool = False) -> dict[tuple[str, str], TierRecord]:
    """Batch resolver — one lookup per pair. Optimizes a single DB round
    trip for the DB-registry read; hero fallback + elevation stay per-pair.
    Used by /tokens which renders many rows at once.
    """
    if not pairs:
        return {}
    out: dict[tuple[str, str], TierRecord] = {}
    pending: list[tuple[str, str]] = []

    if db.pg_available():
        try:
            keys = [(cx.upper(), iss) for cx, iss in pairs]
            with db.pg_connect() as conn:
                with conn.cursor() as cur:
                    # ANY(ARRAY[...]) with row-tuple compare is awkward in
                    # psycopg; simpler to build a VALUES table.
                    values_sql = ",".join(["(%s, %s)"] * len(keys))
                    params = [v for pair in keys for v in pair]
                    cur.execute(
                        f"""
                        SELECT c.currency_hex, c.issuer, c.tier, c.category,
                               c.source, c.citation_url, c.observed_at
                        FROM token_category_current c
                        JOIN (VALUES {values_sql}) AS q(cx, iss)
                          ON c.currency_hex = q.cx AND c.issuer = q.iss
                        """,
                        params,
                    )
                    for cx, iss, raw_tier, cat, src, cit, obs in cur.fetchall():
                        out[(cx, iss)] = TierRecord(
                            currency_hex=cx, issuer=iss,
                            tier=normalize_tier(raw_tier),
                            category=cat,
                            source=f"db-registry:{src}" if src else "db-registry",
                            citation_url=cit,
                            observed_at=obs.isoformat() if obs else None,
                            db_tier_raw=raw_tier,
                        )
        except Exception:
            pass

    for cx, iss in pairs:
        key = (cx.upper(), iss)
        if key in out:
            continue
        pending.append((cx, iss))

    for cx, iss in pending:
        rec = _hero_snapshot_lookup(cx, iss)
        if rec is None:
            rec = TierRecord(
                currency_hex=cx.upper(), issuer=iss,
                tier=TIER_UNKNOWN, category=None,
                source="unknown", citation_url=None, observed_at=None,
                db_tier_raw=None,
            )
        out[(cx.upper(), iss)] = rec

    if elevate:
        for k, rec in list(out.items()):
            out[k] = _try_elevate(rec)

    return out

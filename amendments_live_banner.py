"""amendments_live_banner.py — the "Just went LIVE" box on /amendments.

Branch amendments-live-banner-2026-10-09. Motivating incident: when
PermissionDelegationV1_1 activated on 2026-10-08 at 5:29:50 PM ET
(21:29:50 UTC, ledger 107,524,865), its countdown card simply VANISHED
from /amendments — the hash left the Amendments ledger object's Majorities
array, and the page drives its cards from that array alone. Viewers who had
been watching the countdown saw the card disappear instead of turn green.

This module answers one question, with no I/O at all: which amendments
went live within the last WINDOW_HOURS (48), newest first? Callers pass
rows shaped exactly like app._load_amendment_majority_history() returns,
so this is unit-testable with plain-dict fakes only — never the production
DB (owner rule 2026-10-08).

Every field is LEDGER TRUTH recorded by amendment_majority_walker:
`enabled_iso` / `enabled_seen_ledger` / `enabled_tx_hash` are the true
EnableAmendment pseudo-transaction's close time, ledger index and hash as
of the true-enable-time fix (PR #33). Nothing here is derived or guessed.
"""
from __future__ import annotations

import datetime as dt

WINDOW_HOURS = 48


def _parse_iso_utc(iso):
    """Best-effort ISO8601 -> aware UTC datetime; None on anything
    unusable (missing/malformed/wrong type). Never raises."""
    if not iso:
        return None
    try:
        d = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def recently_enabled(majority_history, now=None, window_hours=WINDOW_HOURS):
    """Amendments enabled within `window_hours` of `now`, newest first.

    Returns a list of dicts: {name, hash, enabled_iso, enabled_seen_ledger,
    enabled_tx_hash}. `enabled_iso` is always a UTC ISO string the template
    renders through the existing datetime_to_et_first_html filter (ET first,
    UTC in parens) — this module never formats a time itself.

    Rules:
      * only rows with enabled=True are considered;
      * the enable instant is `enabled_iso`, falling back to
        `enabled_close_iso` (what the walker recorded before the
        true-enable-time fix). A row with NEITHER is dropped: we cannot
        prove it is inside the window, and guessing is not an option;
      * the window is inclusive at exactly `window_hours`;
      * a future-dated enable instant (clock skew / bad row) is dropped
        rather than shown as "just went live";
      * duplicate hashes keep only the newest enable instant, so a
        re-enabled hash can never render twice in one box.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    window = dt.timedelta(hours=window_hours)

    best_by_hash = {}
    for mh in (majority_history or []):
        if not mh.get("enabled"):
            continue
        iso = mh.get("enabled_iso") or mh.get("enabled_close_iso")
        when = _parse_iso_utc(iso)
        if when is None:
            continue
        age = now - when
        if age < dt.timedelta(0) or age > window:
            continue
        key = (mh.get("hash") or "").upper()
        entry = {
            "name": mh.get("name"),
            "hash": mh.get("hash"),
            "enabled_iso": iso,
            "enabled_seen_ledger": mh.get("enabled_seen_ledger"),
            "enabled_tx_hash": mh.get("enabled_tx_hash"),
            "_when": when,
        }
        prior = best_by_hash.get(key)
        if prior is None or when > prior["_when"]:
            best_by_hash[key] = entry

    out = sorted(best_by_hash.values(), key=lambda e: e["_when"], reverse=True)
    for e in out:
        e.pop("_when", None)
    return out


def live_fingerprint(state, live_list):
    """A short, stable string that changes exactly when the page's live
     content should change: the on-ledger enabled count, the set of hashes
     currently in the Majorities array, and the recently-enabled set.

    The /amendments page embeds its own fingerprint; the 30s poll compares
    the endpoint's fingerprint against it and reloads only on a mismatch.
    Sorted so an upstream reordering of the same data never triggers a
    spurious reload.
    """
    state = state or {}
    count = state.get("enabled_count")
    majorities = sorted(
        (m.get("hash") or "").upper()
        for m in (state.get("majorities") or [])
    )
    live = sorted(
        "{}@{}".format((e.get("hash") or "").upper(), e.get("enabled_iso") or "")
        for e in (live_list or [])
    )
    return "{}|{}|{}".format(count, ",".join(majorities), ",".join(live))

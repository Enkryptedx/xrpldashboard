"""amendments_permalink — dated, signed record for /amendments/<date>.

Design: docs/AMENDMENTS_PERMALINKS_DESIGN_2026-09-26.md (Charlie go
2026-09-26 with all four recommendations: v1 labels majority events and
roll-call as "not in the leaf"; unsigned day = 200 "not yet signed";
roll-call day counts included from v1; URL form /amendments/YYYY-MM-DD).

Everything on the permalink that is INSIDE the signed leaf comes from the
leaf's `amendments_block` metric (schema 5, since 2026-09-24). Majority
events (amendment_majority_history) and roll-call day counts
(amendment_roll_call_rounds/_tallies) are ledger-sourced by walkers and
are shown LABELED as not inside the leaf. Per-validator rows are never
read here (standing rule 2026-09-26: counts only on public surfaces).

Pure helpers take data; DB reads are best-effort and return empty on any
failure so the page never 500s (render-killer guard).
"""
from __future__ import annotations

import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import db  # noqa: E402

# Dates with no leaf because the signing machine was down. The gap IS the
# record (Charlie ruling: never sign past dates). Copy mirrors
# /methodology "Known gaps".
KNOWN_GAP_SENTENCES = {
    "2026-07-15": (
        "No leaf exists for this date. The signing job was unloaded by a "
        "launchd session restart on the evening of 2026-07-14 and re-bootstrapped "
        "at 20:13 ET on 2026-07-15; its first run landed after 00:00 UTC, so the "
        "next leaf is dated 2026-07-16. That leaf's previous_root equals "
        "2026-07-14's chain_root — the chain bridges the day directly. The gap "
        "is the record — we do not sign past dates."
    ),
}
_SEPT_OUTAGE_SENTENCE = (
    "No leaf exists for this date. The signing walker was offline from "
    "Tuesday 2026-09-15 ~15:15 UTC to Saturday 2026-09-19 ~15:20 UTC because "
    "the machine that runs it lost power for approximately 96 hours. Signing "
    "resumed on 2026-09-19; that day's leaf bridges directly back to "
    "2026-09-15's chain root. The gap is the record — we do not sign past dates."
)
for _d in ("2026-09-16", "2026-09-17", "2026-09-18"):
    KNOWN_GAP_SENTENCES[_d] = _SEPT_OUTAGE_SENTENCE
KNOWN_GAP_DATES = frozenset(KNOWN_GAP_SENTENCES)
KNOWN_GAP_SENTENCE = _SEPT_OUTAGE_SENTENCE  # back-compat name
MISSING_SENTENCE = (
    "No signed leaf was recorded for this date. We never sign a past date, "
    "so nothing is shown for it."
)
# amendments_block joined the signed schema on this date (Ship A, schema 5).
AMENDMENTS_BLOCK_SINCE = "2026-09-24"
# The leaf dated D is written at D 01:00 UTC, which is 21:00 ET on the
# evening of D-1 (launchd StartCalendarInterval 21:00 local; verified from
# signed_snapshots.written_at 2026-09-26).
SIGNING_TIME_NOTE = "signed at 01:00 UTC, i.e. 21:00 ET the evening before"

ET = dt.timezone(dt.timedelta(hours=-4))  # display-only; ISO strings carry UTC


def _et_zone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:  # noqa: BLE001
        return ET


def classify_date(date_str: str, signed_dates: list[str], today_utc: dt.date) -> str:
    """One of: 'signed' | 'not_yet_signed' | 'known_gap' | 'missing' |
    'before_first' | 'future'.

    `signed_dates` is any-order list of YYYY-MM-DD with a leaf. A date
    after today is 'future' (404); today or a past date with no leaf that
    is newer than the newest leaf is 'not_yet_signed' (200, no tallies);
    a hole inside the chain is 'known_gap' or 'missing' (200, sentence);
    before the first leaf is 'before_first' (404)."""
    if date_str in signed_dates:
        return "signed"
    d = dt.date.fromisoformat(date_str)
    if d > today_utc:
        return "future"
    if not signed_dates:
        return "not_yet_signed"
    first = min(signed_dates)
    newest = max(signed_dates)
    if date_str < first:
        return "before_first"
    if date_str > newest:
        return "not_yet_signed"
    if date_str in KNOWN_GAP_DATES:
        return "known_gap"
    return "missing"


def amendments_block_from_envelope(envelope: dict) -> dict | None:
    """The `amendments_block` metric VALUE from a leaf, or None when the
    leaf predates the block (before 2026-09-24)."""
    for m in envelope.get("metrics") or []:
        if m.get("name") == "amendments_block" and isinstance(m.get("value"), dict):
            return m["value"]
    return None


def signed_rows(block: dict | None) -> list[dict]:
    """Flatten per_amendment into display rows, name-sorted. Every field
    here is inside the signed payload."""
    if not block:
        return []
    rows = []
    for name, a in (block.get("per_amendment") or {}).items():
        nv = a.get("network_votes") if isinstance(a, dict) else None
        rows.append({
            "name": name,
            "hash": (a or {}).get("hash"),
            "enabled": bool((a or {}).get("enabled")),
            "supported_by_responding_node": (a or {}).get("supported_by_responding_node"),
            "votes_count": (nv or {}).get("count") if nv else None,
            "votes_validations": (nv or {}).get("validations") if nv else None,
            "votes_threshold": (nv or {}).get("threshold") if nv else None,
            "votes_as_of_iso": (nv or {}).get("as_of_iso") if nv else None,
            "votes_source_url": (nv or {}).get("source_url") if nv else None,
        })
    rows.sort(key=lambda r: (r["name"] or "").lower())
    return rows


def hash_to_name_map(block: dict | None) -> dict[str, str]:
    out = {}
    for name, a in ((block or {}).get("per_amendment") or {}).items():
        h = (a or {}).get("hash")
        if h:
            out[h.upper()] = name
    return out


def verdict_from_verify(result: dict | None) -> dict:
    """Collapse the verifier result into one honest word.
      verified — all checks passed
      partial  — only the chain-link soft note remains (verifier had no
                 chain.json / prior-day file; signature, leaf hash, audit
                 path and fingerprint all passed)
      failed   — anything else"""
    if not result:
        return {"state": "failed", "issues": ["verifier returned nothing"]}
    issues = list(result.get("issues") or [])
    if result.get("ok"):
        return {"state": "verified", "issues": []}
    soft_only = issues and all(i.startswith("chain_link: could not verify") for i in issues)
    return {"state": "partial" if soft_only else "failed", "issues": issues}


def _utc_day_of(iso: str | None) -> str | None:
    if not iso:
        return None
    return str(iso)[:10]


def majority_events_for_day(rows: list[dict], date_str: str) -> list[dict]:
    """Pure: from _load_amendment_majority_history()-shaped rows, the
    gained / lost / regained events whose flag-ledger timestamp falls on
    the UTC day `date_str`. One event per transition."""
    events = []
    for r in rows or []:
        if _utc_day_of(r.get("first_seen_close_iso") or r.get("first_seen_iso")) == date_str:
            events.append({
                "kind": "majority gained",
                "name": r.get("name"), "hash": r.get("hash"),
                "ledger": r.get("first_seen_ledger"),
                "close_iso": r.get("first_seen_close_iso") or r.get("first_seen_iso"),
                "activation_eta_iso": r.get("activation_eta_iso"),
                "vote_count_at_first": r.get("vote_count_at_first"),
                "unl_threshold": r.get("unl_threshold"),
                "correction_note": r.get("correction_note"),
            })
        if _utc_day_of(r.get("removed_close_iso") or r.get("removed_iso")) == date_str:
            events.append({
                "kind": "majority lost",
                "name": r.get("name"), "hash": r.get("hash"),
                "ledger": r.get("removed_seen_ledger"),
                "close_iso": r.get("removed_close_iso") or r.get("removed_iso"),
                "activation_eta_iso": None,
                "vote_count_at_first": None,
                "unl_threshold": r.get("unl_threshold"),
                "correction_note": r.get("correction_note"),
            })
    events.sort(key=lambda e: (e.get("close_iso") or "", e.get("name") or ""))
    return events


def roll_call_day_counts(rounds: list[dict], date_str: str, names: dict[str, str]) -> dict | None:
    """Pure: counts-only summary of the roll-call rounds observed on the
    UTC day `date_str`. `rounds` are read_rounds()-shaped (newest first,
    tallies {HASH: (yes_round, yes_carried, passes)}). Never returns
    validator identities — the inputs don't carry them either."""
    day = [r for r in rounds or [] if _utc_day_of(r.get("observed_iso")) == date_str]
    if not day:
        return None
    day.sort(key=lambda r: r["voting_ledger"])
    last = day[-1]
    per = []
    for h, (yr, yc, passes) in (last.get("tallies") or {}).items():
        per.append({
            "name": names.get(h.upper()) or (h[:12] + "…"),
            "hash": h,
            "yes_round": yr, "yes_incl_carry": yc, "passes": bool(passes),
        })
    per.sort(key=lambda p: (-p["yes_incl_carry"], p["name"].lower()))
    return {
        "rounds": len(day),
        "first_voting_ledger": day[0]["voting_ledger"],
        "last_voting_ledger": last["voting_ledger"],
        "first_observed_iso": day[0].get("observed_iso"),
        "last_observed_iso": last.get("observed_iso"),
        "unl_size": last.get("unl_size"),
        "heard_min": min(r.get("seen") or 0 for r in day),
        "heard_max": max(r.get("seen") or 0 for r in day),
        "trusted_available": last.get("trusted_available"),
        "threshold": last.get("threshold"),
        "needed": last.get("needed"),
        "per_amendment": per,
    }


def load_roll_call_rounds_for_day(date_str: str) -> list[dict]:
    """DB read (best-effort): rounds observed on the UTC day, with tallies.
    Reuses roll_call_card.read_rounds' row shape via its own SQL so the
    counts-only contract holds (no votes table touched)."""
    try:
        if not db.pg_available():
            return []
        import roll_call_card
        with db.pg_connect() as conn, conn.cursor() as cur:
            # Bound the read: rounds are every 256 ledgers (~16 min), so a
            # day is ≤ ~90 rounds; read the newest 400 and filter by day.
            rounds = roll_call_card.read_rounds(cur, limit=400)
        return [r for r in rounds if _utc_day_of(r.get("observed_iso")) == date_str]
    except Exception:  # noqa: BLE001 — never 500 the page
        return []


def et_label(iso: str | None) -> str | None:
    """'2026-09-25 10:46 ET' for an ISO UTC string, or None."""
    if not iso:
        return None
    try:
        d = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(_et_zone()).strftime("%Y-%m-%d %H:%M ET")
    except ValueError:
        return None

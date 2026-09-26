"""roll_call_card — the /amendments "validator roll call" card.

Design approved 2026-09-25 20:51 ET (Charlie decisions 1–4) + standing rule
2026-09-26 (roll-call-counts-only-public): the card shows COUNTS only —
validations seen, UNL size, trusted_available, rippled threshold, votes
needed, per-amendment yes votes (this round + 24h carry-forward) and the
pass/short/RESET state. Per-validator vote rows (`amendment_roll_call_votes`)
never reach this module.

Source: our own node's `validations` stream, recorded on the Lenovo by
`scripts/roll_call_recorder.py` into `amendment_roll_call_rounds` +
`amendment_roll_call_tallies` (first round 2026-09-26 11:44:53Z). No public
RPC is consulted here: the "next roll call" estimate is derived from the
recorder's own observed round times (256 ledgers per round).

Gates (both must pass for the card to render):
  ROLL_CALL_CARD_ENABLED=1        env flag (default OFF — a timer never
                                  publishes; Charlie flips it on Render)
  NOT_BEFORE_UTC                  hard-coded 24 h after the first recorded
                                  round (decision 3: one full day of
                                  recording before the card reads it)

The read is best-effort: any DB error → None → the page renders without
the card (render-killer rule). The existing per-amendment
"Trusted-validator vote" strip (Foundation VHS) stays as the labeled
fallback regardless.
"""
from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

ROUND_LEDGERS = 256
NOT_BEFORE_UTC = dt.datetime(2026, 9, 27, 11, 44, 53, tzinfo=dt.timezone.utc)
# A round every ~256 × 3.9 s ≈ 16.6 min. Two missed rounds = recorder stale.
STALE_AFTER_S = 2 * ROUND_LEDGERS * 4.0  # 2048 s
FALLBACK_SECONDS_PER_LEDGER = 3.9
SOURCE_LABEL = "own-node validations stream (Lenovo rippled → roll-call recorder)"
_ET = ZoneInfo("America/New_York")


def is_enabled(now: dt.datetime | None = None, env: dict | None = None) -> bool:
    e = os.environ if env is None else env
    if str(e.get("ROLL_CALL_CARD_ENABLED", "")).strip().lower() not in ("1", "true", "yes", "on"):
        return False
    now = now or dt.datetime.now(dt.timezone.utc)
    return now >= NOT_BEFORE_UTC


# ── DB read (cursor in, plain dicts out) ──

def read_rounds(cur, limit: int = 6) -> list[dict]:
    """Newest-first rounds, each with its tallies {hash: (round, carried, passes)}."""
    cur.execute(
        "SELECT voting_ledger_index, flag_ledger_index, observed_iso, unl_size, "
        "       validations_seen, trusted_available, threshold_rippled, votes_needed, "
        "       unl_sequence "
        "FROM amendment_roll_call_rounds ORDER BY voting_ledger_index DESC LIMIT %s",
        (int(limit),),
    )
    rounds = []
    for r in cur.fetchall():
        rounds.append({
            "voting_ledger": int(r[0]), "flag_ledger": int(r[1]) if r[1] is not None else int(r[0]) + 1,
            "observed_iso": r[2], "unl_size": int(r[3] or 0), "seen": int(r[4] or 0),
            "trusted_available": int(r[5] or 0), "threshold": int(r[6] or 0),
            "needed": int(r[7]) if r[7] is not None else int(r[6] or 0) + 1,
            "unl_sequence": r[8], "tallies": {},
        })
    if not rounds:
        return rounds
    idx = {r["voting_ledger"]: r for r in rounds}
    cur.execute(
        "SELECT voting_ledger_index, amendment_hash, yes_votes_round, yes_votes_carried, passes_rippled "
        "FROM amendment_roll_call_tallies WHERE voting_ledger_index = ANY(%s)",
        ([r["voting_ledger"] for r in rounds],),
    )
    for vl, h, yr, yc, p in cur.fetchall():
        if int(vl) in idx:
            idx[int(vl)]["tallies"][(h or "").upper()] = (int(yr or 0), int(yc or 0), bool(p))
    cur.execute("SELECT count(*), min(observed_iso) FROM amendment_roll_call_rounds")
    n, first = cur.fetchone()
    for r in rounds:
        r["rounds_recorded"] = int(n or 0)
        r["first_round_iso"] = first
    return rounds


def _parse_iso(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def seconds_per_ledger(rounds: list[dict]) -> float:
    """Measured from the recorder's own consecutive rounds; falls back to a
    typical close interval when fewer than two plausible samples exist."""
    samples = []
    for a, b in zip(rounds, rounds[1:]):
        ta, tb = _parse_iso(a["observed_iso"]), _parse_iso(b["observed_iso"])
        gap = a["voting_ledger"] - b["voting_ledger"]
        if ta and tb and gap > 0:
            spl = (ta - tb).total_seconds() / gap
            if 2.5 <= spl <= 6.0:
                samples.append(spl)
    if not samples:
        return FALLBACK_SECONDS_PER_LEDGER
    return sum(samples) / len(samples)


# ── pure card builder ──

def build_card(rounds: list[dict], in_flight: list[dict], now: dt.datetime | None = None) -> dict | None:
    """rounds newest-first (from read_rounds); in_flight = state['in_flight']
    ([{hash, name, ...}]). Returns the card dict or None when no round exists."""
    if not rounds:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    latest = rounds[0]
    prev = rounds[1] if len(rounds) > 1 else None
    observed = _parse_iso(latest["observed_iso"])
    age_s = (now - observed).total_seconds() if observed else None
    spl = seconds_per_ledger(rounds)
    next_vl = latest["voting_ledger"] + ROUND_LEDGERS
    eta_s = None
    if observed is not None:
        eta_s = (observed + dt.timedelta(seconds=ROUND_LEDGERS * spl) - now).total_seconds()
    rows = []
    any_reset = False
    for a in in_flight or []:
        h = (a.get("hash") or "").upper()
        yr, yc, passes = latest["tallies"].get(h, (0, 0, False))
        prev_passes = prev["tallies"].get(h, (0, 0, False))[2] if prev else None
        if passes:
            status = "passing"
        elif prev_passes:
            status = "reset"
            any_reset = True
        else:
            status = "short"
        rows.append({
            "hash": h, "name": a.get("name") or h[:8],
            "yes_round": yr, "yes_carried": yc, "passes": passes,
            "prev_passes": prev_passes, "status": status,
            "short_by": max(0, latest["needed"] - yc),
        })
    rows.sort(key=lambda r: (-r["yes_carried"], r["name"].lower()))
    return {
        "voting_ledger": latest["voting_ledger"],
        "flag_ledger": latest["flag_ledger"],
        "observed_utc": observed.strftime("%Y-%m-%d %H:%M:%S UTC") if observed else None,
        "observed_et": observed.astimezone(_ET).strftime("%-I:%M:%S %p ET") if observed else None,
        "age_min": round(age_s / 60) if age_s is not None else None,
        "stale": bool(age_s is not None and age_s > STALE_AFTER_S),
        "seen": latest["seen"], "unl_size": latest["unl_size"],
        "trusted_available": latest["trusted_available"],
        "threshold": latest["threshold"], "needed": latest["needed"],
        "unl_sequence": latest.get("unl_sequence"),
        "next_voting_ledger": next_vl,
        "next_eta_min": (max(0, round(eta_s / 60)) if eta_s is not None else None),
        "next_overdue": bool(eta_s is not None and eta_s < -60),
        "seconds_per_ledger": round(spl, 2),
        "rows": rows,
        "any_reset": any_reset,
        "rounds_recorded": latest.get("rounds_recorded", len(rounds)),
        "first_round_iso": latest.get("first_round_iso"),
        "source": SOURCE_LABEL,
    }


def load_for_page(state: dict, now: dt.datetime | None = None) -> dict | None:
    """Route entry point. Gated + best-effort: returns None unless enabled,
    past NOT_BEFORE, and the read succeeds."""
    if not is_enabled(now):
        return None
    try:
        import db
        if not db.pg_available():
            return None
        with db.pg_connect() as conn:
            with conn.cursor() as cur:
                rounds = read_rounds(cur)
    except Exception:  # noqa: BLE001 — render-killer rule: never 500 the page
        return None
    return build_card(rounds, state.get("in_flight") or [], now)

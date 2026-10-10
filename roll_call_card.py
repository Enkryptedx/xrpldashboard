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

Gate (must pass for the card to render):
  ROLL_CALL_CARD_ENABLED=1        env flag (default OFF — a timer never
                                  publishes; Charlie flips it on Render)

  (The hard 24 h NOT_BEFORE gate was removed 2026-09-27 — one full day of
  recording has elapsed since the first round 2026-09-26 11:44:53Z, so the
  env flag alone now gates the card.)

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
# A round every ~256 × 3.9 s ≈ 16.6 min. Two missed rounds = recorder stale.
STALE_AFTER_S = 2 * ROUND_LEDGERS * 4.0  # 2048 s
FALLBACK_SECONDS_PER_LEDGER = 3.9
# Flag-ledger math (Charlie 2026-10-02): the node closes a ledger about every
# 3.86 s, so a 256-ledger flag interval is ~16.5 min. The counter uses the LIVE
# validated ledger (passed in by the route from network_pulse), never the
# recorder's stale voting_ledger.
SECONDS_PER_LEDGER_NOMINAL = 3.86
LEDGERS_PER_FLAG = 256
SOURCE_LABEL = "own-node validations stream (Lenovo rippled → roll-call recorder)"
# The node is in Indianapolis, Indiana. Use the named IANA zone so the EDT->EST
# fall-back on 2026-11-01 is handled by the tz database, never a fixed offset.
_ET = ZoneInfo("America/Indiana/Indianapolis")


def votes_needed(heard):
    """Yes votes a majority needs out of `heard` validators, or None.

    rippled's AmendmentSet computes ``threshold = max(1, trusted * 80 / 100)``
    with INTEGER division and requires yes votes **strictly greater** than it.
    So the number a reader needs is ``threshold + 1``:

        35 heard -> threshold 28 -> needs 29
        34 heard -> threshold 27 -> needs 28
        33 heard -> threshold 26 -> needs 27

    Integer arithmetic on purpose. ``0.8 * 35`` is ``28.000000000000004`` in
    binary floating point, so a float path would make the 35 case depend on
    rounding luck.

    `heard` is the validators we actually HEARD, not the full published UNL
    (Charlie 2026-10-10, superseding the 2026-09-26 full-UNL ruling): saying
    "needs 29 of 35" when only 34 were heard overstates the bar, because
    rippled thresholds on the trusted validators whose votes it has.

    A one-validator list is the degenerate case: threshold is clamped to 1
    and one yes vote is both the threshold and enough, so `needed` stays 1
    rather than becoming an unreachable 2.
    """
    if not heard or heard < 1:
        return None
    threshold = max(1, (heard * 80) // 100)
    return threshold if heard == 1 else threshold + 1


def is_enabled(now: dt.datetime | None = None, env: dict | None = None) -> bool:
    e = os.environ if env is None else env
    return str(e.get("ROLL_CALL_CARD_ENABLED", "")).strip().lower() in ("1", "true", "yes", "on")


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

def flag_ledger_counter(current_validated_ledger: int,
                        now: dt.datetime | None = None,
                        seconds_per_ledger: float = SECONDS_PER_LEDGER_NOMINAL) -> dict | None:
    """Next flag ledger from the LIVE validated ledger (not a stale snapshot).

    next_flag = ((current // 256) + 1) * 256

    Returns ledgers remaining, minutes remaining at ~3.86 s/ledger, the next
    flag index, and the projected next-flag time as a tz-aware UTC datetime
    (the template/filter converts it Eastern-first). None on a bad input.
    """
    try:
        current = int(current_validated_ledger)
    except (TypeError, ValueError):
        return None
    if current <= 0:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    next_flag = ((current // LEDGERS_PER_FLAG) + 1) * LEDGERS_PER_FLAG
    ledgers_remaining = next_flag - current
    seconds_remaining = ledgers_remaining * seconds_per_ledger
    next_flag_utc = now + dt.timedelta(seconds=seconds_remaining)
    return {
        "current_ledger": current,
        "next_flag_ledger": next_flag,
        "ledgers_remaining": ledgers_remaining,
        "minutes_remaining": round(seconds_remaining / 60, 1),
        "seconds_per_ledger": seconds_per_ledger,
        "next_flag_utc": next_flag_utc,
        "next_flag_iso": next_flag_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def build_card(rounds: list[dict], in_flight: list[dict], now: dt.datetime | None = None,
               majority_active: dict | None = None,
               current_validated_ledger: int | None = None,
               enabled: list[dict] | None = None) -> dict | None:
    """rounds newest-first (from read_rounds); in_flight = state['in_flight']
    ([{hash, name, ...}]).

    majority_active: {HASH_UPPER: bool} — LEDGER TRUTH from the majority walker
    (amendment_majority_history active flag = removed_iso is None). This is the
    ONLY input that decides the per-row Holding vs Countdown-restarted headline.
    The vote count NEVER touches the headline; it only drives the amber/green/
    red count-line state. Absent/unknown hash -> None -> template shows no
    ledger headline (count line still renders).

    Returns the card dict or None when no round exists."""
    majority_active = {(k or "").upper(): v for k, v in (majority_active or {}).items()}
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
    # Denominator + threshold on the FULL published UNL, not just what our
    # node heard (Charlie ruling 2026-09-26). rippled's rule on 35: threshold
    # = 35*80//100 = 28, a majority needs 29 (strictly > threshold). The count
    # we can VERIFY first-hand is only from the validators we heard; the rest
    # are unheard and their votes are unknown to us.
    unl_full = latest["unl_size"] or latest["trusted_available"]
    thr_full = max(1, (unl_full * 80) // 100)
    needed_full = thr_full if unl_full == 1 else thr_full + 1
    not_heard = max(0, unl_full - latest["seen"])
    # Public "needs N of M" is computed on the validators we actually HEARD
    # (Charlie 2026-10-10), which supersedes the 2026-09-26 ruling that
    # pinned the denominator to the full published UNL. Quoting "needs 29
    # of 35" while only 34 were heard overstates the bar: rippled's
    # AmendmentSet thresholds on the trusted validators whose votes it has,
    # so 34 heard really needs 28.
    needed_heard = votes_needed(latest["seen"])
    # Build a row for EVERY hash the recorder tallied this round, keyed by
    # hash — not just the in_flight list (Charlie 2026-09-27). An amendment our
    # node recognizes on a newer binary (e.g. fixBatchV1_2 on rippled 3.4.1)
    # is a live Majorities entry with a real tally (32/32) but may be absent
    # from state['in_flight']; matching by name/in_flight dropped its count.
    # in_flight only supplies the display name when we have one.
    name_by_hash = {(a.get("hash") or "").upper(): a.get("name")
                    for a in (in_flight or []) if a.get("hash")}
    # 2026-10-08 (Charlie): validators keep voting yes for an amendment AFTER
    # it is enabled, so its hash stays in the tally. The name lookup used to
    # know only in_flight; the moment PermissionDelegationV1_1 activated
    # (17:29 ET) its row degraded to the bare hash "0F48FF56" and still said
    # "majority". Names for enabled amendments come from state's
    # recognized_enabled (the feature RPC knows every name); such rows are
    # flagged enabled so the template shows "enabled" instead of a vote state.
    enabled_by_hash = {(a.get("hash") or "").upper(): a.get("name")
                       for a in (enabled or []) if a.get("hash")}
    for _h, _n in enabled_by_hash.items():
        name_by_hash.setdefault(_h, _n)
    tally_hashes = list(latest["tallies"].keys())
    # Union preserves in_flight order first, then any tally-only hashes.
    # in_flight first (page order), then enabled-but-still-voted, then tally-only.
    ordered_hashes = [(a.get("hash") or "").upper() for a in (in_flight or []) if a.get("hash")]
    for h in enabled_by_hash:
        if h in latest["tallies"] and h not in ordered_hashes:
            ordered_hashes.append(h)
    for h in tally_hashes:
        if h not in ordered_hashes:
            ordered_hashes.append(h)
    rows = []
    any_reset = False
    for h in ordered_hashes:
        yr, yc, passes = latest["tallies"].get(h, (0, 0, False))
        prev_passes = prev["tallies"].get(h, (0, 0, False))[2] if prev else None
        # TWO BARS, on purpose - they answer different questions.
        #
        # `bar` is what the page PRINTS: the heard-based number, identical
        # to the headline's (Charlie 2026-10-10). The table used to print
        # the recorder's stored full-UNL `needed` instead, so at 34 heard
        # it read "34 / 29" under a headline saying "needs 28 of 34".
        #
        # `needed_full` stays the bar for the CERTAINTY BAND, and that is
        # not an oversight. The band's whole job is to warn that validators
        # we have not heard could still decide the outcome. At 34 heard
        # with 28 yes the amendment clears the bar among those heard - but
        # if the 35th is heard next round the bar itself rises to 29 and 28
        # no longer passes. Scoring the band on the heard bar would paint
        # that green and throw the warning away.
        #
        # Evidence (verified against our own recorded roll call, not from
        # memory): on 2026-10-09 at 5:34 AM ET fixCleanup3_4_0 gained its
        # majority on 28 yes while our node heard all 35 and our bar said
        # 29 - our count and the ledger can disagree, so the page must not
        # present our number as the ledger's verdict. Round 107536127,
        # observed 2026-10-09T09:34:33Z: validations_seen 35, votes_needed
        # 29, yes_carried 28, passes_rippled FALSE - while
        # amendment_majority_history recorded majority_close 09:34:30Z.
        # Note this case does NOT argue for the heard bar: we heard all 35,
        # so the heard bar was also 29. It argues for keeping the band
        # conservative and letting the ledger have the last word.
        #  - heard yes already >= needed_full -> passing for sure
        #  - even if EVERY unheard validator voted yes, still < needed_full -> short for sure
        #  - otherwise the unheard votes could decide it -> amber, too close to call
        bar = needed_heard if needed_heard else needed_full
        best_possible = yc + not_heard
        if yc >= needed_full:
            count_state = "passing"
        elif best_possible < needed_full:
            count_state = "short"
        else:
            count_state = "too_close"
        # Legacy on-wire status (passing/reset/short vs our-heard threshold) is
        # kept for the reset detector, but the DISPLAYED count state uses the
        # full-UNL certainty band above.
        is_enabled_amendment = h in enabled_by_hash
        if is_enabled_amendment:
            status = "enabled"   # on-ledger fact; a vote state is meaningless now
        elif passes:
            status = "passing"
        elif prev_passes:
            status = "reset"
            any_reset = True
        else:
            status = "short"
        # Ledger-truth headline (Charlie 2026-09-27): active majority on the
        # Amendments object -> "Holding"; a run that was removed at a flag
        # ledger -> "Countdown restarted". None = hash not tracked by the
        # majority walker -> template omits the headline. Never vote-derived.
        ledger_holding = majority_active.get(h)  # True / False / None
        rows.append({
            "hash": h, "name": name_by_hash.get(h) or h[:8],
            "enabled": is_enabled_amendment,
            "yes_round": yr, "yes_carried": yc, "passes": passes,
            "prev_passes": prev_passes, "status": status,
            "count_state": count_state, "not_heard": not_heard,
            "needed_full": needed_full, "best_possible": best_possible,
            # EVERYTHING THE PAGE PRINTS uses the same heard-based bar:
            # the table cell, "N votes to spare" and "short by N". They were
            # split across two bars until 2026-10-10, which made the card
            # contradict itself below full attendance - at 34 heard the
            # headline said "needs 28 of 34" while the wording line said
            # "short by 1" against 29. The printed bar and the margin have
            # to be the same number or the reader is given two answers.
            "needed": bar,
            "short_by": max(0, bar - yc),
            # Margin against that same printed bar, BY OUR NODE'S COUNT
            # only (branch amendments-wording-2026-10-09). Positive = votes
            # to spare; 0 = exactly at the bar (one defection loses it);
            # negative = short by that many. The ledger's own Majorities
            # decision is still the final word on the page, not this
            # number - see the certainty-band note above for the
            # fixCleanup3_4_0 case where the two disagreed outright.
            "spare_votes": yc - bar,
            "ledger_holding": ledger_holding,
        })
    rows.sort(key=lambda r: (-r["yes_carried"], r["name"].lower()))
    # Flag-ledger counter from the LIVE validated ledger supplied by the route
    # (network_pulse). Never derived from latest["voting_ledger"] (the stale
    # recorder snapshot). None when the route couldn't supply a live ledger.
    flag_counter = flag_ledger_counter(current_validated_ledger, now) \
        if current_validated_ledger else None
    return {
        "voting_ledger": latest["voting_ledger"],
        "flag_counter": flag_counter,
        "flag_ledger": latest["flag_ledger"],
        "observed_utc": observed.strftime("%Y-%m-%d %H:%M:%S UTC") if observed else None,
        "observed_et": observed.astimezone(_ET).strftime("%-I:%M:%S %p ET") if observed else None,
        # owner ruling: a UTC ISO instant the page runs through the single
        # datetime_to_et_first filter so the roll-call "last roll call" time
        # reads Eastern-first with UTC in parentheses, same as every other
        # time on /amendments, instead of the bare 24h-less "4:00:00 PM ET"
        # string or the raw UTC string. One source of truth for the format.
        "observed_iso": observed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if observed else None,
        "age_min": round(age_s / 60) if age_s is not None else None,
        "stale": bool(age_s is not None and age_s > STALE_AFTER_S),
        "seen": latest["seen"], "unl_size": latest["unl_size"],
        "trusted_available": latest["trusted_available"],
        "threshold": latest["threshold"], "needed": latest["needed"],
        # Full-UNL denominator + rippled's rule on it (Charlie 2026-09-26):
        "unl_full": unl_full, "threshold_full": thr_full,
        "needed_full": needed_full, "not_heard": not_heard,
        # What the page prints: "needs {needed_heard} of {seen}".
        "needed_heard": needed_heard,
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


def load_for_page(state: dict, now: dt.datetime | None = None,
                  majority_active: dict | None = None,
                  current_validated_ledger: int | None = None) -> dict | None:
    """Route entry point. Gated + best-effort: returns None unless enabled
    and the read succeeds.

    majority_active: {HASH_UPPER: bool} LEDGER TRUTH (active = removed_iso is
    None) from amendment_majority_history, passed by the route. It alone drives
    the per-amendment Holding/Countdown-restarted headline — never the vote
    count. When the route can't supply it, the headline is simply omitted.
    """
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
    return build_card(rounds, state.get("in_flight") or [], now,
                      majority_active=majority_active,
                      current_validated_ledger=current_validated_ledger,
                      enabled=state.get("recognized_enabled") or [])

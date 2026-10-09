"""
Live network vote tallies for in-flight XRPL amendments.

The rippled `feature` RPC returns only the responding node's OWN validator
vote. It never exposes the trusted-validator network tally. This module
fetches the tally from the Validator History Service (VHS) — the
aggregation backend powering the XRPL Foundation explorer at
`livenet.xrpl.org`.

Field semantics (critical — see
`feedback_verify_field_semantics_before_reporting_movement.md`):

    The VHS payload includes both `voted.count` (total broadcasting nodes,
    INCLUDING non-UNL) and `consensus` (UNL-scoped percentage of vl.ripple.com
    validators). Only UNL votes count toward the 14-day 80% activation
    window. The parser here reads UNL-scoped fields only:

      * Iterate `voted.validators`, keep entries where
        `unl == "vl.ripple.com"`; that count is the numerator.
      * Parse `threshold` "N/M" — M is the UNL denominator.
      * Cross-check against `consensus` percentage. If the filter-based
        count diverges from the consensus-derived count by more than 1,
        skip the entry (treat as schema drift, don't publish).

    Never surface `voted.count` unmodified.

Rollout, cache TTL alignment, kill switch, and stale ceiling are per
docs/LIVE_FETCH_AMENDMENTS_DESIGN.md sections 5, 6, and 10.
"""

from __future__ import annotations

import os
import threading
import time

import httpx

ENDPOINT = os.environ.get(
    "NETWORK_VOTES_ENDPOINT",
    "https://data.xrpl.org/v1/network/amendments/vote/main",
)
CACHE_TTL = int(os.environ.get("NETWORK_VOTES_CACHE_TTL", "300"))
STALE_CEILING_SECONDS = int(
    os.environ.get("NETWORK_VOTES_STALE_CEILING_SECONDS", "21600")  # 6h
)
HTTP_TIMEOUT = float(os.environ.get("NETWORK_VOTES_HTTP_TIMEOUT", "10.0"))

# UNL we scope to. VHS labels each validator with the publisher URL of the
# UNL it appears on; `vl.ripple.com` is Ripple's default recommended UNL,
# which rippled ships with and which the activation math is denominated
# against on mainnet today.
UNL_KEY = "vl.ripple.com"

_lock = threading.Lock()
# Cache state:
#   last_success_ts   — monotonic time of last successful fetch (0 if never)
#   last_success_data — parsed dict from last successful fetch
#   last_success_iso  — wall-clock ISO of the successful fetch (for display)
#   last_attempt_ts   — monotonic time of last attempt (success OR failure)
_cache = {
    "last_success_ts": 0.0,
    "last_success_data": None,
    "last_success_iso": None,
    "last_attempt_ts": 0.0,
}


def _now_monotonic() -> float:
    return time.monotonic()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _enabled() -> bool:
    return os.environ.get("NETWORK_VOTES_ENABLED", "true").lower() != "false"


def _parse_threshold(raw) -> tuple[int, int] | None:
    """Parse VHS `"28/35"` → (28, 35). Returns None on unparseable input."""
    if not isinstance(raw, str) or "/" not in raw:
        return None
    num, _, den = raw.partition("/")
    try:
        return (int(num.strip()), int(den.strip()))
    except (ValueError, TypeError):
        return None


def _parse_consensus_pct(raw) -> float | None:
    """Parse VHS `"40.00%"` → 40.0. Returns None on unparseable input."""
    if not isinstance(raw, str):
        return None
    s = raw.strip().rstrip("%").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _count_unl_yes(validators) -> int:
    """Count validators broadcasting yes AND affiliated with our UNL."""
    if not isinstance(validators, list):
        return 0
    return sum(
        1
        for v in validators
        if isinstance(v, dict) and v.get("unl") == UNL_KEY
    )


def _parse_amendments(payload: dict, source_url: str, fetched_iso: str) -> dict:
    """Turn a VHS response into `{UPPERCASE_HASH: entry}`.

    Skips: enabled amendments (no threshold), retired, obsolete,
    unparseable threshold, and entries where the UNL-filter count
    diverges from the consensus-derived count by >1 (schema drift or
    upstream inconsistency)."""
    out: dict[str, dict] = {}
    if not isinstance(payload, dict):
        return out
    amendments = payload.get("amendments")
    if not isinstance(amendments, list):
        return out

    for a in amendments:
        if not isinstance(a, dict):
            continue
        if a.get("retired") or a.get("obsolete"):
            continue

        thr = _parse_threshold(a.get("threshold"))
        if thr is None:
            # No threshold = enabled amendment (or malformed); skip.
            continue
        thr_num, thr_den = thr
        if thr_den <= 0:
            continue

        voted = a.get("voted") or {}
        unl_yes = _count_unl_yes(voted.get("validators"))

        # Cross-validate with the consensus percentage. If the filter and
        # the aggregator's own math disagree by more than 1 validator,
        # something is off — either UNL membership changed mid-fetch or
        # the schema drifted. Skip rather than publish a wrong count.
        consensus_pct = _parse_consensus_pct(a.get("consensus"))
        if consensus_pct is not None:
            expected = round(consensus_pct * thr_den / 100.0)
            if abs(unl_yes - expected) > 1:
                continue

        hash_id = a.get("id")
        if not isinstance(hash_id, str) or not hash_id:
            continue

        # `threshold` is rippled's AmendmentSet::threshold_ (trusted*80/100,
        # integer division; VHS reports the same number). rippled requires
        # yes votes STRICTLY GREATER than it — with 35 trusted validators the
        # threshold is 28 and a majority needs 29; 28 loses it. Confirmed
        # against our own record: PermissionDelegationV1_1 sat at 28/35 and
        # lost its majority at flag ledger 107181569 (2026-09-23). `needed`
        # is the human number the page shows.
        out[hash_id.upper()] = {
            "count": unl_yes,
            "validations": thr_den,
            "threshold": thr_num,
            "needed": thr_num + 1,
            "as_of_iso": fetched_iso,
            "source_url": source_url,
        }
    return out


def _fetch_once() -> dict | None:
    """One HTTP round-trip. Returns parsed dict on success, None on any
    failure. Never raises."""
    try:
        resp = httpx.get(ENDPOINT, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return None
        payload = resp.json()
    except Exception:
        return None
    return _parse_amendments(payload, ENDPOINT, _now_iso())


def _envelope_disabled() -> dict:
    return {
        "data": {},
        "status": "disabled",
        "as_of_iso": None,
        "source_url": ENDPOINT,
    }


def _envelope_unavailable() -> dict:
    return {
        "data": {},
        "status": "unavailable",
        "as_of_iso": None,
        "source_url": ENDPOINT,
    }


def _envelope_ok(data: dict, iso: str) -> dict:
    return {
        "data": data,
        "status": "ok",
        "as_of_iso": iso,
        "source_url": ENDPOINT,
    }


def _envelope_stale(data: dict, iso: str, age_seconds: int) -> dict:
    return {
        "data": data,
        "status": "stale",
        "as_of_iso": iso,
        "stale_age_seconds": age_seconds,
        "source_url": ENDPOINT,
    }


def fetch_network_vote_tallies_cached() -> dict:
    """Fetch and cache UNL-scoped network vote tallies from VHS.

    Returns an envelope:
        {
          "data": {UPPERCASE_HASH: {"count", "validations", "threshold",
                                    "as_of_iso", "source_url"}},
          "status": "ok" | "stale" | "unavailable" | "disabled",
          "as_of_iso": <iso of the data (None when unavailable/disabled)>,
          "stale_age_seconds": <int, only when status == "stale">,
          "source_url": <VHS URL>,
        }

    Never raises. `data` is empty when status is unavailable or disabled;
    consumers must treat missing entries as 'not fetched', never as zero
    votes."""
    if not _enabled():
        return _envelope_disabled()

    # 2026-10-08 (Charlie): the fetch used to run INSIDE _lock, so when the
    # 300 s TTL lapsed every concurrent /amendments request queued behind
    # one 10 s upstream round-trip (14.6 s page at 14:54 ET with no other
    # load). Now the lock only guards the cache dict; the HTTP call runs
    # outside it, in a background thread when we have last-good data to
    # serve meanwhile (stale-while-revalidate), or synchronously — still
    # outside the lock — only on a cold cache with nothing to show.
    # A failed fetch never overwrites last_success_*: failures are never
    # cached, they only flip the served status to "stale".
    with _lock:
        now_mono = _now_monotonic()
        last_success_ts = _cache["last_success_ts"]
        data = _cache["last_success_data"]
        iso = _cache["last_success_iso"]
        age = now_mono - last_success_ts if last_success_ts else float("inf")

        # Fresh cache — return without a network call.
        if data is not None and age < CACHE_TTL:
            return _envelope_ok(data, iso)

        if data is not None:
            # TTL lapsed but we have something to show. Kick ONE background
            # refresh and answer immediately from the last good fetch.
            if not _refresh["in_flight"]:
                _refresh["in_flight"] = True
                _refresh["done"].clear()
                t = threading.Thread(target=_refresh_worker, daemon=True,
                                     name="network-votes-refresh")
                _refresh["thread"] = t
                t.start()
            stale_age = int(age)
            if stale_age > STALE_CEILING_SECONDS:
                # Too old to present as live data, even while refreshing.
                return _envelope_unavailable()
            if _cache["last_attempt_ts"] > last_success_ts:
                # At least one refresh has failed since this data was
                # fetched: be honest about its age.
                return _envelope_stale(data, iso, stale_age)
            return _envelope_ok(data, iso)

        # Cold cache: nothing to serve. One thread fetches (outside the
        # lock); concurrent cold callers wait for that result instead of
        # each hitting upstream.
        if _refresh["in_flight"]:
            waiter = True
        else:
            _refresh["in_flight"] = True
            _refresh["done"].clear()
            waiter = False

    if waiter:
        _refresh["done"].wait(timeout=HTTP_TIMEOUT + 1)
    else:
        _refresh_worker()

    with _lock:
        data = _cache["last_success_data"]
        if data is None:
            return _envelope_unavailable()
        return _envelope_ok(data, _cache["last_success_iso"])


_refresh = {"in_flight": False, "done": threading.Event(), "thread": None}


def _refresh_worker() -> None:
    """One upstream round-trip, NOT under _lock. Writes the cache only on
    success; on failure records the attempt time so readers can report
    'stale'. Always clears in_flight and signals waiters."""
    try:
        fresh = _fetch_once()
        with _lock:
            now_mono = _now_monotonic()
            _cache["last_attempt_ts"] = now_mono
            if fresh is not None:
                _cache["last_success_ts"] = now_mono
                _cache["last_success_data"] = fresh
                _cache["last_success_iso"] = _now_iso()
    finally:
        with _lock:
            _refresh["in_flight"] = False
        _refresh["done"].set()


def _join_refresh_for_tests(timeout: float = 5.0) -> None:
    """Test-only: wait for an in-flight background refresh."""
    t = _refresh.get("thread")
    if t is not None and t.is_alive():
        t.join(timeout)


def _reset_cache_for_tests() -> None:
    """Test-only: reset module-level cache so tests are order-independent.
    Not part of the public contract."""
    _join_refresh_for_tests()
    with _lock:
        _cache["last_success_ts"] = 0.0
        _cache["last_success_data"] = None
        _cache["last_success_iso"] = None
        _cache["last_attempt_ts"] = 0.0
        _refresh["in_flight"] = False
        _refresh["thread"] = None
        _refresh["done"].set()

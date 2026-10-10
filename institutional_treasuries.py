"""Public companies holding XRP — treasury card data (branch
institutional-treasuries-2026-10-09).

Feeds the "Public companies holding XRP" section on /institutional.
Evernorth Holdings Inc. is the first entry.

TWO STRICTLY SEPARATED TIERS, because they have different evidentiary
weight and must never be blended in the UI:

  TIER_FILING    — a fact stated in an SEC filing, with the filing URL.
  TIER_ATTRIBUTED— a wallet address attributed to the company by a third
                   party (XRPScan curation, xrp-insights.com), NOT named
                   in any filing.

Verified read-only on 2026-10-09 against the three filings Evernorth made
that day (8-K 0001193125-26-419146, 424B3 0001193125-26-419096, 8-A12B
0001193125-26-418582): an XRPL-address regex found ZERO addresses in all
three, and none of the watched addresses appear anywhere in them. The
filings DO name the custodian (BitGo Bank & Trust, N.A.). So every wallet
below is TIER_ATTRIBUTED — nothing on this page may claim a filing
confirms an address until a filing actually names one.

Node load: balances come from `fetch_treasury_snapshot()`, memoized for
SNAPSHOT_TTL seconds. One `account_info` per wallet per TTL window, never
per page render. Any failure degrades to balance=None and the card renders
the filing facts alone (render-killer rule).
"""
from __future__ import annotations

import datetime as dt
import threading

TIER_FILING = "filing"
TIER_ATTRIBUTED = "attributed"

# Attribution groups within TIER_ATTRIBUTED. The two groups have different
# evidence behind them and must never be merged into one claim.
GROUP_XRPSCAN = "xrpscan"
GROUP_INFERRED = "inferred"

# Exact strings the owner approved 2026-10-10. Do not reword without asking.
GROUP_LABELS = {
    GROUP_XRPSCAN: "labeled Evernorth by XRPScan — not confirmed by Evernorth",
    GROUP_INFERRED: (
        "inferred by xrp-insights from amounts matching the filing "
        "— not confirmed by Evernorth"
    ),
}

# Each group label links to the source that makes the claim.
GROUP_SOURCE_URLS = {
    GROUP_XRPSCAN: "https://xrpscan.com",
    GROUP_INFERRED: "https://xrp-insights.com/xrp-radar/evernorth",
}

#: Per-wallet explorer link. The card shows a shortened address; the full
#: address is only ever reachable through this link, never truncated silently
#: in a way that could be mistaken for the real thing.
EXPLORER_URL = "https://xrpscan.com/account/{address}"

# Public-facing source labels. Exact strings the owner approved.
TIER_LABELS = {
    TIER_FILING: "confirmed by Evernorth filing",
    TIER_ATTRIBUTED: "attributed by XRPScan / xrp-insights — not confirmed by Evernorth",
}

SNAPSHOT_TTL = 900  # 15 min

SEC_8K = ("https://www.sec.gov/Archives/edgar/data/2092592/"
          "000119312526419146/d24481d8k.htm")
SEC_424B3 = ("https://www.sec.gov/Archives/edgar/data/2092592/"
             "000119312526419096/d824062d424b3.htm")

# ── Tier 1: facts from the filings, each with its own citation ──
# Figures are quoted, not computed. No estimates, no projections.
EVERNORTH = {
    "company": "Evernorth Holdings Inc.",
    "cik": "0002092592",
    "facts": [
        {"label": "Business combination closed",
         "value": "October 9, 2026",
         "detail": "Closing Date per the 8-K Introductory Note.",
         "source": "8-K", "url": SEC_8K},
        {"label": "Listing",
         "value": "Nasdaq — XRPN (Class A), XRPNW (warrants)",
         "detail": "Securities registered pursuant to Section 12(b).",
         "source": "8-K", "url": SEC_8K},
        {"label": "Expected XRP holdings at closing",
         "value": "at least 473,276,430 XRP",
         "detail": "Stated as Pubco's initial XRP holdings at Closing.",
         "source": "424B3", "url": SEC_424B3},
        {"label": "Signing XRP price",
         "value": "$2.36609",
         "detail": "Defined term in the 8-K private-placement section.",
         "source": "8-K", "url": SEC_8K},
        {"label": "Closing XRP price",
         "value": "$1.43069",
         # Re-checked against the 8-K 2026-10-10: the filing says
         # "arithmetic average", not "three-day average" (the earlier
         # wording described the window correctly but not the method).
         "detail": "Arithmetic average of the CME CF XRP-Dollar Reference "
                   "Rate \u2014 New York Variant benchmark at 4:00 p.m. New "
                   "York City time on each of the three days immediately "
                   "preceding the Closing Date.",
         "source": "8-K", "url": SEC_8K},
        {"label": "Shareholder redemptions",
         "value": "approximately $195.39 million",
         "detail": "At a redemption price of approximately $10.58 per share.",
         "source": "8-K", "url": SEC_8K},
        {"label": "Ripple contribution",
         "value": "126,791,458 XRP",
         "detail": "Contributed under the Contribution Agreement in a "
                   "private placement.",
         "source": "424B3", "url": SEC_424B3},
        {"label": "Custodian",
         "value": "BitGo Bank & Trust, National Association",
         "detail": "Named as primary custodian and a \u201cqualified "
                   "custodian\u201d for purposes of Rule 206(4)-2 under the "
                   "Investment Advisers Act of 1940. The filings name the "
                   "custodian but do not publish any wallet address.",
         "source": "424B3", "url": SEC_424B3},
    ],
}

# ── Tier 2: addresses attributed by third parties, NOT by any filing ──
WALLETS = [
    ("rsT3yYMkuicxW1hYsy787mg5XHhkz2uQRk", "Evernorth 1"),
    ("rKXXrAgpkHQN8m4HxAQCYmDCPPUByc9mVq", "Evernorth 2"),
    ("rKhjV48GdbgxAAfSvusqGNktGwAxnzzXpv", "Custody A"),
    ("rGJBNGkDeRPNvNJCi57Ht1ncdht9SuctLe", "Custody B"),
    ("rJX1qoSGYmx5NWJEpsBGKvxmYpGR7mDtop", "Custody C"),
    ("rJuyHPDFpfeVhxfxZboTf7BYu1ptGus1v3", "Custody D"),
    ("rUgQciCPP1AiwQ9f5zstYu9RzVfsKQRGc2", "Custody E"),
    ("rPhQdyEaz4kcSoYKTAQhvkvdYxWKKw2vSC", "Custody F"),
    ("rGy4zJtGfGtF7dtjZmBraQTcfZSQgwqpaa", "Custody G"),
    ("rfiEXPM2ZgdvH6stURRF2t5Tk3SmRX7EDR", "Custody H"),
    ("r3tTJV8rppEqixa4LDxCvMtYAbWgFFyA6Y", "Custody I"),
    ("rBZWJZpVDSDFBFmRpcuXJ8ePqX7wE4BeYa", "Custody J"),
    ("rNtaHck1h268GQpfrQQ5AW68CEF2q919WU", "Staging"),
]

#: The two addresses XRPScan itself labels "Evernorth" (domain evernorth.xyz,
#: XRPScan curation; no on-ledger Domain field, no TOML). Everything else in
#: WALLETS was inferred by xrp-insights from amounts matching the filing.
#: These two have a named third-party labeller; the other eleven do not.
XRPSCAN_LABELLED = frozenset({
    "rsT3yYMkuicxW1hYsy787mg5XHhkz2uQRk",
    "rKXXrAgpkHQN8m4HxAQCYmDCPPUByc9mVq",
})

# The filing's stated minimum, for the side-by-side comparison.
FILING_MIN_XRP = 473276430

# Most recent moves shown on the card.
MOVES_LIMIT = 10

_lock = threading.Lock()
_cache = {"at": None, "data": None}


def short_address(addr, head=6, tail=4):
    """first6…last4 for display. The full address is never shown inline — it
    is reachable only via the explorer link — so the ellipsis must always be
    present on a shortened value. An address too short to shorten is returned
    unchanged rather than padded into something that looks truncated.
    """
    if not addr:
        return ""
    if len(addr) <= head + tail + 1:
        return addr
    return f"{addr[:head]}\u2026{addr[-tail:]}"


def _fmt_xrp(drops):
    """drops -> XRP float. None stays None (never guess a balance)."""
    if drops is None:
        return None
    try:
        return int(drops) / 1_000_000
    except (TypeError, ValueError):
        return None


def build_rows(balances, last_moved=None):
    """Pure: assemble the wallet rows. `balances` maps address -> drops (or
    None when unreadable); `last_moved` maps address -> ISO string or None.

    Every row carries TIER_ATTRIBUTED, because no filing names an address.
    """
    last_moved = last_moved or {}
    rows = []
    for addr, _internal_name in WALLETS:
        group = GROUP_XRPSCAN if addr in XRPSCAN_LABELLED else GROUP_INFERRED
        rows.append({
            "address": addr,
            "short": short_address(addr),
            # NO display name. The xrp-insights names (Custody A-J, Staging)
            # are a third party's naming convention; rendered on a public
            # card "Custody A" reads as a first-party fact about Evernorth's
            # custody structure that no filing supports. Owner ruling
            # 2026-10-10: short addresses only, group heading carries the
            # source. Names stay in WALLETS for matching, never for display.
            "balance_xrp": _fmt_xrp((balances or {}).get(addr)),
            "last_moved_iso": last_moved.get(addr),
            "tier": TIER_ATTRIBUTED,
            "tier_label": TIER_LABELS[TIER_ATTRIBUTED],
            "group": group,
            "group_label": GROUP_LABELS[group],
            "group_source_url": GROUP_SOURCE_URLS[group],
            "explorer_url": EXPLORER_URL.format(address=addr),
        })
    return rows


def build_moves(raw_moves, known_labels=None, limit=MOVES_LIMIT):
    """Pure: assemble the "recent moves" rows, newest first.

    `raw_moves` items need: hash, iso (UTC ISO-8601), amount_xrp,
    direction ('out'/'in'), counterparty (address or None).

    Counterparty naming rule (owner 2026-10-09): a counterparty is NAMED
    only when it is a first-party labelled account in `known_labels`.
    Otherwise the address is shown bare, with name=None — we never invent
    an exchange or entity name from an unlabelled address, because a label
    that reads like a fact is worse than no label.

    Times are passed through as ISO for the template's shared ET-first
    filter; this module never formats a time.
    """
    known_labels = known_labels or {}
    out = []
    for m in (raw_moves or []):
        cp = m.get("counterparty")
        name = known_labels.get(cp) if cp else None
        out.append({
            "hash": m.get("hash"),
            "iso": m.get("iso"),
            "amount_xrp": m.get("amount_xrp"),
            "direction": m.get("direction"),
            "counterparty": cp,
            "counterparty_name": name,
            "counterparty_named": name is not None,
        })
    out.sort(key=lambda r: (r["iso"] or ""), reverse=True)
    return out[:limit]


def total_xrp(rows):
    """Sum of readable balances. Returns (total, readable_count). A row we
    could not read is excluded rather than counted as zero."""
    vals = [r["balance_xrp"] for r in rows if r.get("balance_xrp") is not None]
    return (sum(vals) if vals else None), len(vals)


def filing_delta(total):
    """total - FILING_MIN_XRP, or None. The filing says "at least", so a
    positive delta is consistent with it, not a contradiction.

    Rounded to 6 dp: XRP has exactly 6 decimal places (1 drop), so anything
    beyond that is float noise, not ledger precision.
    """
    if total is None:
        return None
    return round(total - FILING_MIN_XRP, 6)


def cache_state(now=None):
    """(age_seconds, is_fresh) for the memoized snapshot."""
    now = now or dt.datetime.now(dt.timezone.utc)
    at = _cache.get("at")
    if at is None:
        return None, False
    age = (now - at).total_seconds()
    return age, age < SNAPSHOT_TTL


def fetch_treasury_snapshot(reader=None, now=None, force=False):
    """Memoized snapshot. `reader(addresses)` must return
    (balances, last_moved) and is injected in tests so nothing touches a
    node or a DB. Best-effort: on failure returns rows with balance=None.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    with _lock:
        age, fresh = cache_state(now)
        if fresh and not force and _cache.get("data") is not None:
            return _cache["data"]
    balances, last_moved, raw_moves, labels = {}, {}, [], {}
    if reader is not None:
        try:
            got = reader([a for a, _ in WALLETS])
            # Backward compatible: (balances, last_moved) or
            # (balances, last_moved, moves) or (.., .., moves, labels)
            balances = got[0] or {}
            last_moved = got[1] or {} if len(got) > 1 else {}
            raw_moves = got[2] or [] if len(got) > 2 else []
            labels = got[3] or {} if len(got) > 3 else {}
        except Exception:  # noqa: BLE001 — card degrades, page never 500s
            balances, last_moved, raw_moves, labels = {}, {}, [], {}
    rows = build_rows(balances, last_moved)
    tot, readable = total_xrp(rows)
    data = {
        "company": EVERNORTH["company"],
        "facts": EVERNORTH["facts"],
        "rows": rows,
        "total_xrp": tot,
        "readable_count": readable,
        "wallet_count": len(WALLETS),
        "filing_min_xrp": FILING_MIN_XRP,
        "filing_delta": filing_delta(tot),
        "moves": build_moves(raw_moves, labels),
        "fetched_at_iso": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with _lock:
        _cache["at"] = now
        _cache["data"] = data
    return data


#: The correction contact line. A public card carrying third-party
#: attribution must say where a correction goes, in our own voice.
CORRECTION_CONTACT = (
    "Spotted something wrong? Tell us and we will correct it: "
    "corrections@xrpldashboard.com"
)


def live_line(total, wallet_count=None):
    """The side-by-side sentence, assembled here rather than in the template.

    Built in Python on purpose: the owner specified this sentence verbatim,
    and a dynamic number inside a gettext string means ``%``-in-gettext,
    which is a known render-killer in these templates. Assembling it here
    lets a test pin the exact sentence.

    Returns None when no balance was readable — better to omit the line than
    to publish a total that excludes wallets without saying so.
    """
    if total is None:
        return None
    n = len(WALLETS) if wallet_count is None else wallet_count
    return (f"These {n} wallets total {total:,.0f} XRP; "
            f"the filing says at least {FILING_MIN_XRP:,} XRP at closing.")


def build_card(snapshot=None, raw_moves=None, history=None, labels=None):
    """Pure: the whole card context from already-read Postgres rows.

    `snapshot` is a ``read_evernorth_latest_snapshot()`` dict whose balances
    are **XRP** (that is what the nightly job stored), so they are used
    as-is — never re-derived through drops, which would round-trip a value
    that is already exact to the drop.

    Every argument may be missing: with no snapshot the card still renders
    the filing facts alone (render-killer rule — the filing half of this
    card never depends on our own pipeline being up).
    """
    balances = (snapshot or {}).get("balances") or {}
    rows = []
    for addr, _internal_name in WALLETS:
        group = GROUP_XRPSCAN if addr in XRPSCAN_LABELLED else GROUP_INFERRED
        rows.append({
            "address": addr,
            "short": short_address(addr),
            # No display name — owner ruling 2026-10-10. See build_rows.
            "balance_xrp": balances.get(addr),
            "tier": TIER_ATTRIBUTED,
            "group": group,
            "group_label": GROUP_LABELS[group],
            "group_source_url": GROUP_SOURCE_URLS[group],
            "explorer_url": EXPLORER_URL.format(address=addr),
        })
    tot, readable = total_xrp(rows)
    groups = [
        {"key": g, "label": GROUP_LABELS[g], "source_url": GROUP_SOURCE_URLS[g],
         "rows": [r for r in rows if r["group"] == g]}
        for g in (GROUP_XRPSCAN, GROUP_INFERRED)
    ]
    return {
        "company": EVERNORTH["company"],
        "facts": EVERNORTH["facts"],
        "groups": groups,
        "wallet_count": len(WALLETS),
        "total_xrp": tot,
        "readable_count": readable,
        "filing_min_xrp": FILING_MIN_XRP,
        "filing_delta": filing_delta(tot),
        "live_line": live_line(tot, len(WALLETS)),
        "partial_read": readable != len(WALLETS),
        "as_of": (snapshot or {}).get("date"),
        "moves": build_moves(raw_moves, labels),
        "history": list(history or []),
        "correction_contact": CORRECTION_CONTACT,
    }


def reset_cache():
    """Test helper."""
    with _lock:
        _cache["at"] = None
        _cache["data"] = None

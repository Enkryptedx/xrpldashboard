"""Daily /changes builder — computes the machine-written daily ledger changelog.

Charlie ruling 2026-09-08 evening: the site needs a daily "what changed"
feed sourced from our own walkers. No LLM-generated prose — derived diffs
only, one line per change, ledger-index-anchored, prove-URL-carrying.
Humans write the weekly post; the machine writes the daily ledger.

Sources this v1 covers:
  1. Signed-snapshot day-over-day deltas on every anchored metric
     (xrpl_validated_ledger_index, amm_pools_count, amm_pools_total_tvl_usd,
      mpt_total_count, named_accounts_count, rlusd_xrpl_supply, rwa_total_aum_usd)
  2. Registry_state delta — new tokens flagged, collision count, taxonomy version
  3. UNL churn — validators added/removed (via unl_snapshots table)
  4. Chain events — chain_root advancement (proves the snapshot chain
     is intact), leaf_index advance, pubkey_fp changes

Not yet covered (v2 additions):
  - Amendments: reads live from rippled; will pull via amendments_state
    module once its historical-state table exists
  - Whales: needs the whales_cache_daily table + a per-day top-mover query
  - Registry curator decisions: needs a count per day from token_category_history

Output shape (JSON twin; HTML renders the same list):
  {
    "date": "2026-09-08",
    "generated_at_utc": "...",
    "changes": [
      {"category": "chain", "line": "...", "before": ..., "after": ...,
       "prove_url": "/anchors", "source": "signed_snapshot", "as_of_utc": "..."},
      ...
    ],
    "categories_no_change": ["amendments", "whales", "curator"],
    "disclosure": "..."
  }

The disclosure line names what is NOT covered so a reader knows the ceiling.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from typing import Any, Optional

HERE = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, HERE)


DISCLOSURE = (
    "Coverage: on-chain state changes captured by our own walkers, plus our "
    "signing/anchor events. NOT covered by this feed: off-chain events (Ripple "
    "corporate news, exchange listings), XRP price movements, amendment votes "
    "not yet reflected in our amendments_state snapshot, whale movements "
    "(daily whale line pending — v2), and any external attestations. "
    "Homepage strip floors (per Charlie ruling 2026-09-08): USD metrics "
    "qualify if |Δ| ≥ max($10,000, 0.1% × prior value); count metrics if "
    "|Δ| ≥ 10 units AND |Δ%| ≥ 1%. Changes below floor still appear on this "
    "page with their exact deltas, just not on the homepage strip. Anomalies "
    "(chain discontinuity, signing-key rotation, UNL churn, taxonomy version "
    "bump, amendment status change) always take priority over scalar deltas "
    "in the strip. Chain-leaf advance + ledger-index advance are always true "
    "and never take a strip slot — they live in the strip header instead. "
    "See /methodology for how each metric is derived."
)


# Ordered list of scalar metric names from signed_snapshot envelopes.
# metric_type controls how significant_changes() treats it:
#   'header_only' → never slot, feeds the strip header only
#   'usd'         → USD floor: max($10K, 0.1% × prior)
#   'count'       → count floor: |Δ| ≥ 10 units AND |Δ%| ≥ 1%
_SCALAR_METRICS: list[tuple[str, str, str, str]] = [
    # (metric_name, human_label, prove_url, metric_type)
    ("xrpl_validated_ledger_index", "XRPL validated ledger index", "/.well-known/snapshots/", "header_only"),
    ("amm_pools_count", "AMM pools count", "/pools", "count"),
    ("amm_pools_total_tvl_usd", "AMM total TVL (USD)", "/pools", "usd"),
    ("mpt_total_count", "MPT total count", "/mpts", "count"),
    ("named_accounts_count", "Named accounts count", "/network", "count"),
    ("rlusd_xrpl_supply", "RLUSD XRPL supply", "/rlusd", "usd"),
    ("rwa_total_aum_usd", "RWA total AUM (USD)", "/rwa", "usd"),
]

# Floor constants (Charlie ruling 2026-09-08 "scaled floor").
USD_ABS_FLOOR = 10_000.0     # $10,000
USD_PCT_FLOOR = 0.001        # 0.1% of prior value
COUNT_ABS_FLOOR = 10         # 10 units
COUNT_PCT_FLOOR = 0.01       # 1% of prior value


# Charlie ruling 2026-09-08 evening: verb pair per metric_type. USD
# metrics "grew by / shrank by"; count metrics "rose by / fell by"
# (because "Named accounts shrank" reads wrong; "fell by 3" is right).
_VERB_PAIR = {
    "usd":   ("grew by",  "shrank by"),
    "count": ("rose by",  "fell by"),
}

# "What this means" clause per metric_name — factual identity, never
# speculation about causes (Charlie ruling 2026-09-08). Some metrics
# use direction-conditioned clauses (RLUSD: issuance vs redemption is
# an on-chain event), others use a single static definitional clause.
# Shape: metric_name → ("up_clause", "down_clause") — if both strings
# are identical the metric has a single static clause.
_MEANING = {
    "rlusd_xrpl_supply": (
        "more issued on-chain than redeemed.",
        "more redeemed than newly issued.",
    ),
    "amm_pools_total_tvl_usd": (
        "value held in AMM liquidity pools.",
        "value held in AMM liquidity pools.",
    ),
    "amm_pools_count": (
        "the count of live AMM instances.",
        "the count of live AMM instances.",
    ),
    "rwa_total_aum_usd": (
        "tokenized real-world assets.",
        "tokenized real-world assets.",
    ),
    "mpt_total_count": (
        "the count of Multi-Purpose Tokens on XRPL.",
        "the count of Multi-Purpose Tokens on XRPL.",
    ),
    "named_accounts_count": (
        "accounts we've catalogued with an entity name.",
        "accounts we've catalogued with an entity name.",
    ),
}

# Short display name per metric_name — used as the lead word in the
# strip headline. "RWA AUM grew by $50K" not "rwa_total_aum_usd..."
_SHORT_LABEL = {
    "rlusd_xrpl_supply":       "RLUSD supply",
    "amm_pools_total_tvl_usd": "AMM TVL",
    "amm_pools_count":         "AMM pools",
    "rwa_total_aum_usd":       "RWA AUM",
    "mpt_total_count":         "MPTs",
    "named_accounts_count":    "Named accounts",
}


def _fmt_num(v: Any) -> str:
    if isinstance(v, (int, float)):
        if isinstance(v, float) and abs(v) >= 1000:
            return f"{v:,.2f}"
        if isinstance(v, int):
            return f"{v:,}"
        return f"{v:.4f}"
    return str(v)


def _fmt_rounded_usd(v: float) -> str:
    """Round a USD delta for the strip headline. Charlie ruling
    2026-09-08: "Rounded figure + direction + percent". Exact number
    stays in the .detail field for /changes body + JSON twin.
        ≥ 1,000,000  → $9.8M (one decimal)
        ≥ 1,000      → $110K (whole thousands)
        < 1,000      → $9    (whole dollars)"""
    absv = abs(v)
    if absv >= 1_000_000:
        return f"${absv/1_000_000:.1f}M"
    if absv >= 1_000:
        return f"${round(absv/1_000):,}K"
    if absv >= 1:
        return f"${round(absv)}"
    return f"${absv:.2f}"


def _fmt_rounded_count(v: float) -> str:
    """Round a count delta for the strip headline. Whole units."""
    absv = abs(v)
    if absv >= 1_000_000:
        return f"{absv/1_000_000:.1f}M"
    if absv >= 1_000:
        return f"{round(absv/1_000):,}K"
    return f"{int(round(absv)):,}"


def _metric_by_name(envelope: dict, name: str) -> Optional[Any]:
    """Return the value of a named metric in an envelope, or None if absent."""
    for m in envelope.get("metrics", []) or []:
        if m.get("name") == name:
            return m.get("value")
    return None


def _load_envelope_for_date(cur, date: dt.date) -> Optional[dict]:
    """Read one signed_snapshot envelope from PG for the given date, or None."""
    cur.execute(
        "SELECT envelope FROM signed_snapshots WHERE snapshot_date = %s "
        "ORDER BY written_at DESC LIMIT 1",
        (date,),
    )
    row = cur.fetchone()
    if not row:
        return None
    env = row[0]
    if isinstance(env, str):
        env = json.loads(env)
    return env


def _scalar_metrics_map(envelope: dict) -> dict:
    """Return {metric_name: value} for the scalar metrics that drive
    change detection. Used to compare two envelopes for distinctness.

    2026-09-11: excludes header-only metrics like `xrpl_validated_ledger_index`
    (always monotonically increasing, so it always registers as "different"
    even when the substantive source data hasn't changed — that would
    defeat the whole find-previous-distinct point).
    """
    substantive_names = {
        n for n, _, _, mtype in _SCALAR_METRICS if mtype != "header_only"
    }
    out = {}
    if not envelope:
        return out
    for m in envelope.get("metrics") or []:
        if isinstance(m, dict) and m.get("name") in substantive_names:
            out[m["name"]] = m.get("value")
    return out


def _find_previous_distinct_envelope(
    cur, today_envelope: dict, today_date: dt.date, max_lookback_days: int = 30
) -> tuple[Optional[dict], Optional[dt.date], list[str]]:
    """Walk back day-by-day to find the last snapshot whose scalar metrics
    differ from today's. Charlie ruling 2026-09-11: prevents diffing
    identical duplicates (root cause of the exact-three-zeros bug —
    two signed_snapshot fires 2h12min apart on 2026-09-09 with no
    source-data change created 09-09.json and 09-10.json with byte-
    identical metrics; naive prev_date=today-1day diffed 09-10 vs 09-09
    and reported "AMM: no change today / MPT: no change today / RWA: no
    change today" — three exact zeros on a day when AMM TVL moved).

    Returns (envelope, actual_prev_date, skipped_dates_iso).
    - envelope: previous DISTINCT envelope or None if search exhausted.
    - actual_prev_date: date of the returned envelope (may be != today-1).
    - skipped_dates_iso: dates skipped because they had identical scalars
      (transparency — logged in the envelope so a reader can see the
      duplicate-suppression path took effect).
    """
    today_scalars = _scalar_metrics_map(today_envelope)
    look_date = today_date - dt.timedelta(days=1)
    skipped: list[str] = []
    for _ in range(max_lookback_days):
        prev_env = _load_envelope_for_date(cur, look_date)
        if prev_env is None:
            look_date -= dt.timedelta(days=1)
            continue
        prev_scalars = _scalar_metrics_map(prev_env)
        if prev_scalars != today_scalars:
            return prev_env, look_date, skipped
        # Identical-scalars duplicate — walk past it.
        skipped.append(look_date.isoformat())
        look_date -= dt.timedelta(days=1)
    return None, None, skipped


def _load_unl_snapshot_for_date(cur, date: dt.date) -> Optional[dict]:
    """Read one unl_snapshot payload for the given date, or None."""
    cur.execute(
        "SELECT payload FROM unl_snapshots WHERE snapshot_date = %s "
        "ORDER BY fetched_at_iso DESC LIMIT 1",
        (date,),
    )
    row = cur.fetchone()
    if not row:
        return None
    payload = row[0]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload


def _unl_validator_set(payload: Optional[dict]) -> set[str]:
    """Extract the set of validator public keys from a UNL snapshot payload.
    Returns empty set if payload shape unfamiliar."""
    if not isinstance(payload, dict):
        return set()
    # Various UNL formats — try common shapes
    for key in ("validators", "public_validation_keys", "pubkeys"):
        vals = payload.get(key)
        if isinstance(vals, list):
            return {v if isinstance(v, str) else v.get("validation_public_key", "") for v in vals if v}
    return set()


def _plain_english_scalar_line(metric_name: str, before: float, after: float,
                                metric_type: str) -> str:
    """Charlie ruling 2026-09-08: strip and /changes body both lead with
    plain-English "grew by / shrank by" (USD) or "rose by / fell by"
    (count) + rounded delta + percent + "— what this means" clause."""
    delta = after - before
    if not before:
        return f"{_SHORT_LABEL.get(metric_name, metric_name)}: appeared"
    pct = (delta / abs(before)) * 100
    up_verb, down_verb = _VERB_PAIR.get(metric_type, ("grew by", "shrank by"))
    verb = up_verb if delta > 0 else down_verb
    if metric_type == "usd":
        rounded = _fmt_rounded_usd(delta)
    else:
        rounded = _fmt_rounded_count(delta)
    sign = "+" if delta > 0 else "−"
    pct_str = f"({sign}{abs(pct):.2f}%)"
    lead = f"{_SHORT_LABEL.get(metric_name, metric_name)} {verb} {rounded} {pct_str}"
    up_clause, down_clause = _MEANING.get(metric_name, ("", ""))
    meaning = up_clause if delta > 0 else down_clause
    if meaning:
        return f"{lead} — {meaning}"
    return lead


def _detail_line(before: float, after: float, metric_type: str) -> str:
    """Exact before → after — never removed per Charlie's rule, just
    demoted from the headline. Homepage strip omits this; /changes
    page shows it below each headline."""
    if metric_type == "usd":
        return f"was ${_fmt_num(before)}, now ${_fmt_num(after)}"
    return f"was {_fmt_num(before)}, now {_fmt_num(after)}"


def _scalar_delta_line(name: str, label: str, prove_url: str,
                       before: Any, after: Any,
                       metric_type: str = "count") -> Optional[dict]:
    """Return a change dict for a scalar metric delta, or None if unchanged."""
    if before is None or after is None:
        if before != after:
            return {
                "category": _category_for_metric(name),
                "line": f"{label} {'appeared' if before is None else 'went missing'} "
                        f"(now={_fmt_num(after)}, was={_fmt_num(before)})",
                "before": before, "after": after,
                "prove_url": prove_url, "source": "signed_snapshot",
                "metric_name": name, "metric_type": metric_type,
            }
        return None
    if before == after:
        return None
    # Compute magnitude
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        delta = after - before
        # `line` = plain-English headline (Charlie ruling 2026-09-08).
        # `detail` = exact before → after, shown on /changes body,
        # available in JSON twin. Never removed — just not the headline.
        return {
            "category": _category_for_metric(name),
            "line": _plain_english_scalar_line(name, before, after, metric_type),
            "detail": _detail_line(before, after, metric_type),
            "before": before, "after": after, "delta": delta,
            "prove_url": prove_url, "source": "signed_snapshot",
            "metric_name": name, "metric_type": metric_type, "label": label,
        }
    return {
        "category": _category_for_metric(name),
        "line": f"{label}: {before} → {after}",
        "before": before, "after": after,
        "prove_url": prove_url, "source": "signed_snapshot",
        "metric_name": name, "metric_type": metric_type, "label": label,
    }


def _category_for_metric(name: str) -> str:
    """Map a signed-snapshot metric name to a top-level /changes category."""
    if name == "xrpl_validated_ledger_index":
        return "chain"
    if name.startswith("amm_"):
        return "amm"
    if name.startswith("mpt_"):
        return "mpt"
    if name == "rlusd_xrpl_supply":
        return "rlusd"
    if name.startswith("rwa_"):
        return "rwa"
    if name == "named_accounts_count":
        return "network"
    return "other"


def _registry_state_deltas(before: Optional[dict], after: Optional[dict]) -> list[dict]:
    """Diff the registry_state dict metric between two snapshots. Emits at
    most a handful of lines (row counts, taxonomy version, merkle root
    advance)."""
    if not before and not after:
        return []
    b = before or {}
    a = after or {}
    lines: list[dict] = []
    # Taxonomy version transition
    b_tax = b.get("taxonomy_version")
    a_tax = a.get("taxonomy_version")
    if b_tax and a_tax and b_tax != a_tax:
        lines.append({
            "category": "registry",
            "line": (f"Registry taxonomy bumped: {b_tax} → {a_tax} — "
                     f"the vocabulary curators can classify tokens against."),
            "before": b_tax, "after": a_tax,
            "prove_url": "/registry/taxonomy", "source": "signed_registry_snapshot",
        })
    # Merkle root advance is proof of new curator writes today
    b_mr = b.get("history_merkle_root_hex") or b.get("history_merkle_root")
    a_mr = a.get("history_merkle_root_hex") or a.get("history_merkle_root")
    if a_mr and b_mr and b_mr != a_mr:
        lines.append({
            "category": "registry",
            "line": f"Registry history merkle root advanced ({(a_mr or '')[:12]}…, prior {(b_mr or '')[:12]}…)",
            "before": (b_mr or "")[:16], "after": (a_mr or "")[:16],
            "prove_url": "/.well-known/registry/", "source": "signed_registry_snapshot",
        })
    # Row-count deltas
    b_counts = (b.get("row_counts") or {}) if isinstance(b.get("row_counts"), dict) else {}
    a_counts = (a.get("row_counts") or {}) if isinstance(a.get("row_counts"), dict) else {}
    for k in sorted(set(b_counts) | set(a_counts)):
        bv, av = b_counts.get(k) or 0, a_counts.get(k) or 0
        if bv != av:
            sign = "+" if av > bv else ""
            lines.append({
                "category": "registry",
                "line": f"Registry {k}: {_fmt_num(bv)} → {_fmt_num(av)} ({sign}{_fmt_num(av - bv)})",
                "before": bv, "after": av, "delta": av - bv,
                "prove_url": "/registry/taxonomy", "source": "signed_registry_snapshot",
                "metric_name": f"registry_{k}", "metric_type": "count",
                "label": f"Registry {k}",
            })
    return lines


def _chain_lineage_delta(before: Optional[dict], after: Optional[dict]) -> list[dict]:
    """Chain-of-snapshots proof events — leaf advance, chain root change,
    fingerprint (never should change; if it does, it's a paging event)."""
    if not after:
        return []
    lines: list[dict] = []
    b = before or {}
    a = after
    # leaf_index MUST advance by exactly 1 per day; call it out explicitly
    b_leaf = b.get("leaf_index")
    a_leaf = a.get("leaf_index")
    if a_leaf is not None:
        if b_leaf is None:
            lines.append({
                "category": "chain",
                "line": f"Signed-snapshot chain leaf #{a_leaf} written",
                "before": None, "after": a_leaf,
                "prove_url": "/.well-known/snapshots/", "source": "signed_snapshot",
            })
        elif a_leaf - b_leaf == 1:
            lines.append({
                "category": "chain",
                "line": f"Signed-snapshot chain advanced: leaf #{b_leaf} → #{a_leaf}",
                "before": b_leaf, "after": a_leaf,
                "prove_url": "/.well-known/snapshots/", "source": "signed_snapshot",
            })
        elif a_leaf - b_leaf != 1:
            # Charlie ruling 2026-09-08: no ops language on a public
            # page. What happened + what we're doing + where to read
            # more. No internal component names.
            lines.append({
                "category": "chain",
                "line": (f"⚠ The signed-snapshot chain skipped from leaf "
                         f"#{b_leaf} to #{a_leaf} (expected +1). "
                         f"We're investigating — see /methodology."),
                "detail": f"leaf {b_leaf} → {a_leaf}",
                "before": b_leaf, "after": a_leaf,
                "prove_url": "/methodology", "source": "signed_snapshot",
            })
    # Signing pubkey fingerprint change = key rotation event
    b_fp = b.get("signing_pubkey_fingerprint")
    a_fp = a.get("signing_pubkey_fingerprint")
    if b_fp and a_fp and b_fp != a_fp:
        lines.append({
            "category": "chain",
            "line": (f"⚠ Our signing key was rotated. Verify the new key "
                     f"before trusting today's snapshot — see /methodology."),
            "detail": f"fingerprint {b_fp} → {a_fp}",
            "before": b_fp, "after": a_fp,
            "prove_url": "/methodology", "source": "signed_snapshot",
        })
    return lines


def _unl_delta(before: Optional[dict], after: Optional[dict]) -> list[dict]:
    """UNL validator churn — added/removed pubkeys between two UNL snapshots."""
    b_set = _unl_validator_set(before)
    a_set = _unl_validator_set(after)
    if not a_set and not b_set:
        return []
    added = sorted(a_set - b_set)
    removed = sorted(b_set - a_set)
    lines: list[dict] = []
    # Charlie ruling 2026-09-08 evening: readers don't run a node.
    # UNL clause becomes "— the default list of validators the network
    # trusts." (not "your rippled follows").
    UNL_MEANING = " — the default list of validators the network trusts."
    if added:
        preview = ", ".join(v[:10] + "…" for v in added[:3])
        rest = f" (+{len(added) - 3} more)" if len(added) > 3 else ""
        lines.append({
            "category": "unl",
            "line": f"UNL: {len(added)} validator(s) added ({preview}{rest}){UNL_MEANING}",
            "before": None, "after": added,
            "prove_url": "/network", "source": "unl_snapshot",
        })
    if removed:
        preview = ", ".join(v[:10] + "…" for v in removed[:3])
        rest = f" (+{len(removed) - 3} more)" if len(removed) > 3 else ""
        lines.append({
            "category": "unl",
            "line": f"UNL: {len(removed)} validator(s) removed ({preview}{rest}){UNL_MEANING}",
            "before": removed, "after": None,
            "prove_url": "/network", "source": "unl_snapshot",
        })
    return lines


def build_changes_for_date(date: dt.date, *, pg_connect=None) -> dict:
    """Build a /changes envelope for `date` by diffing today's snapshot
    against yesterday's. Fails soft when yesterday's snapshot is missing —
    the envelope still renders with 'first day of chain' semantics."""
    if pg_connect is None:
        import db as _db
        pg_connect = _db.pg_connect

    with pg_connect() as conn:
        with conn.cursor() as cur:
            today = _load_envelope_for_date(cur, date)
            # 2026-09-11 fix: walk back to previous DISTINCT snapshot so
            # duplicate-metric days (two signed_snapshot fires with no
            # source-data change) don't produce exact-zero-delta envelopes.
            if today:
                yesterday, prev_date, skipped_dup_dates = _find_previous_distinct_envelope(cur, today, date)
            else:
                yesterday, prev_date, skipped_dup_dates = None, date - dt.timedelta(days=1), []
            today_unl = _load_unl_snapshot_for_date(cur, date)
            yesterday_unl = _load_unl_snapshot_for_date(cur, prev_date) if prev_date else None

    changes: list[dict] = []
    categories_no_change: list[str] = []

    if today is None:
        # No snapshot for this date at all — bail with an empty envelope
        return {
            "date": date.isoformat(),
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "changes": [],
            "categories_no_change": ["all"],
            "disclosure": DISCLOSURE + (
                f" No signed_snapshot for {date.isoformat()} was found — the chain "
                "did not fire that day or the walker was down. See /health."
            ),
        }

    # Chain lineage delta
    changes.extend(_chain_lineage_delta(yesterday, today))

    # Scalar metric diffs — one line per changed metric
    for name, label, prove, mtype in _SCALAR_METRICS:
        b = _metric_by_name(yesterday, name) if yesterday else None
        a = _metric_by_name(today, name)
        line = _scalar_delta_line(name, label, prove, b, a, metric_type=mtype)
        if line:
            changes.append(line)

    # Registry_state delta
    b_reg = _metric_by_name(yesterday, "registry_state") if yesterday else None
    a_reg = _metric_by_name(today, "registry_state")
    changes.extend(_registry_state_deltas(b_reg, a_reg))
    if not any(c["category"] == "registry" for c in changes):
        categories_no_change.append("registry")

    # UNL churn
    changes.extend(_unl_delta(yesterday_unl, today_unl))
    if not any(c["category"] == "unl" for c in changes):
        categories_no_change.append("unl")

    # v2 slots — explicitly flag what we don't cover yet
    for missing in ("amendments", "whales", "curator_decisions"):
        if missing not in categories_no_change:
            categories_no_change.append(missing)

    # If no scalar-metric change fired, mark those quiet categories
    scalar_categories_seen = {c["category"] for c in changes if c["source"] == "signed_snapshot"}
    for cat in ("amm", "mpt", "rlusd", "rwa", "network"):
        if cat not in scalar_categories_seen and cat not in categories_no_change:
            categories_no_change.append(cat)

    # 2026-09-11 fix: zero-scalar-delta suspect guard. If today's snapshot
    # produced NO scalar changes AND we didn't find a distinct-previous
    # (or the only "changes" are the always-present chain/ledger-index
    # heartbeats), that's the exact-zero-deltas signature Charlie flagged
    # 2026-09-11 morning ("AMM: no change today / MPT: no change today /
    # RWA: no change today" on a day AMM TVL had actually moved). Mark as
    # SUSPECT rather than confidently publishing "no change" — a reader
    # should see "diff suspect" and know we didn't confirm zero motion.
    scalar_change_count = sum(1 for c in changes if c.get("metric_type") in ("usd", "count"))
    suspect_reason = None
    if scalar_change_count == 0:
        if prev_date is None and skipped_dup_dates:
            suspect_reason = (
                f"walked back {len(skipped_dup_dates)} day(s) "
                f"({', '.join(skipped_dup_dates)}) — all had identical scalars to today; "
                "no distinct previous snapshot within lookback window."
            )
        elif yesterday and prev_date and prev_date == (date - dt.timedelta(days=1)):
            # We diffed against calendar-yesterday and got zero. Not
            # duplicate-suppression path — could be legitimately quiet, but
            # 5 of 5 scalar metrics all-zero on a live chain is unusual.
            # Mark suspect if EVERY scalar metric matched exactly.
            today_scalars = _scalar_metrics_map(today)
            yesterday_scalars = _scalar_metrics_map(yesterday)
            if today_scalars and today_scalars == yesterday_scalars:
                suspect_reason = (
                    "all 5 scalar metrics identical between today's snapshot "
                    "and yesterday's — data-source silence suspect."
                )

    return {
        "date": date.isoformat(),
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "changes": changes,
        "categories_no_change": categories_no_change,
        "prev_snapshot_date": prev_date.isoformat() if prev_date else None,
        "prev_skipped_duplicate_dates": skipped_dup_dates,
        "diff_suspect_reason": suspect_reason,
        "disclosure": DISCLOSURE,
    }


CHANGES_DIR = os.path.join(HERE, "changes")


def write_changes_file(date: dt.date, envelope: dict) -> str:
    """Write the envelope to changes/YYYY-MM-DD.json (creates dir if missing)."""
    os.makedirs(CHANGES_DIR, exist_ok=True)
    path = os.path.join(CHANGES_DIR, f"{date.isoformat()}.json")
    with open(path, "w") as f:
        json.dump(envelope, f, indent=2, sort_keys=False)
        f.write("\n")
    return path


def _is_anomaly(c: dict) -> bool:
    """Anomaly = something worth flagging vs the daily heartbeat.
    Charlie ruling 2026-09-08: chain discontinuity, signing-key rotation,
    amendment status change (v2), UNL churn, taxonomy bump."""
    line = c.get("line", "") or ""
    cat = c.get("category")
    if line.startswith("⚠"):        # chain discontinuity, key rotation
        return True
    if cat == "unl":
        return True
    if cat == "registry" and "taxonomy" in line.lower():
        return True
    # v2 slot — amendments status changes will emit category='amendments'
    if cat == "amendments":
        return True
    return False


def _is_routine_heartbeat(c: dict) -> bool:
    """Routine daily 'we wrote today' proofs — chain leaf advance,
    registry merkle root advance. Never slot; live in header/page body."""
    line = c.get("line", "") or ""
    if line.startswith("Signed-snapshot chain advanced"):
        return True
    if line.startswith("Registry history merkle root advanced"):
        return True
    return False


def _scalar_floor_check(c: dict) -> tuple[bool, str]:
    """Return (qualifies, reason_when_below). Charlie ruling 2026-09-08:
        USD:   |Δ| ≥ max($10,000, 0.1% × prior)
        count: |Δ| ≥ 10 units AND |Δ%| ≥ 1%
    """
    mtype = c.get("metric_type")
    b, a = c.get("before"), c.get("after")
    if mtype not in ("usd", "count"):
        return (False, "not a scalar metric")
    if not isinstance(b, (int, float)) or not isinstance(a, (int, float)):
        return (False, "non-numeric delta")
    if not b:
        return (False, "zero prior value")
    delta = a - b
    abs_delta = abs(delta)
    pct = abs_delta / abs(b)
    if mtype == "usd":
        rel_floor = USD_PCT_FLOOR * abs(b)
        floor = max(USD_ABS_FLOOR, rel_floor)
        if abs_delta >= floor:
            return (True, "")
        # Human-readable reason (Charlie ruling 2026-09-08 evening —
        # exact voice match to his example: "(+$9, below the $10K floor)".
        # No Δ prefix, rounded dollars, comma before "below the".
        sign = "+" if delta > 0 else "−"
        rounded = _fmt_rounded_usd(abs_delta)
        if floor <= USD_ABS_FLOOR:
            return (False, f"{sign}{rounded}, below the $10K floor")
        return (False, f"{sign}{rounded}, below the "
                       f"{_fmt_rounded_usd(floor)} floor (0.1% × prior)")
    # count — same voice: "(−5, below the 10-unit / 1% floor)"
    if abs_delta >= COUNT_ABS_FLOOR and pct >= COUNT_PCT_FLOOR:
        return (True, "")
    sign = "+" if delta > 0 else "−"
    rounded = _fmt_rounded_count(abs_delta)
    return (False, f"{sign}{rounded}, below the 10-unit / 1% floor")


def _pct_signed(c: dict) -> float:
    """Signed % change for ranking; robust to zero prior."""
    b, a = c.get("before"), c.get("after")
    if not isinstance(b, (int, float)) or not isinstance(a, (int, float)) or not b:
        return 0.0
    return (a - b) / abs(b)


def _label_for_line(c: dict) -> str:
    """Short label for quiet-day fill ('AMM', 'RWA', 'MPT', ...)."""
    lab = c.get("label")
    if lab:
        # Strip the "(USD)" trailer and "total" filler for the fill text
        return (lab.replace(" total TVL (USD)", " TVL")
                    .replace(" total AUM (USD)", "")
                    .replace(" XRPL supply", " supply")
                    .replace(" total count", "")
                    .strip())
    return c.get("category", "").upper()


def build_strip(envelope: dict, k: int = 3) -> dict:
    """Homepage strip data. Charlie ruling 2026-09-08 (scaled floor):
    header carries the heartbeat (ledger index + UTC time), slots go to
    anomalies (newest first) then scalar deltas that clear the floor
    (ranked by |%|), quiet-day fill for empty slots (below-floor category
    first, with reason). Chain-leaf + ledger-index advances never slot.
    """
    changes = envelope.get("changes") or []

    # Header: latest validated ledger index (from the ledger-index change if
    # present, else best-effort None so the template can render dashes).
    ledger_index = None
    for c in changes:
        if c.get("metric_name") == "xrpl_validated_ledger_index":
            ledger_index = c.get("after")
            break

    # Classify
    anomalies = [c for c in changes if _is_anomaly(c)]
    scalar_candidates = [c for c in changes
                         if c.get("metric_type") in ("usd", "count")
                         and c.get("metric_name") != "xrpl_validated_ledger_index"
                         and not _is_anomaly(c)]

    # Split scalars into qualifiers and below-floor
    qualifiers, below_floor = [], []
    for c in scalar_candidates:
        ok, reason = _scalar_floor_check(c)
        (qualifiers if ok else below_floor).append(
            dict(c, _below_floor_reason=reason) if not ok else c
        )
    qualifiers.sort(key=lambda c: abs(_pct_signed(c)), reverse=True)
    # Below-floor also ranked by |%| — the "biggest near-miss" is the most
    # informative quiet-day fill (2.91% RWA reads more interesting than a
    # 0.02% AMM-count drift).
    below_floor.sort(key=lambda c: abs(_pct_signed(c)), reverse=True)

    slots: list[dict] = []
    # Tier 1: anomalies (newest first — envelope already emits in a stable
    # deterministic order; we treat that as "newest first"). The
    # anomaly lines are pre-worded by the builder now (Charlie edit
    # 2026-09-08 evening: "what happened + what we're doing + where to
    # read more, no internal component names").
    for c in anomalies:
        if len(slots) >= k:
            break
        slots.append({
            "category": c.get("category"),
            "line": c.get("line"),
            "detail": c.get("detail"),
            "prove_url": c.get("prove_url"),
            "source": c.get("source"),
            "kind": "anomaly",
        })
    # Tier 2: scalar qualifiers by |%| — line already carries the
    # plain-English lead + "what this means" clause per Charlie's
    # ruling. detail carries the exact before → after for the /changes
    # body and JSON twin.
    for c in qualifiers:
        if len(slots) >= k:
            break
        slots.append({
            "category": c.get("category"),
            "line": c.get("line"),
            "detail": c.get("detail"),
            "prove_url": c.get("prove_url"),
            "source": c.get("source"),
            "kind": "scalar",
            "metric_name": c.get("metric_name"),
            "metric_type": c.get("metric_type"),
            "delta_pct": _pct_signed(c),
        })
    # Tier 3: quiet-day fill — below-floor categories first. Charlie
    # ruling 2026-09-08: "RWA: no material change today (+$9, below the
    # $10K floor)." — one sentence, same voice as the leads.
    for c in below_floor:
        if len(slots) >= k:
            break
        reason = c.get("_below_floor_reason", "below floor")
        prefix = _SHORT_LABEL.get(c.get("metric_name") or "",
                                   _label_for_line(c))
        slots.append({
            "category": c.get("category"),
            "line": f"{prefix}: no material change today ({reason}).",
            "prove_url": c.get("prove_url"),
            "source": c.get("source"),
            "kind": "quiet_day_below_floor",
            "metric_name": c.get("metric_name"),
        })
    # Tier 4: pure quiet-day fill (no candidate at all in a category)
    if len(slots) < k:
        seen_cats = {s.get("category") for s in slots}
        _CAT_LABEL = {
            "amm": "AMM", "mpt": "MPT", "rwa": "RWA", "rlusd": "RLUSD",
            "network": "Network", "registry": "Registry", "unl": "UNL",
        }
        for cat in ("amm", "mpt", "rwa", "rlusd", "network", "registry", "unl"):
            if len(slots) >= k:
                break
            if cat in seen_cats:
                continue
            slots.append({
                "category": cat,
                "line": f"{_CAT_LABEL.get(cat, cat.upper())}: no change today.",
                "prove_url": None,
                "source": None,
                "kind": "quiet_day_no_candidate",
            })
            seen_cats.add(cat)

    return {
        "header": {
            "date": envelope.get("date"),
            "validated_ledger_index": ledger_index,
            "as_of_utc": envelope.get("generated_at_utc"),
        },
        # Charlie ruling 2026-09-08: one-line explainer under the header
        # for first-time visitors. Verbatim from the ruling.
        "explainer": ("The biggest things that moved on the XRP Ledger "
                      "in the last 24 hours, measured from our own node. "
                      "Every line links to its proof."),
        "slots": slots[:k],
        "total_changes": len(changes),
        "changes_url": f"/changes/{envelope.get('date')}" if envelope.get("date") else "/changes",
    }


# Backward-compat shim — the earlier significant_changes() name is kept
# so external callers (route canary, /changes template historically) still
# resolve while everything migrates to build_strip().
def significant_changes(envelope: dict, k: int = 3) -> list[dict]:
    return build_strip(envelope, k=k).get("slots", [])


if __name__ == "__main__":  # pragma: no cover
    # CLI: print today's envelope to stdout
    date = dt.date.today()
    if len(sys.argv) > 1:
        date = dt.date.fromisoformat(sys.argv[1])
    env = build_changes_for_date(date)
    print(json.dumps(env, indent=2, sort_keys=False))

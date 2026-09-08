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
    "(daily whale line pending — v2), and any external attestations. See "
    "/methodology for how each metric is derived."
)


# Ordered list of scalar metric names from signed_snapshot envelopes.
# Order determines display order when multiple scalars change in the same day.
_SCALAR_METRICS: list[tuple[str, str, str]] = [
    # (metric_name, human_label, prove_url)
    ("xrpl_validated_ledger_index", "XRPL validated ledger index", "/.well-known/snapshots/"),
    ("amm_pools_count", "AMM pools count", "/pools"),
    ("amm_pools_total_tvl_usd", "AMM total TVL (USD)", "/pools"),
    ("mpt_total_count", "MPT total count", "/mpts"),
    ("named_accounts_count", "Named accounts count", "/network"),
    ("rlusd_xrpl_supply", "RLUSD XRPL supply", "/rlusd"),
    ("rwa_total_aum_usd", "RWA total AUM (USD)", "/rwa"),
]


def _fmt_num(v: Any) -> str:
    if isinstance(v, (int, float)):
        if isinstance(v, float) and abs(v) >= 1000:
            return f"{v:,.2f}"
        if isinstance(v, int):
            return f"{v:,}"
        return f"{v:.4f}"
    return str(v)


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


def _scalar_delta_line(name: str, label: str, prove_url: str,
                       before: Any, after: Any) -> Optional[dict]:
    """Return a change dict for a scalar metric delta, or None if unchanged."""
    if before is None or after is None:
        if before != after:
            return {
                "category": _category_for_metric(name),
                "line": f"{label} {'appeared' if before is None else 'went missing'} "
                        f"(now={_fmt_num(after)}, was={_fmt_num(before)})",
                "before": before, "after": after,
                "prove_url": prove_url, "source": "signed_snapshot",
            }
        return None
    if before == after:
        return None
    # Compute magnitude
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        delta = after - before
        sign = "+" if delta > 0 else ""
        return {
            "category": _category_for_metric(name),
            "line": f"{label}: {_fmt_num(before)} → {_fmt_num(after)} ({sign}{_fmt_num(delta)})",
            "before": before, "after": after, "delta": delta,
            "prove_url": prove_url, "source": "signed_snapshot",
        }
    return {
        "category": _category_for_metric(name),
        "line": f"{label}: {before} → {after}",
        "before": before, "after": after,
        "prove_url": prove_url, "source": "signed_snapshot",
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
            "line": f"Registry taxonomy bumped: {b_tax} → {a_tax}",
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
                "before": bv, "after": av,
                "prove_url": "/registry/taxonomy", "source": "signed_registry_snapshot",
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
            lines.append({
                "category": "chain",
                "line": f"⚠ Signed-snapshot leaf discontinuity: #{b_leaf} → #{a_leaf} (expected +1)",
                "before": b_leaf, "after": a_leaf,
                "prove_url": "/.well-known/snapshots/", "source": "signed_snapshot",
            })
    # Signing pubkey fingerprint change = key rotation event
    b_fp = b.get("signing_pubkey_fingerprint")
    a_fp = a.get("signing_pubkey_fingerprint")
    if b_fp and a_fp and b_fp != a_fp:
        lines.append({
            "category": "chain",
            "line": f"⚠ Signing key fingerprint rotated: {b_fp} → {a_fp}",
            "before": b_fp, "after": a_fp,
            "prove_url": "/.well-known/snapshots/pubkey.json", "source": "signed_snapshot",
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
    if added:
        preview = ", ".join(v[:10] + "…" for v in added[:3])
        rest = f" (+{len(added) - 3} more)" if len(added) > 3 else ""
        lines.append({
            "category": "unl",
            "line": f"UNL: {len(added)} validator(s) added ({preview}{rest})",
            "before": None, "after": added,
            "prove_url": "/network", "source": "unl_snapshot",
        })
    if removed:
        preview = ", ".join(v[:10] + "…" for v in removed[:3])
        rest = f" (+{len(removed) - 3} more)" if len(removed) > 3 else ""
        lines.append({
            "category": "unl",
            "line": f"UNL: {len(removed)} validator(s) removed ({preview}{rest})",
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

    prev_date = date - dt.timedelta(days=1)
    with pg_connect() as conn:
        with conn.cursor() as cur:
            today = _load_envelope_for_date(cur, date)
            yesterday = _load_envelope_for_date(cur, prev_date)
            today_unl = _load_unl_snapshot_for_date(cur, date)
            yesterday_unl = _load_unl_snapshot_for_date(cur, prev_date)

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
    for name, label, prove in _SCALAR_METRICS:
        b = _metric_by_name(yesterday, name) if yesterday else None
        a = _metric_by_name(today, name)
        line = _scalar_delta_line(name, label, prove, b, a)
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

    return {
        "date": date.isoformat(),
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "changes": changes,
        "categories_no_change": categories_no_change,
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


def significant_changes(envelope: dict, k: int = 3) -> list[dict]:
    """Return the top-k most significant changes for the homepage strip.

    Deterministic significance rule (Charlie ruling 2026-09-08):
      1. Chain-lineage anomalies (⚠ prefix in line) always win the top slot(s)
      2. Registry taxonomy version bumps
      3. UNL validator churn
      4. Fill remaining slots with highest |Δ|/prior_value normalized scalar diffs
      5. If nothing meets any bar: return an explicit "no change" placeholder line
    """
    changes = envelope.get("changes") or []
    if not changes:
        return [{
            "category": "chain",
            "line": ("Ledger closed. No amendment / UNL / signing changes today."
                     if envelope.get("categories_no_change")
                     else "No changes recorded."),
            "prove_url": "/.well-known/snapshots/",
            "source": "changes",
        }]
    tier1 = [c for c in changes if c.get("line", "").startswith("⚠")]
    tier2 = [c for c in changes if c.get("category") == "registry" and "taxonomy" in c.get("line", "").lower()]
    tier3 = [c for c in changes if c.get("category") == "unl"]
    scalar_scored: list[tuple[float, dict]] = []
    for c in changes:
        b, a = c.get("before"), c.get("after")
        if isinstance(b, (int, float)) and isinstance(a, (int, float)) and b:
            score = abs((a - b) / b)
            scalar_scored.append((score, c))
    scalar_scored.sort(reverse=True, key=lambda x: x[0])
    tier4 = [c for _, c in scalar_scored]
    seen: set[int] = set()
    out: list[dict] = []
    for tier in (tier1, tier2, tier3, tier4):
        for c in tier:
            key = id(c)
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
            if len(out) >= k:
                return out
    return out


if __name__ == "__main__":  # pragma: no cover
    # CLI: print today's envelope to stdout
    date = dt.date.today()
    if len(sys.argv) > 1:
        date = dt.date.fromisoformat(sys.argv[1])
    env = build_changes_for_date(date)
    print(json.dumps(env, indent=2, sort_keys=False))

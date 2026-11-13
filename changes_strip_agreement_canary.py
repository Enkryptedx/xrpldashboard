"""Strip-vs-/changes agreement canary.

The homepage "What's new" strip and /changes are two renders of ONE
envelope. On 2026-11-11 and 2026-11-12 they disagreed: the strip said
"Comparison unavailable: we have no prior-day snapshot to compare
against" while /changes listed 7 derived changes for the same date, and
the envelope itself named its prev_snapshot_date. The box contradicted
the page it linked to, and nothing noticed until a human read it.

Cause was an all-or-nothing guard -- any missing producer set
comparable=False and build_strip replaced every slot. The per-category
fix (2026-11-13) scopes that to the categories a producer actually feeds.
This canary is the detector for the same CLASS of defect, so a future
regression surfaces without a human spotting it.

Three findings, each a separate failure mode:

  contradiction  strip reports not-comparable while the envelope carries
                 a non-zero change count. This is the exact 11-11/11-12
                 shape and is always wrong: a change count above zero
                 proves a comparison happened.
  count_mismatch strip's total_changes disagrees with the number of
                 changes in the envelope it was built from.
  basis_claim    envelope says comparable while listing the basis
                 producer as missing, or vice versa -- the two fields
                 must not disagree about whether a diff was possible.

Read-only: one SELECT plus a pure rebuild of the strip from the stored
envelope. No HTTP, so it cannot be fooled by a cached homepage body and
cannot be blamed for one either; it tests the DATA both surfaces render.

Cadence: 30 min. L1 pager reads walker_health.findings_count.
"""
from __future__ import annotations

import datetime as dt
import json
import sys

import changes_builder
import db

WALKER_NAME = "changes_strip_agreement_canary"
CADENCE_SECONDS = 1800

# How many recent envelopes to check. The homepage renders only the
# newest, but a disagreement on an older date still means the builder
# produced a self-contradictory record, so a short window catches a
# regression that landed overnight.
LOOKBACK = 3


def _load_recent(cur, limit: int) -> list[tuple[str, dict, int]]:
    cur.execute(
        "SELECT snapshot_date, envelope, change_count "
        "FROM changes_envelopes ORDER BY snapshot_date DESC LIMIT %s",
        (limit,),
    )
    out = []
    for row in cur.fetchall():
        date, env, cc = row[0], row[1], row[2]
        if isinstance(env, str):
            env = json.loads(env)
        out.append((str(date), env, cc))
    return out


def check_envelope(date: str, env: dict, change_count: int) -> list[dict]:
    """Return findings for one stored envelope. Pure."""
    findings: list[dict] = []
    changes = env.get("changes") or []
    missing = env.get("inputs_missing") or []

    strip = changes_builder.build_strip(env)
    strip_comparable = strip.get("comparable")
    strip_total = strip.get("total_changes")

    # 1. The 11-11/11-12 shape: strip says nothing to compare, envelope
    #    carries changes anyway.
    if strip_comparable is False and len(changes) > 0:
        findings.append({
            "kind": "contradiction",
            "date": date,
            "detail": (
                f"strip reports comparable=False while the envelope carries "
                f"{len(changes)} change(s); /changes would list them and the "
                f"homepage would say none. inputs_missing={missing}"
            ),
        })

    # 2. The two surfaces must agree on how many changes exist.
    if strip_total is not None and strip_total != len(changes):
        findings.append({
            "kind": "count_mismatch",
            "date": date,
            "detail": (
                f"strip.total_changes={strip_total} but envelope has "
                f"{len(changes)} change(s)"
            ),
        })

    # 3. comparable must track the BASIS producer, not any producer.
    basis_missing = changes_builder._basis_missing(missing)
    env_comparable = env.get("comparable")
    if env_comparable is not None and env_comparable == basis_missing:
        findings.append({
            "kind": "basis_claim",
            "date": date,
            "detail": (
                f"envelope comparable={env_comparable} but basis_missing="
                f"{basis_missing} (inputs_missing={missing}); a missing "
                f"side-category producer must not void the comparison, and "
                f"a missing basis must not read as comparable"
            ),
        })

    # Stored change_count column should match the envelope it describes.
    if change_count is not None and change_count != len(changes):
        findings.append({
            "kind": "count_mismatch",
            "date": date,
            "detail": (
                f"changes_envelopes.change_count={change_count} but envelope "
                f"carries {len(changes)}"
            ),
        })
    return findings


def main() -> int:
    db.write_walker_health_start(WALKER_NAME, cadence_seconds=CADENCE_SECONDS)
    ok, message, findings = True, "", []
    try:
        with db.pg_connect() as conn:
            with conn.cursor() as cur:
                rows = _load_recent(cur, LOOKBACK)
        if not rows:
            ok, message = False, "no changes_envelopes rows to check"
        else:
            for date, env, cc in rows:
                findings.extend(check_envelope(date, env, cc))
            message = (
                f"checked {len(rows)} envelope(s); "
                f"{len(findings)} disagreement(s)"
            )
    except Exception as exc:  # noqa: BLE001
        ok, message = False, f"{type(exc).__name__}: {exc}"

    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    print(f"[{WALKER_NAME}] {message} @ {stamp}")
    for f in findings:
        print(f"  FINDING {f['kind']:<14} {f['date']}  {f['detail']}")

    try:
        db.write_walker_health_end(
            WALKER_NAME, ok=ok, message=message,
            findings_count=len(findings),
        )
    except TypeError:
        db.write_walker_health_end(WALKER_NAME, ok=ok, message=message)
    return 0 if (ok and not findings) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

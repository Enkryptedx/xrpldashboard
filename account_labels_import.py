"""
Account labels importer.

Two passes, both idempotent:

  1) Manual pass — reads `account_labels_manual.json` and UPSERTs each
     entry with its declared source (manual / xrpscan / bithomp).
     Curated sources always overwrite each other on conflict; they NEVER
     get clobbered by the derived pass below.

  2) Derived pass — auto-generates labels from on-chain state we already
     have in Postgres:
       - amm_ranked_pools  →  "AMM Pool {pair}"   source=derived:amm
       - mpt_snapshot      →  "MPT Issuer: {name}" source=derived:mpt
     These only land on addresses that don't already have a curated label.

Usage:
    python account_labels_import.py            # full sync (manual + derived)
    python account_labels_import.py --manual   # manual-only (skip derived)
    python account_labels_import.py --derived  # derived-only (skip manual)
"""

import argparse
import json
import os
import sys

import db

HERE = os.path.dirname(os.path.abspath(__file__))
MANUAL_PATH = os.environ.get(
    "ACCOUNT_LABELS_MANUAL_PATH",
    os.path.join(HERE, "account_labels_manual.json"),
)


def _load_manual_entries(path):
    """Return the list of label dicts from the manual JSON. Tolerates the
    two shapes the file may have: a top-level list (legacy) or a dict with
    a `labels` key (current). README/comment keys are ignored."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[labels] manual file not found: {path}", flush=True)
        return []
    except json.JSONDecodeError as e:
        print(f"[labels] manual file malformed: {e}", flush=True)
        return []

    if isinstance(data, dict):
        entries = data.get("labels") or []
    elif isinstance(data, list):
        entries = data
    else:
        return []
    return [e for e in entries if isinstance(e, dict) and not e.get("_skip")]


def import_manual():
    entries = _load_manual_entries(MANUAL_PATH)
    written = skipped = 0
    for e in entries:
        address = e.get("address")
        name = e.get("name")
        source = e.get("source")
        if not address or not name or not source:
            skipped += 1
            continue
        ok = db.upsert_account_label(
            address=address,
            name=name,
            source=source,
            category=e.get("category"),
            confidence=e.get("confidence"),
            extra=e.get("extra"),
        )
        if ok:
            written += 1
        else:
            skipped += 1
    print(f"[labels] manual: {written} written, {skipped} skipped, "
          f"{len(entries)} total", flush=True)
    return written


def import_derived_amm():
    """Label every AMM account in amm_ranked_pools as 'AMM Pool {pair}'.
    Will not overwrite a curated label — server-side WHERE clause inside
    db.bulk_upsert_derived_labels enforces that. Bulk path because per-row
    UPSERTs against Neon for 20k+ accounts took 15+ min in testing."""
    pools = db.read_amm_ranked_pools()
    if not pools:
        print("[labels] derived:amm: no pools in PG; skipped.", flush=True)
        return 0
    rows = []
    for p in pools:
        acct = p.get("amm_account")
        if not acct:
            continue
        pair = p.get("pair") or "?"
        rows.append((
            acct, f"AMM Pool {pair}", "amm", "derived:amm", 0.7,
            json.dumps({"pair": pair}),
        ))
    written = db.bulk_upsert_derived_labels(rows)
    print(f"[labels] derived:amm: {written} attempted from {len(pools)} pools "
          f"(curated rows preserved)", flush=True)
    return written


def import_derived_mpt():
    """Label every MPT issuer in the snapshot. The 'address' of an MPT
    is its issuer account, and one issuer may issue many MPTs — we keep
    the most-prominent (highest outstanding) issuance per issuer for the
    label string so the page reads cleanly."""
    snap = db.read_mpt_snapshot()
    if not snap:
        print("[labels] derived:mpt: no snapshot in PG; skipped.", flush=True)
        return 0
    issuances = snap.get("issuances") or []
    if not issuances:
        print("[labels] derived:mpt: snapshot has zero issuances; skipped.",
              flush=True)
        return 0
    by_issuer = {}
    for r in issuances:
        issuer = r.get("issuer")
        if not issuer:
            continue
        existing = by_issuer.get(issuer)
        if existing is None or (r.get("outstanding_amount") or 0) > \
                (existing.get("outstanding_amount") or 0):
            by_issuer[issuer] = r
    rows = []
    for issuer, r in by_issuer.items():
        # Defensive strip: JS-side enrichment upstream has been observed to
        # emit `"<name> - undefined"` and `"<name> undefined"` when a
        # metadata field it expected was `undefined`. Never propagate that
        # into a public label (audit finding 2026-08-30).
        def _clean(v):
            if not v:
                return v
            s = str(v).strip()
            for tail in (" - undefined", " undefined"):
                if s.endswith(tail):
                    s = s[: -len(tail)].strip()
            return s or None
        ticker = _clean(r.get("ticker")) or _clean(r.get("name")) or "?"
        issuer_name = _clean(r.get("issuer_name"))
        display = f"MPT Issuer: {issuer_name} ({ticker})" if issuer_name \
            else f"MPT Issuer ({ticker})"
        extra = json.dumps({
            "ticker": _clean(r.get("ticker")),
            "name": _clean(r.get("name")),
        })
        rows.append((issuer, display, "mpt_issuer", "derived:mpt", 0.7, extra))
    written = db.bulk_upsert_derived_labels(rows)
    print(f"[labels] derived:mpt: {written} attempted from "
          f"{len(by_issuer)} unique issuers", flush=True)
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manual", action="store_true",
                    help="Run only the manual pass (skip derived).")
    ap.add_argument("--derived", action="store_true",
                    help="Run only the derived pass (skip manual).")
    args = ap.parse_args()

    if not db.pg_available():
        print("[labels] DATABASE_URL unset; nothing to do.", flush=True)
        return 1

    do_manual = not args.derived
    do_derived = not args.manual

    if do_manual:
        import_manual()
    if do_derived:
        import_derived_amm()
        import_derived_mpt()

    counts = db.count_account_labels_by_source()
    print(f"[labels] account_labels totals by source: {counts}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

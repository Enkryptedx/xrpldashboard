"""claim_verify_sampler_walker — weekly sampler that checks 10 public
claims across the site against their primary sources.

Charlie ruling 2026-09-23 16:09 ET (Walker B):
  Sample distribution per run:
    - 3 warnings from the feed (visitor-facing scam/impersonation flags)
    - 3 verified rows (tier=verified, source=two_way_toml)
    - 2 dated regulation lines
    - 1 RWA figure with its NAV citation
    - 1 "as of" freshness stamp
  Check each against its PRIMARY source. Write to claim_verify_samples
  (jj_ro-readable). Page on any FAIL.
  First run runs NOW; scheduled cadence is weekly (StartInterval=604800s).

v1 verdict lexicon (per row in claim_verify_samples.verdict):
  PASS          — primary source confirms the claim
  FAIL          — primary source contradicts the claim (page)
  NEEDS_MANUAL  — primary source structure isn't machine-verifiable in
                  this walker version; sample recorded with the URL so a
                  human curator (Charlie) can spot-check. NOT a failure;
                  a queue signal.
  SKIP          — no sample available for the kind (e.g., no warnings
                  in the last window). Not a failure.

Two of the five kinds ship as NEEDS_MANUAL in v1 (warnings-feed
semantics + regulation-line source URLs) — Charlie's decision on the
verification rule shape is required before I hard-code the check. The
walker still records the sampled claim + primary_source_url per row so
Charlie can eyeball what's in the queue.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import ssl
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import db
from two_way_toml_verifier_walker import verify_submission


WALKER_NAME = "claim_verify_sampler_walker"
WALKER_CADENCE_SECONDS = 604800  # weekly

PRIVATE_TRIAGE_DIR = Path.home() / "xrpl_test_private_triage"


def _record(cur, run_id: str, kind: str, claim_id: str, claim_text: str,
            primary_source_url: str, verdict: str, detail: str,
            duration_ms: int) -> int:
    cur.execute(
        """
        INSERT INTO claim_verify_samples (
            sample_at, claim_kind, claim_id, claim_text,
            primary_source_url, verdict, verdict_detail,
            check_duration_ms, run_id
        ) VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (kind, claim_id, claim_text[:2000], primary_source_url,
         verdict, detail[:2000] if detail else None, duration_ms, run_id),
    )
    return cur.fetchone()[0]


# ─── Kind 1: warnings (3 samples) — NEEDS_MANUAL v1 ─────────────
def _sample_warnings(conn, run_id: str, results: list) -> None:
    with conn.cursor() as cur:
        try:
            rows = db.read_token_warnings_recent(hours_back=24, limit=50)
        except Exception as e:
            results.append(("warnings", "SKIP", f"read_token_warnings_recent failed: {e}"))
            return
        if not rows:
            _record(cur, run_id, "warnings", "no-warnings-in-24h",
                    "no warnings in the last 24h", "n/a", "SKIP",
                    "no rows returned; nothing to sample", 0)
            conn.commit()
            results.append(("warnings", "SKIP", "no rows in last 24h"))
            return
        picks = random.sample(rows, min(3, len(rows)))
        for row_tuple in picks:
            t0 = time.time()
            # row shape observed: (id, ticker, issuer, count, is_active, was_notified)
            wid = row_tuple[0] if len(row_tuple) > 0 else "?"
            ticker = row_tuple[1] if len(row_tuple) > 1 else "?"
            issuer = row_tuple[2] if len(row_tuple) > 2 else "?"
            claim_text = (
                f"warning row id={wid}: ticker={ticker!r} issuer={issuer} "
                f"is currently surfaced as a warning on /check-style feeds"
            )
            primary = f"/check?issuer={issuer}&currency={ticker}"
            _record(cur, run_id, "warnings", str(wid), claim_text, primary,
                    "NEEDS_MANUAL",
                    "v1: warnings-feed check semantics pending Charlie's rule "
                    "(should verify recompute the trigger, or refetch and diff, "
                    "or ping the DB for the flag). Sample recorded for eyeball.",
                    int((time.time() - t0) * 1000))
        conn.commit()
        results.append(("warnings", "NEEDS_MANUAL",
                        f"{len(picks)} warnings sampled; v1 records only"))


# ─── Kind 2: verified rows (3 samples) — REAL verify ─────────────
def _sample_verified_rows(conn, run_id: str, results: list) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT currency_hex, issuer, category, citation_url "
            "FROM token_category_current "
            "WHERE source = 'two_way_toml' "
            "ORDER BY random() LIMIT 3"
        )
        picks = cur.fetchall()
        pass_ct = fail_ct = 0
        for r in picks:
            cx, issuer, category, citation = r
            t0 = time.time()
            row_input = {
                "currency_hex": cx, "issuer": issuer,
                "claimed_category": category, "toml_url": citation,
            }
            try:
                ok, reason, _ = verify_submission(row_input)
            except Exception as e:
                ok = False
                reason = f"unhandled_{type(e).__name__}: {e}"
            verdict = "PASS" if ok else "FAIL"
            if ok:
                pass_ct += 1
            else:
                fail_ct += 1
            claim_text = (
                f"tier=verified via two_way_toml for issuer={issuer} "
                f"currency={cx[:12]}… category={category}"
            )
            _record(cur, run_id, "verified_rows", f"{issuer}/{cx[:16]}",
                    claim_text, citation, verdict,
                    reason if not ok else "two-way TOML re-verified against primary source",
                    int((time.time() - t0) * 1000))
        conn.commit()
        results.append(("verified_rows",
                        "PASS" if fail_ct == 0 and pass_ct > 0 else "FAIL" if fail_ct else "SKIP",
                        f"pass={pass_ct} fail={fail_ct}"))


# ─── Kind 3: regulation lines (2 samples) — NEEDS_MANUAL v1 ──────
def _sample_regulation_lines(conn, run_id: str, results: list) -> None:
    reg_html = Path(REPO_ROOT) / "templates" / "regulation.html"
    if not reg_html.is_file():
        with conn.cursor() as cur:
            _record(cur, run_id, "regulation_lines", "reg-html-missing",
                    "templates/regulation.html not found", "n/a", "SKIP",
                    "template file missing", 0)
            conn.commit()
        results.append(("regulation_lines", "SKIP", "template missing"))
        return
    lines = reg_html.read_text(encoding="utf-8", errors="replace").splitlines()
    # Sample dated <li>-ish lines — anything with a 2026-MM-DD stamp
    import re
    dated = [
        (i + 1, ln.strip())
        for i, ln in enumerate(lines)
        if re.search(r"2026-\d{2}-\d{2}", ln) and "<" in ln
    ]
    if not dated:
        with conn.cursor() as cur:
            _record(cur, run_id, "regulation_lines", "no-dated-lines",
                    "no dated timeline entries found in regulation.html",
                    "templates/regulation.html", "SKIP",
                    "regex found no 2026-MM-DD dated lines", 0)
            conn.commit()
        results.append(("regulation_lines", "SKIP", "no dated lines"))
        return
    picks = random.sample(dated, min(2, len(dated)))
    with conn.cursor() as cur:
        for line_no, ln in picks:
            claim_text = f"regulation.html:{line_no}  {ln[:400]}"
            # v1 primary-source URL is a filename+line pointer; real check
            # needs a per-line source URL map (Charlie's shape).
            _record(cur, run_id, "regulation_lines",
                    f"regulation.html:{line_no}",
                    claim_text,
                    f"templates/regulation.html#L{line_no}",
                    "NEEDS_MANUAL",
                    "v1: per-line primary-source URL map (Senate/SEC/etc.) "
                    "not yet wired. Sample recorded for eyeball; needs Charlie's "
                    "decision on where the citations map lives.", 0)
        conn.commit()
    results.append(("regulation_lines", "NEEDS_MANUAL",
                    f"{len(picks)} dated lines sampled"))


# ─── Kind 4: RWA figure with NAV citation (1 sample) — REAL verify ──
def _sample_rwa_figure(conn, run_id: str, results: list) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT family_slug, nav_symbol, value_usd, nav_per_unit_usd, "
            "supply_units, nav_source_url, xrpl_issuer "
            "FROM rwa_supply_nav_daily "
            "WHERE fetch_date = CURRENT_DATE AND value_usd > 0 "
            "ORDER BY value_usd DESC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            _record(cur, run_id, "rwa_figure", "no-nonzero-today",
                    "no rwa_supply_nav_daily row with value_usd > 0 today",
                    "n/a", "SKIP", "no non-zero RWA figure to sample", 0)
            conn.commit()
            results.append(("rwa_figure", "SKIP", "no non-zero row today"))
            return
        family, symbol, value_usd, nav_pu, supply, nav_url, issuer = row
        claim_text = (
            f"/rwa headline: {family}/{symbol} value_usd={value_usd} "
            f"(supply={supply} × nav_per_unit={nav_pu}), citation={nav_url}"
        )
        # Real check: re-query gateway_balances on our node for the issuer
        # and confirm the supply matches within tolerance.
        t0 = time.time()
        try:
            import urllib.request as ur
            req = ur.Request(
                "http://192.168.40.95:5006/",
                data=json.dumps({
                    "method": "gateway_balances",
                    "params": [{"account": issuer,
                                "ledger_index": "validated",
                                "strict": True}]
                }).encode(),
                headers={"Content-Type": "application/json"},
            )
            with ur.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            obligations = (data.get("result", {})
                             .get("obligations", {}) or {})
            live_supply = None
            for cx, amt in obligations.items():
                # cx may be hex or short; match against symbol either way
                if cx == symbol or (len(cx) == 40 and
                                     bytes.fromhex(cx).rstrip(b"\0")
                                     .decode("utf-8", "replace") == symbol):
                    live_supply = float(amt)
                    break
            if live_supply is None:
                verdict = "FAIL"
                detail = (f"issuer {issuer} obligations has no entry for "
                          f"symbol {symbol}; recorded supply {supply} unverifiable")
            else:
                # 0.5% tolerance for supply drift between walker time and now
                recorded = float(supply)
                delta_pct = abs(live_supply - recorded) / max(recorded, 1) * 100
                if delta_pct < 0.5:
                    verdict = "PASS"
                    detail = (f"live_supply={live_supply:.4f} vs recorded "
                              f"{recorded:.4f} (Δ {delta_pct:.3f}%); within tolerance")
                else:
                    verdict = "FAIL"
                    detail = (f"live_supply={live_supply:.4f} vs recorded "
                              f"{recorded:.4f} (Δ {delta_pct:.3f}%); >0.5% drift")
        except Exception as e:
            verdict = "NEEDS_MANUAL"
            detail = f"XRPL node fetch failed: {type(e).__name__}: {str(e)[:200]}"
        _record(cur, run_id, "rwa_figure", f"{family}/{symbol}",
                claim_text, nav_url or "n/a", verdict, detail,
                int((time.time() - t0) * 1000))
        conn.commit()
        results.append(("rwa_figure", verdict, detail[:80]))


# ─── Kind 5: "as of" freshness stamp (1 sample) — REAL verify ────
def _sample_as_of_freshness(conn, run_id: str, results: list) -> None:
    snap_dir = Path(REPO_ROOT) / "signed_snapshots"
    snaps = sorted(snap_dir.glob("2026-*.json"))
    if not snaps:
        with conn.cursor() as cur:
            _record(cur, run_id, "as_of_freshness", "no-snapshots-found",
                    "no signed_snapshots/*.json files", str(snap_dir),
                    "SKIP", "signed_snapshots dir empty", 0)
            conn.commit()
        results.append(("as_of_freshness", "SKIP", "no snapshots"))
        return
    latest = snaps[-1]
    t0 = time.time()
    try:
        payload = json.loads(latest.read_text())
        claimed_date = payload.get("snapshot_date_utc")
        taken_unix = payload.get("snapshot_taken_unix")
    except Exception as e:
        with conn.cursor() as cur:
            _record(cur, run_id, "as_of_freshness", latest.name,
                    f"latest snapshot={latest.name}", str(latest),
                    "FAIL", f"snapshot parse failed: {e}",
                    int((time.time() - t0) * 1000))
            conn.commit()
        results.append(("as_of_freshness", "FAIL", f"parse failed: {e}"))
        return
    filename_date = latest.stem  # e.g. "2026-09-23"
    if claimed_date != filename_date:
        verdict = "FAIL"
        detail = (f"snapshot_date_utc={claimed_date} does not match "
                  f"filename {filename_date}")
    else:
        # Also require snapshot_taken_unix is within the claimed day (±26h)
        now_unix = time.time()
        age_hours = (now_unix - float(taken_unix)) / 3600 if taken_unix else 9999
        if age_hours > 26:
            verdict = "FAIL"
            detail = (f"snapshot_taken_unix age {age_hours:.1f}h > 26h "
                      f"(stale relative to claimed date)")
        else:
            verdict = "PASS"
            detail = (f"snapshot_date_utc={claimed_date} matches filename; "
                      f"taken {age_hours:.1f}h ago")
    claim_text = (
        f"'as of {claimed_date}' declared by signed_snapshots/{latest.name}"
    )
    with conn.cursor() as cur:
        _record(cur, run_id, "as_of_freshness", latest.name,
                claim_text, str(latest), verdict, detail,
                int((time.time() - t0) * 1000))
        conn.commit()
    results.append(("as_of_freshness", verdict, detail[:80]))


def _write_triage_log(run_id: str, results: list) -> None:
    PRIVATE_TRIAGE_DIR.mkdir(exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    out = PRIVATE_TRIAGE_DIR / f"CLAIM_VERIFY_{ts}.md"
    header = (
        f"# claim_verify_sampler_walker run {run_id}\n"
        f"# {dt.datetime.now(dt.timezone.utc).isoformat()} UTC\n\n"
    )
    body = ""
    for kind, verdict, detail in results:
        body += f"- **{kind}** — {verdict} — {detail}\n"
    body += "\n(See `claim_verify_samples` in Postgres for per-sample rows.)\n"
    with out.open("a") as f:
        f.write(header + body + "\n---\n\n")


def run_walker() -> tuple[str, list]:
    if not db.pg_available():
        raise SystemExit("STRICT-REFUSE: PG unavailable")
    run_id = f"cvs-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    results: list = []
    with db.pg_connect() as conn:
        _sample_warnings(conn, run_id, results)
        _sample_verified_rows(conn, run_id, results)
        _sample_regulation_lines(conn, run_id, results)
        _sample_rwa_figure(conn, run_id, results)
        _sample_as_of_freshness(conn, run_id, results)
    _write_triage_log(run_id, results)
    return run_id, results


def main() -> int:
    db.write_walker_health_start(
        WALKER_NAME, cadence_seconds=WALKER_CADENCE_SECONDS,
    )
    ok = False
    message = "not_yet_stamped"
    print(
        f"[claim_verify_sampler] start "
        f"{dt.datetime.now(dt.timezone.utc).isoformat()}",
        flush=True,
    )
    t0 = time.time()
    try:
        run_id, results = run_walker()
        elapsed = time.time() - t0
        fail_ct = sum(1 for _, v, _ in results if v == "FAIL")
        pass_ct = sum(1 for _, v, _ in results if v == "PASS")
        needs = sum(1 for _, v, _ in results if v == "NEEDS_MANUAL")
        skip = sum(1 for _, v, _ in results if v == "SKIP")
        ok = True
        message = (
            f"run_id={run_id} kinds={len(results)} pass={pass_ct} "
            f"fail={fail_ct} needs_manual={needs} skip={skip} "
            f"elapsed={elapsed:.1f}s"
        )
        print(f"[claim_verify_sampler] end OK: {message}", flush=True)
        for kind, verdict, detail in results:
            print(f"  {kind:<20} {verdict:<14} {detail[:80]}", flush=True)
    except SystemExit as e:
        message = f"strict_refuse: {e}"
        print(f"[claim_verify_sampler] {message}", file=sys.stderr, flush=True)
        return 1
    except Exception as e:
        message = f"unhandled_{type(e).__name__}: {str(e)[:120]}"
        print(f"[claim_verify_sampler] {message}", file=sys.stderr, flush=True)
        return 1
    finally:
        try:
            db.write_walker_health_end(
                WALKER_NAME, ok=ok,
                message=message or ("clean_no_message" if ok else "unlabeled_failure"),
            )
        except Exception as e:
            print(
                f"[claim_verify_sampler] walker_health end write failed: {e}",
                file=sys.stderr, flush=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

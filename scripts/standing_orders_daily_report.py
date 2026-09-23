"""Standing-orders daily 5-line phone report.

Fires each morning during Charlie's away-window (per
standing_orders_charlie_away memory). Sends 5 lines to Charlie's
Telegram covering: referrers, geo, AI crawlers, signed surfaces,
chain health. Format is EXACT — 5 lines only. Append a 6th
'Q for Charlie:' line ONLY when a decision requires his input.

Charlie ruling 2026-09-08 evening: dry-run this format for real
during Wed 09-09 and Thu 09-10 while Charlie is still present, so
gaps surface before Sunday evening when standing orders LIVE.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import sys
import subprocess

sys.path.insert(0, "/Users/charliebruce/xrpl_test")
import db

HUMAN_WHERE = "(is_bot IS NULL OR is_bot = FALSE)"


def _yesterday_utc_window() -> tuple[int, int, str]:
    """Return (start_ts, end_ts, human_date) for yesterday's full UTC day."""
    now = dt.datetime.now(dt.timezone.utc)
    yday = now.date() - dt.timedelta(days=1)
    start = int(dt.datetime.combine(yday, dt.time(0, 0), tzinfo=dt.timezone.utc).timestamp())
    end = start + 86400
    return start, end, yday.strftime("%Y-%m-%d")


def line_referrers(cur, ts_s, ts_e) -> str:
    cur.execute(f"""
        SELECT
          -- Charlie ruling 2026-09-21: X bucket must include t.co
          -- (Twitter's shortener). The evening Sunday-close report
          -- included it and showed 6 clicks off the /thisweek tweet;
          -- this daily report read X=0 because the regex only matched
          -- twitter|x\.com. Align on the same definition everywhere
          -- the site groups referrers.
          SUM(CASE WHEN referrer ~* 't\\.co|twitter|x\\.com' THEN 1 ELSE 0 END) x,
          SUM(CASE WHEN referrer ~* 'reddit' THEN 1 ELSE 0 END) reddit,
          SUM(CASE WHEN referrer ~* 'news|hackernews|linkedin' THEN 1 ELSE 0 END) news,
          SUM(CASE WHEN referrer IS NULL OR referrer = '' THEN 1 ELSE 0 END) direct,
          SUM(CASE WHEN referrer ~* 'google|bing|duckduckgo|yandex|baidu' THEN 1 ELSE 0 END) search
        FROM page_views WHERE ts >= %s AND ts < %s AND {HUMAN_WHERE}
    """, (ts_s, ts_e))
    r = cur.fetchone()
    x, reddit, news, direct, search = [x or 0 for x in r]
    return f"1. Referrers: X={x}, Reddit={reddit}, news={news}, direct={direct}, search={search}."


def line_geo(cur, ts_s, ts_e) -> str:
    cur.execute("""
        SELECT country, MIN(ts) FROM page_views WHERE country IS NOT NULL
        GROUP BY country HAVING MIN(ts) >= %s AND MIN(ts) < %s
    """, (ts_s, ts_e))
    new_countries = [r[0] for r in cur.fetchall()]
    cur.execute("""
        SELECT region_code, MIN(ts) FROM page_views
        WHERE country='US' AND region_code LIKE 'US-%%'
        GROUP BY region_code HAVING MIN(ts) >= %s AND MIN(ts) < %s
    """, (ts_s, ts_e))
    new_states = [r[0].replace('US-', '') for r in cur.fetchall()]
    cur.execute("""
        SELECT COUNT(*) FROM (
          SELECT country, region_code FROM page_views
          WHERE country IS NOT NULL AND country != 'US' AND region_code IS NOT NULL
          GROUP BY country, region_code
          HAVING MIN(ts) >= %s AND MIN(ts) < %s
        ) x
    """, (ts_s, ts_e))
    n_regions = cur.fetchone()[0]
    parts = []
    parts.append(f"{len(new_countries)} new countries" +
                 (f" ({','.join(new_countries)})" if new_countries else ""))
    parts.append(f"{len(new_states)} new US states" +
                 (f" ({','.join(new_states)})" if new_states else ""))
    parts.append(f"{n_regions} non-US regions")
    return f"2. Geo: {', '.join(parts)}."


def line_ai_crawlers(cur, ts_s, ts_e) -> str:
    """Line 3 reads from ai_crawler_sitewide_daily_rollup (Charlie ruling
    2026-09-23 Wed 09:19 ET: same source as /observatory). Prior version
    read from ai_crawler_hits — that stream only sees the 9 agent-tier
    files, so daily reports understated real crawler activity by 10-100×.

    Format: AI answer engines detail on the numbered line; training +
    search-seo + scraper buckets on a wrapped sub-line under it.
    Bucket assignment is the module's own CRAWLER_BUCKETS map so
    /observatory and this report can't drift.
    """
    import datetime as _dt
    import agent_tier_rate_limit as _atrl

    y = _dt.datetime.fromtimestamp(ts_s, tz=_dt.timezone.utc).date().isoformat()
    cur.execute(
        "SELECT ua_class, SUM(hits)::bigint "
        "FROM ai_crawler_sitewide_daily_rollup "
        "WHERE as_of_date = %s::date "
        "GROUP BY ua_class",
        (y,),
    )
    rows = cur.fetchall()  # [(ua_class, hits), ...]

    # Bucket every row via the shared classifier. UNLISTED is not in a
    # bucket — surface it as its own aggregate count.
    from collections import defaultdict
    per_bucket = defaultdict(list)  # bucket -> [(ua_class, hits), ...]
    unlisted_hits = 0
    for uac, hits in rows:
        if not uac:
            continue
        if uac.upper() == "UNLISTED":
            unlisted_hits += int(hits)
            continue
        per_bucket[_atrl.bucket_for(uac)].append((uac, int(hits)))

    def _sort(lst):
        return sorted(lst, key=lambda kv: -kv[1])

    ai_answer = _sort(per_bucket.get("ai-answer") or [])
    training = _sort(per_bucket.get("training") or [])
    search_seo = _sort(per_bucket.get("search-seo") or [])
    scraper = _sort(per_bucket.get("scraper") or [])
    other = _sort(per_bucket.get("other") or [])

    ai_total = sum(h for _u, h in ai_answer)
    tr_total = sum(h for _u, h in training)
    ss_total = sum(h for _u, h in search_seo)
    sc_total = sum(h for _u, h in scraper)

    # Line 3 (numbered): AI answer engines detail. Cap at top-6 to fit
    # the phone-report format; overflow rolls into an "+N more" suffix.
    TOP_N = 6
    top_ai = ai_answer[:TOP_N]
    ai_parts = [f"{u}={h}" for u, h in top_ai]
    ai_overflow = len(ai_answer) - TOP_N
    ai_suffix = f", +{ai_overflow} more" if ai_overflow > 0 else ""
    line3 = (
        f"3. AI answer engines ({ai_total} hits / {len(ai_answer)} classes): "
        f"{', '.join(ai_parts)}{ai_suffix}."
    )

    # Sub-line: other buckets. Compact per-bucket totals + top-3 members.
    def _bucket_bit(label, total, members):
        if not members:
            return f"{label}=0"
        top3 = ", ".join(f"{u}:{h}" for u, h in members[:3])
        rest = len(members) - 3
        rest_suffix = f",+{rest}" if rest > 0 else ""
        return f"{label}={total} ({top3}{rest_suffix})"

    sub_bits = [
        _bucket_bit("training", tr_total, training),
        _bucket_bit("search-seo", ss_total, search_seo),
        _bucket_bit("scrapers", sc_total, scraper),
    ]
    if other:
        sub_bits.append(_bucket_bit("other", sum(h for _u, h in other), other))
    sub_line = "   " + " · ".join(sub_bits) + f" · UNLISTED={unlisted_hits}"

    return f"{line3}\n{sub_line}"


def line_signed_surfaces(cur, ts_s, ts_e) -> str:
    cur.execute(f"""
        SELECT COUNT(*) FROM page_views
        WHERE ts >= %s AND ts < %s AND (path='/check.json' OR path LIKE '/check.json?%%')
    """, (ts_s, ts_e))
    check_calls = cur.fetchone()[0]
    # External (non-JJ, non-canary) /check.json calls. Charlie 2026-09-14:
    # this is the number that flips the signed-data story. Zero is fine —
    # we want to see the day it isn't. Exclusion list: every UA prefix
    # our infrastructure emits. Deliberate false-positive on curl/%,
    # Python-urllib/%, Werkzeug/% (real external clients use these too)
    # so a rise is a signal Charlie can investigate.
    #
    # Charlie ruling 2026-09-21: count DELIVERED SIGNED ENVELOPES only.
    # A 429 (fleet-block) or 4xx (bad request) never made it to the sign
    # step, so it isn't a consumer of signed data. Adding `status = 200`
    # to the filter. Yesterday's report showed "1 external" — that was
    # an AionBot 429; today it correctly reads 0.
    cur.execute(f"""
        SELECT COUNT(*) FROM page_views
        WHERE ts >= %s AND ts < %s
          AND (path='/check.json' OR path LIKE '/check.json?%%')
          AND status = 200
          AND user_agent NOT LIKE 'xrpldashboard-%%'
          AND user_agent NOT LIKE 'public-route-canary%%'
          AND user_agent NOT LIKE 'station-audit%%'
          AND user_agent NOT LIKE 'PROOF-%%'
          AND user_agent NOT LIKE 'Werkzeug/%%'
          AND user_agent NOT LIKE 'Python-urllib/%%'
          AND user_agent NOT LIKE 'curl/%%'
          AND user_agent NOT IN ('audit', 'test')
          AND user_agent IS NOT NULL
    """, (ts_s, ts_e))
    check_external = cur.fetchone()[0]
    cur.execute(f"""
        SELECT COUNT(*) FROM page_views
        WHERE ts >= %s AND ts < %s AND path LIKE '/thisweek%%'
    """, (ts_s, ts_e))
    thisweek_hits = cur.fetchone()[0]
    cur.execute(f"""
        SELECT COUNT(*) FROM page_views
        WHERE ts >= %s AND ts < %s AND path LIKE '/.well-known/registry/%%'
    """, (ts_s, ts_e))
    registry_hits = cur.fetchone()[0]
    # Signed count: hard to know without parsing responses; approximate
    # via sig-service audit log for the day.
    signed = "?"
    try:
        import glob
        yday = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
        audit = f"/Users/charliebruce/xrpl_test/launchd_logs/sig_service/{yday}.jsonl"
        if os.path.exists(audit):
            with open(audit) as f:
                lines = [json.loads(l) for l in f if l.strip()]
            signed = sum(1 for l in lines if l.get('event') == 'signed' and l.get('kind') == 'check')
    except Exception:
        pass
    return (
        f"4. Signed surfaces: /check.json {check_calls} calls (signed:{signed}), "
        f"/thisweek {thisweek_hits} hits, /.well-known/registry {registry_hits}. "
        f"/check.json external (non-JJ, non-canary) calls: {check_external}."
    )


def line_chain_health(cur, ts_s_yday, ts_e_yday) -> str:
    # Snapshot verify (yesterday's date)
    yday = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    yday_str = yday.strftime('%Y-%m-%d')
    snap_verify = "?"
    try:
        r = subprocess.run(
            ["/Users/charliebruce/xrpl_test/venv/bin/python3", "signed_snapshot.py", "--verify", yday_str],
            capture_output=True, text=True, timeout=30,
            cwd="/Users/charliebruce/xrpl_test",
        )
        snap_verify = "✓" if "VERIFIED" in r.stdout else "✗"
    except Exception:
        pass
    # Registry verify (file exists + fingerprint match check)
    reg_verify = "?"
    reg_path = f"/Users/charliebruce/xrpl_test/signed_registry_snapshots/{yday_str}.json"
    if os.path.exists(reg_path):
        try:
            with open(reg_path) as f:
                env = json.load(f)
            if env.get('signature', {}).get('signing_key_fingerprint') == 'A4:0F:B1:0A:9D:33:64:03':
                reg_verify = "✓"
        except Exception:
            reg_verify = "?"
    # Receipt verify — sample /check.json call, confirm the response
    # carries an ed25519 signature block with a non-empty sig field.
    # 2026-09-12: prior code looked for 'sig_status' == 'signed', a
    # field that doesn't exist in the response schema — it never
    # resolved to ✓ and, on any exception, stayed at "?". Charlie
    # ruling: receipt check must resolve ✓ or ✗, never "?". Any
    # failure path now downgrades to ✗ with the reason captured.
    receipt_verify = "✗"
    receipt_reason = ""
    try:
        import urllib.request, json as _json, ssl, certifi
        # 2026-09-12: same certifi-backed SSL context the two-way-toml
        # walker uses. Prior httpx-based code raised SSL cert-verify
        # errors on the launchd wrapper and the exception silently
        # left receipt_verify at "?".
        _ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(
            "https://xrpldashboard.com/check.json?q=rrrrrrrrrrrrrrrrrrrrrhoLvTp",
            headers={"User-Agent": "xrpldashboard-standing-orders/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15, context=_ctx) as resp:
            code = resp.status
            body = resp.read()
        if code != 200:
            receipt_reason =f"http_{code}"
        else:
            parsed = _json.loads(body)
            sig = parsed.get('proof', {}).get('check_v09_signature', {})
            if sig.get('sig_ed25519') and sig.get('canonical_hash_sha256'):
                receipt_verify = "✓"
            else:
                receipt_reason ="no_sig_ed25519"
    except Exception as e:
        receipt_reason =f"{type(e).__name__}"
    # Stream 24h restarts + watchdog subset from walker_health msg
    stream_msg = "?"
    try:
        cur.execute("SELECT last_run_message FROM walker_health WHERE walker_name = 'xrpl_stream_restart_rate'")
        r = cur.fetchone()
        if r and r[0]:
            # Extract 24h= and watchdog= from message
            msg = r[0]
            import re
            m24 = re.search(r'24h=(\d+) restarts \(watchdog=(\d+)', msg)
            if m24:
                stream_msg = f"restarts={m24.group(1)} (wd={m24.group(2)})"
    except Exception:
        pass
    receipt_suffix = f" ({receipt_reason})" if (receipt_verify == "✗" and receipt_reason) else ""
    return (f"5. Chain: snapshot {snap_verify}, registry {reg_verify}, "
            f"receipt {receipt_verify}{receipt_suffix}, stream 24h {stream_msg}.")


def line_5xx_summary(cur, yesterday) -> str:
    """Line 6 (Charlie ruling 2026-09-09): worst monitored route by 5xx%
    yesterday, floored at 1% via DAILY_SUMMARY_PCT in the walker module."""
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import route_5xx_rate_walker
        return "6. " + route_5xx_rate_walker.daily_summary_line(cur, yesterday)
    except Exception as e:  # noqa: BLE001
        return f"6. 5xx: (walker query failed — {type(e).__name__})"


def main() -> int:
    ts_s, ts_e, ymd = _yesterday_utc_window()
    import datetime as dt
    y = dt.date.fromisoformat(ymd)
    with db.pg_connect() as conn:
        with conn.cursor() as cur:
            report = "\n".join([
                line_referrers(cur, ts_s, ts_e),
                line_geo(cur, ts_s, ts_e),
                line_ai_crawlers(cur, ts_s, ts_e),
                line_signed_surfaces(cur, ts_s, ts_e),
                line_chain_health(cur, ts_s, ts_e),
                line_5xx_summary(cur, y),
            ])
    print(f"# Standing-orders daily report — window {ymd} UTC (full day)")
    print(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

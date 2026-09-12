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
          SUM(CASE WHEN referrer ~* 'twitter|x\\.com' THEN 1 ELSE 0 END) x,
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
    # AI-citation crawlers — classified into ai_crawler_hits by
    # classify_ai_crawler (agent_tier_rate_limit.py:207) against
    # AI_CRAWLER_UA_SUBSTRINGS. bingbot/amazonbot/googlebot are NOT in
    # that list (search-index legacy, not AI-citation), so they never
    # land in ai_crawler_hits — they get counted separately below from
    # page_views raw UA per Charlie's Option 2 ruling (2026-09-10,
    # escalation #17729): keep AI-citation semantics clean but still
    # surface search-crawler volume in the same report line.
    AI_UAS = ["chatgpt-user", "claudebot", "oai-searchbot", "gptbot",
              "perplexitybot", "google-extended"]
    cur.execute(f"""
        SELECT lower(ua_class) AS uac, COUNT(*) FROM ai_crawler_hits
        WHERE ts >= %s AND ts < %s AND lower(ua_class) = ANY(%s)
        GROUP BY 1
    """, (ts_s, ts_e, AI_UAS))
    ai_counts = {r[0]: r[1] for r in cur.fetchall()}
    ai_parts = [f"{ua}={ai_counts.get(ua, 0)}" for ua in AI_UAS]

    # Traditional search crawlers — count from page_views raw user_agent.
    # These aren't AI-citation, so they don't earn a spot in ai_crawler_hits;
    # but they're the loudest bots on the site and worth surfacing.
    SEARCH_PATTERNS = [("bingbot", "%bingbot%"),
                       ("amazonbot", "%amazonbot%"),
                       ("googlebot", "%googlebot%")]
    search_parts = []
    for name, pat in SEARCH_PATTERNS:
        cur.execute(
            "SELECT COUNT(*) FROM page_views "
            "WHERE ts >= %s AND ts < %s AND user_agent ILIKE %s",
            (ts_s, ts_e, pat),
        )
        search_parts.append(f"{name}={cur.fetchone()[0]}")

    return (f"3. AI crawlers: {', '.join(ai_parts)}. "
            f"Search: {', '.join(search_parts)}.")


def line_signed_surfaces(cur, ts_s, ts_e) -> str:
    cur.execute(f"""
        SELECT COUNT(*) FROM page_views
        WHERE ts >= %s AND ts < %s AND (path='/check.json' OR path LIKE '/check.json?%%')
    """, (ts_s, ts_e))
    check_calls = cur.fetchone()[0]
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
    return f"4. Signed surfaces: /check.json {check_calls} calls (signed:{signed}), /thisweek {thisweek_hits} hits, /.well-known/registry {registry_hits}."


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

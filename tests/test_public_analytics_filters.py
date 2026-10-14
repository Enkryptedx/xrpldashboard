"""ONE-definition lock for the self-probe exclusion (Charlie ruling
2026-09-25 18:39 ET): /health and every monitor/canary path are part of
the same self-probe definition as the UA fragments, and that definition
is what weekly_analytics.py, every db.py public-analytics reader, the
ingest-time stamp in app.py, and the is_bot_writer's pattern lists all use.

Hermetic: no DB, no network. pytest-collected; also runs standalone:
    ./venv/bin/python tests/test_public_analytics_filters.py
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import public_analytics_filters as paf
import db


def _load_weekly():
    spec = importlib.util.spec_from_file_location(
        "weekly_analytics", os.path.join(ROOT, "scripts", "weekly_analytics.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"


def test_predicate_flags_monitor_paths_regardless_of_ua():
    assert paf.is_self_probe(_BROWSER, "/health")
    assert paf.is_self_probe(_BROWSER, "/healthz")
    assert paf.is_self_probe(_BROWSER, "/health/deep")
    assert paf.is_self_probe(_BROWSER, "/api/health")
    assert paf.is_self_probe(None, "/health")


def test_predicate_flags_canary_uas_on_any_path():
    assert paf.is_self_probe("xrpldashboard-public-route-canary/1.0", "/methodology")
    assert paf.is_self_probe("BetterStack Uptime/1.0", "/whales")
    assert paf.is_self_probe("JJ-shell curl", "/")


def test_predicate_leaves_real_readers_alone():
    assert not paf.is_self_probe(_BROWSER, "/whales")
    assert not paf.is_self_probe(_BROWSER, "/")
    assert not paf.is_self_probe(_BROWSER, "/healthcheck-notes")   # not a monitor path
    assert not paf.is_self_probe(_BROWSER, "/pools?tier=500")


def test_sql_clauses_carry_the_path_half():
    for fn in (paf.sql_not_bot_ua_clause, paf.sql_not_self_probe_ua_clause):
        c = fn("p")
        assert "p.path NOT IN ('/health', '/healthz')" in c, c
        assert "p.path NOT LIKE '/health/%'" in c
        assert "p.path NOT LIKE '/api/health%'" in c
        assert "NOT ILIKE '%xrpldashboard-%'" in c
        e = fn("p", psycopg_escape=True)
        assert "'/health/%%'" in e and "%%xrpldashboard-%%" in e
        assert "'/health/%'" not in e.replace("%%", "")  # every % doubled


def test_weekly_analytics_uses_the_shared_clause():
    wa = _load_weekly()
    assert wa._sql_not_bot_ua_clause("p") == paf.sql_not_bot_ua_clause("p")


def test_is_bot_writer_pattern_lists_fold_the_definition():
    for frag in paf.SELF_PROBE_UA_FRAGMENTS:
        assert f"%{frag}%" in db.BOT_UA_PATTERNS, frag
    for p in paf.SELF_PROBE_PATHS:
        assert p in db.BOT_PATH_PATTERNS, p
    for pre in paf.SELF_PROBE_PATH_PREFIXES:
        assert f"{pre}%" in db.BOT_PATH_PATTERNS, pre


def test_ingest_stamps_is_bot_via_shared_predicate():
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    assert "from public_analytics_filters import is_self_probe as _is_self_probe" in src
    assert "is_bot=True if _self_probe else None" in src
    import inspect
    assert "is_bot" in inspect.signature(db.log_page_view).parameters


# ── Non-browser UAs counted as HUMAN until 2026-10-14 ───────────────────
# Both were passing `is_bot IS NULL AND sql_not_bot_ua_clause()` and landing
# in the human pageview total. They are bot-shaped: a bare name/version with
# no "Mozilla/" prefix, one path each, no referrer chain.
#   private-asset-ops/0.1 - third-party scraper of /amendments. 99 hits, 27
#     distinct visitor hashes, KR only, every response 200, Oct 8-12.
#     Confirmed NOT ours by five independent greps (tracked repo, repo code,
#     private infra tree, workspace, launchd) against a control that finds 8
#     files for "public-route-canary".
#   loadtest - our own load testing. 4 visitor hashes, ~139 hits, all on
#     /amendments, Oct 8, no country.
OBSERVED_BOT_UAS = (
    "private-asset-ops/0.1",
    "loadtest",
)


def test_new_bot_fragments_are_declared():
    for frag in ("private-asset-ops", "loadtest"):
        assert frag in paf.KNOWN_BOT_UA_FRAGMENTS, (
            f"{frag!r} missing from KNOWN_BOT_UA_FRAGMENTS"
        )


def test_observed_bot_uas_are_excluded_by_the_shared_clause():
    """The real UA strings must be removed by the one human definition.

    Asserted against the rendered clause rather than a hand-written LIKE, so
    this breaks if the clause stops covering KNOWN_BOT_UA_FRAGMENTS.
    """
    clause = paf.sql_not_bot_ua_clause("p")
    for frag in ("private-asset-ops", "loadtest"):
        assert f"NOT ILIKE '%{frag}%'" in clause, (
            f"clause does not exclude {frag!r}"
        )


def test_observed_bot_uas_match_their_fragment_case_insensitively():
    """ILIKE is case-insensitive; the fragments must match as substrings of
    the UA strings actually seen in page_views."""
    for ua in OBSERVED_BOT_UAS:
        hit = [f for f in paf.KNOWN_BOT_UA_FRAGMENTS
               if f.lower() in ua.lower()]
        assert hit, f"no KNOWN_BOT_UA_FRAGMENTS entry matches {ua!r}"


def test_new_fragments_do_not_catch_real_browser_uas():
    """Guard against the obvious false positive. These are the dominant real
    reader UAs in page_views; none may contain the new fragments."""
    real = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148",
        "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like "
        "Gecko) Chrome/143.0.0.0 Mobile Safari/537.36",
    )
    for ua in real:
        for frag in ("private-asset-ops", "loadtest"):
            assert frag.lower() not in ua.lower(), (
                f"{frag!r} would wrongly exclude real reader UA {ua!r}"
            )


def test_loadtest_substring_risk_is_acknowledged():
    """`loadtest` matches as a case-insensitive SUBSTRING, so a UA like
    "DownloadTester" would also be excluded. No such UA has ever appeared,
    and the trade is documented at the fragment -- this test pins the
    REASONING so a future reader does not treat the collision as unnoticed.
    """
    assert "loadtest" in "DownloadTester".lower(), (
        "premise wrong: recheck the collision note on the loadtest fragment"
    )
    src = open(os.path.join(ROOT, "public_analytics_filters.py")).read()
    assert "DownloadTester" in src, (
        "the loadtest substring collision must stay documented in the module"
    )


TESTS = [
    ("new_bot_fragments_are_declared", test_new_bot_fragments_are_declared),
    ("observed_bot_uas_are_excluded_by_the_shared_clause",
     test_observed_bot_uas_are_excluded_by_the_shared_clause),
    ("observed_bot_uas_match_their_fragment_case_insensitively",
     test_observed_bot_uas_match_their_fragment_case_insensitively),
    ("new_fragments_do_not_catch_real_browser_uas",
     test_new_fragments_do_not_catch_real_browser_uas),
    ("loadtest_substring_risk_is_acknowledged",
     test_loadtest_substring_risk_is_acknowledged),
    ("predicate_flags_monitor_paths_regardless_of_ua", test_predicate_flags_monitor_paths_regardless_of_ua),
    ("predicate_flags_canary_uas_on_any_path", test_predicate_flags_canary_uas_on_any_path),
    ("predicate_leaves_real_readers_alone", test_predicate_leaves_real_readers_alone),
    ("sql_clauses_carry_the_path_half", test_sql_clauses_carry_the_path_half),
    ("weekly_analytics_uses_the_shared_clause", test_weekly_analytics_uses_the_shared_clause),
    ("is_bot_writer_pattern_lists_fold_the_definition", test_is_bot_writer_pattern_lists_fold_the_definition),
    ("ingest_stamps_is_bot_via_shared_predicate", test_ingest_stamps_is_bot_via_shared_predicate),
]


def main():
    ok = bad = 0
    for name, fn in TESTS:
        try:
            fn(); print(f"  PASS {name}"); ok += 1
        except AssertionError as e:
            print(f"  FAIL {name}: {e}"); bad += 1
    print(f"\n== {ok} PASS / {bad} FAIL ==")
    return bad


if __name__ == "__main__":
    sys.exit(main())

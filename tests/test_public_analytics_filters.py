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


TESTS = [
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

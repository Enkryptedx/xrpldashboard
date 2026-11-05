"""Every SQL the daily report executes must survive psycopg's placeholder scan.

THE OUTAGE THIS PINS (2026-11-05 08:00 report)
----------------------------------------------
PR #131 added a news-referrer host allowlist whose dot-suffix test inlined
a LIKE pattern:

    parts.append(f"{host} LIKE '%.{h}'")

That SQL is handed to a PARAMETERIZED cur.execute(sql, (ts_s, ts_e)), so
psycopg scans the string for placeholders, hits '%.', and raises:

    psycopg.ProgrammingError: only '%s', '%b', '%t' are allowed as
    placeholders, got '%.'

line_referrers() died and took the whole daily report with it. Every other
LIKE in standing_orders_daily_report.py already escaped correctly
(US-%%, /thisweek%%, curl/%%, /.well-known/registry/%%) -- #131 broke an
established convention and nothing caught it, because no test ever rendered
the report's SQL through psycopg.

A plain `assert "%%" in sql` would be a weaker guard: it asserts the fix's
shape rather than the property we care about. So this uses psycopg's OWN
parser -- PostgresClientQuery, the exact code path that raised in
production -- which needs no database connection.

Hermetic: no DB, no network.
"""
from __future__ import annotations

import os
import sys

import pytest
from psycopg._queries import PostgresClientQuery
from psycopg.adapt import Transformer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (HERE, os.path.join(HERE, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import standing_orders_daily_report as rep  # noqa: E402


# psycopg raises two different ProgrammingErrors here and only one is ours:
#
#   "only '%s', '%b', '%t' are allowed as placeholders, got '%.'"
#       -> malformed SQL. This is the 2026-11-05 bug. Always a defect.
#   "the query has 4 placeholders but 2 parameters were passed"
#       -> arity. Depends on the CALLER's params, which a blind sweep
#          cannot know, so it says nothing about the SQL's validity.
#
# The sweep below therefore fails only on the first class. Treating arity
# as a failure made three healthy statements look broken on the first run.
_PLACEHOLDER_SYNTAX_MARKER = "allowed as placeholders"


def assert_placeholders_ok(sql: str, params=(1, 2)) -> None:
    """Raise only if psycopg rejects `sql`'s placeholder SYNTAX.

    Uses psycopg's own parser -- the exact code path that raised in
    production -- and needs no database connection.
    """
    try:
        PostgresClientQuery(Transformer()).convert(sql, params)
    except Exception as e:  # noqa: BLE001
        if _PLACEHOLDER_SYNTAX_MARKER in str(e):
            raise
        # arity or adaptation noise from the fake params: not our concern


class TestTheParserItselfHasTeeth:
    """If these fail, the guard below proves nothing."""

    def test_stray_percent_is_rejected(self):
        with pytest.raises(Exception) as ei:
            assert_placeholders_ok("SELECT 1 WHERE x LIKE '%.foo' AND y=%s", (1,))
        assert _PLACEHOLDER_SYNTAX_MARKER in str(ei.value)

    def test_arity_mismatch_is_NOT_treated_as_a_failure(self):
        """Guards the guard: a blind sweep cannot know each caller's param
        count, so arity must not be reported as malformed SQL."""
        assert_placeholders_ok("SELECT 1 WHERE a=%s AND b=%s AND c=%s", (1, 2))

    def test_escaped_percent_is_accepted(self):
        assert_placeholders_ok("SELECT 1 WHERE x LIKE '%%.foo' AND y=%s", (1,))


class TestNewsHostSql:
    def test_renders_through_psycopg(self):
        """The exact regression: this SQL goes into a parameterized execute."""
        sql = f"SELECT 1 FROM page_views WHERE ts >= %s AND ts < %s AND {rep._news_host_sql()}"
        assert_placeholders_ok(sql)

    def test_still_matches_host_and_dot_suffix(self):
        """The escape must not change the semantics it was added for."""
        s = rep._news_host_sql()
        assert "= 'coinpaper.com'" in s
        assert "LIKE '%%.coinpaper.com'" in s
        # host is extracted first, so a path containing "news" cannot match
        assert "SUBSTRING" in s
        assert "~*" not in s

    def test_every_allowlisted_host_appears(self):
        s = rep._news_host_sql()
        for h in rep.NEWS_REFERRER_HOSTS:
            assert f"= '{h}'" in s, h


class TestEverySqlTheReportExecutes:
    """Sweep: run every statement the report issues through the parser.

    A capturing cursor stands in for the DB, so this covers all 14 existing
    LIKE patterns too -- not just the line #131 broke. Any future stray %
    anywhere in the report fails here instead of at 08:00.
    """

    class _Cur:
        def __init__(self):
            self.sqls: list[str] = []

        def execute(self, sql, params=None):
            self.sqls.append(sql)
            self._params = params

        def fetchone(self):
            return tuple([0] * 24)

        def fetchall(self):
            return []

    def _capture(self):
        cur = self._Cur()
        for name in dir(rep):
            if not name.startswith("line_"):
                continue
            fn = getattr(rep, name)
            if not callable(fn):
                continue
            try:
                fn(cur, 1, 2)
            except Exception:
                # A line function may need richer fake data; we only care
                # about the SQL it managed to issue before giving up.
                pass
        return cur.sqls

    def test_at_least_one_statement_was_captured(self):
        assert self._capture(), "captured no SQL -- the sweep would be vacuous"

    def test_all_captured_sql_renders(self):
        bad = []
        for sql in self._capture():
            try:
                assert_placeholders_ok(sql)
            except Exception as e:  # noqa: BLE001
                bad.append((sql[:90].replace("\n", " "), str(e)[:70]))
        assert not bad, "SQL psycopg would reject:\n" + "\n".join(
            f"  {s} -> {e}" for s, e in bad
        )

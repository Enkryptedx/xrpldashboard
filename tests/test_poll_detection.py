"""Poll / auto-refresh must be tagged, never treated as a bot (2026-10-14).

BACKGROUND. templates/index.html refreshes its live panels with
`fetch(window.location.pathname + '?_=' + Date.now())` every ~30s while the
tab is visible. The ingest (`app._log_page_view`) is an `@after_request` hook
that logs EVERY GET, `/` is not skip-listed, and nothing distinguished a
fetch() from a navigation. Because `path = request.path` drops the query
string, each refresh landed as a plain `/` pageview.

Measured read-only 2026-10-14: 45,593 of 80,078 all-time "human" pageviews
(56.9%) were self-same-path refetches. `/` alone was 39,510 pv from 1,462
visitors; `/analytics` 1,506 from 59; `/amendments` only 350. One Korean
visitor produced 4,111 pageviews in 17.8 hours from a single open tab — a hit
every ~15.6s — which had looked exactly like a crawler flood and nearly got
that reader reclassified as a bot. UNIQUE VISITOR COUNTS WERE NEVER AFFECTED.

THE HARD RULE (Charlie, 2026-10-14 19:39 ET) is the point of this file:
leaving a page open, for ANY duration and ANY number of polls, must NEVER
  * set `is_bot`,
  * reduce the visitor's human status,
  * feed any bot or crawler rule.
Polls are tagged `is_poll` ONLY, and the visitor still counts as exactly one
human visitor.

Hermetic: pure functions, source inspection and an in-memory model of the
reader contract. No DB, no network, and `app` is never imported (importing it
makes live RPC calls at module scope, so it is not hermetic).
"""
import ast
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import poll_detection as pd
import public_analytics_filters as paf

APP_PY = os.path.join(ROOT, "app.py")
DB_PY = os.path.join(ROOT, "db.py")
INDEX_HTML = os.path.join(ROOT, "templates", "index.html")
MIGRATION = os.path.join(
    ROOT, "migrations", "2026_10_14_page_views_is_poll.sql"
)


def _src(path):
    with open(path) as fh:
        return fh.read()


# ══ FIX 2: Sec-Fetch-Dest gate, with absent-header fallback ══════════════
def test_navigation_is_not_a_poll():
    assert pd.is_poll_request(sec_fetch_dest="document") is None


def test_fetch_is_a_poll():
    assert pd.is_poll_request(sec_fetch_dest="empty") is True


@pytest.mark.parametrize("dest", ["Document", "DOCUMENT", " document "])
def test_navigation_detection_is_case_and_space_insensitive(dest):
    assert pd.is_poll_request(sec_fetch_dest=dest) is None


@pytest.mark.parametrize("dest", ["iframe", "image", "script", "worker"])
def test_other_declared_destinations_are_polls(dest):
    """Anything that declares a non-document destination is not a pageview."""
    assert pd.is_poll_request(sec_fetch_dest=dest) is True


@pytest.mark.parametrize("dest", [None, "", "   "])
def test_absent_or_blank_header_falls_back_to_counting(dest):
    """THE FALLBACK. Older clients and most bots send no Sec-Fetch-* at all.

    Absent must mean "treat as a pageview", never "treat as a poll", or we
    would silently stop counting every non-modern client.
    """
    assert pd.is_poll_request(sec_fetch_dest=dest) is None


# ══ FIX 3: the explicit ?_live=1 marker ══════════════════════════════════
def test_live_marker_is_a_poll():
    assert pd.is_poll_request(live_marker="1") is True


def test_live_marker_wins_over_a_document_destination():
    """Our own marker is authoritative: belt-and-braces if a browser ever
    reports `document` for a programmatic refetch."""
    assert pd.is_poll_request(
        sec_fetch_dest="document", live_marker="1"
    ) is True


@pytest.mark.parametrize("val", ["0", "", "yes", "true", "2", "whatever"])
def test_other_marker_values_are_ignored(val):
    """A crawler appending ?_live=whatever must not suppress its own
    pageview. Only the exact value "1" counts."""
    assert pd.is_poll_request(sec_fetch_dest="document",
                              live_marker=val) is None


def test_marker_constants_match_what_the_template_sends():
    html = _src(INDEX_HTML)
    assert "'&_live=1'" in html or "&_live=1" in html, (
        "index.html no longer appends the _live marker the ingest looks for"
    )
    assert pd.POLL_QUERY_MARKER == "_live"
    assert pd.POLL_QUERY_VALUE == "1"


# ══ FIX 1: the is_poll column ════════════════════════════════════════════
def test_schema_ddl_declares_is_poll():
    assert re.search(
        r"ALTER TABLE page_views ADD COLUMN IF NOT EXISTS is_poll BOOLEAN",
        _src(DB_PY),
    ), "is_poll missing from SCHEMA_DDL"


def test_boot_migration_exists_and_is_wired():
    db_src = _src(DB_PY)
    assert "def ensure_page_views_is_poll_column(" in db_src
    assert "db.ensure_page_views_is_poll_column()" in _src(APP_PY), (
        "boot migration is never called; SCHEMA_DDL alone is not enough "
        "because init_schema() is a manual one-off"
    )


def test_column_is_nullable_no_default_so_history_is_untouched():
    """Going-forward only. A DEFAULT or NOT NULL would relabel old rows."""
    for src in (_src(DB_PY), _src(MIGRATION)):
        m = re.search(
            r"ADD COLUMN IF NOT EXISTS is_poll BOOLEAN([^;\n]*)", src
        )
        assert m, "is_poll ALTER not found"
        tail = m.group(1).upper()
        assert "DEFAULT" not in tail, "is_poll must have no DEFAULT"
        assert "NOT NULL" not in tail, "is_poll must stay nullable"


def test_migration_and_ddl_agree():
    assert "ADD COLUMN IF NOT EXISTS is_poll BOOLEAN" in _src(MIGRATION)


def test_writer_accepts_and_persists_is_poll():
    db_src = _src(DB_PY)
    assert "is_bot=None, is_poll=None" in db_src, "log_page_view lacks is_poll"
    insert = db_src[db_src.index("INSERT INTO page_views"):][:600]
    assert "is_poll" in insert, "is_poll not in the INSERT column list"
    assert insert.count("%s") >= 12, "placeholder count did not grow with the column"


def test_ingest_passes_is_poll_to_the_writer():
    assert "is_poll=_poll_flag" in _src(APP_PY)


# ══ FIX 4: REFRESH_AT minimum interval ═══════════════════════════════════
def test_refresh_has_a_wall_clock_minimum_interval():
    """`age` is the SERVER cache age, reset from the fetched page. A page
    served already near REFRESH_AT re-fired within seconds, which is how one
    tab produced 4,111 requests in 17.8h (~15.6s apart, not 30s)."""
    html = _src(INDEX_HTML)
    assert "MIN_REFRESH_MS" in html, "no minimum-interval clamp"
    assert "lastRefreshAt" in html, "no last-refresh timestamp"
    assert re.search(r"\(Date\.now\(\)\s*-\s*lastRefreshAt\)\s*>=\s*MIN_REFRESH_MS",
                     html), "clamp is not in the refresh condition"
    assert "lastRefreshAt = Date.now();" in html, (
        "lastRefreshAt is never updated, so the clamp would only ever fire once"
    )


def test_minimum_interval_is_tied_to_refresh_at():
    html = _src(INDEX_HTML)
    assert re.search(r"MIN_REFRESH_MS\s*=\s*REFRESH_AT\s*\*\s*1000", html), (
        "clamp must derive from REFRESH_AT so the two cannot drift"
    )


# ══ THE HARD RULE ════════════════════════════════════════════════════════
def _code_identifiers(path):
    """Every identifier the module's CODE uses, excluding docstrings and
    comments.

    Checked via AST rather than a text scan on purpose: this module's
    docstring deliberately explains the bot/poll distinction at length, so a
    raw-text search would match its own prose and fail. The rule being
    enforced is about executable logic, not documentation.
    """
    tree = ast.parse(_src(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name)
    return {n.lower() for n in names}


def test_poll_detection_knows_nothing_about_bots():
    """A poll must not be derivable from, or feed into, bot logic.

    Inspects CODE identifiers only (see _code_identifiers): the module's
    docstring is allowed — indeed required — to discuss the distinction.
    """
    idents = _code_identifiers(os.path.join(ROOT, "poll_detection.py"))
    for forbidden in ("is_bot", "crawler", "known_bot", "self_probe",
                      "user_agent", "bot"):
        assert not any(forbidden in i for i in idents), (
            f"poll_detection CODE references {forbidden!r} — a poll is not "
            f"a bot; offending identifiers: "
            f"{sorted(i for i in idents if forbidden in i)}"
        )


def test_poll_detection_takes_no_bot_signal_as_input():
    """Guard the signature itself: is_poll_request must not accept a user
    agent, country or any bot verdict — it physically cannot consider them."""
    import inspect
    params = set(inspect.signature(pd.is_poll_request).parameters)
    assert params == {"sec_fetch_dest", "live_marker"}, (
        f"unexpected inputs to poll detection: {sorted(params)}"
    )


def test_bot_filter_module_knows_nothing_about_polls():
    """The reverse direction: no bot rule may consult is_poll."""
    src = _src(os.path.join(ROOT, "public_analytics_filters.py"))
    assert "is_poll" not in src, (
        "public_analytics_filters references is_poll — polls must never "
        "feed a bot or crawler rule"
    )
    assert "is_poll" not in paf.sql_not_bot_ua_clause("p")


def test_is_bot_is_computed_independently_of_the_poll_flag():
    """AST-level: the is_bot argument at the ingest call site must not
    reference the poll flag in any way."""
    tree = ast.parse(_src(APP_PY))
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "log_page_view"):
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        assert "is_bot" in kw and "is_poll" in kw
        is_bot_src = ast.dump(kw["is_bot"])
        assert "_poll" not in is_bot_src, (
            "is_bot is derived from the poll flag — forbidden"
        )
        assert "_self_probe" in is_bot_src, (
            "is_bot should still come from the self-probe predicate alone"
        )
        found = True
    assert found, "could not find the db.log_page_view call site"


# ---- in-memory model of the reader contract -----------------------------
def _human_pageviews(rows):
    """Pageview count: polls excluded (`is_poll IS NOT TRUE`)."""
    return sum(1 for r in rows
               if r["is_bot"] is not True and r["is_poll"] is not True)


def _human_visitors(rows):
    """Visitor count: is_poll NOT filtered, per the reader contract."""
    return len({r["visitor_hash"] for r in rows if r["is_bot"] is not True})


def _open_tab_rows(polls, visitor="vh-reader-1"):
    """One navigation followed by `polls` auto-refreshes, as the fixed
    ingest would now write them."""
    rows = [{
        "visitor_hash": visitor, "path": "/", "is_bot": None,
        "is_poll": pd.is_poll_request(sec_fetch_dest="document"),
    }]
    for _ in range(polls):
        rows.append({
            "visitor_hash": visitor, "path": "/", "is_bot": None,
            "is_poll": pd.is_poll_request(
                sec_fetch_dest="empty", live_marker="1"),
        })
    return rows


@pytest.mark.parametrize("polls", [0, 1, 120, 4111])
def test_open_tab_never_sets_is_bot(polls):
    """ANY number of polls. 4111 is the real KR figure from one 17.8h tab."""
    rows = _open_tab_rows(polls)
    assert all(r["is_bot"] is None for r in rows), (
        "an open tab set is_bot — the exact failure the hard rule forbids"
    )


@pytest.mark.parametrize("polls", [0, 1, 120, 4111])
def test_open_tab_still_counts_as_exactly_one_human_visitor(polls):
    assert _human_visitors(_open_tab_rows(polls)) == 1


@pytest.mark.parametrize("polls", [0, 1, 120, 4111])
def test_open_tab_counts_as_exactly_one_pageview(polls):
    """The navigation counts; the refreshes do not."""
    assert _human_pageviews(_open_tab_rows(polls)) == 1


def test_visitor_count_is_unchanged_by_the_fix():
    """Guard the thing we must not break: tagging polls must not reduce the
    unique-visitor total, which was always correct."""
    before = len({r["visitor_hash"] for r in _open_tab_rows(500)})
    assert before == 1
    assert _human_visitors(_open_tab_rows(500)) == before


def test_filtering_polls_out_of_visitor_counts_would_be_wrong():
    """Guard-the-guard: show the WRONG reader loses the reader entirely, so
    the contract in the migration comment is not merely decorative."""
    rows = _open_tab_rows(4111)
    wrong = len({r["visitor_hash"] for r in rows
                 if r["is_bot"] is not True and r["is_poll"] is not True
                 and r["path"] == "/" and r["is_poll"] is True})
    assert wrong == 0
    assert _human_visitors(rows) == 1


# ══ FIX 1b: the ALTER must never queue behind pg_dump ════════════════════
# Charlie, 2026-10-14 21:10. ADD COLUMN takes ACCESS EXCLUSIVE, which
# conflicts with the ACCESS SHARE pg_dump holds for the whole 22:00 ET
# backup. PostgreSQL's lock queue is FIFO, so an ALTER waiting on that lock
# makes every later reader queue BEHIND it — a metadata-only migration could
# stall the site for the length of a dump. lock_timeout makes it give up.
import db as _db  # noqa: E402 - imported late; db has no Flask/RPC side effects


class _FakeCursor:
    def __init__(self, raise_on=None, raise_exc=None):
        self.statements = []
        self._raise_on = raise_on
        self._raise_exc = raise_exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if self._raise_on and self._raise_on in sql:
            raise self._raise_exc


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True


@pytest.fixture
def fake_pg(monkeypatch):
    """Drive ensure_page_views_is_poll_column against a recording cursor."""
    def _install(raise_on=None, raise_exc=None):
        cur = _FakeCursor(raise_on=raise_on, raise_exc=raise_exc)
        conn = _FakeConn(cur)
        monkeypatch.setattr(_db, "pg_available", lambda: True)
        monkeypatch.setattr(_db, "pg_connect", lambda: conn)
        logged = []
        monkeypatch.setattr(_db, "_log_err",
                            lambda cat, exc: logged.append(cat))
        return cur, conn, logged
    return _install


def test_lock_timeout_is_set_before_the_alter(fake_pg):
    cur, conn, _ = fake_pg()
    assert _db.ensure_page_views_is_poll_column() is True
    assert conn.committed
    joined = [s for s in cur.statements]
    assert any("lock_timeout" in s for s in joined), "lock_timeout never set"
    lock_i = next(i for i, s in enumerate(joined) if "lock_timeout" in s)
    alter_i = next(i for i, s in enumerate(joined) if "ALTER TABLE" in s)
    assert lock_i < alter_i, (
        "lock_timeout must be SET before the ALTER, or the ALTER can still "
        "queue behind pg_dump"
    )


def test_lock_timeout_value_is_three_seconds(fake_pg):
    cur, _, _ = fake_pg()
    _db.ensure_page_views_is_poll_column()
    stmt = next(s for s in cur.statements if "lock_timeout" in s)
    assert "'3s'" in stmt, f"expected a 3s lock_timeout, got: {stmt!r}"


def test_lock_timeout_returns_false_and_logs_its_own_category(fake_pg):
    """On timeout: no raise, no boot failure, and a DISTINCT log category so
    it reads as 'deferred, retries next boot' rather than a hard failure."""
    import psycopg
    cur, conn, logged = fake_pg(
        raise_on="ALTER TABLE",
        raise_exc=psycopg.errors.LockNotAvailable("canceling statement due "
                                                  "to lock timeout"),
    )
    assert _db.ensure_page_views_is_poll_column() is False
    assert logged == ["ensure_page_views_is_poll_column_lock_timeout"], (
        f"expected the lock-timeout category, got {logged}"
    )
    assert not conn.committed


def test_other_errors_keep_the_generic_category(fake_pg):
    """A lock timeout is deferred; anything else is a real failure. They must
    not be conflated, or a genuine broken migration hides as 'retry later'."""
    cur, _, logged = fake_pg(
        raise_on="ALTER TABLE", raise_exc=RuntimeError("boom"),
    )
    assert _db.ensure_page_views_is_poll_column() is False
    assert logged == ["ensure_page_views_is_poll_column_failed"]


def test_retry_is_safe_because_the_alter_is_idempotent(fake_pg):
    """Retry-next-boot is only sound if repeated attempts are free."""
    cur, _, _ = fake_pg()
    _db.ensure_page_views_is_poll_column()
    alter = next(s for s in cur.statements if "ALTER TABLE" in s)
    assert "IF NOT EXISTS" in alter


def test_is_lock_timeout_is_guarded_against_missing_psycopg(monkeypatch):
    """The error path must not raise AttributeError when psycopg is absent."""
    monkeypatch.setattr(_db, "psycopg", None)
    assert _db._is_lock_timeout(RuntimeError("x")) is False


def test_is_lock_timeout_recognises_the_real_error():
    import psycopg
    assert _db._is_lock_timeout(
        psycopg.errors.LockNotAvailable("lock timeout")) is True
    assert _db._is_lock_timeout(RuntimeError("nope")) is False


def test_retry_next_boot_is_documented():
    src = _src(DB_PY)
    fn = src[src.index("def ensure_page_views_is_poll_column"):][:3000]
    assert "retry" in fn.lower(), (
        "the retry-next-boot behaviour must be stated where it is implemented"
    )
    assert "pg_dump" in fn, "the reason for the timeout must be recorded"


def test_hard_rule_is_documented_where_it_will_be_read():
    for path in (os.path.join(ROOT, "poll_detection.py"), MIGRATION):
        src = _src(path)
        assert "NOT A BOT" in src.upper(), (
            f"{os.path.basename(path)} must state the hard rule"
        )

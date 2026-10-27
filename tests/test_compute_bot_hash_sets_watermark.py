"""compute_bot_hash_sets(): watermark-gated incremental accumulation
(branch bot-hash-sets-watermark-2026-10-27).

2026-10-27 egress audit (Charlie): this accessor re-scanned the FULL
history of visitor_hash/ip_day_hash rows matching the bot row_pred on
every single call (~56k / ~48k rows) -- exactly the cost it exists to
save the 44-subquery-per-render downstream path from, except it paid
that cost itself every time it was asked.

Fix: a module-level watermark (MAX(ts) seen so far). Each call only
queries rows newer than the last call's watermark and UNIONs their
hashes into the accumulated set -- correct because a hash that ever
matched row_pred stays tainted forever (same unbounded-window invariant
the legacy subquery form already documents), so additive-only
accumulation across calls is exact, not an approximation. The caller
(app.py) is a long-running process, so a module-level cache persists
correctly across calls the way it would not for a short-lived walker
invocation.

Hermetic: no DB, no network. Fakes a minimal cursor/connection.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import db  # noqa: E402


class _FakeCursor:
    def __init__(self, rows_by_query):
        self.rows_by_query = rows_by_query
        self.last_kind = None

    def execute(self, sql, params=None):
        if "MAX(ts)" in sql:
            self.last_kind = "max_ts"
        elif "visitor_hash" in sql:
            self.last_kind = "visitor"
        elif "ip_day_hash" in sql:
            self.last_kind = "ip_day"

    def fetchone(self):
        return (self.rows_by_query.get("max_ts"),)

    def fetchall(self):
        return [(v,) for v in self.rows_by_query.get(self.last_kind, [])]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows_by_query):
        self.rows_by_query = rows_by_query

    def cursor(self):
        return _FakeCursor(self.rows_by_query)


@pytest.fixture(autouse=True)
def _reset_cache():
    db._bot_hash_sets_cache = {"watermark_ts": 0, "visitor_hashes": set(), "ip_day_hashes": set()}
    yield
    db._bot_hash_sets_cache = {"watermark_ts": 0, "visitor_hashes": set(), "ip_day_hashes": set()}


def test_first_call_is_a_full_scan_watermark_zero():
    conn = _FakeConn({"max_ts": 1000, "visitor": ["vA", "vB"], "ip_day": ["iA"]})
    vh, ih = db.compute_bot_hash_sets(conn)
    assert vh == {"vA", "vB"}
    assert ih == {"iA"}
    assert db._bot_hash_sets_cache["watermark_ts"] == 1000


def test_second_call_only_adds_new_hashes_accumulates_old_ones():
    conn1 = _FakeConn({"max_ts": 1000, "visitor": ["vA"], "ip_day": ["iA"]})
    db.compute_bot_hash_sets(conn1)

    # second call: Postgres would only return rows newer than watermark=1000
    # in reality (WHERE ts > %s) -- the fake just simulates what comes back.
    conn2 = _FakeConn({"max_ts": 2000, "visitor": ["vB"], "ip_day": ["iB"]})
    vh, ih = db.compute_bot_hash_sets(conn2)

    assert vh == {"vA", "vB"}, "old hash must survive, new hash must be added"
    assert ih == {"iA", "iB"}
    assert db._bot_hash_sets_cache["watermark_ts"] == 2000


def test_watermark_advances_to_max_ts_even_when_no_new_matching_rows():
    conn1 = _FakeConn({"max_ts": 1000, "visitor": ["vA"], "ip_day": []})
    db.compute_bot_hash_sets(conn1)

    conn2 = _FakeConn({"max_ts": 5000, "visitor": [], "ip_day": []})
    vh, ih = db.compute_bot_hash_sets(conn2)

    assert vh == {"vA"}, "nothing new matched, but the old set must still be returned"
    assert db._bot_hash_sets_cache["watermark_ts"] == 5000, \
        "watermark must advance to MAX(ts) regardless of whether any row matched row_pred"


def test_query_params_include_the_watermark():
    seen_params = []

    class _Cursor(_FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if self.last_kind in ("visitor", "ip_day"):
                seen_params.append((self.last_kind, params[0] if params else None))

    class _Conn(_FakeConn):
        def cursor(self):
            return _Cursor(self.rows_by_query)

    db._bot_hash_sets_cache["watermark_ts"] = 777
    conn = _Conn({"max_ts": 999, "visitor": [], "ip_day": []})
    db.compute_bot_hash_sets(conn)

    assert all(watermark == 777 for _, watermark in seen_params), \
        "both the visitor_hash and ip_day_hash queries must bound on the cached watermark"


def test_on_exception_returns_whatever_was_already_accumulated_not_empty():
    conn1 = _FakeConn({"max_ts": 1000, "visitor": ["vA"], "ip_day": ["iA"]})
    db.compute_bot_hash_sets(conn1)

    class _BoomConn:
        def cursor(self):
            raise RuntimeError("neon down")

    vh, ih = db.compute_bot_hash_sets(_BoomConn())
    assert vh == {"vA"} and ih == {"iA"}, \
        "a failed incremental pull must preserve the already-accumulated set, not reset to empty"


def test_returns_frozensets():
    conn = _FakeConn({"max_ts": 1000, "visitor": ["vA"], "ip_day": ["iA"]})
    vh, ih = db.compute_bot_hash_sets(conn)
    assert isinstance(vh, frozenset) and isinstance(ih, frozenset)

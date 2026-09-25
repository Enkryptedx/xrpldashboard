"""Tests for the fail-safe route_status_log hooks (item 11, 2026-09-25).

Contract verified:
  - a row is logged for EVERY response (all methods, all paths — no skip-lists),
  - 404s and non-GET are logged (page_views drops these; this must not),
  - a logging failure NEVER alters the response (fail-safe),
  - the raising-view path is logged exactly once (after_request wins; teardown
    skips when after_request already recorded, via g._route_status_logged).

These assert against the REAL DB writer (db.log_route_status → route_status_log)
because the hook resolves db.log_route_status inside a closure and the honest
test is "did a row actually land." Tests skip cleanly if PG is unavailable
(e.g. CI with empty DATABASE_URL) — the fail-safe test still runs without a DB.
"""
import time

import pytest

import app as A
import db


# Register a raising route at import time (before first request).
def _raise_view():
    raise RuntimeError("boom")

if "/__test_raise__" not in {r.rule for r in A.app.url_map.iter_rules()}:
    A.app.add_url_rule("/__test_raise__", "__test_raise__", _raise_view)


pg = pytest.mark.skipif(not db.pg_available(), reason="no DB (route_status_log write path)")


def _rows_since(ts, path_like):
    with db.pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT path, method, status, via, exc_type FROM route_status_log "
            "WHERE ts >= %s AND path LIKE %s ORDER BY id DESC",
            (int(ts) - 2, path_like),
        )
        return cur.fetchall()


@pg
def test_normal_response_logged(client):
    t0 = time.time()
    r = client.get("/health")
    assert r.status_code == 200
    time.sleep(0.3)
    rows = _rows_since(t0, "/health")
    assert any(row[3] == "after_request" and row[2] == 200 and row[1] == "GET"
               for row in rows), f"no after_request/200/GET row for /health: {rows}"


@pg
def test_404_logged(client):
    t0 = time.time()
    r = client.get("/__nope_zzz_404__")
    assert r.status_code == 404
    time.sleep(0.3)
    rows = _rows_since(t0, "/__nope_zzz_404__")
    assert rows and rows[0][2] == 404, f"404 not logged: {rows}"


@pg
def test_non_get_logged(client):
    # page_views._log_page_view skips non-GET; route_status_log must NOT.
    t0 = time.time()
    client.post("/health")
    time.sleep(0.3)
    rows = _rows_since(t0, "/health")
    assert any(row[1] == "POST" for row in rows), f"POST not logged: {rows}"


@pg
def test_raising_view_logged_once(client):
    A.app.config["PROPAGATE_EXCEPTIONS"] = False
    t0 = time.time()
    r = client.get("/__test_raise__")
    assert r.status_code == 500
    time.sleep(0.3)
    rows = _rows_since(t0, "/__test_raise__")
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}: {rows}"
    assert rows[0][2] == 500
    assert rows[0][3] == "after_request"  # after_request runs for the converted 500


def test_logging_failure_never_breaks_response(client, monkeypatch):
    """Fail-safe contract — runs WITHOUT a DB too. If the writer raises,
    the response must still be 200."""
    monkeypatch.setattr(db, "log_route_status",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = client.get("/health")
    assert r.status_code == 200, "a logging failure altered/broke the response"

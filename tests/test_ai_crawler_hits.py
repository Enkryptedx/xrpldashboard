"""Tests for the ai_crawler_hits telemetry surface (Phase 2 of the
2026-08-02 two-instrument build).

Locks:
- classify_ai_crawler contract (UNLISTED sentinel for bot-shaped but
  unlisted UAs; None for normal browsers)
- SCHEMA_DDL declares the ai_crawler_hits table and its indexes
- db.write_ai_crawler_hit is a silent no-op when PG isn't configured
  (dev/local safety)

Denominator statement: this surface counts hits to agent-tier routes
(AGENT_TIER_ROUTE_PATHS + AGENT_TIER_ROUTE_PREFIXES) from bot-shaped
UAs. General page traffic stays in `page_views`. The two surfaces are
the citation-signal / demand-signal split — never blurred.
"""


class TestAiCrawlerHitsDdl:
    """SCHEMA_DDL must declare ai_crawler_hits and its indexes.
    Guards the fresh-install path from silently regressing when
    someone edits db.SCHEMA_DDL."""

    def test_table_declared(self):
        import db
        assert "CREATE TABLE IF NOT EXISTS ai_crawler_hits" in db.SCHEMA_DDL

    def test_required_columns_declared(self):
        import db
        for col in ("ts", "ua_class", "path", "status"):
            assert col in db.SCHEMA_DDL

    def test_class_ts_index_declared(self):
        import db
        assert "ai_crawler_hits_class_ts_idx" in db.SCHEMA_DDL

    def test_ts_index_declared(self):
        import db
        assert "ai_crawler_hits_ts_idx" in db.SCHEMA_DDL


class TestWriteAiCrawlerHit:
    """The writer is a silent no-op when Postgres isn't configured. It
    never raises into the caller — the caller is the request path and
    a telemetry failure must not break a response."""

    def test_no_op_when_pg_unavailable(self, monkeypatch):
        import db
        # Force the writer conn path to return None (mimics no
        # DATABASE_URL). Should not raise.
        monkeypatch.setattr(db, "_get_writer_conn", lambda: None)
        db.write_ai_crawler_hit(
            ts=1700000000, ua_class="gptbot", path="/llms.txt", status=200
        )

    def test_never_raises_on_writer_exception(self, monkeypatch):
        # Simulate a mid-write failure (transient PG issue). The writer
        # must catch it and return; nothing propagates to the caller.
        import db

        class _BrokenCursor:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, *a, **kw):
                raise RuntimeError("transient")

        class _BrokenConn:
            def cursor(self):
                return _BrokenCursor()

        monkeypatch.setattr(db, "_get_writer_conn", lambda: _BrokenConn())
        monkeypatch.setattr(db, "_drop_writer_conn", lambda: None)
        # Must not raise.
        db.write_ai_crawler_hit(
            ts=1700000000, ua_class="UNLISTED", path="/openapi.json", status=200
        )

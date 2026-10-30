"""xrp_usd_history: the XRP/USD anchor gets recorded, 300s at a time.

WHY THIS EXISTS. price_oracle.xrp_usd() was never persisted anywhere -- it
lived in a ~60s in-memory cache and nowhere else. token_prices has history
but stores token->XRP; oracles_snapshot is replace-on-write. So nothing
could answer "what was XRP worth when this payment closed?", and /wallet
could only convert a historical payment at the RENDER-time anchor: the same
185,017,485.5 XRP transfer read $276.9M on one page and $232.1M on another
(2026-10-30), i.e. $1.4966/XRP vs $1.2545/XRP.

PR #110 made every /wallet USD figure state its rate and sample time. This
is the foundation for the real fix -- pricing at the transaction's own close
time -- which only ever works going forward, because the past was never
recorded and cannot be backfilled.

Hermetic: the oracle is monkeypatched and the database connection is a
fake that records SQL. No node, no DB, no network, no DDL executed.
"""

import os
import re
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import price_oracle  # noqa: E402

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import tokens_summary_walker as walker  # noqa: E402


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION = os.path.join(REPO, "migrations", "2026_10_30_xrp_usd_history.sql")

# The two rates from the incident.
RATE_HIGH = 1.4966
RATE_LOW = 1.2545


# ── fake psycopg plumbing ────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, rowcount=1, raise_exc=None):
        self.rowcount = rowcount
        self._raise = raise_exc
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self._raise is not None:
            raise self._raise


class _FakeTxn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def transaction(self):
        return _FakeTxn()

    def cursor(self):
        return self._cur


def _wire_db(monkeypatch, cur, pg=True):
    monkeypatch.setattr(db, "pg_available", lambda: pg)
    monkeypatch.setattr(db, "_get_writer_conn", lambda: _FakeConn(cur) if pg else None)
    monkeypatch.setattr(db, "_drop_writer_conn", lambda: None)


def _patch_oracle(monkeypatch, rate, sources, fetched_at, check_failed=False):
    monkeypatch.setattr(price_oracle, "xrp_usd", lambda: rate)
    monkeypatch.setattr(price_oracle, "xrp_usd_sources", lambda: sources)
    monkeypatch.setattr(price_oracle, "xrp_usd_fetched_at", lambda: fetched_at)
    monkeypatch.setattr(price_oracle, "xrp_usd_check_failed", lambda: check_failed)


def _good_sources(rlusd=RATE_HIGH):
    return [("RLUSD", rlusd), ("USD.GH", rlusd * 1.001),
            ("USD.Bitstamp", rlusd * 0.999)]


# ── the sample ───────────────────────────────────────────────────────────

def test_sample_records_all_three_anchors_and_the_chosen_rate(monkeypatch):
    ts = 1793000000
    _patch_oracle(monkeypatch, RATE_HIGH, _good_sources(), ts)
    s = price_oracle.xrp_usd_sample()
    assert s["chosen_rate"] == RATE_HIGH
    assert s["rlusd_rate"] == RATE_HIGH
    assert s["usd_gh_rate"] == pytest.approx(RATE_HIGH * 1.001)
    assert s["usd_bitstamp_rate"] == pytest.approx(RATE_HIGH * 0.999)
    assert s["check_failed"] is False
    assert s["derivation_method"] == "rlusd_amm_primary"


def test_snapshot_ts_is_the_price_sample_time_not_now(monkeypatch):
    """The PK must be the PRICE's own sample time, so re-observing the same
    cached rate is idempotent under ON CONFLICT DO NOTHING."""
    ts = 1793000000
    _patch_oracle(monkeypatch, RATE_HIGH, _good_sources(), ts)
    assert price_oracle.xrp_usd_sample()["snapshot_ts"] == ts
    # observed twice, same sample time -> the writer will dedupe
    assert price_oracle.xrp_usd_sample()["snapshot_ts"] == ts


def test_divergence_failure_is_recorded_with_its_sources(monkeypatch):
    """chosen_rate NULL + check_failed TRUE: RLUSD resolved but disagreed
    with both sanity checks. The sources are exactly what a later reader
    needs to see, so they must survive."""
    ts = 1793000500
    _patch_oracle(monkeypatch, None, [("RLUSD", 1.9), ("USD.GH", 1.25),
                                      ("USD.Bitstamp", 1.26)], ts,
                  check_failed=True)
    s = price_oracle.xrp_usd_sample()
    assert s["chosen_rate"] is None
    assert s["check_failed"] is True
    assert s["rlusd_rate"] == 1.9
    assert s["usd_gh_rate"] == 1.25
    assert s["usd_bitstamp_rate"] == 1.26
    assert s["snapshot_ts"] == ts


def test_rlusd_unavailable_falls_back_to_observation_time(monkeypatch):
    """chosen_rate NULL + check_failed FALSE and no fetch time at all --
    the failure is still worth a row, stamped when we observed it."""
    _patch_oracle(monkeypatch, None, [], None, check_failed=False)
    before = int(time.time())
    s = price_oracle.xrp_usd_sample()
    assert s["chosen_rate"] is None
    assert s["check_failed"] is False
    assert s["rlusd_rate"] is None
    assert s["snapshot_ts"] >= before


def test_missing_sanity_anchor_is_none_not_zero(monkeypatch):
    """A sanity check that failed to resolve must be NULL, never 0.0 --
    0.0 would read as 'XRP was worthless' to a later query."""
    _patch_oracle(monkeypatch, RATE_LOW, [("RLUSD", RATE_LOW)], 1793000000)
    s = price_oracle.xrp_usd_sample()
    assert s["usd_gh_rate"] is None
    assert s["usd_bitstamp_rate"] is None


# ── the writer ───────────────────────────────────────────────────────────

def _sample(**over):
    base = {
        "snapshot_ts": 1793000000,
        "chosen_rate": RATE_HIGH,
        "rlusd_rate": RATE_HIGH,
        "usd_gh_rate": RATE_HIGH,
        "usd_bitstamp_rate": RATE_HIGH,
        "check_failed": False,
        "derivation_method": "rlusd_amm_primary",
    }
    base.update(over)
    return base


def test_write_is_append_only_insert_on_conflict_do_nothing(monkeypatch):
    cur = _FakeCursor(rowcount=1)
    _wire_db(monkeypatch, cur)
    assert db.write_xrp_usd_history_sample(_sample()) == db.XRP_USD_HISTORY_WRITTEN
    sql, params = cur.calls[0]
    assert "INSERT INTO xrp_usd_history" in sql
    assert "ON CONFLICT DO NOTHING" in sql
    assert params[0] == 1793000000


def test_conflict_is_reported_not_treated_as_a_write(monkeypatch):
    cur = _FakeCursor(rowcount=0)
    _wire_db(monkeypatch, cur)
    assert db.write_xrp_usd_history_sample(_sample()) == db.XRP_USD_HISTORY_CONFLICT


def test_fail_closed_row_is_kept_with_a_null_rate(monkeypatch):
    """The whole point: a sample where the oracle refused to serve still
    gets a row, with NULL rather than a fabricated number."""
    cur = _FakeCursor(rowcount=1)
    _wire_db(monkeypatch, cur)
    s = _sample(chosen_rate=None, check_failed=True)
    assert db.write_xrp_usd_history_sample(s) == db.XRP_USD_HISTORY_WRITTEN
    _sql, params = cur.calls[0]
    assert params[1] is None, "chosen_rate must be written as NULL, not dropped"
    assert params[5] is True, "check_failed must be recorded"


def test_missing_table_skips_cleanly(monkeypatch):
    """This writer ships BEFORE the DDL is applied, so a missing table is a
    normal state: report it, write nothing, do not raise."""
    psycopg = pytest.importorskip("psycopg")
    exc = psycopg.errors.UndefinedTable("relation does not exist")
    cur = _FakeCursor(raise_exc=exc)
    _wire_db(monkeypatch, cur)
    assert db.write_xrp_usd_history_sample(_sample()) == db.XRP_USD_HISTORY_NO_TABLE


def test_no_pg_skips_cleanly(monkeypatch):
    cur = _FakeCursor()
    _wire_db(monkeypatch, cur, pg=False)
    assert db.write_xrp_usd_history_sample(_sample()) == db.XRP_USD_HISTORY_NO_PG
    assert cur.calls == []


def test_a_real_db_failure_still_raises(monkeypatch):
    """Only schema drift is swallowed. Everything else must stay loud."""
    cur = _FakeCursor(raise_exc=RuntimeError("connection reset"))
    _wire_db(monkeypatch, cur)
    monkeypatch.setattr(db, "_log_err_and_raise",
                        lambda cat, e: (_ for _ in ()).throw(e))
    with pytest.raises(RuntimeError):
        db.write_xrp_usd_history_sample(_sample())


def test_sample_without_a_timestamp_is_rejected(monkeypatch):
    cur = _FakeCursor()
    _wire_db(monkeypatch, cur)
    with pytest.raises(ValueError):
        db.write_xrp_usd_history_sample(_sample(snapshot_ts=None))


def test_writer_never_updates_or_deletes():
    """Append-only is a property of the code, not just of the comment."""
    import inspect
    src = inspect.getsource(db.write_xrp_usd_history_sample)
    upper = src.upper()
    assert "UPDATE XRP_USD_HISTORY" not in upper
    assert "DELETE FROM XRP_USD_HISTORY" not in upper
    assert "ON CONFLICT DO NOTHING" in upper


# ── the walker passenger ─────────────────────────────────────────────────

class _FakeDbMod:
    def __init__(self, result=None, raise_exc=None):
        self.result = result
        self.raise_exc = raise_exc
        self.calls = 0

    def write_xrp_usd_history_sample(self, sample):
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


def test_walker_reports_the_write_status(monkeypatch):
    _patch_oracle(monkeypatch, RATE_HIGH, _good_sources(), 1793000000)
    fake = _FakeDbMod(result="written")
    status = walker._record_xrp_usd_sample(fake, dry_run=False)
    assert status.startswith("xrp_usd=written")
    assert "rate=1.4966" in status
    assert fake.calls == 1


def test_walker_dry_run_writes_nothing(monkeypatch):
    _patch_oracle(monkeypatch, RATE_HIGH, _good_sources(), 1793000000)
    fake = _FakeDbMod(result="written")
    status = walker._record_xrp_usd_sample(fake, dry_run=True)
    assert "dry-run" in status
    assert fake.calls == 0, "--dry-run must not write"


def test_walker_reports_a_missing_table_without_failing(monkeypatch):
    _patch_oracle(monkeypatch, RATE_HIGH, _good_sources(), 1793000000)
    fake = _FakeDbMod(result="skipped_no_table")
    status = walker._record_xrp_usd_sample(fake, dry_run=False)
    assert "skipped_no_table" in status


def test_a_write_failure_never_fails_the_tick(monkeypatch):
    """PASSENGER RULE. The walker's job is the /tokens summary row. A price
    sample that cannot be written must not page L1 or suppress the row the
    route actually serves -- but it must still be visible in the message.
    """
    _patch_oracle(monkeypatch, RATE_HIGH, _good_sources(), 1793000000)
    fake = _FakeDbMod(raise_exc=RuntimeError("writer conn gone"))
    status = walker._record_xrp_usd_sample(fake, dry_run=False)
    assert status.startswith("xrp_usd=error:RuntimeError")


def test_an_oracle_failure_never_fails_the_tick(monkeypatch):
    def boom():
        raise RuntimeError("oracle down")
    monkeypatch.setattr(price_oracle, "xrp_usd", boom)
    fake = _FakeDbMod(result="written")
    status = walker._record_xrp_usd_sample(fake, dry_run=False)
    assert status.startswith("xrp_usd=oracle_error:")
    assert fake.calls == 0


def test_fail_closed_sample_is_labelled_in_the_message(monkeypatch):
    _patch_oracle(monkeypatch, None, [("RLUSD", 1.9)], 1793000000,
                  check_failed=True)
    fake = _FakeDbMod(result="written")
    status = walker._record_xrp_usd_sample(fake, dry_run=False)
    assert "rate=none" in status
    assert "check_failed" in status


def test_status_is_folded_into_the_walker_message():
    """The status must reach walker_health, or a silent failure is silent."""
    import inspect
    src = inspect.getsource(walker.run)
    assert "_record_xrp_usd_sample(db, dry_run)" in src
    assert src.count("{xrp_usd_status}") >= 2, (
        "status must appear in both the dry-run and the normal message")


def test_walker_cadence_is_300s():
    assert walker.CADENCE_SECONDS == 300


# ── the migration ────────────────────────────────────────────────────────

def test_migration_file_exists_and_creates_the_table():
    with open(MIGRATION, encoding="utf-8") as f:
        sql = f.read()
    assert "CREATE TABLE IF NOT EXISTS xrp_usd_history" in sql
    assert "PRIMARY KEY (snapshot_ts)" in sql
    assert "xrp_usd_history_ts_idx" in sql


def test_migration_columns_match_the_writer_exactly():
    """Drift guard. If the INSERT and the DDL disagree, the writer fails on
    the first tick AFTER the owner pastes the SQL -- i.e. in production,
    hours later. Compare them here instead.
    """
    import inspect
    with open(MIGRATION, encoding="utf-8") as f:
        ddl = f.read()
    body = ddl.split("CREATE TABLE IF NOT EXISTS xrp_usd_history", 1)[1]
    body = body.split(");", 1)[0]
    ddl_cols = []
    for line in body.splitlines():
        line = line.strip()
        m = re.match(r"^([a-z_]+)\s+(BIGINT|DOUBLE|BOOLEAN|TEXT)", line)
        if m:
            ddl_cols.append(m.group(1))

    src = inspect.getsource(db.write_xrp_usd_history_sample)
    cols_blob = src.split("INSERT INTO xrp_usd_history", 1)[1].split(")", 1)[0]
    insert_cols = re.findall(r"[a-z_]+", cols_blob.replace("\n", " "))
    insert_cols = [c for c in insert_cols if c in set(ddl_cols)]

    assert ddl_cols == [
        "snapshot_ts", "chosen_rate", "rlusd_rate", "usd_gh_rate",
        "usd_bitstamp_rate", "check_failed", "derivation_method",
    ], f"DDL columns changed: {ddl_cols}"
    assert insert_cols == ddl_cols, (
        f"INSERT {insert_cols} does not match DDL {ddl_cols}")


def test_migration_is_not_executed_by_any_code():
    """The owner pastes this in the Neon SQL Editor. Nothing may run it --
    a walker that silently creates the table would make the DDL review
    pointless.
    """
    hits = []
    for name in os.listdir(REPO):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(REPO, name), encoding="utf-8",
                  errors="ignore") as f:
            body = f.read()
        if "CREATE TABLE IF NOT EXISTS xrp_usd_history" in body:
            hits.append(name)
    assert hits == [], f"xrp_usd_history DDL is executed from: {hits}"


def test_sample_keys_cover_every_ddl_column(monkeypatch):
    """The sampler must supply everything the writer inserts."""
    _patch_oracle(monkeypatch, RATE_HIGH, _good_sources(), 1793000000)
    s = price_oracle.xrp_usd_sample()
    for col in ("snapshot_ts", "chosen_rate", "rlusd_rate", "usd_gh_rate",
                "usd_bitstamp_rate", "check_failed", "derivation_method"):
        assert col in s, f"sampler missing {col}"

"""Evernorth daily snapshot job (2026-10-10).

The job runs at 21:30 ET, after the 21:00 ET signed snapshot.

What this pins:

1. **The snapshot date is the Eastern date, not UTC.** 21:30 ET is already
   the next UTC day, so keying on UTC would file every reading under
   tomorrow and the card's history would sit a day ahead of its heading.
   This is the bug most likely to go unnoticed, because it only shows up
   between 20:00 ET and midnight.
2. An unreadable wallet is recorded as None, never 0 — a failed read
   counted as zero understates the total and renders as an outflow that
   never happened. readable_count exposes the partial read.
3. Both writes are best-effort and independent: a Postgres failure still
   leaves the local JSON copy, so a reading is never lost entirely, and
   neither failure propagates out of run().

Hermetic: fake reader, patched writer, temp file. No node, no DB.
"""
import datetime as dt
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_REPO, os.path.join(_REPO, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import db  # noqa: E402
import evernorth_daily_snapshot as S  # noqa: E402
import institutional_treasuries as T  # noqa: E402

#: 21:30 ET on 2026-10-10 is 01:30 UTC on 2026-10-11.
LATE_ET = dt.datetime(2026, 10, 11, 1, 30, tzinfo=dt.timezone.utc)


def _reader(unreadable=0):
    def read(addresses):
        bal = {a: 1_000_000_000 for a in addresses}   # 1000 XRP each
        for a in list(addresses)[:unreadable]:
            bal[a] = None
        return bal, {}
    return read


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "LOCAL_JSON", str(tmp_path / "snap.json"))
    monkeypatch.setattr(db, "write_evernorth_daily_snapshot",
                        lambda *a, **k: None)
    yield


def test_snapshot_date_is_eastern_not_utc():
    """The whole point: 01:30 UTC on the 11th is still the 10th in ET."""
    out = S.run(reader=_reader(), now=LATE_ET)
    assert out["snapshot_date"] == "2026-10-10"
    assert LATE_ET.date().isoformat() == "2026-10-11"  # UTC would be wrong


def test_unreadable_wallet_is_null_never_zero():
    out = S.run(reader=_reader(unreadable=1), now=LATE_ET)
    nulls = [v for v in out["balances"].values() if v is None]
    assert len(nulls) == 1
    assert 0 not in out["balances"].values()
    assert out["readable_count"] == out["wallet_count"] - 1


def test_total_excludes_unreadable_rows():
    full = S.run(reader=_reader(), now=LATE_ET)
    partial = S.run(reader=_reader(unreadable=2), now=LATE_ET)
    assert partial["total_xrp"] == full["total_xrp"] - 2000.0
    assert partial["wallet_count"] == full["wallet_count"]


def test_local_copy_written_and_matches_payload():
    out = S.run(reader=_reader(), now=LATE_ET)
    with open(S.LOCAL_JSON) as fh:
        on_disk = json.load(fh)
    assert on_disk["snapshot_date"] == out["snapshot_date"]
    assert on_disk["total_xrp"] == out["total_xrp"]
    assert len(on_disk["balances"]) == out["wallet_count"]


def test_postgres_failure_still_writes_local_copy(monkeypatch):
    """A dead DB must not cost us the reading."""
    def boom(*a, **k):
        raise RuntimeError("pg down")
    monkeypatch.setattr(db, "write_evernorth_daily_snapshot", boom)
    out = S.run(reader=_reader(), now=LATE_ET)      # must not raise
    assert os.path.exists(S.LOCAL_JSON)
    assert out["total_xrp"] > 0


def test_local_copy_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(S, "LOCAL_JSON", "/nonexistent-dir/snap.json")
    out = S.run(reader=_reader(), now=LATE_ET)      # must not raise
    assert out["snapshot_date"] == "2026-10-10"


def test_payload_covers_all_thirteen_wallets():
    out = S.run(reader=_reader(), now=LATE_ET)
    assert out["wallet_count"] == len(T.WALLETS) == 13
    assert len(out["balances"]) == 13

"""/rlusd event log is a polled SNAPSHOT (walker every REFRESH_INTERVAL,
page poll every 60 s), not a stream. Charlie 2026-09-26: label it with
its real cadence and last-refresh age; timestamps as data-ts with
client-side relative time; the freshness chip anchors on the server's
fetched_at, never on poll time."""
from __future__ import annotations

import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import app as app_module  # noqa: E402


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def _page(client):
    r = client.get("/rlusd")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_event_log_is_labeled_as_a_snapshot_with_cadence(client):
    html = _page(client)
    assert "Live event log" not in html
    assert "Event log" in html
    assert re.search(r"snapshot every\s+\d+\s+min", html), "cadence label missing"
    assert 'id="tickerRefreshed" data-ts="' in html
    assert "A polled snapshot, not a live stream" in html


def test_freshness_chip_says_snapshot_not_live(client):
    html = _page(client)
    assert "Live · updated" not in html
    # The chip only renders when the route had a cached_at; when it does,
    # both templates must carry the honest wording.
    if "rlusd-freshness-chip" in html:
        assert "Snapshot · refreshed {age} ago" in html


def test_row_times_are_data_ts_and_chip_anchors_on_server_time(client):
    html = _page(client)
    # Row renderer emits <time data-ts=…> and a 30 s re-render loop exists.
    assert '<time data-ts="${Math.floor(r.t / 1000)}"' in html
    assert "updateRefreshedLabel" in html
    # Chip anchors on fetched_at from the poll payload, not Date.now().
    assert "chipSetFresh(data.fetched_at)" in html
    assert "chipAnchor = Date.now() / 1000;\n    renderChip();" not in html

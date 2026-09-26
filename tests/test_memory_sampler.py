"""memory_sampler unit tests — units, Render gating, and the line-7 aggregate.

Founding incident 2026-09-26: the standing-orders report read
"peak 377664 MB, avg 531 MB ... 1 sample above 414" for a Render dyno that
never left ~270 MB. One row came from a Mac mini that imported app.py:
macOS ru_maxrss is BYTES, the code divided as KB, and the report blended
hosts. No DB: the cursor is faked.
"""
from __future__ import annotations
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
import memory_sampler as M  # noqa: E402


def test_maxrss_divisor_per_platform():
    assert M._maxrss_divisor("linux") == 1024.0
    assert M._maxrss_divisor("darwin") == 1024.0 * 1024.0
    # 377664 KB-as-MB was the founding bad value; in bytes it is 369 MB.
    assert round(377664 * 1024 / M._maxrss_divisor("darwin")) == 369
    assert round(276480 / M._maxrss_divisor("linux")) == 270


def test_current_rss_mb_is_plausible_on_this_host():
    v = M._current_rss_mb()
    assert v is not None
    assert 5 < v < 4096, v  # a test process, whatever the platform


def test_should_run_sampler_gating():
    assert M.should_run_sampler({}) is False
    assert M.should_run_sampler({"RENDER": "true"}) is True
    assert M.should_run_sampler({"RENDER": "1"}) is True
    assert M.should_run_sampler({"MEMORY_SAMPLER_FORCE": "1"}) is True
    assert M.should_run_sampler({"RENDER": "false"}) is False


def test_start_background_sampler_noop_off_render(monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("MEMORY_SAMPLER_FORCE", raising=False)
    M._sampler_thread = None
    M.start_background_sampler("web")
    assert M._sampler_thread is None


class _FakeCur:
    def __init__(self, rows):
        self.rows = list(rows)
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0)


def test_daily_memory_line_filters_hosts_and_names_excluded():
    cur = _FakeCur([(271.0, 270.4, 1447, 0, 7), (1,)])
    line = M.daily_memory_line(cur, 0, 86400)
    assert line.startswith("7. Memory (Render web dyno")
    assert "peak 271 MB" in line and "avg 270 MB" in line
    assert "1447 samples on 7 dynos" in line
    assert "0 samples above 414 MB" in line
    assert "1 off-dyno sample excluded." in line
    # Both queries constrain by hostname prefix
    assert "hostname LIKE %s" in cur.sql[0][0]
    assert cur.sql[0][1][-1] == M.RENDER_HOSTNAME_PREFIX + "%"
    assert "hostname NOT LIKE %s" in cur.sql[1][0]


def test_daily_memory_line_no_off_dyno_suffix_when_clean():
    cur = _FakeCur([(271.0, 270.4, 1447, 0, 7), (0,)])
    line = M.daily_memory_line(cur, 0, 86400)
    assert "off-dyno" not in line


def test_daily_memory_line_no_samples():
    cur = _FakeCur([(None, None, 0, None, 0), (3,)])
    assert M.daily_memory_line(cur, 0, 86400).startswith("7. Memory: no samples")

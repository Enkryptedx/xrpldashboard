"""changes_builder scalar-metric source-change gate (Charlie 2026-10-01).

Regression coverage for the AMM-leaf-undercount false-rise bug: the
2026-10-01 signed leaf caught rank_amms mid-pass (amm_pools_count=10,549,
source=amm_ranked.json) before the leaf-undercount fix (da36921/0a26f2c)
switched the signer to read the finished-pass copy
(source=amm_ranked_finished.json). The 2026-09-11 duplicate-suppression
only catches a byte-identical scalar duplicate; it does NOT catch a
baseline that is numerically distinct but wrong, so the next good leaf's
diff against the bad 10-01 leaf would report a fabricated jump.

Rule now: before emitting a scalar metric's normal delta line, compare the
before/after envelope's `source` base for that metric name (parenthetical
qualifier stripped). If it changed, suppress the delta and emit one plain
source-changed notice (metric_type='source_change', flagged as an anomaly)
instead.
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import changes_builder as CB  # noqa: E402


def _env(amm_count, amm_tvl, source):
    return {
        "metrics": [
            {"name": "amm_pools_count", "value": amm_count, "source": source},
            {"name": "amm_pools_total_tvl_usd", "value": amm_tvl,
             "source": f"{source} (sum of tvl_usd)"},
        ],
    }


def test_source_base_strips_parenthetical_qualifier():
    assert CB._source_base("amm_ranked.json (sum of tvl_usd)") == "amm_ranked.json"
    assert CB._source_base("amm_ranked_finished.json") == "amm_ranked_finished.json"
    assert CB._source_base(None) is None


def test_source_change_suppresses_delta_and_emits_plain_notice():
    before = _env(10_549, 20_963_820.07, "amm_ranked.json")
    after = _env(30_376, 33_111_964.73, "amm_ranked_finished.json")
    b_src = CB._metric_source_by_name(before, "amm_pools_count")
    a_src = CB._metric_source_by_name(after, "amm_pools_count")
    assert CB._source_base(b_src) != CB._source_base(a_src)
    line = CB._source_change_line("amm_pools_count", "AMM pools count",
                                   "/pools", CB._source_base(b_src),
                                   CB._source_base(a_src))
    assert line["metric_type"] == "source_change"
    assert "data source changed" in line["line"]
    assert "19,827" not in line["line"]  # never states the fabricated delta
    assert CB._is_anomaly(line) is True  # still earns a strip slot


def test_same_source_base_unaffected_by_gate():
    before = _env(30_000, 32_000_000.0, "amm_ranked_finished.json")
    after = _env(30_376, 33_111_964.73, "amm_ranked_finished.json")
    b_src = CB._metric_source_by_name(before, "amm_pools_count")
    a_src = CB._metric_source_by_name(after, "amm_pools_count")
    assert CB._source_base(b_src) == CB._source_base(a_src)
    # Normal delta line still fires when the source base is unchanged.
    line = CB._scalar_delta_line("amm_pools_count", "AMM pools count",
                                  "/pools", 30_000, 30_376, metric_type="count")
    assert line is not None
    assert line["metric_type"] == "count"


def test_full_build_withholds_fabricated_amm_rise_across_bad_leaf():
    import datetime as dt

    bad_today = {
        "date": "2026-10-01",
        "metrics": [
            {"name": "xrpl_validated_ledger_index", "value": 107348721,
             "source": "own-node (LAN) -> ledger(validated)"},
            {"name": "amm_pools_count", "value": 10_549, "source": "amm_ranked.json"},
            {"name": "amm_pools_total_tvl_usd", "value": 20_963_820.07,
             "source": "amm_ranked.json (sum of tvl_usd)"},
            {"name": "mpt_total_count", "value": 100, "source": "mpt.json"},
            {"name": "named_accounts_count", "value": 500, "source": "named.json"},
            {"name": "rlusd_xrpl_supply", "value": 1000.0, "source": "rlusd.json"},
            {"name": "rwa_total_aum_usd", "value": 242.09,
             "source": "amm_ranked.json (rwa_pool_attribution cross-ref)"},
        ],
    }
    tonight = {
        "date": "2026-10-02",
        "metrics": [
            {"name": "xrpl_validated_ledger_index", "value": 107363721,
             "source": "own-node (LAN) -> ledger(validated)"},
            {"name": "amm_pools_count", "value": 30_376,
             "source": "amm_ranked_finished.json"},
            {"name": "amm_pools_total_tvl_usd", "value": 33_111_964.73,
             "source": "amm_ranked_finished.json (sum of tvl_usd)"},
            {"name": "mpt_total_count", "value": 100, "source": "mpt.json"},
            {"name": "named_accounts_count", "value": 500, "source": "named.json"},
            {"name": "rlusd_xrpl_supply", "value": 1000.0, "source": "rlusd.json"},
            {"name": "rwa_total_aum_usd", "value": 242.09,
             "source": "amm_ranked_finished.json (rwa_pool_attribution cross-ref)"},
        ],
    }

    class FakeCur:
        def __init__(self, rows):
            self.rows, self._res = rows, None
        def execute(self, sql, params):
            self._res = self.rows.get(params[0])
        def fetchone(self):
            r, self._res = self._res, None
            return None if r is None else (r,)

    class FakeConn:
        def __init__(self, cur):
            self.cur = cur
        def cursor(self):
            return self
        def __enter__(self):
            return self.cur
        def __exit__(self, *a):
            return False

    class FakePg:
        def __init__(self, envs):
            self.envs = envs
        def __call__(self):
            return self
        def __enter__(self):
            return FakeConn(FakeCur(self.envs))
        def __exit__(self, *a):
            return False

    envs = {dt.date(2026, 10, 1): bad_today, dt.date(2026, 10, 2): tonight}
    result = CB.build_changes_for_date(dt.date(2026, 10, 2), pg_connect=FakePg(envs))
    lines = [c["line"] for c in result["changes"]]

    assert not any("19,827" in l or "rose by" in l and "AMM pools" in l for l in lines)
    assert any("AMM pools count: data source changed" in l for l in lines)
    assert any("AMM total TVL" in l and "data source changed" in l for l in lines)

    strip = CB.build_strip(result, k=3)
    strip_lines = " ".join(s["line"] for s in strip["slots"])
    assert "19,827" not in strip_lines
    assert "12,148,144" not in strip_lines  # the fabricated TVL delta

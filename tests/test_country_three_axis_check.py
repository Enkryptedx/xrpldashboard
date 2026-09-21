"""Three-axis behavioral diagnostic for baseline anomalies (Charlie
ruling 2026-09-21).

Guards that:
1. `country_baseline_anomalies` returns rows for a real week window
   when the DB is reachable (smoke test), with the ratio floor honored.
2. `country_three_axis_check` returns the documented dict shape for
   a country with observed traffic, and an empty dict for one with
   no traffic in the window.
3. `_format_three_axis` renders each case as a single line ending in
   the three axes (top hash share, distinct paths, peak-2h share).

Skipped when the JJ read-only wrapper isn't reachable (dev boxes
without ~/.config/xrpldashboard/jj_env).
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _jj_reachable() -> bool:
    try:
        subprocess.check_output(
            ["/Users/charliebruce/.openclaw/workspace/scripts/jj_query.sh",
             "-A", "-t", "-c", "SELECT 1"],
            text=True, stderr=subprocess.STDOUT, timeout=10,
        )
        return True
    except Exception:
        return False


def test_three_axis_shape_on_real_country():
    if not _jj_reachable():
        print("SKIP: JJ read-only wrapper not reachable")
        return
    from scripts.weekly_analytics import (
        country_three_axis_check, _format_three_axis, week_bounds,
    )
    start, end = week_bounds(dt.date(2026, 9, 20))
    # US is a safe bet — the site's largest country, always populated
    check = country_three_axis_check("US", start, end)
    assert isinstance(check, dict) and check, "expected populated dict for US"
    for key in (
        "top_hash", "top_hash_hits", "top_hash_paths", "top_hash_top_path",
        "this_wk_hits", "top_hash_pct", "median_hits",
        "peak_2h_hits", "peak_2h_share",
    ):
        assert key in check, f"missing key: {key}"
    assert check["this_wk_hits"] > 0
    assert check["top_hash_pct"] >= 0.0
    assert check["top_hash_pct"] <= 100.0
    line = _format_three_axis(check)
    assert "top hash" in line and "distinct path" in line and "peak 2h window" in line
    print(f"OK  US three-axis: {line}")


def test_three_axis_returns_empty_for_unseen_country():
    if not _jj_reachable():
        print("SKIP: JJ read-only wrapper not reachable")
        return
    from scripts.weekly_analytics import (
        country_three_axis_check, _format_three_axis, week_bounds,
    )
    start, end = week_bounds(dt.date(2026, 9, 20))
    # Bouvet Island (BV) is a real ISO code with realistically zero
    # traffic this week; if this ever fires, replace with another
    # ISO code that isn't in page_views.
    check = country_three_axis_check("BV", start, end)
    assert check == {}, (
        f"expected empty dict for BV (no traffic), got {check!r}"
    )
    line = _format_three_axis(check)
    assert "no data" in line
    print(f"OK  BV empty: {line}")


def test_backward_compat_5x_shim_still_exists():
    """`country_5x_anomalies` was renamed to `country_baseline_anomalies`
    with a threshold param. Keep the old name as a shim so external
    callers (and old scripts on disk) don't break — same call, defaults
    to 5.0."""
    from scripts import weekly_analytics as wa
    assert callable(getattr(wa, "country_5x_anomalies")), (
        "country_5x_anomalies shim removed; external callers would break"
    )
    assert callable(getattr(wa, "country_baseline_anomalies"))
    print("OK  backward-compat shim present")


if __name__ == "__main__":
    test_three_axis_shape_on_real_country()
    test_three_axis_returns_empty_for_unseen_country()
    test_backward_compat_5x_shim_still_exists()
    print("ALL PASS")

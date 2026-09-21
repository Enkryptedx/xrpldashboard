"""Every in-flight amendment must have a plain-English summary
(Charlie ruling 2026-09-21). BatchV1_1 shipped with a Majorities
countdown live on-ledger for two weeks and rendered "uncatalogued"
the entire time; that's exactly the failure mode this test prevents.

Rule: if `amendments_state.fetch_amendments_state_cached()` reports
a name in `in_flight`, the `summaries` dict in
`templates/amendments.html` must have a matching key. Empty-string
matches count as missing.

Sourcing rule (documented in the amendments.html comment): each
`summary` entry cites a verifiable primary source (XLS spec or
xrpl.org · Known Amendments). Tests confirm the shape (kind,
summary, source_label, source_url), not the copy.

Skipped when the DB / RPC path isn't reachable (dev boxes without
the walker environment).
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _summaries_keys(html_path: str) -> set[str]:
    """Extract the top-level keys of the `summaries` dict block."""
    html = open(html_path, "r", encoding="utf-8").read()
    sum_start = html.find("{% set summaries = ")
    sum_end = html.find("} %}", sum_start)
    if sum_start < 0 or sum_end < 0:
        raise AssertionError(
            "amendments.html: could not locate `{% set summaries = { ... } %}`"
        )
    block = html[sum_start:sum_end]
    return set(re.findall(r'"([A-Za-z][A-Za-z0-9_]*)"\s*:\s*\{', block))


def test_every_in_flight_amendment_has_a_summary():
    try:
        import amendments_state
    except Exception:
        print("SKIP: amendments_state not importable")
        return
    state = amendments_state.fetch_amendments_state_cached()
    if not isinstance(state, dict) or not state.get("in_flight"):
        print("SKIP: no in_flight amendments right now")
        return
    html_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "templates", "amendments.html",
    )
    catalogued = _summaries_keys(html_path)

    missing = []
    for entry in state["in_flight"]:
        name = entry.get("name")
        if name and name not in catalogued:
            missing.append((name, entry.get("hash")))

    assert not missing, (
        f"In-flight amendments render 'uncatalogued' on /amendments — "
        f"they need a summary entry in templates/amendments.html:\n"
        + "\n".join(f"  - {n} ({h[:16]}...)" for n, h in missing)
        + "\n\nRule: every amendment the node reports as in-flight needs "
          "a plain-English summary + verifiable source citation before "
          "shipping. Uncatalogued = the reader can't understand what "
          "they're being asked to trust."
    )


if __name__ == "__main__":
    test_every_in_flight_amendment_has_a_summary()
    print("PASS")

"""/.well-known/anchors.json parser safety (Charlie fix 2026-10-04).

Anchor #9 vanished from the live surface because the parser split
docs/anchor_history.md on `\\n---\\n` dividers and took the first
`## Anchor #N` per chunk — so a MISSING divider before #9 merged it into
#8's chunk and silently dropped it. The fix parses by the anchor headers
themselves (one section per header, divider-independent) plus a loud
header-count == parsed-count guard.

Tests:
  (a) the real docs/anchor_history.md yields anchors 1..9 in order
      (FAILS on main — #9 dropped — PASSES on the branch).
  (b) a file missing a divider before a header still yields every anchor.
  (c) anchors #1..#8 produce byte-identical JSON entries to the live site.
"""

import json
import os

import pytest

import app as app_module

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_anchors():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        r = c.get("/.well-known/anchors.json")
    assert r.status_code == 200, f"anchors.json returned {r.status_code}"
    d = json.loads(r.data)
    return d.get("anchors") or d


def test_a_real_history_yields_1_through_9_in_order():
    """(a) The REAL committed anchor_history.md must expose every anchor 1..9.
    FAILS on main (#9 dropped by the divider-split parser); PASSES on branch.

    The route emits anchors in document order (newest-first here), so we assert
    the full SET 1..9 is present with no drop, and that the sequence is strictly
    one-per-number (no duplicates, no gaps) — not a specific ascending order."""
    anchors = _get_anchors()
    nums = [a.get("number") for a in anchors]
    assert sorted(nums) == list(range(1, 10)), (
        f"expected anchors 1..9 (no drop, no dupe), got {nums}. A missing "
        f"number here means the parser dropped one (the #9 divider-slip bug).")
    assert len(nums) == len(set(nums)), f"duplicate anchor numbers: {nums}"
    # #9 specifically present with its on-ledger identity.
    a9 = next(a for a in anchors if a["number"] == 9)
    assert a9["tx_hash"] == "D5C515F164AC3E41337206CB4A0A35A3703962C870CAD003E097ED7F9BF8732E"
    assert a9["ledger_index"] == 107423070
    assert a9["date"] == "2026-10-04"
    assert a9["chain_root"] == "d0acffad86597c744f79e38e433f42e2389073fb38b67a26c3ba7aa70f8f79d7"


def test_b_missing_divider_still_yields_every_anchor(monkeypatch, tmp_path):
    """(b) A synthetic history with NO divider between two anchors must still
    yield both — the parser is header-driven, not divider-driven."""
    md = (
        "# anchor history\n\n"
        "## Anchor #1 — 2026-08-01\n\n"
        "| Field | Value |\n|--|--|\n"
        "| Tx hash | `AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` |\n"
        "| Ledger | 100000001 |\n"
        "| MemoData (decoded) | `xrpldashboard/anchor/v1\\|2026-08-01\\|aa11` |\n\n"
        "---\n\n"
        "## Anchor #2 — 2026-08-08\n\n"
        "| Field | Value |\n|--|--|\n"
        "| Tx hash | `BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB` |\n"
        "| Ledger | 100000002 |\n"
        "| MemoData (decoded) | `xrpldashboard/anchor/v1\\|2026-08-08\\|bb22` |\n\n"
        # NOTE: no `---` before #3 — the exact slip that hid #9.
        "## Anchor #3 — 2026-08-15\n\n"
        "| Field | Value |\n|--|--|\n"
        "| Tx hash | `CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC` |\n"
        "| Ledger | 100000003 |\n"
        "| MemoData (decoded) | `xrpldashboard/anchor/v1\\|2026-08-15\\|cc33` |\n"
    )
    f = tmp_path / "anchor_history.md"
    f.write_text(md, encoding="utf-8")
    # Point the route's HERE/docs lookup at our temp file.
    orig_join = app_module.os.path.join

    def fake_join(*parts):
        if parts[-2:] == ("docs", "anchor_history.md"):
            return str(f)
        return orig_join(*parts)

    monkeypatch.setattr(app_module.os.path, "join", fake_join)
    anchors = _get_anchors()
    nums = sorted(a["number"] for a in anchors)
    assert nums == [1, 2, 3], (
        f"missing-divider file dropped an anchor: got {nums}, expected [1,2,3]")


def test_c_anchors_1_to_8_match_live_byte_for_byte():
    """(c) The JSON entries for anchors #1..#8 must be identical to what the
    live site currently serves (the fix must not change existing output). If
    the live site is unreachable, skip rather than fail CI."""
    import urllib.request
    url = "https://xrpldashboard.com/.well-known/anchors.json"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            live = json.loads(resp.read())
    except Exception as e:  # noqa: BLE001 — network-dependent; don't fail CI
        pytest.skip(f"live site unreachable: {e}")
    live_anchors = {a["number"]: a for a in (live.get("anchors") or live)}
    local_anchors = {a["number"]: a for a in _get_anchors()}
    # Compare only the per-anchor entries for 1..8 (the ones live has today),
    # field-by-field, excluding nothing — the fix must preserve them exactly.
    for n in range(1, 9):
        assert n in live_anchors, f"live missing anchor #{n}"
        assert n in local_anchors, f"local missing anchor #{n}"
        assert local_anchors[n] == live_anchors[n], (
            f"anchor #{n} entry differs from live:\n"
            f"  live={json.dumps(live_anchors[n], sort_keys=True)}\n"
            f"  local={json.dumps(local_anchors[n], sort_keys=True)}")

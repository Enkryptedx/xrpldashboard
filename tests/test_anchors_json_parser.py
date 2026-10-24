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


def _expected_anchor_numbers():
    """Every `## Anchor #N` header in the committed file -- the ground truth
    the parser must reproduce. Derived from the file itself so this test
    can never go stale as anchors are added (it pinned 1..9 until #12,
    failing silently on every anchor since #10 because it was not in CI)."""
    import re, pathlib
    md = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "anchor_history.md").read_text()
    return sorted(int(n) for n in re.findall(r"^## Anchor #(\d+)", md, flags=re.M))


def test_a_real_history_yields_every_committed_anchor_exactly_once():
    """(a) The REAL committed anchor_history.md must expose every anchor whose
    header is in the file, exactly once, with no gaps from 1 to the newest.
    A missing number means the parser dropped one (the #9 divider-slip bug,
    2026-10-04); a count mismatch means a section merged into its neighbour."""
    expected = _expected_anchor_numbers()
    assert expected, "no '## Anchor #N' headers found in docs/anchor_history.md"
    assert expected == list(range(1, expected[-1] + 1)), f"headers have a gap: {expected}"
    anchors = _get_anchors()
    nums = [a.get("number") for a in anchors]
    assert sorted(nums) == expected, (
        f"expected anchors {expected[0]}..{expected[-1]} (no drop, no dupe), got {sorted(nums)}. "
        f"A missing number here means the parser dropped one.")
    assert len(nums) == len(set(nums)), f"duplicate anchor numbers: {nums}"
    # every parsed anchor carries its on-ledger identity
    for a in anchors:
        assert a.get("tx_hash") and len(a["tx_hash"]) == 64, a
        assert a.get("ledger_index"), a


# Anchors whose committed record has no `MemoData (decoded)` row, so the
# feed cannot carry their chain_root. Found 2026-10-23 when this test was
# generalised: #11 (2026-10-16) had been recorded without that row and the
# public feed carried no chain_root for it for a week. The row was added the
# same day (memo bytes re-verified on two full-history servers). The set is
# kept, empty, so the test fails the moment a NEW anchor is recorded without
# the row -- the feed must carry a root for every anchor.
_KNOWN_ANCHORS_WITHOUT_MEMO_ROW = set()   # #11's row added 2026-10-23; every anchor now carries its root


def test_a2_every_anchor_exposes_its_chain_root_except_known_gaps():
    anchors = _get_anchors()
    missing = sorted(a["number"] for a in anchors
                     if not (a.get("chain_root") and len(a["chain_root"]) == 64))
    assert set(missing) <= _KNOWN_ANCHORS_WITHOUT_MEMO_ROW, (
        f"anchors.json has no chain_root for {missing}; only "
        f"{sorted(_KNOWN_ANCHORS_WITHOUT_MEMO_ROW)} are known gaps. "
        f"Add the `MemoData (decoded)` row to the record.")
    # and the known gap is real, not stale -- remove it from the set once fixed
    assert set(missing) == _KNOWN_ANCHORS_WITHOUT_MEMO_ROW, (
        f"known-gap set is stale: feed now lacks chain_root for {missing}")
    # #9 specifically: the anchor the divider bug dropped, pinned forever.
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


def test_c_anchors_1_to_8_match_expected_fixture():
    """(c) The JSON entries for anchors #1..#8 must be identical to a committed
    golden fixture (tests/fixtures/anchors_1_8_expected.json) captured from the
    live site at the pre-#9 state. This proves the parser rewrite did NOT change
    existing output, with NO network dependence — it never skips and can never
    fail CI because the live site is unreachable or mid-deploy. If anchors
    #1..#8 ever legitimately change, regenerate the fixture in the same commit."""
    fixture_path = os.path.join(HERE, "tests", "fixtures",
                                "anchors_1_8_expected.json")
    with open(fixture_path, encoding="utf-8") as f:
        expected = json.load(f)
    expected_by_num = {a["number"]: a for a in expected}
    assert set(expected_by_num) == set(range(1, 9)), (
        f"fixture must hold exactly anchors 1..8, got {sorted(expected_by_num)}")
    local_anchors = {a["number"]: a for a in _get_anchors()}
    # Field-by-field, excluding nothing — the fix must preserve each entry
    # byte-for-byte against the captured pre-#9 live output.
    for n in range(1, 9):
        assert n in local_anchors, f"local build missing anchor #{n}"
        assert local_anchors[n] == expected_by_num[n], (
            f"anchor #{n} entry differs from the golden fixture:\n"
            f"  expected={json.dumps(expected_by_num[n], sort_keys=True)}\n"
            f"  local={json.dumps(local_anchors[n], sort_keys=True)}")

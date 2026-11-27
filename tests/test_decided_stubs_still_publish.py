"""Deciding these 29 did NOT hide them. Confirmed from rendered HTML.

Suppression is not withholding. Only the impersonation class replaces a
name; every other reason class keeps publishing the name with the
`unverified` caveat. That is deliberate, and it is the single most
counter-intuitive thing about the `suppressed` field, so it gets a test
that reads the page rather than a predicate.

The fixture rows are built FROM token_names.json itself, so the file under
test supplies the population and this cannot drift out of step with a later
curation pass. mpt_snapshot.json is untracked, so no snapshot file is read.
"""

import json
import re

import pytest

import app as app_module
import token_label as tl


RECV_ISSUER = "rETtxwLwyBkKhVKJFLi4Z7h8YLN9kk63kQ"
MINT_ISSUER = "r3iMKCiKqu522oRGQcAdyvwoRUaTn7s8fi"
DECIDED_CLASSES = ("display_collision_unreviewed",
                   "owner_suppressed_unreviewed",
                   "bulk_serial_mint_unreviewed")
# This commit's stamp. Deliberately distinct from the anchor #17 carve-out
# stamp earlier the same day, "owner decision at anchor #17, 2026-11-27",
# which also suppressed five serial-mint entries at the SAME minter issuer.
# Filtering on class + issuer alone pulled those five in and made this file
# claim 34 entries; filtering on the date alone pulled in TST1/TST2/LTP.
# Only issuer AND this exact stamp isolates the 29.
STAMP = "owner decision 2026-11-27"

TRUST_SPAN = re.compile(
    r'<span class="(?:trust-label|hero-trust)">\s*\u00b7\s*([^<]+?)\s*</span>')


def _decided():
    """The 29 decided in this commit, read from the data under test."""
    with open("token_names.json", encoding="utf-8") as fh:
        raw = json.load(fh)
    out = []
    for key, v in raw.items():
        if not isinstance(v, dict):
            continue
        if v.get("issuer") not in (RECV_ISSUER, MINT_ISSUER):
            continue
        reason = str(v.get("suppressed") or "")
        if reason.startswith(DECIDED_CLASSES) and STAMP in reason:
            out.append((key, v))
    return out


def _row(v):
    return {
        "issuance_id": v["currency_hex"], "issuer": v["issuer"],
        "sequence": 1, "name": v.get("name"),
        "ticker": v.get("currency_display"), "asset_class_raw": "rwa",
        "asset_subclass": None, "classification": "rwa",
        "issuer_name": v.get("issuer_name"), "icon": None, "uris": [],
        "additional_info": None, "desc": v.get("desc"),
        "outstanding_amount": 1000, "maximum_amount": 10000,
        "asset_scale": 0, "transfer_fee": 0, "flags": 68,
        "metadata_present": True, "metadata_error": None, "holders": None,
    }


@pytest.fixture
def surfaces(monkeypatch):
    rows = _decided()
    # 29 -> 30 on 2026-11-27: ZUVMP joined, suppressed
    # bulk_serial_mint_unreviewed at the same minter with this same stamp.
    # The stamp filter is SHARED, so any later decision reusing it at these
    # two issuers lands here and must move this count with its own evidence.
    # That is the third time today a stamp-or-substring filter widened under
    # me; the group assertions below are the real pins.
    assert len(rows) == 30, f"expected the decided stubs, found {len(rows)}"
    snap = {
        "ok": True, "node": "fixture", "written_at": 1764000000,
        "issuances": [_row(v) for _, v in rows],
        "total": len(rows), "unique_issuers": 2,
        "by_class": {"rwa": len(rows), "stablecoin": 0, "utility": 0,
                     "other": 0},
        "by_status": {"live": len(rows), "prepared": 0, "test": 0},
    }
    sup_idx = {(v["currency_hex"], v["issuer"]): v for _, v in rows}
    monkeypatch.setattr(app_module, "load_mpt_snapshot", lambda: snap)
    monkeypatch.setattr(app_module, "_suppressed_token_index",
                        lambda: sup_idx)
    monkeypatch.setattr(app_module, "_load_token_names_dict", lambda: {})
    app_module.app.config["TESTING"] = True
    if hasattr(app_module, "limiter"):
        app_module.limiter.enabled = False
    return rows


def _body(path):
    with app_module.app.test_client() as c:
        resp = c.get(path)
    assert resp.status_code == 200, (path, resp.status_code)
    html = resp.get_data(as_text=True)
    html = re.sub(r"<style\b.*?</style>", "", html, flags=re.S | re.I)
    return re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)


class TestEveryDecidedStubStillPublishes:
    def test_every_one_prints_its_name_on_the_list(self, surfaces):
        html = _body("/mpts")
        missing = [v.get("currency_display") for _, v in surfaces
                   if (v.get("name") or "") not in html]
        assert not missing, (
            f"{len(missing)} decided entries stopped printing their name: "
            f"{missing[:8]}")
        assert tl.WITHHELD_LABEL not in html, (
            "a decided entry was WITHHELD; only the impersonation class "
            "replaces a name")

    def test_every_row_carries_the_unverified_tag(self, surfaces):
        html = _body("/mpts")
        rows = [tr for tr in re.findall(r"<tr\b.*?</tr>", html, flags=re.S)
                if "name-link" in tr]
        assert len(rows) == 30, len(rows)
        bare, wrong = [], []
        for tr in rows:
            m = TRUST_SPAN.search(tr)
            if not m:
                bare.append(re.sub(r"\s+", " ", tr)[:120])
            elif m.group(1) != tl.LABEL_UNVERIFIED:
                wrong.append(m.group(1))
        assert not bare, f"{len(bare)} decided rows rendered with no caveat"
        assert not wrong, f"unexpected caveat text: {sorted(set(wrong))}"

    def test_the_detail_heading_publishes_name_and_caveat(self, surfaces):
        """One row is enough to pin the detail surface; the list test covers
        the population."""
        _, v = surfaces[0]
        html = _body(f"/mpt/{v['currency_hex']}")
        h1 = re.search(r"<h1\b.*?</h1>", html, flags=re.S)
        assert h1, "no <h1> on the detail page"
        assert (v.get("name") or "") in h1.group(0), re.sub(
            r"\s+", " ", h1.group(0))[:160]
        m = TRUST_SPAN.search(h1.group(0))
        assert m and m.group(1) == tl.LABEL_UNVERIFIED, h1.group(0)

    def test_the_issuer_page_publishes_them_too(self, surfaces):
        html = _body(f"/mpt/issuer/{RECV_ISSUER}")
        assert "Receivable principal" in html
        assert tl.WITHHELD_LABEL not in html


class TestTheDecisionShape:
    def test_none_of_the_new_reasons_names_the_impersonation_class(self):
        """A reason string must never contain another class's literal name:
        code has matched these values as substrings, so prose becomes
        control flow. This is what withheld LRC for the wrong reason."""
        for _, v in _decided():
            assert tl.IMPERSONATION_REASON not in v["suppressed"], (
                v.get("currency_display"))

    def test_the_collision_group_is_twenty_four_by_measurement(self):
        """Counted from the data, never carried from a written list: a first
        pass described this group as 14 while naming 15 members."""
        rows = [v for _, v in _decided()
                if v["suppressed"].startswith("display_collision_unreviewed")]
        assert len(rows) == 24, len(rows)
        assert len({v.get("name") for v in rows}) == 1, "one shared name"
        assert len({v.get("currency_display") for v in rows}) == 24, \
            "distinct displays -- the collision is in the rendered name"

    def test_the_other_two_groups(self):
        got = {}
        for _, v in _decided():
            got.setdefault(v["suppressed"].split(":")[0], []).append(
                v.get("currency_display"))
        assert sorted(got["owner_suppressed_unreviewed"]) == [
            "M3D467", "M77354", "MC4896", "SE5FAB"]
        # FLW159 from the stub batch, plus ZUVMP, the pre-existing undecided
        # entry at the same minter that the whole-issuer sweep surfaced.
        assert sorted(got["bulk_serial_mint_unreviewed"]) == ["FLW159",
                                                              "ZUVMP"]

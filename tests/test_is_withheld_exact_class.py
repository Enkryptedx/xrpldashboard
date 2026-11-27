"""Withholding is decided by the reason CLASS, never by prose.

A `suppressed` value is `<class>: <evidence prose>`. The prose is free text
written for a human. `is_withheld()` used to ask whether the whole value
CONTAINED "impersonation_risk_pending_review", which turned that prose into
control flow.

Measured on production 2026-11-27 at x-commit fe056699229b: LRC is suppressed
`placeholder_unfinished_issuance`, yet its name was withheld on all three MPT
surfaces -- only because its evidence note contains the sentence "This
REPLACES impersonation_risk_pending_review recorded earlier...". The outcome
looked correct and rested on nothing. Deleting one explanatory clause would
have silently started publishing the name, and any future note that merely
cites the class by name would withhold an unrelated entry.

SCOPE. This is the mechanism only. 225 suppressed entries across ten other
classes publish their names today with a caveat tag; that is deliberate and
unchanged. The only behaviour difference is that a non-impersonation entry
whose prose happens to mention the impersonation class is no longer withheld.
"""

import re

import pytest

import app as app_module
import token_label as tl


IMP = tl.IMPERSONATION_REASON
IMP_ISSUER = "rDXwXPcbfupmFRtSye7E5pe1xdpGeNPB3W"
IMP_ID = "0661B4328955F03F243B4C36867E3E5EF42DC92CEFC42D8F"
PROSE_ISSUER = "rLr3ZxWy7owtSwaCH2n7tCauQENrt9MVgU"
PROSE_ID = "0646C2FDD070B520AC6C9275BFDE7814561544CC173BDF20"

# The real shape of LRC's note: a different class, whose prose names the
# impersonation class while explaining the reclass.
PROSE_REASON = (
    "placeholder_unfinished_issuance: an unfinished placeholder, not an "
    "imitation. This REPLACES " + IMP + " recorded earlier on 2026-11-27, "
    "which did not fit."
)
IMP_REASON = (
    IMP + ": the name imitates an established asset or brand. Displaying it "
    "amplifies the imitation."
)

TRUST_SPAN = re.compile(
    r'<span class="(?:trust-label|hero-trust)">\s*\u00b7\s*([^<]+?)\s*</span>')


def _row(issuance_id, issuer, name, ticker, outstanding):
    return {
        "issuance_id": issuance_id, "issuer": issuer, "sequence": 105300733,
        "name": name, "ticker": ticker, "asset_class_raw": "rwa",
        "asset_subclass": "treasury", "classification": "rwa",
        "issuer_name": "Fixture Issuer", "icon": None, "uris": [],
        "additional_info": None, "desc": "fixture", "metadata_present": True,
        "outstanding_amount": outstanding, "maximum_amount": 1000,
        "asset_scale": 0, "transfer_fee": 0, "flags": 68,
        "metadata_error": None, "holders": None,
    }


IMP_ENTRY = {
    "currency_hex": IMP_ID, "currency_display": "\ud55c\uad6d\uc804\ub825",
    "issuer": IMP_ISSUER, "source": "mpt_metadata", "category": "other",
    "name": "\ud55c\uad6d\uc804\ub825", "suppressed": IMP_REASON,
}
PROSE_ENTRY = {
    "currency_hex": PROSE_ID, "currency_display": "LRC",
    "issuer": PROSE_ISSUER, "source": "mpt_metadata", "category": "rwa",
    "name": "Licuido RC Issuance", "desc": "TEXT",
    "suppressed": PROSE_REASON,
}


@pytest.fixture
def surfaces(monkeypatch):
    """Both MPT lookups replaced, so no test depends on live curation data."""
    snap = {
        "ok": True, "node": "fixture", "written_at": 1764000000,
        "issuances": [
            _row(IMP_ID, IMP_ISSUER, "\ud55c\uad6d\uc804\ub825", None, 300),
            _row(PROSE_ID, PROSE_ISSUER, "Licuido RC Issuance", "LRC", 0),
        ],
        "total": 2, "unique_issuers": 2,
        "by_class": {"rwa": 2, "stablecoin": 0, "utility": 0, "other": 0},
        "by_status": {"live": 2, "prepared": 0, "test": 0},
    }
    sup_idx = {(IMP_ID, IMP_ISSUER): IMP_ENTRY,
               (PROSE_ID, PROSE_ISSUER): PROSE_ENTRY}
    monkeypatch.setattr(app_module, "load_mpt_snapshot", lambda: snap)
    monkeypatch.setattr(app_module, "_suppressed_token_index",
                        lambda: sup_idx)
    # Both are suppressed, so the published-names lookup excludes them,
    # exactly as _load_token_names_dict() does in production.
    monkeypatch.setattr(app_module, "_load_token_names_dict", lambda: {})
    app_module.app.config["TESTING"] = True
    if hasattr(app_module, "limiter"):
        app_module.limiter.enabled = False
    return snap


def _body(path):
    with app_module.app.test_client() as c:
        resp = c.get(path)
    assert resp.status_code == 200, (path, resp.status_code)
    html = resp.get_data(as_text=True)
    html = re.sub(r"<style\b.*?</style>", "", html, flags=re.S | re.I)
    return re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)


ALL_SURFACES = [
    ("list", "/mpts"),
    ("detail-imp", f"/mpt/{IMP_ID}"),
    ("detail-prose", f"/mpt/{PROSE_ID}"),
    ("issuer-imp", f"/mpt/issuer/{IMP_ISSUER}"),
    ("issuer-prose", f"/mpt/issuer/{PROSE_ISSUER}"),
]


class TestImpersonationNamesNeverPrint:
    @pytest.mark.parametrize("label,path", ALL_SURFACES)
    def test_the_impersonated_name_is_absent(self, surfaces, label, path):
        """THE guard. The name itself is the harm, so it must appear on no
        MPT surface at all -- list, detail or issuer."""
        html = _body(path)
        assert "\ud55c\uad6d\uc804\ub825" not in html, (
            f"impersonated name printed on {path}")

    def test_the_withheld_placeholder_is_shown_instead(self, surfaces):
        """Absence alone could mean the row vanished. Assert the row is
        PRESENT and carries the placeholder, so this cannot pass for the
        wrong reason."""
        for path in (f"/mpt/{IMP_ID}", f"/mpt/issuer/{IMP_ISSUER}", "/mpts"):
            html = _body(path)
            assert tl.WITHHELD_LABEL in html, path
            assert IMP_ID in html, f"row disappeared entirely on {path}"


class TestProseMentionIsNotWithheld:
    """The behaviour change. A non-impersonation entry whose prose mentions
    the impersonation class keeps publishing its name with its caveat tag."""

    def test_is_withheld_is_false_despite_the_prose(self):
        assert IMP in PROSE_ENTRY["suppressed"], "fixture must contain it"
        assert tl.reason_class(PROSE_ENTRY) == "placeholder_unfinished_issuance"
        assert tl.is_withheld(PROSE_ENTRY) is False
        assert tl.is_withheld(IMP_ENTRY) is True

    @pytest.mark.parametrize("path", ["/mpts", f"/mpt/{PROSE_ID}",
                                      f"/mpt/issuer/{PROSE_ISSUER}"])
    def test_its_name_prints(self, surfaces, path):
        html = _body(path)
        assert "Licuido RC Issuance" in html, (
            f"a non-impersonation name was withheld on {path}")

    def test_it_still_carries_a_caveat_tag(self, surfaces):
        """Publishing the name is only correct because the caveat rides
        with it. Suppressed and untagged would be the worst outcome."""
        html = _body(f"/mpt/{PROSE_ID}")
        h1 = re.search(r"<h1\b.*?</h1>", html, flags=re.S)
        assert h1 and TRUST_SPAN.search(h1.group(0)), (
            "name published with no caveat: " + re.sub(r"\s+", " ", html[:0])
            + (re.sub(r"\s+", " ", h1.group(0)) if h1 else "no h1"))


class TestTheMechanism:
    def test_class_is_read_before_the_first_colon_only(self):
        assert tl.reason_class({"suppressed": "foo: bar: baz"}) == "foo"
        assert tl.reason_class({"suppressed": "   padded  : x"}) == "padded"
        assert tl.reason_class({"suppressed": IMP}) == IMP, "bare class"
        assert tl.reason_class({}) == ""
        assert tl.reason_class(None) == ""

    def test_a_trailing_mention_cannot_withhold(self):
        assert tl.is_withheld({"suppressed": f"other_class: see {IMP}"}) is False

    def test_an_unsuppressed_entry_is_never_withheld(self):
        assert tl.is_withheld({"currency_display": "X"}) is False
        assert tl.is_withheld({"suppressed": ""}) is False

    def test_no_substring_containment_remains_in_the_source(self):
        """Pin the mechanism so a later edit cannot reintroduce the
        substring form, which failed silently and read as correct."""
        src = open("token_label.py", encoding="utf-8").read()
        body = src[src.index("def is_withheld("):]
        body = body[:body.index("\ndef ", 1)]
        assert "reason_class(entry) == IMPERSONATION_REASON" in body
        assert "IMPERSONATION_REASON in " not in body, (
            "substring containment is back in is_withheld")

"""A withheld MPT publishes NO issuer-authored metadata; everyone else's
description prints under an explicit caveat.

These tests assert on RENDERED HTML from the Flask test client. That is the
point of the file, and it is the same lesson as
test_mpt_trust_label_rendered.py: a unit test over ``token_label`` asks the
producer what it computed, which cannot tell a printed field from an
unprinted one.

Background, 2026-11-27. ``is_withheld()`` replaced the NAME slot with
``WITHHELD_LABEL`` for the three impersonation entries decided at anchor
#17 -- and stopped there. The MPT surfaces build their rows from the LEDGER
snapshot, which carries the issuer's own ``desc``, ``issuer_name``, ``icon``
and ``uris`` straight from XLS-89 metadata, and nothing cleared them. So
NXRP rendered ``name withheld - under review`` while still publishing
issuer_name "Network Broker" and an icon URL from the same issuer, on the
same row, to the same reader. Withholding a name and then printing the
author's other free text beside it withholds nothing.

Note there is no ``website`` field. XLS-89 carries site URLs inside the
``uris`` array as elements whose category (``c``) is "website", so that
array is what has to be scrubbed.
"""

import re

import pytest

import app as app_module
import token_label as tl


# Real curated keys, so these tests run against the live curation rather
# than a fixture's opinion of it. Each is pinned by a guard test below.
WITHHELD_ID = "0676F744F3D56B1B35DF9DE297379C38CD0735548B83B30D"   # NXRP
WITHHELD_ISSUER = "rPNG1BgDTcmCidcZDWxLS8mnVkHoJKNZG2"

# CRSIV: suppressed `pending_review`, which is NOT a withholding class, so
# it keeps publishing its name and its description -- and therefore must
# carry the caveat.
CRSIV_ID = "06719B7E21DD2BD4CC5A4D43EB62F664AE7B806B2F177390"
CRSIV_ISSUER = "rhahE2QG6F3kGJxZVxd8hYZG74Zxf6DCY9"
CRSIV_DESC = ("Non-transferable receipt: 1 note = 1 RLUSD of principal "
              "subscribed, held by Credelit for the holder. First-mortgage "
              "loans, diversified.")

# Deliberately absurd sentinels. A real-looking value could pass or fail by
# coinciding with genuine copy elsewhere on the page; these cannot.
FAKE_DESC = "ZZFAKEDESCZZ this description must never reach a reader"
FAKE_ISSUER_NAME = "ZZFAKEISSUERNAMEZZ"
FAKE_ICON = "https://zzfakeiconzz.example/icon.svg"
FAKE_WEBSITE = "https://zzfakewebsitezz.example/vault"

CAVEAT = tl.DESC_CAVEAT_LABEL


def _row(issuance_id, issuer, name, ticker, desc, issuer_name,
         icon, website, outstanding=1000):
    """A snapshot row shaped the way mpt_snapshot.py writes them."""
    return {
        "issuance_id": issuance_id,
        "issuer": issuer,
        "sequence": 108460194,
        "name": name,
        "ticker": ticker,
        "asset_class_raw": "rwa",
        "asset_subclass": "private_credit",
        "classification": "rwa",
        "issuer_name": issuer_name,
        "icon": icon,
        "uris": [{"c": "website", "t": "Vault", "u": website}],
        "additional_info": None,
        "desc": desc,
        "outstanding_amount": outstanding,
        "maximum_amount": 2000000000,
        "asset_scale": 0,
        "transfer_fee": 0,
        "flags": 70,
        "metadata_present": True,
        "metadata_error": None,
        "holders": None,
    }


@pytest.fixture
def fixture_snapshot(monkeypatch):
    """Two rows: one withheld entry handed FAKE metadata, one publishable
    entry with its real description.

    The withheld row is given a description it never had on-ledger, because
    the dangerous case is not "today's data happens to be empty" -- NRLUSD's
    desc is in fact null right now, which would make a weaker test pass for
    free. The question is whether the code WOULD print one, so the fixture
    supplies it.
    """
    snap = {
        "ok": True,
        "node": "fixture",
        "written_at": 1764000000,
        "issuances": [
            _row(WITHHELD_ID, WITHHELD_ISSUER, "Native XRP Yield", "NXRP",
                 FAKE_DESC, FAKE_ISSUER_NAME, FAKE_ICON, FAKE_WEBSITE),
            _row(CRSIV_ID, CRSIV_ISSUER, "Senior Income Vault receipt note",
                 "CRSIV", CRSIV_DESC, "Credelit",
                 "https://yield.credelit.com/icon.svg",
                 "https://yield.credelit.com/vaults/senior-income"),
        ],
        "total": 2,
        "unique_issuers": 2,
        "by_class": {"rwa": 2, "stablecoin": 0, "utility": 0, "other": 0},
        "by_status": {"live": 2, "prepared": 0, "test": 0},
    }
    monkeypatch.setattr(app_module, "load_mpt_snapshot", lambda: snap)
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


class TestTheFixtureStillMatchesTheCuration:
    """If a later owner decision reclassifies these entries, say so in one
    clear sentence instead of failing somewhere confusing downstream."""

    def test_the_withheld_entry_is_still_a_withholding_class(self):
        idx = app_module._suppressed_token_index()
        entry = idx.get((WITHHELD_ID, WITHHELD_ISSUER))
        assert entry is not None, (
            f"{WITHHELD_ID} is no longer a suppressed entry in "
            "token_names.json; this test needs a new withheld fixture")
        assert tl.is_withheld(entry), (
            "fixture entry is no longer withheld -- reason class is now "
            f"{tl.reason_class(entry)!r}; pick another impersonation entry")

    def test_crsiv_is_suppressed_but_NOT_withheld(self):
        idx = app_module._suppressed_token_index()
        entry = idx.get((CRSIV_ID, CRSIV_ISSUER))
        assert entry is not None, "CRSIV is no longer suppressed"
        assert not tl.is_withheld(entry), (
            "CRSIV became a withholding class, so it can no longer stand "
            "for the publish-with-caveat case")


class TestWithheldRowPublishesNoIssuerMetadata:
    """THE regression guard, on all three MPT surfaces."""

    @pytest.mark.parametrize("path_name", ["detail", "list", "issuer"])
    def test_no_fake_metadata_anywhere_on_the_surface(
            self, fixture_snapshot, path_name):
        path = {
            "detail": f"/mpt/{WITHHELD_ID}",
            "list": "/mpts",
            "issuer": f"/mpt/issuer/{WITHHELD_ISSUER}",
        }[path_name]
        html = _body(path)
        leaked = [s for s in (FAKE_DESC, FAKE_ISSUER_NAME,
                              FAKE_ICON, FAKE_WEBSITE) if s in html]
        assert not leaked, (
            f"withheld entry leaked issuer-authored metadata on {path}: "
            f"{leaked}")

    def test_the_withheld_detail_page_prints_no_description_block(
            self, fixture_snapshot):
        """Not just the text -- the labelled block must be absent too, or
        the page would show a caveat introducing nothing."""
        html = _body(f"/mpt/{WITHHELD_ID}")
        assert "desc-caveat" not in html, (
            "withheld detail page rendered a description caveat block")
        assert CAVEAT not in html

    def test_the_withheld_name_is_still_withheld(self, fixture_snapshot):
        """Guards against 'fixing' this by scrubbing metadata while the
        name quietly starts publishing again."""
        html = _body(f"/mpt/{WITHHELD_ID}")
        assert tl.WITHHELD_LABEL in html
        assert "Native XRP Yield" not in html

    def test_the_name_echo_attribute_cannot_republish_the_name(self):
        """templates/mpts.html prints name_as_issued inside a title=
        attribute. For a withheld row that tooltip would hand back the
        exact name WITHHELD_LABEL just removed."""
        row = _row(WITHHELD_ID, WITHHELD_ISSUER, "Native XRP Yield", "NXRP",
                   FAKE_DESC, FAKE_ISSUER_NAME, FAKE_ICON, FAKE_WEBSITE)
        row["name_as_issued"] = "Native XRP Yield undefined"
        cleared = tl.scrub_withheld_metadata(row)
        assert "name_as_issued" in cleared
        assert row["name_as_issued"] is None


class TestPublishableRowCarriesTheCaveat:
    def test_crsiv_prints_its_description_with_the_caveat(
            self, fixture_snapshot):
        html = _body(f"/mpt/{CRSIV_ID}")
        assert CRSIV_DESC in html, "CRSIV lost its description entirely"
        assert CAVEAT in html, (
            "CRSIV published the issuer's description with no caveat")

    def test_the_caveat_sits_DIRECTLY_above_the_paragraph(
            self, fixture_snapshot):
        """Ordering is the requirement, not mere presence. A disclaimer
        underneath is read after the sentence it qualifies, so assert the
        caveat element is immediately followed by the description."""
        html = _body(f"/mpt/{CRSIV_ID}")
        pair = re.search(
            r'<div class="desc-caveat">\s*([^<]+?)\s*</div>\s*'
            r'<p class="desc-body">\s*([^<]+?)\s*</p>',
            html, flags=re.S)
        assert pair, (
            "no adjacent caveat+description pair on the CRSIV page; the "
            "caveat may have drifted away from the paragraph")
        assert pair.group(1) == CAVEAT, pair.group(1)
        assert pair.group(2).startswith("Non-transferable receipt"), \
            pair.group(2)[:80]

    def test_the_exact_caveat_wording(self):
        """Owner-approved string, pinned verbatim."""
        assert tl.DESC_CAVEAT_LABEL == \
            "issuer's own description, unverified"


class TestPrintSitesAndStyling:
    def test_the_caveat_is_muted_italic_and_not_a_badge(self):
        """An MPT cannot reach the `toml` tier, so this stays a caveat. A
        border or background would read as a vouching badge on the surface
        where impersonation risk is highest."""
        css = open("templates/mpt_detail.html", encoding="utf-8").read()
        i = css.index(".desc-caveat {")
        rule = css[i:css.index("}", i)]
        assert "font-style: italic" in rule, rule
        assert "var(--muted)" in rule, rule
        for banned in ("border", "background", "\u2713", "\u2714"):
            assert banned not in rule, (banned, rule)

    def test_the_detail_template_has_a_separate_issuer_name_print_site(self):
        """Reported 2026-11-27: the Issuer card prints mpt.issuer_name as a
        `.addr-label`, structurally separate from the description paragraph
        in the hero. An earlier probe could not tell the two apart because
        both render as muted text. Pin that the second site exists, so the
        scrub above is understood to cover two print sites on this page and
        not one."""
        html = open("templates/mpt_detail.html", encoding="utf-8").read()
        assert html.count("mpt.issuer_name") >= 1, (
            "the Issuer card no longer prints issuer_name")
        assert 'class="addr-label"' in html

    def test_the_chokepoint_records_what_it_cleared(self):
        """The scrub happens in ONE place. Pin that app.py still calls it,
        so a refactor cannot quietly drop the call and leave the templates
        reading populated fields."""
        src = open("app.py", encoding="utf-8").read()
        assert "scrub_withheld_metadata" in src, (
            "app.py no longer scrubs withheld metadata at the chokepoint")

    def test_every_withheld_field_is_actually_cleared(self):
        row = _row(WITHHELD_ID, WITHHELD_ISSUER, "n", "T", FAKE_DESC,
                   FAKE_ISSUER_NAME, FAKE_ICON, FAKE_WEBSITE)
        cleared = tl.scrub_withheld_metadata(row)
        for field in tl.WITHHELD_METADATA_FIELDS:
            assert row[field] is None, field
            assert field in cleared, field

    def test_scrub_is_idempotent_and_safe_on_an_empty_row(self):
        assert tl.scrub_withheld_metadata({}) == []
        assert tl.scrub_withheld_metadata(None) == []
        row = _row(WITHHELD_ID, WITHHELD_ISSUER, "n", "T", FAKE_DESC,
                   FAKE_ISSUER_NAME, FAKE_ICON, FAKE_WEBSITE)
        tl.scrub_withheld_metadata(row)
        assert tl.scrub_withheld_metadata(row) == []

"""Every MPT name on a public surface renders WITH its trust caveat.

These tests assert on RENDERED HTML from the Flask test client, not on
``token_label`` return values. That distinction is the whole point of the
file.

Background, 2026-11-27. ``_enrich_mpt_rows`` has attached a ``trust_label``
to every MPT row since 2026-11-22 -- tier wording, else the source tag, else
``LABEL_UNVERIFIED`` -- and ``app.py`` even carries the comment "Rule: no
ledger-sourced name renders bare on this page". That rule was false in
rendered output: only ``templates/mpt_issuer.html`` ever read the field, so
``/mpts`` published 169 untiered names with no caveat at all and the
``/mpt/<id>`` heading did the same, while ``/mpt/issuer/<addr>`` showed the
tag for the very same issuance.

Nothing failed, because every existing check asked the producer what it
computed instead of asking the page what it printed. A unit test over
``label_for``/``display_labelled`` cannot tell a printed label from an
unprinted one, so these tests fetch the markup and look for the span.
"""

import re

import pytest

import app as app_module
import token_label as tl


LICUIDO_ISSUER = "rLr3ZxWy7owtSwaCH2n7tCauQENrt9MVgU"
LMI_ID = "0646C2F7D070B520AC6C9275BFDE7814561544CC173BDF20"
# Deliberately absent from token_names.json: a ledger-only issuance, the
# class that reaches /mpts with no curated entry of any kind.
LEDGER_ONLY_ID = "0646C2FFD070B520AC6C9275BFDE7814561544CC173BDF20"

TRUST_SPAN = re.compile(
    r'<span class="(?:trust-label|hero-trust)">\s*\u00b7\s*([^<]+?)\s*</span>')


def _row(issuance_id, issuer, name, ticker, outstanding):
    """A snapshot row shaped the way mpt_snapshot.py writes them."""
    return {
        "issuance_id": issuance_id,
        "issuer": issuer,
        "sequence": 105300727,
        "name": name,
        "ticker": ticker,
        "asset_class_raw": "rwa",
        "asset_subclass": "treasury",
        "classification": "rwa",
        "issuer_name": "Licuido Platform Issuance",
        "icon": None,
        "uris": [],
        "additional_info": None,
        "desc": "fixture row",
        "outstanding_amount": outstanding,
        "maximum_amount": 100000,
        "asset_scale": 0,
        "transfer_fee": 0,
        "flags": 68,
        "metadata_present": True,
        "metadata_error": None,
        "holders": None,
    }


@pytest.fixture
def fixture_snapshot(monkeypatch):
    """Point every MPT surface at two rows we control.

    Row 1 is LMI, which HAS a token_names entry and is untiered, so its
    caveat must come from the source label. Row 2 has no token_names entry
    at all, so its caveat can only come from the route's LABEL_UNVERIFIED
    fallback. Between them they cover both ways an untiered name can reach
    a reader.
    """
    snap = {
        "ok": True,
        "node": "fixture",
        "written_at": 1764000000,
        "issuances": [
            _row(LMI_ID, LICUIDO_ISSUER, "Licuido MPT Issuance", "LMI", 28),
            _row(LEDGER_ONLY_ID, LICUIDO_ISSUER,
                 "Licuido Test Preprod2", "LTP", 60),
        ],
        "total": 2,
        "unique_issuers": 1,
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


def _name_rows(html):
    """Rows that publish a name, i.e. not the withheld placeholder."""
    out = []
    for tr in re.findall(r"<tr\b.*?</tr>", html, flags=re.S):
        if "name-link" not in tr:
            continue
        if tl.WITHHELD_LABEL in tr:
            continue
        out.append(tr)
    return out


class TestMptListRendersTheCaveat:
    def test_every_published_name_on_mpts_carries_a_trust_label(
            self, fixture_snapshot):
        """THE regression guard. Fails if any published name renders bare.

        This is the assertion that was missing: it reads the page, not the
        producer. Deleting the span from templates/mpts.html turns it red.
        """
        html = _body("/mpts")
        rows = _name_rows(html)
        assert rows, "fixture produced no published-name rows to check"
        bare = [re.sub(r"\s+", " ", r)[:160]
                for r in rows if not TRUST_SPAN.search(r)]
        assert not bare, (
            f"{len(bare)} published MPT name(s) rendered with NO trust-label "
            f"on /mpts: {bare}")

    def test_the_untiered_entry_shows_the_short_source_tag(
            self, fixture_snapshot):
        """LMI is untiered with a token_names entry, so the caveat is the
        SHORT source tag -- the same string /mpt/issuer/<addr> shows."""
        html = _body("/mpts")
        row = [r for r in _name_rows(html) if LMI_ID in r]
        assert len(row) == 1, "expected exactly one LMI row"
        m = TRUST_SPAN.search(row[0])
        assert m, f"LMI rendered bare: {re.sub(r'\\s+', ' ', row[0])[:200]}"
        assert m.group(1) == tl.LABEL_UNVERIFIED, m.group(1)

    def test_a_ledger_only_row_still_gets_a_caveat(self, fixture_snapshot):
        """An issuance with no token_names entry cannot carry a tier or a
        source bucket, so only the route's fallback can label it. It must
        still never render bare."""
        html = _body("/mpts")
        row = [r for r in _name_rows(html) if LEDGER_ONLY_ID in r]
        assert len(row) == 1, "expected exactly one ledger-only row"
        m = TRUST_SPAN.search(row[0])
        assert m, "a ledger-only name rendered with no caveat"
        assert m.group(1) == tl.LABEL_UNVERIFIED, m.group(1)


class TestMptDetailRendersTheCaveat:
    def test_detail_heading_carries_the_trust_label(self, fixture_snapshot):
        html = _body(f"/mpt/{LMI_ID}")
        h1 = re.search(r"<h1\b.*?</h1>", html, flags=re.S)
        assert h1, "no <h1> on the MPT detail page"
        m = TRUST_SPAN.search(h1.group(0))
        assert m, (
            "MPT detail heading published the name with no caveat: "
            + re.sub(r"\s+", " ", h1.group(0))[:200])
        assert m.group(1) == tl.LABEL_UNVERIFIED, m.group(1)

    def test_ledger_only_detail_heading_also_carries_it(
            self, fixture_snapshot):
        html = _body(f"/mpt/{LEDGER_ONLY_ID}")
        h1 = re.search(r"<h1\b.*?</h1>", html, flags=re.S)
        assert h1 and TRUST_SPAN.search(h1.group(0)), (
            "ledger-only detail heading rendered bare")


class TestTheThreeSurfacesAgree:
    def test_list_detail_and_issuer_show_the_same_tag_for_one_issuance(
            self, fixture_snapshot):
        """The defect was that they DISAGREED: the issuer page tagged an
        issuance while the list and detail pages published it bare. Pin the
        agreement so a future template can't drift again.
        """
        tags = {}
        for label, path in (("list", "/mpts"),
                            ("detail", f"/mpt/{LMI_ID}"),
                            ("issuer", f"/mpt/issuer/{LICUIDO_ISSUER}")):
            html = _body(path)
            scope = html
            if label == "detail":
                scope = re.search(r"<h1\b.*?</h1>", html, flags=re.S).group(0)
            else:
                hit = [r for r in _name_rows(html) if LMI_ID in r]
                assert len(hit) == 1, (label, len(hit))
                scope = hit[0]
            m = TRUST_SPAN.search(scope)
            assert m, f"{label} surface rendered LMI with no caveat"
            tags[label] = m.group(1)
        assert len(set(tags.values())) == 1, tags


class TestPrintSitesExist:
    """A template that stops reading the field is the whole failure mode,
    so pin the print sites by name as well as by rendered output. The
    rendered assertions above are primary; these name the file to edit when
    one goes red."""

    def test_mpts_template_reads_trust_label(self):
        html = open("templates/mpts.html", encoding="utf-8").read()
        assert html.count("r.trust_label") >= 1, (
            "templates/mpts.html no longer prints trust_label: dead field")

    def test_mpt_detail_template_reads_trust_label(self):
        html = open("templates/mpt_detail.html", encoding="utf-8").read()
        assert html.count("mpt.trust_label") >= 1, (
            "templates/mpt_detail.html no longer prints trust_label")

    def test_the_caveat_carries_no_tick_or_badge(self):
        """An MPT cannot reach the `toml` tier, so issuer level is its
        ceiling and the wording stays a caveat. A tick here would vouch for
        a self-declared name on the surface where impersonation risk is
        highest."""
        for path, cls in (("templates/mpts.html", ".mpt-table .trust-label"),
                          ("templates/mpt_detail.html", ".hero-trust")):
            css = open(path, encoding="utf-8").read()
            i = css.index(cls)
            rule = css[i:css.index("}", i)]
            for banned in ("border", "background", "\u2713", "\u2714"):
                assert banned not in rule, (path, banned, rule)

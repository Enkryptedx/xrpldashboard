"""Audit quick fixes (Charlie 2026-10-08). Hermetic: no DB, no network.

1. /mpts "Precious MPT - undefined": the issuer's own on-chain
   MPTokenMetadata name is literally that string (JS template leak,
   verified at ledger 107,515,071). Display trims the artifact, keeps the
   raw value in name_as_issued, and the row shows a "name trimmed" tag.
2. Broken links: /changes "signed snapshots" -> /snapshots/ (was
   /.well-known/snapshots/, 404); /coverage AccountRoot -> /check (was
   /wallet, no index route) incl. a read-side remap for stale DB rows.
3. Phone sideways scroll: every <table> on /coverage, /methodology, /rwa,
   /regulation and the dated /amendments permalink is wrapped in
   <div class="table-scroll"> with overflow-x:auto.
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import mpt_data  # noqa: E402


# ---------------------------------------------------------------- item 1
def test_strip_js_undefined_pure():
    f = mpt_data.strip_js_undefined
    assert f("Precious MPT - undefined") == ("Precious MPT", True)
    assert f("Precious MPT undefined") == ("Precious MPT", True)
    assert f("Precious MPT \u2013 undefined") == ("Precious MPT", True)
    assert f("undefined") == (None, True)
    assert f("- undefined")[1] is True
    assert f("Precious MPT - 1") == ("Precious MPT - 1", False)
    assert f("Undefined Labs Token") == ("Undefined Labs Token", False)
    assert f("") == (None, False)
    assert f(None) == (None, False)


def test_enrich_trims_display_name_and_keeps_raw(monkeypatch):
    import app
    monkeypatch.setattr(app.db, "read_account_labels", lambda addrs: {
        "rnRbuxseqEHbxXgaBZJu7PAMQKmVAqp9RM": {"name": "MPT Issuer (Precious MPT - undefined)"},
    })
    data = {"issuances": [
        {"issuance_id": "056762A93087A14B122055FA030F9EE6F74F87116559C83D",
         "issuer": "rnRbuxseqEHbxXgaBZJu7PAMQKmVAqp9RM",
         "name": "Precious MPT - undefined", "ticker": None, "outstanding_amount": 0,
         "maximum_amount": 50000000, "asset_scale": 2, "classification": "other"},
        {"issuance_id": "AA", "issuer": "rOther", "name": "Precious MPT - 1", "ticker": "PM1",
         "outstanding_amount": 5, "maximum_amount": 10, "asset_scale": 0, "classification": "other"},
    ]}
    out = app._enrich_mpt_rows(data)
    rows = {r["issuance_id"]: r for r in out["issuances"]}
    pm = rows["056762A93087A14B122055FA030F9EE6F74F87116559C83D"]
    assert pm["name"] == "Precious MPT"
    assert pm["name_as_issued"] == "Precious MPT - undefined"
    assert pm["issuer_name"] == "MPT Issuer (Precious MPT)"
    assert pm["issuer_name_as_issued"] == "MPT Issuer (Precious MPT - undefined)"
    other = rows["AA"]
    assert other["name"] == "Precious MPT - 1" and "name_as_issued" not in other
    # original dict untouched
    assert data["issuances"][0]["name"] == "Precious MPT - undefined"


def test_mpts_template_shows_trimmed_name_and_tag():
    import app
    from flask import render_template
    rows = [{"issuance_id": "056762A9", "issuer": "rnRbuxseqEHbxXgaBZJu7PAMQKmVAqp9RM",
             "name": "Precious MPT", "name_as_issued": "Precious MPT - undefined", "ticker": None,
             "status": "prepared", "classification": "other", "normalized_outstanding": 0,
             "holders": None, "issuer_name": "MPT Issuer (Precious MPT)"}]
    data = {"issuances": rows, "total": 1, "by_class": {"rwa": 0, "stablecoin": 0, "utility": 0, "other": 1},
            "status_count": {"live": 0, "prepared": 1, "test": 0}}
    with app.app.test_request_context("/mpts"):
        html = render_template(
            "mpts.html", data=data, outstanding_min=0, unnamed_total=0, low_supply_total=0,
            combined_hidden_total=0, supply_chip_label="", top_holder_count=0, sort_mode="supply",
            walker_health=None)
    assert '<span class="name">Precious MPT</span>' in html
    assert "Precious MPT - undefined</span>" not in html
    assert 'title="On-chain metadata name: Precious MPT - undefined">name trimmed</span>' in html


# ---------------------------------------------------------------- item 2
def test_changes_signed_snapshots_link_points_at_existing_route():
    src = open(os.path.join(HERE, "templates", "changes.html"), encoding="utf-8").read()
    assert '<a href="/snapshots/">signed snapshots</a>' in src
    assert "/.well-known/snapshots/\"" not in src
    import app
    rules = {r.rule for r in app.app.url_map.iter_rules()}
    assert "/snapshots/" in rules
    assert "/.well-known/snapshots/" not in rules


def test_coverage_accountroot_seed_and_remap():
    import seed_coverage_labels as seed
    import db
    import app
    row = next(r for r in seed.ENTRY_LABELS if r[0] == "AccountRoot")
    assert row[3] == "/check"
    rules = {r.rule for r in app.app.url_map.iter_rules()}
    assert "/check" in rules and "/wallet" not in rules
    assert db._COVERAGE_LINK_REMAP == {"/wallet": "/check"}


def test_read_coverage_labels_remaps_stale_wallet_link(monkeypatch):
    import db

    class Cur:
        def execute(self, *a): pass
        def fetchall(self):
            return [("entry", "AccountRoot", "Account", "Root object", "/wallet", 1),
                    ("entry", "RippleState", "Trust Line State", "x", "/tokens", 1)]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class Conn:
        def cursor(self): return Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(db, "pg_connect", lambda: Conn())
    monkeypatch.setattr(db, "pg_available", lambda: True)
    out = db.read_coverage_labels()
    assert out[("entry", "AccountRoot")]["linked_page"] == "/check"
    assert out[("entry", "RippleState")]["linked_page"] == "/tokens"


# ---------------------------------------------------------------- item 3
def test_every_table_on_the_five_pages_is_in_a_scroll_box():
    for name in ("coverage_register", "methodology", "rwa", "regulation", "amendments_permalink"):
        src = open(os.path.join(HERE, "templates", f"{name}.html"), encoding="utf-8").read()
        opens = re.findall(r"<table\b", src)
        assert opens, name
        # each <table is immediately preceded by the wrapper open
        for m in re.finditer(r"<table\b", src):
            before = src[max(0, m.start() - 80):m.start()]
            assert '<div class="table-scroll">' in before, (name, before)
        assert src.count('<div class="table-scroll">') == len(opens), name
        assert src.count("</table>") == len(opens), name
        assert ".table-scroll { overflow-x: auto;" in src, name

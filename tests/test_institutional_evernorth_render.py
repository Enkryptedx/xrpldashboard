"""/institutional renders the Evernorth card correctly (2026-10-10).

build_card is pinned as a pure function elsewhere; this file drives the
real route and asserts on the HTML a reader actually receives. That is a
different risk: a template can reach for a field that does not exist, a
later edit can print a third-party name, or the card can take the page
down when Postgres is unavailable.

What this pins:

1. The section exists, with its heading, in the right place — after
   "Institutional capital is flowing in".
2. Every filing figure the owner specified is on the page, and each
   filing fact links to a real sec.gov document.
3. The owner's live line renders VERBATIM.
4. Both approved group headings render verbatim, each linked to its own
   source.
5. No third-party wallet name ("Custody A", "Staging", ...) appears
   anywhere in the delivered HTML, and no full wallet address is printed
   as visible text — only short forms, with the full address confined to
   href values.
6. With the card's data layer failing, /institutional still returns 200
   and still shows the filing facts. The card must never take the page
   down, and the filing half must not depend on our pipeline.

Hermetic: the DB readers are stubbed with raising=True so a rename fails
loudly instead of silently reaching the live database.
"""
import os
import re
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import app as app_module  # noqa: E402
import db  # noqa: E402
import institutional_treasuries as T  # noqa: E402

BANNED_NAMES = ["Custody A", "Custody B", "Custody C", "Custody D",
                "Custody E", "Custody F", "Custody G", "Custody H",
                "Custody I", "Custody J", "Staging"]

PER_WALLET = 36_405_879.0


@pytest.fixture
def client():
    return app_module.app.test_client()


def _stub_full_read(monkeypatch, moves=None, history=None):
    balances = {addr: PER_WALLET for addr, _ in T.WALLETS}
    monkeypatch.setattr(
        db, "read_evernorth_latest_snapshot",
        lambda: {"date": "2026-10-10", "taken_at": 1791600000,
                 "total_xrp": PER_WALLET * len(T.WALLETS),
                 "readable_count": len(T.WALLETS),
                 "wallet_count": len(T.WALLETS), "balances": balances},
        raising=True)
    monkeypatch.setattr(db, "read_evernorth_moves",
                        lambda addrs, limit=10: list(moves or []), raising=True)
    monkeypatch.setattr(db, "read_evernorth_daily_totals",
                        lambda limit=30: list(history or []), raising=True)


def _html(client):
    r = client.get("/institutional")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def _visible_text(html):
    """Tag-stripped text. Full addresses legitimately live in href values,
    so a 'not shown to the reader' assertion must run on text, not raw
    HTML (the inline <style> block is stripped out too)."""
    without_style = re.sub(r"(?is)<style.*?</style>", " ", html)
    return re.sub(r"<[^>]+>", " ", without_style)


# ── 1. the section ──────────────────────────────────────────────────────
def test_section_heading_present(monkeypatch, client):
    _stub_full_read(monkeypatch)
    assert "Public companies holding XRP" in _html(client)


def test_section_sits_after_capital_is_flowing_in(monkeypatch, client):
    _stub_full_read(monkeypatch)
    html = _html(client)
    flowing = html.find("Institutional capital is flowing in")
    section = html.find("Public companies holding XRP")
    europe = html.find("Regulated rails are advancing in Europe")
    assert -1 < flowing < section < europe


# ── 2. filing figures + citations ───────────────────────────────────────
@pytest.mark.parametrize("figure", [
    "473,276,430",      # at least ... XRP at closing (424B3)
    "$2.36609",         # Signing XRP Price (8-K)
    "$1.43069",         # Closing XRP Price (8-K)
    "$195.39 million",  # aggregate redemption amount (8-K)
    "October 9, 2026",  # Closing Date (8-K)
    "XRPN",             # Nasdaq ticker (8-K, 12(b) table)
    "BitGo",            # custodian (424B3)
])
def test_filing_figure_renders(monkeypatch, client, figure):
    _stub_full_read(monkeypatch)
    assert figure in _html(client)


def test_every_filing_fact_links_to_a_sec_document(monkeypatch, client):
    _stub_full_read(monkeypatch)
    html = _html(client)
    for fact in T.EVERNORTH["facts"]:
        assert fact["url"].startswith("https://www.sec.gov/Archives/edgar/")
        assert fact["url"] in html, f"{fact['label']} lost its citation link"


# ── 3. the verbatim live line ───────────────────────────────────────────
def test_live_line_renders_verbatim(monkeypatch, client):
    _stub_full_read(monkeypatch)
    expected = (
        "These 13 wallets total 473,276,427 XRP; "
        "the filing says at least 473,276,430 XRP at closing."
    )
    assert expected in _visible_text(_html(client))


# ── 4. group headings ───────────────────────────────────────────────────
def test_both_group_headings_render_verbatim(monkeypatch, client):
    _stub_full_read(monkeypatch)
    text = _visible_text(_html(client))
    assert "labeled Evernorth by XRPScan \u2014 not confirmed by Evernorth" in text
    assert ("inferred by xrp-insights from amounts matching the filing "
            "\u2014 not confirmed by Evernorth") in text


def test_each_group_links_to_its_own_source(monkeypatch, client):
    _stub_full_read(monkeypatch)
    html = _html(client)
    assert "https://xrpscan.com" in html
    assert "https://xrp-insights.com/xrp-radar/evernorth" in html


def test_filing_confirmed_tier_never_rendered(monkeypatch, client):
    """No filing names any address; the strongest claim must be unreachable."""
    _stub_full_read(monkeypatch)
    assert "confirmed by Evernorth filing" not in _visible_text(_html(client))


# ── 5. no third-party names, no full addresses as text ──────────────────
def test_no_third_party_wallet_name_in_html(monkeypatch, client):
    _stub_full_read(monkeypatch)
    html = _html(client)
    for name in BANNED_NAMES:
        assert name not in html, f"{name!r} leaked onto the page"


def test_wallets_render_short_not_full(monkeypatch, client):
    _stub_full_read(monkeypatch)
    text = _visible_text(_html(client))
    for addr, _n in T.WALLETS:
        assert addr not in text, "a full address must not be visible text"
        assert T.short_address(addr) in text


def test_full_address_still_reachable_via_explorer_link(monkeypatch, client):
    _stub_full_read(monkeypatch)
    html = _html(client)
    for addr, _n in T.WALLETS:
        assert f"https://xrpscan.com/account/{addr}" in html


# ── 6. degradation ──────────────────────────────────────────────────────
def test_page_survives_the_data_layer_failing(monkeypatch, client):
    def boom(*a, **k):
        raise RuntimeError("pg down")
    monkeypatch.setattr(db, "read_evernorth_latest_snapshot", boom, raising=True)
    monkeypatch.setattr(db, "read_evernorth_moves", boom, raising=True)
    monkeypatch.setattr(db, "read_evernorth_daily_totals", boom, raising=True)
    html = _html(client)
    # Filing facts do not depend on our pipeline.
    assert "473,276,430" in html
    assert "Public companies holding XRP" in html
    # No total is invented from nothing.
    assert "These 13 wallets total" not in _visible_text(html)


def test_no_snapshot_renders_facts_without_a_total(monkeypatch, client):
    monkeypatch.setattr(db, "read_evernorth_latest_snapshot",
                        lambda: None, raising=True)
    monkeypatch.setattr(db, "read_evernorth_moves",
                        lambda addrs, limit=10: [], raising=True)
    monkeypatch.setattr(db, "read_evernorth_daily_totals",
                        lambda limit=30: [], raising=True)
    text = _visible_text(_html(client))
    assert "$2.36609" in text
    assert "These 13 wallets total" not in text


def test_partial_read_is_disclosed(monkeypatch, client):
    balances = {addr: PER_WALLET for addr, _ in T.WALLETS}
    balances[T.WALLETS[0][0]] = None
    monkeypatch.setattr(
        db, "read_evernorth_latest_snapshot",
        lambda: {"date": "2026-10-10", "taken_at": 1, "total_xrp": None,
                 "readable_count": 12, "wallet_count": 13,
                 "balances": balances}, raising=True)
    monkeypatch.setattr(db, "read_evernorth_moves",
                        lambda addrs, limit=10: [], raising=True)
    monkeypatch.setattr(db, "read_evernorth_daily_totals",
                        lambda limit=30: [], raising=True)
    text = _visible_text(_html(client))
    assert "12/13" in text
    assert "not readable" in text

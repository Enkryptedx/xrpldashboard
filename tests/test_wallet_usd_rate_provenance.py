"""Every /wallet USD figure must name the rate it used and when.

THE BUG (reported 2026-10-30). `price_oracle.value_amount_usd()` takes an
XRPL Amount and nothing else -- there is no timestamp parameter anywhere in
the chain -- so a payment that settled days ago is converted at whatever
the anchor reads at RENDER time. Two renders minutes apart legitimately
disagree: the same 185,017,485.5 XRP transfer read $276.9M on one page and
$232.1M on another, i.e. $1.4966/XRP vs $1.2545/XRP. Neither figure was
wrong when it was computed; what was wrong is that the page let a reader
believe the dollar figure was a property of the transfer.

WHY NOT PRICE AT THE TX's OWN CLOSE TIME. XRP/USD is never persisted:
`token_prices` stores token->XRP history and `oracles_snapshot` is
replace-on-write, so no XRP/USD history exists to look up, and the past
cannot be backfilled. Until an xrp_usd_history table exists, the honest
fix is for each figure to state its rate and that rate's sample time --
the same pattern the nav price chip already uses.

Hermetic: the oracle is monkeypatched, node dicts are built from literals.
No node, no DB, no network, no real price fetch.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import price_oracle  # noqa: E402
import wallet_data as wd  # noqa: E402


# The two rates from the incident, so the fixture is the reported bug.
RATE_HIGH = 1.4966      # 185,017,485.5 XRP -> ~$276.9M
RATE_LOW = 1.2545       # same transfer    -> ~$232.1M
BIG_XRP = 185_017_485.5


# ── provenance helper ────────────────────────────────────────────────────

def _patch_oracle(monkeypatch, rate, fetched_at):
    monkeypatch.setattr(price_oracle, "xrp_usd", lambda: rate)
    monkeypatch.setattr(price_oracle, "xrp_usd_fetched_at", lambda: fetched_at)


def test_provenance_reports_rate_and_formats_utc_hhmm(monkeypatch):
    # 2026-10-30 11:22:33 UTC
    ts = int(time.mktime(time.strptime("2026-10-30 11:22:33", "%Y-%m-%d %H:%M:%S"))
             - time.timezone)
    _patch_oracle(monkeypatch, RATE_HIGH, ts)
    prov = wd._usd_rate_provenance()
    assert prov["rate"] == RATE_HIGH
    assert prov["fetched_at"] == ts
    assert prov["as_of_hhmm"] == time.strftime("%H:%M", time.gmtime(ts))
    assert len(prov["as_of_hhmm"]) == 5 and ":" in prov["as_of_hhmm"]


def test_provenance_survives_an_unavailable_oracle(monkeypatch):
    """Fail-closed: xrp_usd() returns None during an outage or a failed
    divergence check. No rate means no label, not a crash and not a 0."""
    _patch_oracle(monkeypatch, None, None)
    prov = wd._usd_rate_provenance()
    assert prov == {"rate": None, "fetched_at": None, "as_of_hhmm": None}


def test_provenance_handles_a_rate_with_no_timestamp(monkeypatch):
    _patch_oracle(monkeypatch, RATE_HIGH, None)
    prov = wd._usd_rate_provenance()
    assert prov["rate"] == RATE_HIGH
    assert prov["as_of_hhmm"] is None


def test_provenance_never_raises_when_the_oracle_explodes(monkeypatch):
    def boom():
        raise RuntimeError("oracle down")
    monkeypatch.setattr(price_oracle, "xrp_usd", boom)
    prov = wd._usd_rate_provenance()
    assert prov["rate"] is None


# ── the rate rides on the node dicts ────────────────────────────────────

def _counterparties(usd_in=0.0, usd_out=0.0, drops_in=0):
    return [("rCPaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", {
        "tx_count": 1,
        "in_tx_count": 1 if usd_in or drops_in else 0,
        "out_tx_count": 1 if usd_out else 0,
        "volume_in_drops": drops_in,
        "volume_out_drops": 0,
        "volume_in_usd": usd_in,
        "volume_out_usd": usd_out,
        "first_seen": int(time.time()) - 86400,
        "last_seen": int(time.time()) - 3600,
        "tokens_in": {},
        "tokens_out": {},
    })]


def test_node_carries_the_rate_and_timestamp():
    nodes = wd._layout_nodes(
        _counterparties(usd_in=BIG_XRP * RATE_HIGH, drops_in=185_017_485_500_000),
        11, usd_rate=RATE_HIGH, usd_as_of="11:22")
    assert len(nodes) == 1
    assert nodes[0]["usdRate"] == RATE_HIGH
    assert nodes[0]["usdAsOf"] == "11:22"


def test_detail_card_carries_the_rate_and_timestamp():
    nodes = wd._layout_nodes(
        _counterparties(usd_in=BIG_XRP * RATE_HIGH, drops_in=185_017_485_500_000),
        11, usd_rate=RATE_HIGH, usd_as_of="11:22")
    detail = nodes[0]["detail"]
    assert detail["usdRate"] == RATE_HIGH
    assert detail["usdAsOf"] == "11:22"
    # the figure it qualifies is present too, or the label qualifies nothing
    assert detail["usdIn"] > 0


def test_no_rate_attached_when_there_is_no_usd_figure():
    """A counterparty with no priced flow must not carry a rate -- a label
    on a figure that does not exist is noise."""
    nodes = wd._layout_nodes(
        _counterparties(drops_in=5_000_000), 11,
        usd_rate=RATE_HIGH, usd_as_of="11:22")
    assert nodes[0]["usdIn"] is None
    assert nodes[0]["usdRate"] is None
    assert nodes[0]["usdAsOf"] is None
    assert "usdRate" not in nodes[0]["detail"]


def test_no_rate_attached_when_the_oracle_had_none():
    nodes = wd._layout_nodes(
        _counterparties(usd_in=100.0), 11, usd_rate=None, usd_as_of=None)
    assert nodes[0]["usdRate"] is None
    assert "usdRate" not in nodes[0]["detail"]


def test_layout_nodes_still_works_without_the_new_arguments():
    """Back-compat: the provenance args are optional, so existing callers
    and tests keep working."""
    nodes = wd._layout_nodes(_counterparties(usd_in=100.0), 11)
    assert nodes[0]["usdIn"] == 100.0
    assert nodes[0]["usdRate"] is None


def test_every_node_in_one_render_names_the_same_rate():
    """One render, one anchor sample -- two figures on the same page must
    never cite different rates, which is the whole incident."""
    cps = []
    for i, usd in enumerate([500.0, 1000.0, 2500.0]):
        (addr, rec), = _counterparties(usd_in=usd)
        cps.append((f"rCP{i}aaaaaaaaaaaaaaaaaaaaaaaaaaaaa", dict(rec)))
    nodes = wd._layout_nodes(cps, 11, usd_rate=RATE_LOW, usd_as_of="09:04")
    rates = {n["usdRate"] for n in nodes}
    stamps = {n["usdAsOf"] for n in nodes}
    assert rates == {RATE_LOW}
    assert stamps == {"09:04"}


def test_the_two_incident_rates_produce_the_two_reported_figures():
    """Guard the premise: these rates really are what made one transfer
    read $276.9M and $232.1M, so the fixture is the reported bug."""
    assert round(BIG_XRP * RATE_HIGH / 1e6, 1) == 276.9
    assert round(BIG_XRP * RATE_LOW / 1e6, 1) == 232.1


# ── the envelope ─────────────────────────────────────────────────────────

def test_error_envelope_carries_the_keys():
    """Shape consistency: a template reading data.usd_rate must not hit an
    undefined on the error/empty path.

    `fetch_wallet_data` is a thin wrapper that owns the fallback
    collector; the envelope (and its error stub) is built in
    `_fetch_wallet_data_impl`, which is the function to introspect.
    """
    import inspect
    src = inspect.getsource(wd._fetch_wallet_data_impl)
    assert '"usd_rate": None, "usd_rate_as_of_hhmm": None' in src, (
        "error/empty envelope is missing the provenance keys")
    assert '"usd_rate": usd_prov["rate"]' in src, (
        "normal envelope is missing the provenance keys")


# ── the rendered labels ──────────────────────────────────────────────────

def _wallet_html():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "templates", "wallet.html"), encoding="utf-8") as f:
        return f.read()


def test_graph_caption_states_the_rate_and_as_of():
    src = _wallet_html()
    assert "{{ _('Dollar figures on this graph are a render-time conversion at') }}" in src
    assert '${{ "%.4f"|format(data.usd_rate) }}/XRP' in src
    assert "{{ _('as of') }} {{ data.usd_rate_as_of_hhmm }} UTC" in src
    assert "{{ _('not the rate at the time of each transfer.') }}" in src


def test_caption_is_gated_on_having_a_rate():
    """No rate -> no caption, rather than a caption reading '$None/XRP'."""
    src = _wallet_html()
    assert "{% if data.usd_rate and data.nodes | length > 0 %}" in src


def test_node_label_carries_the_provenance_tooltip():
    src = _wallet_html()
    assert "if ((usdInTxt || usdOutTxt) && node.usdRate) {" in src
    assert "'Dollar figures are a render-time conversion at $'" in src
    assert "Number(node.usdRate).toFixed(4) + '/XRP'" in src
    assert "node.usdAsOf ? ', as of ' + node.usdAsOf + ' UTC' : ''" in src


def test_detail_card_has_a_usd_priced_at_tile():
    src = _wallet_html()
    assert "['USD priced at', usdPricedAt]," in src
    assert "usdPricedAt = '$' + Number(d.usdRate).toFixed(4) + '/XRP'" in src
    assert "d.usdAsOf ? ' \\u00b7 as of ' + d.usdAsOf + ' UTC' : ''" in src


def test_priced_at_tile_is_gated_on_a_figure_and_a_rate():
    src = _wallet_html()
    assert "if ((d.usdIn || d.usdOut) && d.usdRate) {" in src


def test_new_gettext_strings_contain_no_percent_sign():
    """A literal % inside _() is a known /wallet render-killer: the format
    pass treats it as a placeholder. The rate is formatted OUTSIDE _().
    """
    src = _wallet_html()
    for needle in (
        "Dollar figures on this graph are a render-time conversion at",
        "not the rate at the time of each transfer.",
    ):
        assert needle in src
        assert "%" not in needle


def test_template_still_parses_as_jinja():
    from jinja2 import Environment, FileSystemLoader
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = Environment(loader=FileSystemLoader(os.path.join(here, "templates")),
                      extensions=["jinja2.ext.i18n"])
    env.install_null_translations(newstyle=False)
    env.get_template("wallet.html")

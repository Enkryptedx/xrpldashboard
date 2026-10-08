"""/amendments roll-call table: an already-enabled amendment keeps its name
and shows state 'enabled' (Charlie 2026-10-08). Drives the REAL route +
template with fakes; no DB, no network."""
from __future__ import annotations
import datetime as dt, os, re, sys
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, HERE)
import app as app_mod  # noqa: E402
import roll_call_card as C  # noqa: E402
from tests.test_roll_call_card import _round, PD, OTHER, UTC  # noqa: E402


def test_real_route_rollcall_enabled_row(monkeypatch):
    state = {
        "ok": True, "enabled_count": 95, "in_flight_count": 12, "ledger_index": 107524991,
        "recognized_enabled": [{"hash": PD, "name": "PermissionDelegationV1_1"}],
        "unrecognized_enabled": [], "unrecognized_enabled_count": 0,
        "in_flight": [{"hash": OTHER, "name": "fixBatchV1_2"}], "superseded": [],
        "majorities": [{"hash": OTHER, "name": "fixBatchV1_2", "majority_reached_iso": "2026-09-25T14:12:51Z",
                        "activation_eta_iso": "2026-10-09T14:12:51Z"}],
        "network_votes_source": {}, "in_development": [], "sourcing": "sovereign", "cached_age_seconds": 0.0,
    }
    monkeypatch.setattr(app_mod, "fetch_amendments_state_cached", lambda *a, **k: dict(state))
    monkeypatch.setattr(app_mod, "_load_amendment_majority_history", lambda *a, **k: [])
    rounds = [_round(107524991, "2026-10-08T21:38:00Z", {PD: (30, 30, True), OTHER: (35, 35, True)}),
              _round(107524735, "2026-10-08T21:21:00Z", {PD: (30, 30, True), OTHER: (35, 35, True)})]
    monkeypatch.setattr(C, "is_enabled", lambda *a, **k: True)
    import db
    monkeypatch.setattr(db, "pg_available", lambda: True)
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    class _Conn(_Cur):
        def cursor(self): return _Cur()
    monkeypatch.setattr(db, "pg_connect", lambda: _Conn())
    monkeypatch.setattr(C, "read_rounds", lambda cur: rounds)
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        r = c.get("/amendments")
    assert r.status_code == 200
    html = r.data.decode()
    table = html[html.find('class="rc-table"'):]
    table = table[:table.find("</table>")]
    assert '<td class="name">0F48FF56</td>' not in table, "bare hash leaked into roll call"
    rows = re.findall(r'<tr data-roll-call-row="([a-z_]+)">\s*<td class="name">([^<]+)</td>.*?</tr>', table, re.S)
    by_name = {n.strip(): s for s, n in rows}
    assert by_name["PermissionDelegationV1_1"] == "enabled"
    assert by_name["fixBatchV1_2"] == "passing"
    pd_row = re.search(r'<tr data-roll-call-row="enabled">.*?</tr>', table, re.S).group(0)
    assert ">enabled<" in pd_row and "majority" not in pd_row and "#22c55e" in pd_row

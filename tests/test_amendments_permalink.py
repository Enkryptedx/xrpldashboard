"""/amendments/<date> — dated, signed permalink (build 2026-09-26).

OPEN + CLOSED per the publish-gate rule: the signed path renders tallies
and a verdict; every unsigned state renders WITHOUT tallies; malformed,
pre-first-leaf and future dates 404. Runs with no DB: PG reads are forced
off and the leaf comes from a fixture copied from a real signed file."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import app as app_module  # noqa: E402
import db  # noqa: E402
import amendments_permalink as ap  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "signed_leaf_2026-09-25.json")
LEAF_DATE = "2026-09-25"


@pytest.fixture
def no_db(monkeypatch, tmp_path):
    """No Postgres; the only leaf on disk is the fixture."""
    monkeypatch.setattr(db, "pg_available", lambda: False)
    monkeypatch.setattr(app_module, "SIGNED_SNAPSHOTS_DIR", str(tmp_path))
    with open(FIXTURE) as f:
        env = json.load(f)
    with open(tmp_path / f"{LEAF_DATE}.json", "w") as f:
        json.dump(env, f)
    return env


@pytest.fixture
def client(no_db):
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


# ── pure helpers ───────────────────────────────────────────────────────

def test_classify_date_states():
    signed = ["2026-05-14", "2026-09-15", "2026-09-19", "2026-09-25"]
    today = dt.date(2026, 9, 26)
    assert ap.classify_date("2026-09-25", signed, today) == "signed"
    assert ap.classify_date("2026-09-26", signed, today) == "not_yet_signed"
    assert ap.classify_date("2026-09-27", signed, today) == "future"
    assert ap.classify_date("2026-09-17", signed, today) == "known_gap"
    assert ap.classify_date("2026-07-15", signed, today) == "known_gap"
    assert ap.classify_date("2026-08-10", signed, today) == "missing"
    assert ap.classify_date("2026-05-01", signed, today) == "before_first"


def test_signed_rows_come_from_the_leaf_block():
    with open(FIXTURE) as f:
        env = json.load(f)
    block = ap.amendments_block_from_envelope(env)
    assert block and "per_amendment" in block
    rows = ap.signed_rows(block)
    names = [r["name"] for r in rows]
    assert names == sorted(names, key=str.lower)
    batch = next(r for r in rows if r["name"] == "BatchV1_1")
    assert batch["votes_count"] == block["per_amendment"]["BatchV1_1"]["network_votes"]["count"]
    assert batch["hash"].startswith("9F287AED")


def test_verdict_partial_only_for_chain_link_soft_note():
    assert ap.verdict_from_verify({"ok": True, "issues": []})["state"] == "verified"
    soft = {"ok": False, "issues": ["chain_link: could not verify (no chain.json and no prior-day file on disk) — x"]}
    assert ap.verdict_from_verify(soft)["state"] == "partial"
    bad = {"ok": False, "issues": ["Ed25519 signature did NOT verify against published pubkey"]}
    assert ap.verdict_from_verify(bad)["state"] == "failed"


def test_majority_events_for_day_filters_by_utc_day():
    rows = [{
        "name": "BatchV1_1", "hash": "9F28", "first_seen_ledger": 107228416,
        "first_seen_close_iso": "2026-09-25T15:02:40Z", "removed_seen_ledger": 107228160,
        "removed_close_iso": "2026-09-25T14:46:02Z", "activation_eta_iso": "2026-10-09T14:46:02Z",
        "vote_count_at_first": 30, "unl_threshold": 28, "correction_note": None,
    }]
    ev = ap.majority_events_for_day(rows, "2026-09-25")
    assert [e["kind"] for e in ev] == ["majority lost", "majority gained"]
    assert ap.majority_events_for_day(rows, "2026-09-24") == []


def test_roll_call_day_counts_are_counts_only():
    rounds = [
        {"voting_ledger": 200, "observed_iso": "2026-09-26T13:40:13Z", "unl_size": 35, "seen": 34,
         "trusted_available": 34, "threshold": 27, "needed": 28,
         "tallies": {"AAA": (29, 29, True), "BBB": (10, 12, False)}},
        {"voting_ledger": 100, "observed_iso": "2026-09-26T11:44:53Z", "unl_size": 35, "seen": 33,
         "trusted_available": 33, "threshold": 26, "needed": 27, "tallies": {}},
        {"voting_ledger": 50, "observed_iso": "2026-09-25T23:00:00Z", "unl_size": 35, "seen": 35,
         "trusted_available": 35, "threshold": 28, "needed": 29, "tallies": {}},
    ]
    rc = ap.roll_call_day_counts(rounds, "2026-09-26", {"AAA": "Alpha"})
    assert rc["rounds"] == 2 and rc["heard_min"] == 33 and rc["heard_max"] == 34
    assert rc["first_voting_ledger"] == 100 and rc["last_voting_ledger"] == 200
    assert rc["per_amendment"][0] == {"name": "Alpha", "hash": "AAA", "yes_round": 29, "yes_incl_carry": 29, "passes": True}
    assert rc["per_amendment"][1]["name"].startswith("BBB")
    assert ap.roll_call_day_counts(rounds, "2026-09-24", {}) is None
    assert "validator" not in json.dumps(rc).lower().replace("validators", "")


# ── routes: OPEN ───────────────────────────────────────────────────────

def test_signed_date_renders_tallies_and_verdict(client):
    r = client.get(f"/amendments/{LEAF_DATE}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'data-permalink-state="signed"' in html
    assert "data-signed-tallies" in html
    assert "BatchV1_1" in html
    assert 'data-verifier="' in html
    # Signature, leaf hash, audit path and fingerprint all pass on the real
    # leaf; the chain-link step may be unprovable with only one file on disk.
    assert 'data-verifier="failed"' not in html
    assert "immutable" in r.headers.get("Cache-Control", "")
    assert "{{" not in html and "{%" not in html


def test_json_twin_matches_html_numbers(client):
    r = client.get(f"/amendments/{LEAF_DATE}.json")
    assert r.status_code == 200
    j = r.get_json()
    assert j["state"] == "signed" and j["date"] == LEAF_DATE
    with open(FIXTURE) as f:
        env = json.load(f)
    assert j["leaf"]["leaf_hash"] == env["leaf_hash"]
    assert j["verifier"]["state"] in ("verified", "partial")
    batch = next(x for x in j["signed_rows"] if x["name"] == "BatchV1_1")
    html = client.get(f"/amendments/{LEAF_DATE}").get_data(as_text=True)
    assert f'<td class="num">{batch["votes_count"]}</td>' in html
    assert "NOT inside" in j["sourcing"]["majority_events"]
    assert "immutable" in r.headers.get("Cache-Control", "")


def test_no_validator_identities_on_permalink(client):
    html = client.get(f"/amendments/{LEAF_DATE}").get_data(as_text=True)
    j = client.get(f"/amendments/{LEAF_DATE}.json").get_json()
    for needle in ("validator_key", "nHU", "nHB", "master_key", "domain"):
        assert needle not in json.dumps(j.get("roll_call_day_counts"))
    assert "Counts only" in html


# ── routes: CLOSED ─────────────────────────────────────────────────────

def test_unsigned_today_is_200_not_yet_signed_without_tallies(client, monkeypatch):
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    if today == LEAF_DATE:  # pragma: no cover — fixture date is fixed
        pytest.skip("fixture date is today")
    r = client.get(f"/amendments/{today}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'data-permalink-state="not_yet_signed"' in html
    assert "data-signed-tallies" not in html
    assert "BatchV1_1" not in html
    assert r.headers.get("Cache-Control") == "no-store"
    j = client.get(f"/amendments/{today}.json").get_json()
    assert j["state"] == "not_yet_signed" and "signed_rows" not in j


def test_known_gap_is_200_with_outage_sentence(client, monkeypatch):
    # Give the fixture a first-leaf before the gap and a newest after it.
    monkeypatch.setattr(app_module, "_list_signed_snapshots",
                        lambda: ["2026-09-25", "2026-09-19", "2026-09-15", "2026-05-14"])
    r = client.get("/amendments/2026-09-17")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'data-permalink-state="known_gap"' in html
    assert "lost power" in html
    assert "data-signed-tallies" not in html


def test_missing_non_gap_day_is_200_no_tallies(client, monkeypatch):
    monkeypatch.setattr(app_module, "_list_signed_snapshots",
                        lambda: ["2026-09-25", "2026-08-11", "2026-08-09", "2026-05-14"])
    r = client.get("/amendments/2026-08-10")
    assert r.status_code == 200
    assert 'data-permalink-state="missing"' in r.get_data(as_text=True)


def test_july_15_is_a_known_gap_with_its_own_sentence(client, monkeypatch):
    monkeypatch.setattr(app_module, "_list_signed_snapshots",
                        lambda: ["2026-09-25", "2026-07-16", "2026-07-14", "2026-05-14"])
    r = client.get("/amendments/2026-07-15")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'data-permalink-state="known_gap"' in html
    assert "re-bootstrapped" in html and "2026-07-16" in html
    assert "data-signed-tallies" not in html


def test_before_first_leaf_future_and_malformed_404(client, monkeypatch):
    monkeypatch.setattr(app_module, "_list_signed_snapshots", lambda: ["2026-09-25", "2026-05-14"])
    assert client.get("/amendments/2026-05-01").status_code == 404
    far = (dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=3)).isoformat()
    assert client.get(f"/amendments/{far}").status_code == 404
    assert client.get("/amendments/2026-9-5").status_code == 404
    assert client.get("/amendments/latest").status_code == 404
    assert client.get("/amendments/2026-05-01.json").status_code == 404
    assert client.get("/amendments/..%2Fetc").status_code == 404


def test_pre_block_leaf_shows_no_tallies_but_still_signed(client, no_db, tmp_path):
    # A leaf dated before amendments_block existed: strip the metric.
    env = dict(no_db)
    env["metrics"] = [m for m in env["metrics"] if m["name"] != "amendments_block"]
    env["snapshot_date_utc"] = "2026-09-01"
    with open(tmp_path / "2026-09-01.json", "w") as f:
        json.dump(env, f)
    r = client.get("/amendments/2026-09-01")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'data-permalink-state="signed"' in html
    assert "data-signed-tallies" not in html
    assert "begin 2026-09-24" in html


# ── /amendments "Cite this day" ────────────────────────────────────────

def test_amendments_page_carries_cite_this_day(client, monkeypatch):
    monkeypatch.setattr(app_module, "_list_signed_snapshots", lambda: ["2026-09-25", "2026-09-24"])
    r = client.get("/amendments")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "data-cite-this-day" in html
    assert "/amendments/2026-09-25" in html
    assert "today's leaf signs at 01:00 UTC" in html


def test_openapi_documents_json_twin(client):
    spec = client.get("/openapi.json").get_json()
    assert "/amendments/{date}.json" in spec["paths"]


def test_llms_txt_lists_permalink(client):
    txt = client.get("/llms.txt").get_data(as_text=True)
    assert "/amendments/YYYY-MM-DD" in txt

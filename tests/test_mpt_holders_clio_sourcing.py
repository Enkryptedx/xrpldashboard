"""The holders walk is a DISCLOSED third-party read, and it fails loudly.

`mpt_holders` is not implemented by rippled 3.4.1. Measured 2026-11-27: our
own node and xrplcluster.com (both rippled 3.4.1) each answer
`{"error": "unknownCmd", "error_code": 32, "error_message": "Unknown
method."}`, while Clio 2.8.0 serves it. So every holder figure on the site
comes from a public Clio endpoint, and three things have to hold:

  1. the endpoint list lives in ONE shared module with an env override
  2. each row carries where it came from and that it is not sovereign, and
     every surface showing a holder count says so
  3. an RPC failure is recorded as an RPC failure -- permanent
     (rpc_unsupported) separated from transient (rpc_error), with counters
     using the same words the rows store, and the job exiting non-zero when
     the permanent class dominates

Part 2 is asserted on RENDERED HTML, not on a helper's return value: a
disclosure that is computed and never printed is the failure mode this file
exists to prevent.
"""

import re
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))

import anchor_nodes  # noqa: E402
import app as app_module  # noqa: E402
import mpt_holders_refresh as job  # noqa: E402
import mpt_snapshot as snap_mod  # noqa: E402


ISSUANCE = "0646C2F7D070B520AC6C9275BFDE7814561544CC173BDF20"
ISSUER = "rLr3ZxWy7owtSwaCH2n7tCauQENrt9MVgU"
CLIO = "https://s2-clio.ripple.com:51234"


# ─── 1. endpoint list ────────────────────────────────────────────────────────

class TestEndpointList:
    def test_clio_only_list_excludes_our_own_node(self):
        """Our node cannot answer this call, so prepending it would spend a
        request per issuance to collect unknownCmd -- and a silent cascade
        onto public infra is the undisclosed read the covenant forbids."""
        nodes = anchor_nodes.mpt_holders_nodes()
        assert nodes, "list must not be empty"
        assert not any("192.168" in u or "localhost" in u for u in nodes)
        assert len(nodes) >= 2, "a single endpoint has no fallback"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(anchor_nodes.MPT_HOLDERS_ENV_NAME,
                           "https://a.example:1, https://b.example:2")
        assert anchor_nodes.mpt_holders_nodes() == [
            "https://a.example:1", "https://b.example:2"]

    def test_it_is_separate_from_the_anchor_witness_list(self):
        """The anchor list contains xrplcluster.com, which is rippled and
        answers unknownCmd. Sharing one list would break the walk."""
        assert "https://xrplcluster.com" in anchor_nodes.full_history_nodes()
        assert "https://xrplcluster.com" not in \
            anchor_nodes.mpt_holders_nodes()


# ─── 2. provenance, asserted on rendered HTML ────────────────────────────────

def _holders(reason, source=CLIO, with_balance=2):
    return {
        "with_balance": with_balance, "authorized": with_balance,
        "top": [{"account": "rHolder1", "mpt_amount": "20",
                 "is_issuer": False}],
        "walked_complete": reason in ("complete", "no_holders"),
        "walked_at": 1764000000, "reason": reason,
        "source": source, "sovereign": False,
    }


def _row(holders):
    return {
        "issuance_id": ISSUANCE, "issuer": ISSUER, "sequence": 1,
        "name": "Licuido MPT Issuance", "ticker": "LMI",
        "asset_class_raw": "rwa", "asset_subclass": "treasury",
        "classification": "rwa", "issuer_name": "Licuido Platform Issuance",
        "icon": None, "uris": [], "additional_info": None, "desc": "fixture",
        "outstanding_amount": 28, "maximum_amount": 100000, "asset_scale": 0,
        "transfer_fee": 0, "flags": 68, "metadata_present": True,
        "metadata_error": None, "holders": holders,
    }


def _render(monkeypatch, holders):
    data = {
        "ok": True, "node": "fixture", "written_at": 1764000000,
        "issuances": [_row(holders)], "total": 1, "unique_issuers": 1,
        "by_class": {"rwa": 1, "stablecoin": 0, "utility": 0, "other": 0},
        "by_status": {"live": 1, "prepared": 0, "test": 0},
    }
    monkeypatch.setattr(app_module, "load_mpt_snapshot", lambda: data)
    app_module.app.config["TESTING"] = True
    if hasattr(app_module, "limiter"):
        app_module.limiter.enabled = False
    with app_module.app.test_client() as c:
        resp = c.get(f"/mpt/{ISSUANCE}")
    assert resp.status_code == 200, resp.status_code
    html = resp.get_data(as_text=True)
    html = re.sub(r"<style\b.*?</style>", "", html, flags=re.S | re.I)
    return re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)


class TestDisclosureIsPrinted:
    def test_a_clean_walk_discloses_the_public_source(self, monkeypatch):
        html = _render(monkeypatch, _holders("complete"))
        assert "2" in html
        assert "holder figures come from public Clio servers" in html, (
            "a holder count rendered with no source disclosure")
        assert "s2-clio.ripple.com:51234" in html, "endpoint not named"
        assert "not our own node" in html
        assert "our rippled node does not offer this lookup" in html

    def test_no_holders_also_discloses(self, monkeypatch):
        html = _render(monkeypatch, _holders("no_holders", with_balance=0))
        assert "holder figures come from public Clio servers" in html

    def test_the_footnote_names_the_cadence_and_the_third_party_read(
            self, monkeypatch):
        html = _render(monkeypatch, _holders("complete"))
        assert "every 6 hours" in html, "footnote still claims hourly"
        assert "our rippled node does not offer this lookup" in html

    def test_the_disclosure_carries_no_version_number(self, monkeypatch):
        """Owner instruction 2026-11-27: the reader-visible wording names no
        build version. A version in copy goes stale the moment rippled is
        upgraded, and the finding is recorded in code comments instead."""
        html = _render(monkeypatch, _holders("complete"))
        assert "3.4.1" not in html
        assert "unknownCmd" not in html

    def test_no_shouting_in_the_disclosure(self, monkeypatch):
        html = _render(monkeypatch, _holders("complete"))
        for shouted in ("DISCLOSED THIRD-PARTY", "NOT OUR OWN NODE"):
            assert shouted not in html, shouted

    @pytest.mark.parametrize("reason,expected", [
        ("rpc_unsupported", "holders RPC not available on the configured"),
        ("rpc_error", "holders RPC failed on the last cycle"),
    ])
    def test_each_rpc_failure_class_renders_its_own_words(
            self, monkeypatch, reason, expected):
        """A reader must be able to tell a misconfigured endpoint from a
        blip, and neither from 'not yet walked'."""
        html = _render(monkeypatch, _holders(reason, source=None,
                                             with_balance=None))
        assert expected in html
        assert "not yet walked" not in html

    def test_a_failed_walk_claims_no_source(self, monkeypatch):
        """Nothing was read, so nothing may be attributed to an endpoint."""
        html = _render(monkeypatch, _holders("rpc_unsupported", source=None,
                                             with_balance=None))
        assert "holder figures come from public Clio servers (" not in html


class TestRowsCarryProvenance:
    def test_pending_rows_are_marked_non_sovereign(self):
        row = snap_mod._holders_pending("pending")
        assert row["sovereign"] is False
        assert row["source"] is None

    def test_every_reason_the_walk_can_return_has_the_two_fields(self):
        for reason in ("pending", "rpc_unsupported", "rpc_error",
                       "skipped_test"):
            row = snap_mod._holders_pending(reason)
            assert "source" in row and "sovereign" in row, reason
            assert row["sovereign"] is False


# ─── 3. failure recording and the non-zero exit ──────────────────────────────

def _code_only(path):
    """Source with docstrings and # comments removed.

    Needed because the thing being asserted is that a WORD does not appear in
    CODE, and this file's own commentary necessarily quotes the old word while
    explaining why it went. Asserting over raw source makes the documentation
    fail the test -- the fourth time on 2026-11-27 that a string match over
    prose produced a wrong answer, which is the same mistake this whole branch
    exists to fix. Compare code against code.
    """
    src = open(path, encoding="utf-8").read()
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    src = re.sub(r"'''.*?'''", "", src, flags=re.S)
    return re.sub(r"(?m)#.*$", "", src)


class TestFailureRecording:
    def test_counter_names_are_the_stored_reason_words(self):
        """The old counter said `holders_incomplete` (a pagination word)
        while the rows said `pending` (an RPC error). A counter that cannot
        be named from the data is how this hid for weeks."""
        code = _code_only("mpt_holders_refresh.py")
        body = code[code.index("counts = {"):]
        body = body[:body.index("}") + 1]
        for word in ("rpc_unsupported", "rpc_error", "incomplete",
                     "pending", "ok", "ledger_entry_failed", "skipped_test"):
            assert f'"{word}"' in body, word
        assert "holders_incomplete" not in code, (
            "the pagination label for an RPC failure is back in CODE")

    def test_the_old_label_survives_only_as_documentation(self):
        """Guard the guard: if the explanation is ever deleted the test above
        still passes, so pin that the reasoning is recorded somewhere."""
        raw = open("mpt_holders_refresh.py", encoding="utf-8").read()
        assert "holders_incomplete" in raw, (
            "the note explaining why the pagination label was wrong is gone")

    @pytest.mark.parametrize("counts,share", [
        ({"ok": 10}, 0.0),
        ({"ok": 0, "rpc_unsupported": 527}, 1.0),
        ({"ok": 5, "rpc_unsupported": 5}, 0.5),
        ({"ok": 9, "rpc_unsupported": 1}, 0.1),
    ])
    def test_share_is_measured_over_walks_only(self, counts, share):
        assert job.unsupported_share(counts) == pytest.approx(share)

    def test_skips_do_not_dilute_the_share(self):
        """skipped_test and ledger_entry_failed never reached the RPC, so
        counting them would let a fully broken list hide behind skips."""
        assert job.unsupported_share(
            {"skipped_test": 500, "ledger_entry_failed": 20,
             "rpc_unsupported": 10}) == 1.0

    def test_no_walks_at_all_is_not_an_alert(self):
        assert job.unsupported_share(
            {"skipped_test": 6, "ledger_entry_failed": 2}) == 0.0

    def test_run_once_FAILS_and_alerts_when_unsupported_dominates(
            self, monkeypatch, tmp_path):
        """THE guard. The job used to report ok=True while producing nothing
        for 527 of 533 issuances."""
        monkeypatch.setattr(job, "_load_issuance_list", lambda p: [
            {"issuance_id": ISSUANCE, "issuer": ISSUER, "is_test": False}])
        monkeypatch.setattr(job, "_load_snapshot", lambda p: {"issuances": []})
        monkeypatch.setattr(job, "fetch_one_issuance",
                            lambda iid: {"issuance_id": iid, "issuer": ISSUER})
        monkeypatch.setattr(job, "_walk_holders",
                            lambda iid, iss: snap_mod._holders_pending(
                                "rpc_unsupported"))
        monkeypatch.setattr(job, "HOLDER_ISSUANCE_PAUSE", 0)
        wrote = []
        monkeypatch.setattr(job.db, "write_mpt_supply_history",
                            lambda rows, ts=None: wrote.append(rows) or 0)
        monkeypatch.setattr(job.db, "write_mpt_snapshot",
                            lambda s: wrote.append("snapshot"))
        r = job.run_once(list_path=str(tmp_path / "l.json"),
                         snapshot_path=str(tmp_path / "s.json"))
        assert r["ok"] is False
        assert r["reason"] == "rpc_unsupported_dominant"
        assert "Clio-only" in r["alert"]
        assert "MPT_HOLDERS_NODES" in r["alert"]
        assert r["counts"]["rpc_unsupported"] == 1
        assert not wrote, "a failed run must not write history or a snapshot"

    def test_dry_run_writes_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(job, "_load_issuance_list", lambda p: [
            {"issuance_id": ISSUANCE, "issuer": ISSUER, "is_test": False}])
        monkeypatch.setattr(job, "_load_snapshot", lambda p: {"issuances": []})
        monkeypatch.setattr(job, "fetch_one_issuance",
                            lambda iid: {"issuance_id": iid, "issuer": ISSUER})
        monkeypatch.setattr(job, "_walk_holders",
                            lambda iid, iss: _holders("complete"))
        monkeypatch.setattr(job, "HOLDER_ISSUANCE_PAUSE", 0)
        calls = []
        monkeypatch.setattr(job.db, "write_mpt_supply_history",
                            lambda rows, ts=None: calls.append("history") or 0)
        monkeypatch.setattr(job.db, "write_mpt_snapshot",
                            lambda s: calls.append("snapshot"))
        snapshot_file = tmp_path / "s.json"
        r = job.run_once(list_path=str(tmp_path / "l.json"),
                         snapshot_path=str(snapshot_file), dry_run=True)
        assert r["ok"] is True and r["dry_run"] is True
        assert r["counts"]["ok"] == 1
        assert not calls, f"dry run touched the database: {calls}"
        assert not snapshot_file.exists(), "dry run rewrote the snapshot"


class TestCadence:
    def test_cadence_is_six_hours(self):
        assert job.WALKER_CADENCE_SECONDS == 21600

    def test_requests_are_paced(self):
        """Pacing matters now that the walk hits infrastructure we do not
        own."""
        assert snap_mod.HOLDER_RPC_PAUSE >= 0.2
        assert snap_mod.HOLDER_ISSUANCE_PAUSE >= 0.1

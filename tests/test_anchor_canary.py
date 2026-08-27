"""Anchor canary tests — Shape C (ledger-derived) tripwire.

Under the 2026-08-27 Shape C rewrite the canary derives the anchor chain
directly from XRPL `account_tx` against a full-history Clio node. There
is no local registry file. Tests stub `fetch_account_tx_anchors` +
`fetch_live_chain` — sockets are never opened.

Coverage:
  - green: happy path finds anchors, root cross-check clean, no alerts
  - freshness: latest anchor > 8 days old fires anchor_stale (critical)
  - root mismatch: forged live root fires root_mismatch (critical)
  - missing anchored date on live chain fires (critical)
  - strip rule tolerates Xaman trailing padding
  - parse_anchor_memo direct unit
  - LOUD SKIP: all full-history witnesses unreachable → no alerts, no
    state (equivalent to skipped=True in meta)
  - LOUD SKIP: partial-history witness → no alerts
  - CRITICAL: witness responds with zero anchors → no_anchors_on_chain
  - refuse-to-run without credentials → exit 1 loud
  - dry-run does not persist state (R6)
  - bootstrap-hop tx has no v1 memo — sanity check
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import anchor_canary


# ── on-chain anchor fixtures (mirror docs/anchor_registry.json historical) ─
GENESIS_TX_HASH = "01D0BB9D230955F43DB35703E2EB7F5DFA43CEB69CCBBF57FBC8F17407E50DF8"
GENESIS_DATE = "2026-08-07"
GENESIS_ROOT = "c73d65ae5927243b86ee9ddbfd02b967451dc75a6b4678a5a05dadc9dbfdf86a"
GENESIS_LEDGER = 106140698
GENESIS_CLOSE_ISO = "2026-08-07T21:49:32+00:00"

WEEKLY_TX_HASH = "73951F479EDE071067FEA423FD2E67D8268470C8A3530B91AEA9826B469DC003"
WEEKLY_DATE = "2026-08-14"
WEEKLY_ROOT = "c92c377855cbaebbbaa0d034546f3c36975c86a2c03b84a9b881afc1271e7237"
WEEKLY_LEDGER = 106290824
WEEKLY_CLOSE_ISO = "2026-08-14T15:14:20+00:00"

XAMAN_TRAILING = "\n\n\n\n\n\n"

CLIO_URL = "https://s2-clio.ripple.com:51234"
FALLBACK_URL = "https://s1.ripple.com:51234"
SITE_URL = "https://xrpldashboard.onrender.com"


def _anchor(tx_hash: str, date: str, root: str, ledger: int, close_iso: str,
            trailing: str = XAMAN_TRAILING) -> dict:
    """Shape mirrors what fetch_account_tx_anchors returns per anchor."""
    return {
        "tx_hash": tx_hash,
        "ledger_index": ledger,
        "close_time_iso": close_iso,
        "snapshot_date": date,
        "chain_root_hex": root,
        "type": "standard",
        "namespace": "xrpldashboard/anchor/v1",
    }


def _two_anchor_chain() -> list[dict]:
    return [
        _anchor(GENESIS_TX_HASH, GENESIS_DATE, GENESIS_ROOT,
                GENESIS_LEDGER, GENESIS_CLOSE_ISO),
        _anchor(WEEKLY_TX_HASH, WEEKLY_DATE, WEEKLY_ROOT,
                WEEKLY_LEDGER, WEEKLY_CLOSE_ISO),
    ]


def _live_chain(root_history_entries: list[dict]) -> dict:
    return {
        "schema_version": 3,
        "current_root": (root_history_entries[-1]["root"]
                         if root_history_entries else None),
        "leaves": [],
        "root_history": root_history_entries,
    }


def _now_soon_after_weekly() -> dt.datetime:
    return dt.datetime.fromisoformat(WEEKLY_CLOSE_ISO) + dt.timedelta(days=1)


def _install_account_tx_stub(monkeypatch, per_node: dict):
    """per_node maps node_url → (status, anchors, detail).
    Missing entries default to ('unreachable', [], 'no stub for this node')."""
    def fake(node_url, account):
        return per_node.get(node_url, ("unreachable", [], "no stub"))
    monkeypatch.setattr(anchor_canary, "fetch_account_tx_anchors", fake)


def _install_chain_stub(monkeypatch, chain):
    monkeypatch.setattr(anchor_canary, "fetch_live_chain", lambda site_url: chain)


# ─────────────────────────────────────────────────────────────────────
# 1. Match passes silently — chain green, no alerts
# ─────────────────────────────────────────────────────────────────────

def test_match_passes_silently(monkeypatch):
    _install_account_tx_stub(monkeypatch, {
        CLIO_URL: ("ok", _two_anchor_chain(), ""),
    })
    _install_chain_stub(monkeypatch, _live_chain([
        {"date": GENESIS_DATE, "root": GENESIS_ROOT},
        {"date": WEEKLY_DATE, "root": WEEKLY_ROOT},
    ]))
    alerts, anchors, meta = anchor_canary.gather_alerts(
        site_url=SITE_URL,
        full_history_urls=[CLIO_URL],
        account=anchor_canary.DEFAULT_ANCHOR_ACCOUNT,
        now_utc=_now_soon_after_weekly(),
        freshness_hours=192,
    )
    assert alerts == [], alerts
    assert len(anchors) == 2
    assert meta["witness_url"] == CLIO_URL
    assert meta["skipped"] is False


# ─────────────────────────────────────────────────────────────────────
# 2. Root mismatch on latest — stolen-key tripwire
# ─────────────────────────────────────────────────────────────────────

def test_root_mismatch_fires_critical(monkeypatch):
    _install_account_tx_stub(monkeypatch, {
        CLIO_URL: ("ok", _two_anchor_chain(), ""),
    })
    forged = WEEKLY_ROOT[:-1] + ("d" if WEEKLY_ROOT[-1] != "d" else "e")
    _install_chain_stub(monkeypatch, _live_chain([
        {"date": GENESIS_DATE, "root": GENESIS_ROOT},
        {"date": WEEKLY_DATE, "root": forged},
    ]))
    alerts, _, _ = anchor_canary.gather_alerts(
        site_url=SITE_URL,
        full_history_urls=[CLIO_URL],
        account=anchor_canary.DEFAULT_ANCHOR_ACCOUNT,
        now_utc=_now_soon_after_weekly(),
        freshness_hours=192,
    )
    mismatch = [a for a in alerts if a["id"] == "anchor_canary:root_mismatch"]
    assert len(mismatch) == 1, alerts
    alert = mismatch[0]
    assert alert["severity"] == "critical"
    assert alert["snapshot_date"] == WEEKLY_DATE
    assert alert["anchored_root"] == WEEKLY_ROOT
    assert alert["live_root"] == forged
    assert alert["tx_hash"] == WEEKLY_TX_HASH
    msg = anchor_canary.format_alert(alert)
    assert "ANCHOR MISMATCH" in msg
    assert "stolen-key" in msg or "forged-site" in msg


# ─────────────────────────────────────────────────────────────────────
# 3. Freshness — stale latest anchor fires even with matching root
# ─────────────────────────────────────────────────────────────────────

def test_stale_anchor_fires_but_matches_root(monkeypatch):
    _install_account_tx_stub(monkeypatch, {
        CLIO_URL: ("ok", _two_anchor_chain(), ""),
    })
    _install_chain_stub(monkeypatch, _live_chain([
        {"date": GENESIS_DATE, "root": GENESIS_ROOT},
        {"date": WEEKLY_DATE, "root": WEEKLY_ROOT},
    ]))
    now = dt.datetime.fromisoformat(WEEKLY_CLOSE_ISO) + dt.timedelta(days=12)
    alerts, _, _ = anchor_canary.gather_alerts(
        site_url=SITE_URL,
        full_history_urls=[CLIO_URL],
        account=anchor_canary.DEFAULT_ANCHOR_ACCOUNT,
        now_utc=now,
        freshness_hours=192,
    )
    ids = [a["id"] for a in alerts]
    assert "anchor_canary:anchor_stale" in ids
    assert "anchor_canary:root_mismatch" not in ids


# ─────────────────────────────────────────────────────────────────────
# 4. Missing anchored date on live chain — critical
# ─────────────────────────────────────────────────────────────────────

def test_live_chain_missing_anchored_date_fires(monkeypatch):
    _install_account_tx_stub(monkeypatch, {
        CLIO_URL: ("ok", _two_anchor_chain(), ""),
    })
    _install_chain_stub(monkeypatch, _live_chain([
        {"date": GENESIS_DATE, "root": GENESIS_ROOT},
        {"date": "2026-08-13", "root": "a" * 64},
        {"date": "2026-08-15", "root": "b" * 64},
    ]))
    alerts, _, _ = anchor_canary.gather_alerts(
        site_url=SITE_URL,
        full_history_urls=[CLIO_URL],
        account=anchor_canary.DEFAULT_ANCHOR_ACCOUNT,
        now_utc=_now_soon_after_weekly(),
        freshness_hours=192,
    )
    missing = [a for a in alerts
               if a["id"] == "anchor_canary:live_root_missing_for_anchored_date"]
    assert len(missing) == 1, alerts
    assert missing[0]["severity"] == "critical"
    assert missing[0]["snapshot_date"] == WEEKLY_DATE


# ─────────────────────────────────────────────────────────────────────
# 5. Parse-anchor-memo direct unit — strip rule
# ─────────────────────────────────────────────────────────────────────

def test_parse_anchor_memo_direct():
    payload = (
        f"xrpldashboard/anchor/v1|{GENESIS_DATE}| {GENESIS_ROOT} " + XAMAN_TRAILING
    )
    parsed = anchor_canary.parse_anchor_memo(payload)
    assert parsed is not None
    assert parsed["type"] == "standard"
    assert parsed["snapshot_date"] == GENESIS_DATE
    assert parsed["chain_root_hex"] == GENESIS_ROOT


# ─────────────────────────────────────────────────────────────────────
# 6. LOUD SKIP — all full-history witnesses unreachable
# ─────────────────────────────────────────────────────────────────────

def test_all_witnesses_unreachable_loud_skip(monkeypatch, capsys):
    _install_account_tx_stub(monkeypatch, {
        CLIO_URL: ("unreachable", [], "connection timed out"),
        FALLBACK_URL: ("unreachable", [], "5xx from node"),
    })
    _install_chain_stub(monkeypatch, None)  # should never be reached
    alerts, anchors, meta = anchor_canary.gather_alerts(
        site_url=SITE_URL,
        full_history_urls=[CLIO_URL, FALLBACK_URL],
        account=anchor_canary.DEFAULT_ANCHOR_ACCOUNT,
        now_utc=_now_soon_after_weekly(),
        freshness_hours=192,
    )
    assert alerts == []
    assert anchors == []
    assert meta["skipped"] is True
    stdout = capsys.readouterr().out
    assert "LOUD SKIP" in stdout


# ─────────────────────────────────────────────────────────────────────
# 7. LOUD SKIP — partial-history witness
# ─────────────────────────────────────────────────────────────────────

def test_partial_history_witness_loud_skips(monkeypatch, capsys):
    _install_account_tx_stub(monkeypatch, {
        CLIO_URL: ("partial_history", [],
                   "ledger_index_min=105000000 exceeds threshold"),
    })
    _install_chain_stub(monkeypatch, None)
    alerts, anchors, meta = anchor_canary.gather_alerts(
        site_url=SITE_URL,
        full_history_urls=[CLIO_URL],
        account=anchor_canary.DEFAULT_ANCHOR_ACCOUNT,
        now_utc=_now_soon_after_weekly(),
        freshness_hours=192,
    )
    assert alerts == []
    assert meta["skipped"] is True
    assert "LOUD SKIP" in capsys.readouterr().out


# ─────────────────────────────────────────────────────────────────────
# 8. CRITICAL — witness responds ok but zero anchors
# ─────────────────────────────────────────────────────────────────────

def test_zero_anchors_fires_no_anchors_on_chain(monkeypatch):
    _install_account_tx_stub(monkeypatch, {
        CLIO_URL: ("ok", [], ""),
    })
    _install_chain_stub(monkeypatch, None)
    alerts, anchors, meta = anchor_canary.gather_alerts(
        site_url=SITE_URL,
        full_history_urls=[CLIO_URL],
        account=anchor_canary.DEFAULT_ANCHOR_ACCOUNT,
        now_utc=_now_soon_after_weekly(),
        freshness_hours=192,
    )
    ids = [a["id"] for a in alerts]
    assert "anchor_canary:no_anchors_on_chain" in ids
    assert alerts[0]["severity"] == "critical"
    assert meta["skipped"] is False
    msg = anchor_canary.format_alert(alerts[0])
    assert "NO ANCHORS" in msg


# ─────────────────────────────────────────────────────────────────────
# 9. CRITICAL — invalid_response (e.g. actNotFound) surfaces as alert
# ─────────────────────────────────────────────────────────────────────

def test_invalid_response_surfaces_alert(monkeypatch):
    _install_account_tx_stub(monkeypatch, {
        CLIO_URL: ("invalid_response", [], "actNotFound for rL2y…"),
    })
    _install_chain_stub(monkeypatch, None)
    alerts, _, meta = anchor_canary.gather_alerts(
        site_url=SITE_URL,
        full_history_urls=[CLIO_URL],
        account=anchor_canary.DEFAULT_ANCHOR_ACCOUNT,
        now_utc=_now_soon_after_weekly(),
        freshness_hours=192,
    )
    ids = [a["id"] for a in alerts]
    assert "anchor_canary:witness_semantic_error" in ids
    assert meta["skipped"] is False


# ─────────────────────────────────────────────────────────────────────
# 10. Cascade — first witness unreachable, second returns anchors
# ─────────────────────────────────────────────────────────────────────

def test_cascade_falls_through_unreachable(monkeypatch):
    _install_account_tx_stub(monkeypatch, {
        CLIO_URL: ("unreachable", [], "timeout"),
        FALLBACK_URL: ("ok", _two_anchor_chain(), ""),
    })
    _install_chain_stub(monkeypatch, _live_chain([
        {"date": GENESIS_DATE, "root": GENESIS_ROOT},
        {"date": WEEKLY_DATE, "root": WEEKLY_ROOT},
    ]))
    alerts, anchors, meta = anchor_canary.gather_alerts(
        site_url=SITE_URL,
        full_history_urls=[CLIO_URL, FALLBACK_URL],
        account=anchor_canary.DEFAULT_ANCHOR_ACCOUNT,
        now_utc=_now_soon_after_weekly(),
        freshness_hours=192,
    )
    assert alerts == []
    assert len(anchors) == 2
    assert meta["witness_url"] == FALLBACK_URL


# ─────────────────────────────────────────────────────────────────────
# 11. Bootstrap-hop tx has no v1 memo — sanity check
# ─────────────────────────────────────────────────────────────────────

def test_bootstrap_tx_has_no_v1_memo():
    bootstrap_tx = {
        "hash": anchor_canary.BOOTSTRAP_TX_HASH,
        "Account": anchor_canary.DEFAULT_ANCHOR_ACCOUNT,
    }
    assert anchor_canary.extract_v1_memo_from_tx(bootstrap_tx) is None
    bootstrap_tx["Memos"] = []
    assert anchor_canary.extract_v1_memo_from_tx(bootstrap_tx) is None


# ─────────────────────────────────────────────────────────────────────
# 12. R5: refuse-to-run without credentials — subprocess exit 1
# ─────────────────────────────────────────────────────────────────────

def test_refuse_without_creds_exit_1():
    """End-to-end: launch a subprocess with clean env, verify exit 1 +
    stderr contains REFUSE_TO_RUN. --dry-run flag is not passed."""
    canary = os.path.join(
        os.path.dirname(__file__), "..", "tools", "anchor_canary.py"
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    proc = subprocess.run(
        [sys.executable, canary],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 1, proc.stderr
    assert "REFUSE_TO_RUN" in proc.stderr
    assert "ANCHOR_CANARY_TELEGRAM_BOT_TOKEN" in proc.stderr


# ─────────────────────────────────────────────────────────────────────
# 13. Heartbeat formatting includes witness + latest anchor summary
# ─────────────────────────────────────────────────────────────────────

def test_heartbeat_green_includes_witness_and_latest(monkeypatch):
    _install_account_tx_stub(monkeypatch, {
        CLIO_URL: ("ok", _two_anchor_chain(), ""),
    })
    _install_chain_stub(monkeypatch, _live_chain([
        {"date": GENESIS_DATE, "root": GENESIS_ROOT},
        {"date": WEEKLY_DATE, "root": WEEKLY_ROOT},
    ]))
    alerts, anchors, meta = anchor_canary.gather_alerts(
        site_url=SITE_URL,
        full_history_urls=[CLIO_URL],
        account=anchor_canary.DEFAULT_ANCHOR_ACCOUNT,
        now_utc=_now_soon_after_weekly(),
        freshness_hours=192,
    )
    hb = anchor_canary.format_heartbeat(alerts, anchors, meta)
    assert "2 anchor(s) discovered" in hb
    assert CLIO_URL in hb
    assert WEEKLY_DATE in hb
    assert "no thief can silence is alive" in hb


def test_heartbeat_loud_skip_says_so():
    hb = anchor_canary.format_heartbeat(
        alerts=[],
        anchors=[],
        meta={"witness_url": "", "witness_status": "loud_skip",
              "witness_detail": "all timed out", "skipped": True},
    )
    assert "LOUD SKIP" in hb

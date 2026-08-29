"""Signed-snapshot v4 acceptance corpus.

Fresh file per Charlie's ruling 2026-08-29 (#14357): schema boundary
gets its own test file. `test_mcp_tools_signed_snapshot.py` stays
untouched — its continued green IS gate-4b evidence (historical v3
snapshots still verify under the v4-aware code).

Coverage maps 1:1 to §4 of `docs/SIGNED_SNAPSHOT_V4_DESIGN_2026-08-27.md`:

  4a  Dry-verify v4 stamp against current chain (offline)
  4b  Every historical v3 snapshot still verifies with v4 code loaded
  4c  Shape-C canary reads v4 correctly
  4d  walker_health_summary digest is stable across re-runs

Plus §5 strict-refuse spot-checks for each v4 metric collector — a
missing SoT MUST raise SystemExit (never stamp a guess), which is the
load-bearing correctness property of v4.

Frozen-now discipline: build_snapshot(now_utc=...) accepts an explicit
instant so gate 4d is deterministic; the collector code never calls
datetime.utcnow() inline.
"""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import os

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import signed_snapshot


HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SNAPSHOTS_DIR = os.path.join(REPO_ROOT, "signed_snapshots")


# ─────────────────────────────────────────────────────────────────────
# Fixtures — deterministic inputs so gates 4a/4d run without PG / git
# ─────────────────────────────────────────────────────────────────────

FROZEN_NOW = dt.datetime(2026, 8, 29, 16, 0, 0, tzinfo=dt.timezone.utc)


def _fake_walker_rows():
    """Three walkers, one of each state. Ages chosen against 3600s
    cadence + FROZEN_NOW so the state classifier hits green/stale/dead
    predictably: 1h old = 1.0× (green), 4h old = 4.0× (stale), 24h old
    = 24.0× (dead)."""
    return [
        {
            "walker_name": "amm_snapshot_walker",
            "last_run_ok": True,
            "consecutive_failures": 0,
            "cadence_seconds": 3600,
            "last_success_at": FROZEN_NOW - dt.timedelta(hours=1),
        },
        {
            "walker_name": "escrow_walker",
            "last_run_ok": True,
            "consecutive_failures": 0,
            "cadence_seconds": 3600,
            "last_success_at": FROZEN_NOW - dt.timedelta(hours=4),
        },
        {
            "walker_name": "rlusd_refresher",
            "last_run_ok": False,
            "consecutive_failures": 3,
            "cadence_seconds": 3600,
            "last_success_at": FROZEN_NOW - dt.timedelta(hours=24),
        },
    ]


@pytest.fixture
def ephemeral_keypair(monkeypatch):
    """Generate a throwaway Ed25519 keypair and point load_private_key /
    load_public_key at it. Tests that exercise sign_snapshot's full
    write path get a working signer without touching the real signing
    key (which lives in an encrypted PEM outside the repo)."""
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    monkeypatch.setattr(signed_snapshot, "load_private_key", lambda: priv)
    monkeypatch.setattr(signed_snapshot, "load_public_key", lambda: pub)
    return priv


@pytest.fixture
def stub_v3_metrics(monkeypatch):
    """Neutralise the v3 collectors (which reach out to XRPL RPC + PG +
    disk files) so v4-focused tests don't need a live environment. v4
    collectors are stubbed individually per test."""
    def _empty_v3(now_utc=None):
        # Simulate the v3 half of collect_metrics returning nothing —
        # the actual `collect_metrics` will then still append v4 rows.
        # We patch collect_metrics directly in build-level tests.
        return [], []
    return _empty_v3


@pytest.fixture
def stub_v4_collectors(monkeypatch):
    """Install deterministic v4 collectors. Returned as a dict so tests
    can override individual ones (e.g. to trigger strict-refuse)."""
    # Capture the real collectors BEFORE monkeypatch clobbers them, so
    # the default walker stub can still call through to the real code
    # with an injected reader without infinite-recursing on itself.
    real_walker = signed_snapshot.collect_walker_health_summary

    def install(*, walker=None, claims=None, editorial=None):
        if walker is None:
            walker = lambda now_utc: real_walker(
                now_utc, read_walker_health_all=_fake_walker_rows
            )
        if claims is None:
            claims = lambda: {
                "name": "claims_index_state",
                "value": {
                    "page_count": 4,
                    "claim_count": 47,
                    "claims_yaml_sha256": "a" * 64,
                    "claims_yaml_git_short": "abcdef1",
                },
                "unit": "claims",
                "source": "CLAIMS.yaml file hash + git log",
            }
        if editorial is None:
            editorial = lambda: {
                "name": "editorial_state",
                "value": {
                    "last_verified_stamps": {
                        "LAST_VERIFIED_REGULATION": "2026-08-17",
                        "LAST_VERIFIED_AGENT_TIER_METHODOLOGY": "2026-08-27",
                    },
                },
                "unit": "editorial",
                "source": "app.py LAST_VERIFIED_* constants (regex-enumerated at stamp time)",
            }

        monkeypatch.setattr(
            signed_snapshot, "collect_walker_health_summary", walker
        )
        monkeypatch.setattr(
            signed_snapshot, "collect_claims_index_state", claims
        )
        monkeypatch.setattr(
            signed_snapshot, "collect_editorial_state", editorial
        )
    return install


# ─────────────────────────────────────────────────────────────────────
# Gate 4a: dry-verify v4 stamp against current chain (offline)
# ─────────────────────────────────────────────────────────────────────

def test_gate_4a_dry_verify_v4_stamp(monkeypatch, tmp_path, stub_v4_collectors, ephemeral_keypair):
    """Build a v4 snapshot in dry-run, sign it, then hand the envelope
    straight to verify_envelope() — must return (True, []). This proves
    leaf-hash derivation + audit-path recomputation + signature check all
    round-trip under SCHEMA_VERSION=4 without touching disk chain state."""
    stub_v4_collectors()

    # Suppress the v1-v3 metric noise so the v4 leaf shape is legible.
    def _v3_pure_stub(now_utc=None):
        if now_utc is None:
            now_utc = dt.datetime.now(dt.timezone.utc)
        metrics = [
            signed_snapshot.collect_walker_health_summary(now_utc),
            signed_snapshot.collect_claims_index_state(),
            signed_snapshot.collect_editorial_state(),
        ]
        return metrics, []

    monkeypatch.setattr(signed_snapshot, "collect_metrics", _v3_pure_stub)

    # Redirect chain state to a temp file so dry-run doesn't read prod chain
    monkeypatch.setattr(signed_snapshot, "CHAIN_PATH", str(tmp_path / "chain.json"))
    monkeypatch.setattr(signed_snapshot, "SNAPSHOTS_DIR", str(tmp_path))

    snap = signed_snapshot.build_snapshot("2026-08-29", now_utc=FROZEN_NOW)
    signed = signed_snapshot.sign_snapshot(snap, dry_run=True)

    assert signed["schema_version"] == 4
    assert signed["snapshot_date_utc"] == "2026-08-29"

    ok, issues = signed_snapshot.verify_envelope(signed)
    assert ok, f"gate 4a fail: {issues}"
    assert issues == []


def test_gate_4a_v4_leaf_contains_three_new_metrics(monkeypatch, tmp_path, stub_v4_collectors):
    """The v4 leaf payload MUST contain walker_health_summary,
    claims_index_state, and editorial_state at the end of the metric
    list (§2 insertion-order ruling)."""
    stub_v4_collectors()

    def _v3_pure_stub(now_utc=None):
        if now_utc is None:
            now_utc = dt.datetime.now(dt.timezone.utc)
        metrics = [
            {"name": "xrpl_validated_ledger_index", "value": 12345, "unit": "ledger", "source": "stub"},
            signed_snapshot.collect_walker_health_summary(now_utc),
            signed_snapshot.collect_claims_index_state(),
            signed_snapshot.collect_editorial_state(),
        ]
        return metrics, []

    monkeypatch.setattr(signed_snapshot, "collect_metrics", _v3_pure_stub)
    monkeypatch.setattr(signed_snapshot, "CHAIN_PATH", str(tmp_path / "chain.json"))
    monkeypatch.setattr(signed_snapshot, "SNAPSHOTS_DIR", str(tmp_path))

    snap = signed_snapshot.build_snapshot("2026-08-29", now_utc=FROZEN_NOW)
    names = [m["name"] for m in snap["metrics"]]

    # Three v4 metrics present, in insertion order at the end
    assert names[-3:] == ["walker_health_summary", "claims_index_state", "editorial_state"]


# ─────────────────────────────────────────────────────────────────────
# Gate 4b: every historical v3 snapshot still verifies with v4 code
# ─────────────────────────────────────────────────────────────────────

def test_gate_4b_every_historical_v3_snapshot_verifies():
    """Sweep every signed_snapshots/YYYY-MM-DD.json on disk (all v3
    today) and verify each with the v4-aware verifier. Backward-compat
    invariant per §3 — v4 must never invalidate a historical stamp."""
    paths = sorted(glob.glob(os.path.join(SNAPSHOTS_DIR, "20??-??-??.json")))
    if not paths:
        pytest.skip("no historical snapshots on disk to verify")

    failures = []
    for path in paths:
        with open(path) as f:
            envelope = json.load(f)
        ok, issues = signed_snapshot.verify_envelope(envelope)
        if not ok:
            failures.append((os.path.basename(path), issues))

    assert failures == [], (
        f"gate 4b regression: {len(failures)} historical snapshots failed v4-aware verify: {failures}"
    )


# ─────────────────────────────────────────────────────────────────────
# Gate 4c: Shape-C canary reads v4 correctly
# ─────────────────────────────────────────────────────────────────────

def test_gate_4c_canary_hex_compare_is_version_agnostic(monkeypatch, tmp_path, stub_v4_collectors, ephemeral_keypair):
    """anchor_canary.check_chain_anchors compares chain_root_hex strings
    end-to-end — nothing schema-version-aware in the compare. Build a v4
    chain.json with one v4 leaf, then confirm the canary's core compare
    path (hex-string equality between on-chain-anchored root and the
    chain.json's current_root) works identically to v3."""
    stub_v4_collectors()

    def _v3_pure_stub(now_utc=None):
        return [
            signed_snapshot.collect_walker_health_summary(now_utc or FROZEN_NOW),
            signed_snapshot.collect_claims_index_state(),
            signed_snapshot.collect_editorial_state(),
        ], []

    monkeypatch.setattr(signed_snapshot, "collect_metrics", _v3_pure_stub)
    monkeypatch.setattr(signed_snapshot, "CHAIN_PATH", str(tmp_path / "chain.json"))
    monkeypatch.setattr(signed_snapshot, "SNAPSHOTS_DIR", str(tmp_path))

    snap = signed_snapshot.build_snapshot("2026-09-04", now_utc=FROZEN_NOW)
    signed = signed_snapshot.sign_snapshot(snap, dry_run=False)  # writes chain.json + file

    # Read what got persisted — this is what a canary would fetch via
    # /.well-known/snapshots/chain.json
    with open(tmp_path / "chain.json") as f:
        chain = json.load(f)

    # Simulate the canary's compare: chain_root_hex from on-chain anchor
    # matches chain.json['current_root']. Both are hex strings; version-
    # agnostic bytes.
    assert chain["schema_version"] == 4
    assert chain["current_root"] == signed["chain_root"]
    assert isinstance(chain["current_root"], str)
    assert len(chain["current_root"]) == 64  # 32-byte sha256 hex

    # And the v4 leaf lives inside chain["leaves"] with the same root
    assert any(leaf["date"] == "2026-09-04" for leaf in chain["leaves"])


def test_gate_4c_schema_version_history_appears_on_transition(monkeypatch, tmp_path, stub_v4_collectors, ephemeral_keypair):
    """§3 ruling: chain.json gains a `schema_version_history` array
    recording the version transition. On the first v4 stamp against a
    pre-existing v3 chain, we should see BOTH the outgoing v3 close-row
    AND the incoming v4 open-row."""
    stub_v4_collectors()

    def _v3_pure_stub(now_utc=None):
        return [
            signed_snapshot.collect_walker_health_summary(now_utc or FROZEN_NOW),
            signed_snapshot.collect_claims_index_state(),
            signed_snapshot.collect_editorial_state(),
        ], []

    monkeypatch.setattr(signed_snapshot, "collect_metrics", _v3_pure_stub)

    chain_path = tmp_path / "chain.json"
    monkeypatch.setattr(signed_snapshot, "CHAIN_PATH", str(chain_path))
    monkeypatch.setattr(signed_snapshot, "SNAPSHOTS_DIR", str(tmp_path))

    # Seed a pre-existing v3 chain — one prior leaf
    chain_path.write_text(json.dumps({
        "schema_version": 3,
        "leaves": [{"date": "2026-08-28", "leaf_hash": "00" * 32, "ledger_index": 99999}],
        "current_root": "aa" * 32,
    }))

    snap = signed_snapshot.build_snapshot("2026-09-04", now_utc=FROZEN_NOW)
    signed_snapshot.sign_snapshot(snap, dry_run=False)

    with open(chain_path) as f:
        chain = json.load(f)

    history = chain.get("schema_version_history", [])
    assert history, "schema_version_history missing after v3→v4 transition"

    versions_closed = {row["version"] for row in history if "last_snapshot_date" in row}
    versions_opened = {row["version"] for row in history if "first_snapshot_date" in row}
    assert 3 in versions_closed, f"v3 close-row missing: {history}"
    assert 4 in versions_opened, f"v4 open-row missing: {history}"


def test_gate_4c_schema_version_history_idempotent(monkeypatch, tmp_path, stub_v4_collectors, ephemeral_keypair):
    """Same-day re-run of a v4 stamp must not duplicate the transition
    row — schema_version_history is a ledger of transitions, not runs."""
    stub_v4_collectors()

    def _v3_pure_stub(now_utc=None):
        return [
            signed_snapshot.collect_walker_health_summary(now_utc or FROZEN_NOW),
            signed_snapshot.collect_claims_index_state(),
            signed_snapshot.collect_editorial_state(),
        ], []

    monkeypatch.setattr(signed_snapshot, "collect_metrics", _v3_pure_stub)

    chain_path = tmp_path / "chain.json"
    monkeypatch.setattr(signed_snapshot, "CHAIN_PATH", str(chain_path))
    monkeypatch.setattr(signed_snapshot, "SNAPSHOTS_DIR", str(tmp_path))

    chain_path.write_text(json.dumps({
        "schema_version": 3,
        "leaves": [{"date": "2026-08-28", "leaf_hash": "00" * 32, "ledger_index": 99999}],
        "current_root": "aa" * 32,
    }))

    signed_snapshot.sign_snapshot(
        signed_snapshot.build_snapshot("2026-09-04", now_utc=FROZEN_NOW),
        dry_run=False,
    )
    signed_snapshot.sign_snapshot(
        signed_snapshot.build_snapshot("2026-09-04", now_utc=FROZEN_NOW),
        dry_run=False,
    )

    with open(chain_path) as f:
        chain = json.load(f)

    history = chain["schema_version_history"]
    # Exactly one row per (version, direction) tuple
    rows_closing_v3 = [r for r in history if r.get("version") == 3 and "last_snapshot_date" in r]
    rows_opening_v4 = [r for r in history if r.get("version") == 4 and "first_snapshot_date" in r]
    assert len(rows_closing_v3) == 1, f"v3 close-row duplicated: {history}"
    assert len(rows_opening_v4) == 1, f"v4 open-row duplicated: {history}"


# ─────────────────────────────────────────────────────────────────────
# Gate 4d: walker_health_summary digest stable across re-runs
# ─────────────────────────────────────────────────────────────────────

def test_gate_4d_walker_digest_stable_across_reruns():
    """Two collector calls with the SAME frozen now_utc + SAME rows
    must produce byte-identical walkers_digest_sha256 values. This is
    the acceptance criterion for the frozen-now discipline."""
    metric_a = signed_snapshot.collect_walker_health_summary(
        FROZEN_NOW, read_walker_health_all=_fake_walker_rows
    )
    metric_b = signed_snapshot.collect_walker_health_summary(
        FROZEN_NOW, read_walker_health_all=_fake_walker_rows
    )
    assert metric_a["value"]["walkers_digest_sha256"] == metric_b["value"]["walkers_digest_sha256"]


def test_gate_4d_walker_digest_stable_across_1s_wall_clock_drift():
    """The critical case: two build_snapshot calls one wall-clock second
    apart. With frozen-now threading, the collector sees the SAME instant
    both times because build_snapshot freezes at entry — but here we
    simulate the design-doc concern directly: two independent frozen-now
    values 0.5s apart against the same walker cadence must still yield
    identical digests (1dp rounding on age_multiples_of_cadence absorbs
    sub-cadence-tenth drift).

    Cadence=3600s; 1s drift = 0.000278 cadence multiples — well below
    the 1dp rounding bucket (0.1). So we assert equality."""
    now_a = FROZEN_NOW
    now_b = FROZEN_NOW + dt.timedelta(seconds=1)

    metric_a = signed_snapshot.collect_walker_health_summary(
        now_a, read_walker_health_all=_fake_walker_rows
    )
    metric_b = signed_snapshot.collect_walker_health_summary(
        now_b, read_walker_health_all=_fake_walker_rows
    )
    assert metric_a["value"]["walkers_digest_sha256"] == metric_b["value"]["walkers_digest_sha256"], (
        "1s wall-clock drift broke walker digest — 1dp rounding is insufficient, "
        "or something else in the digest input is time-sensitive"
    )


def test_gate_4d_build_snapshot_freezes_now(monkeypatch, tmp_path, stub_v4_collectors):
    """build_snapshot must accept explicit now_utc AND, when omitted,
    freeze once at entry so downstream v4 collectors see one instant.
    This is the load-bearing correctness property for 4d."""
    seen = []

    def _v4_walker_capturing_now(now_utc):
        seen.append(now_utc)
        return {
            "name": "walker_health_summary",
            "value": {"total_walkers": 0, "green_count": 0, "stale_count": 0, "dead_count": 0,
                      "walkers_digest_sha256": "0" * 64},
            "unit": "walkers",
            "source": "test-stub",
        }

    stub_v4_collectors(walker=_v4_walker_capturing_now)

    def _v3_pure_stub(now_utc=None):
        return [
            signed_snapshot.collect_walker_health_summary(now_utc or FROZEN_NOW),
        ], []

    monkeypatch.setattr(signed_snapshot, "collect_metrics", _v3_pure_stub)
    monkeypatch.setattr(signed_snapshot, "CHAIN_PATH", str(tmp_path / "chain.json"))
    monkeypatch.setattr(signed_snapshot, "SNAPSHOTS_DIR", str(tmp_path))

    snap = signed_snapshot.build_snapshot("2026-08-29", now_utc=FROZEN_NOW)

    assert seen == [FROZEN_NOW]
    assert snap["snapshot_taken_unix"] == int(FROZEN_NOW.timestamp())


# ─────────────────────────────────────────────────────────────────────
# §5 strict-refuse spot-checks — each v4 SoT failure MUST raise
# SystemExit. Never stamp a guess.
# ─────────────────────────────────────────────────────────────────────

def test_strict_refuse_walker_health_summary_empty_rows():
    """PG returns [] — could mean empty table OR silent failure. Either
    way, the collector MUST refuse rather than stamp a zero-walker
    summary that implies our fleet doesn't exist."""
    with pytest.raises(SystemExit) as exc:
        signed_snapshot.collect_walker_health_summary(
            FROZEN_NOW, read_walker_health_all=lambda: []
        )
    assert "STRICT-REFUSE" in str(exc.value)
    assert "walker_health_summary" in str(exc.value)


def test_strict_refuse_walker_health_summary_reader_raises():
    """A read that raises (PG connection error, permission, etc.) MUST
    surface as strict-refuse, not silently continue."""
    def _raising():
        raise RuntimeError("simulated PG failure")

    with pytest.raises(SystemExit) as exc:
        signed_snapshot.collect_walker_health_summary(
            FROZEN_NOW, read_walker_health_all=_raising
        )
    assert "STRICT-REFUSE" in str(exc.value)


def test_strict_refuse_claims_index_state_missing_file(tmp_path):
    """CLAIMS.yaml absent → refuse."""
    with pytest.raises(SystemExit) as exc:
        signed_snapshot.collect_claims_index_state(
            claims_yaml_path=str(tmp_path / "does_not_exist.yaml")
        )
    assert "STRICT-REFUSE" in str(exc.value)


def test_strict_refuse_claims_index_state_unparseable(tmp_path):
    """CLAIMS.yaml exists but is invalid YAML → refuse."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("this is: not: valid: yaml: [unclosed")

    with pytest.raises(SystemExit) as exc:
        signed_snapshot.collect_claims_index_state(claims_yaml_path=str(bad))
    assert "STRICT-REFUSE" in str(exc.value)


def test_strict_refuse_claims_index_state_git_short_missing(tmp_path):
    """CLAIMS.yaml readable + parseable BUT git returns empty → refuse.
    A file with no git history is unusual and worth surfacing."""
    yaml_file = tmp_path / "empty.yaml"
    yaml_file.write_text("pages: {}\n")

    with pytest.raises(SystemExit) as exc:
        signed_snapshot.collect_claims_index_state(
            claims_yaml_path=str(yaml_file),
            git_short_reader=lambda p: "",
        )
    assert "STRICT-REFUSE" in str(exc.value)
    assert "git-short" in str(exc.value)


def test_strict_refuse_editorial_state_missing_source(tmp_path):
    """app.py unreadable → refuse."""
    with pytest.raises(SystemExit) as exc:
        signed_snapshot.collect_editorial_state(
            app_py_path=str(tmp_path / "does_not_exist.py")
        )
    assert "STRICT-REFUSE" in str(exc.value)


def test_strict_refuse_editorial_state_zero_stamps_found(tmp_path):
    """app.py exists but contains no LAST_VERIFIED_* stamps → refuse.
    The stamps disappearing between v4 ship and a later run is a signal,
    not a normal state; forcing us to visibly investigate before shipping
    an empty editorial commitment."""
    bare = tmp_path / "bare_app.py"
    bare.write_text("# nothing to see here\ndef hello(): pass\n")

    with pytest.raises(SystemExit) as exc:
        signed_snapshot.collect_editorial_state(app_py_path=str(bare))
    assert "STRICT-REFUSE" in str(exc.value)
    assert "zero LAST_VERIFIED_" in str(exc.value)


# ─────────────────────────────────────────────────────────────────────
# Positive-path collector shape tests — belt & braces for §1a/§1b/§1c
# ─────────────────────────────────────────────────────────────────────

def test_walker_health_summary_shape_and_counts():
    """Three-walker fixture: 1 green, 1 stale, 1 dead. Counts + shape."""
    metric = signed_snapshot.collect_walker_health_summary(
        FROZEN_NOW, read_walker_health_all=_fake_walker_rows
    )
    assert metric["name"] == "walker_health_summary"
    assert metric["unit"] == "walkers"
    v = metric["value"]
    assert v["total_walkers"] == 3
    assert v["green_count"] == 1
    assert v["stale_count"] == 1
    assert v["dead_count"] == 1
    assert len(v["walkers_digest_sha256"]) == 64  # sha256 hex


def test_walker_health_summary_digest_is_over_canonical_json():
    """The digest MUST be the SHA-256 of _canonical_json over the sorted
    detail list — verifiable by an independent reader with the same
    walker rows + threshold code."""
    metric = signed_snapshot.collect_walker_health_summary(
        FROZEN_NOW, read_walker_health_all=_fake_walker_rows
    )
    # Reconstruct the expected detail list independently
    detail = []
    for row in sorted(_fake_walker_rows(), key=lambda r: r["walker_name"]):
        state, multiples = signed_snapshot._walker_state(row, FROZEN_NOW)
        detail.append({
            "walker": row["walker_name"],
            "state": state,
            "consecutive_failures": row["consecutive_failures"],
            "age_multiples_of_cadence": multiples,
        })
    expected = hashlib.sha256(signed_snapshot._canonical_json(detail)).hexdigest()
    assert metric["value"]["walkers_digest_sha256"] == expected


def test_claims_index_state_reads_real_repo_file():
    """Positive-path against the real CLAIMS.yaml on disk — no stubs.
    Confirms the collector works end-to-end in the environment the
    ceremony will run in."""
    metric = signed_snapshot.collect_claims_index_state()
    assert metric["name"] == "claims_index_state"
    assert metric["unit"] == "claims"
    v = metric["value"]
    assert v["page_count"] >= 1
    assert v["claim_count"] >= 1
    assert len(v["claims_yaml_sha256"]) == 64
    assert len(v["claims_yaml_git_short"]) >= 4  # typical git short is 7-8 chars


def test_editorial_state_reads_real_app_py():
    """Positive-path against the real app.py on disk — every
    LAST_VERIFIED_* constant currently defined must appear."""
    metric = signed_snapshot.collect_editorial_state()
    assert metric["name"] == "editorial_state"
    assert metric["unit"] == "editorial"
    stamps = metric["value"]["last_verified_stamps"]
    assert stamps, "no LAST_VERIFIED_* stamps found in real app.py"
    # Every value should be an ISO date string
    for name, date_val in stamps.items():
        assert name.startswith("LAST_VERIFIED_")
        assert len(date_val) == 10
        dt.date.fromisoformat(date_val)  # raises if malformed

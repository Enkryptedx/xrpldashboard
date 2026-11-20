"""Anchor memo padding must never reach a compared value.

Anchor #16 (2026-11-20) carried 102 bytes of MemoData on-ledger: the 99-byte
canonical string followed by three line feeds appended by the signing client.
Anchor #15 carried 100 bytes (one line feed), and
tests/test_anchor_canary.py::XAMAN_TRAILING uses six. The padding is variable,
so no parser may assume a length and every parser must strip before comparing.

Both call sites hardened here were SAFE at the time of writing only because
their one caller happened to pre-strip. These tests remove that dependency by
feeding each site the real raw 102-byte #16 memo directly, so a future caller
that forgets to strip fails here instead of publishing a wrong chain_root.

Measured on-ledger 2026-11-20 and byte-identical across our own node,
s1.ripple.com, s2.ripple.com and xrplcluster.com.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── The real anchor #16 memo, exactly as it sits on-ledger ──────────────────
ANCHOR16_ROOT = "4e9ddee54755873ef5008b2ab81ee8baa021d4a72af629b45ac620d41ddf59ea"
ANCHOR16_DATE = "2026-11-20"
ANCHOR16_CANONICAL = f"xrpldashboard/anchor/v1|{ANCHOR16_DATE}|{ANCHOR16_ROOT}"
# MemoData hex as returned by `tx` on all four sources (204 hex chars = 102 B)
ANCHOR16_MEMO_HEX = (ANCHOR16_CANONICAL + "\n\n\n").encode("utf-8").hex().upper()
ANCHOR16_MEMO_RAW = bytes.fromhex(ANCHOR16_MEMO_HEX).decode("utf-8")


def test_the_fixture_really_is_the_102_byte_onledger_memo():
    """Guard the guard: if this drifts, the tests below prove nothing."""
    assert len(ANCHOR16_MEMO_RAW.encode("utf-8")) == 102
    assert len(ANCHOR16_CANONICAL.encode("utf-8")) == 99
    assert ANCHOR16_MEMO_RAW.startswith(ANCHOR16_CANONICAL)
    assert ANCHOR16_MEMO_RAW[len(ANCHOR16_CANONICAL):] == "\n\n\n"
    # no leading whitespace on-ledger: first byte is 'x' (0x78)
    assert ANCHOR16_MEMO_RAW.encode("utf-8")[0] == 0x78


# ── Site 1: xrpl_notary_verify._parse_memo, fed the RAW memo ───────────────
def test_parse_memo_strips_raw_onledger_padding_itself():
    """_parse_memo must not depend on its caller having stripped.

    Pre-hardening this returned sha256_digest == '<root>\\n\\n\\n' and raised
    nothing, so a caller that skipped the upstream .strip() would compare a
    padded digest against a clean one and report a false mismatch.
    """
    import xrpl_notary_verify as nv

    parsed = nv._parse_memo(ANCHOR16_MEMO_RAW)
    assert parsed is not None, "raw 102-byte #16 memo must still parse"
    assert parsed["sha256_digest"] == ANCHOR16_ROOT
    assert "\n" not in parsed["sha256_digest"]
    assert parsed["utc_date"] == ANCHOR16_DATE
    assert parsed["protocol"] == "xrpldashboard/anchor/v1"


def test_parse_memo_tolerates_other_padding_widths():
    """#15 had one LF, #16 three, the canary fixture six. None may differ."""
    for pad in ("", "\n", "\n\n\n", "\n\n\n\n\n\n", "  \n ", "\r\n"):
        parsed = nv_parse(ANCHOR16_CANONICAL + pad)
        assert parsed["sha256_digest"] == ANCHOR16_ROOT, f"failed on {pad!r}"


def nv_parse(text):
    import xrpl_notary_verify as nv
    return nv._parse_memo(text)


# ── Site 2: the REAL /.well-known/anchors.json route ───────────────────────
RECORD_TEMPLATE = """# Anchor history (test fixture)

## Anchor #16 — {date} (on-time Friday)

| Field | Value |
|-------|-------|
| Type | A (weekly cadence — on-time Friday) |
| Tx hash | `6356EE41A75D8894F4387B9333A0F8A60DD54661B71B1084C5CCF41E3B3F6FC7` |
| Ledger | 108461275 |
| Close time | {date} 3:54:50 PM ET (20:54:50 UTC) |
| From | `rL2yMECEyUT94pLDrAcetMNMG1H4xqpNWQ` (Anchor) |
| To | `rwrcJL3Exd1ZUYz11Wug6wvWC448CiTXfd` (Dashboard) |
| Amount | 1 drop |
| Fee | 12 drops |
| Sequence | 106138946 |
| MemoData (decoded) | `{memo_row}` |
| Notes | tesSUCCESS. Fixture carrying the raw on-ledger padding. |
"""


def _memo_row_from_raw(raw: str) -> str:
    r"""Render the raw memo into a record row the way the file does.

    The real file escapes the pipes as ``\|`` inside the backticks. The
    on-ledger trailing line feeds and per-field spacing are preserved
    verbatim, because that is the whole point of the test.
    """
    date, root = raw.rstrip().split("|")[1], raw.rstrip().split("|")[2]
    # interior spacing + the real trailing LFs, exactly what a careless
    # transcription of the ledger value produces
    return f"xrpldashboard/anchor/v1\\| {date} \\| {root} " + raw[len(raw.rstrip()):]


@pytest.fixture()
def anchors_client(tmp_path, monkeypatch):
    """Point the real route at a fixture record and hand back a test client."""
    import app as app_module

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "anchor_history.md").write_text(
        RECORD_TEMPLATE.format(
            date=ANCHOR16_DATE, memo_row=_memo_row_from_raw(ANCHOR16_MEMO_RAW)
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "HERE", str(tmp_path), raising=True)
    app_module.app.config.update(TESTING=True)
    try:
        app_module.limiter.enabled = False
    except Exception:
        pass
    return app_module.app.test_client()


def test_wellknown_anchors_json_route_publishes_a_clean_chain_root(anchors_client):
    """The published surface must never carry memo padding into chain_root.

    This drives the actual Flask route, not the helper: a regression here is
    visible at /.well-known/anchors.json, which the daily public-route canary
    GETs and which third parties parse.
    """
    resp = anchors_client.get("/.well-known/anchors.json")
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]
    payload = json.loads(resp.get_data(as_text=True))

    anchors = payload.get("anchors") or payload.get("onchain_anchors") or []
    assert anchors, f"route returned no anchors: {sorted(payload)}"
    entry = next(a for a in anchors if a.get("number") == 16)

    assert entry["chain_root"] == ANCHOR16_ROOT
    assert "\n" not in (entry["chain_root"] or "")
    assert not (entry["chain_root"] or "").startswith(" ")
    assert not (entry["chain_root"] or "").endswith(" ")
    assert len(entry["chain_root"]) == 64

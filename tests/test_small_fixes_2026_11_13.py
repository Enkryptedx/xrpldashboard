"""Three small truth fixes from the 2026-11-12 read-only site audit.

Each one is a case where the site said something that was not true, and
the fix is to report the measured value instead of a wrong one.

1. /pools "TVL snapshot · Nh ago" aged the chip from `snapshot_ts`, which
   is a TOUCH time -- in the file path literally
   `os.path.getmtime(AMM_RANKED_PATH)` -- so any re-read or re-write made
   an old ranking look fresh. Measured 2026-11-12: the chip read "1h ago"
   while `finished_at` was 2026-11-12T04:50Z, about 20h earlier. The TVL
   values were correct; only the age label lied.

2. /check.json advertised the WRONG signing key. Its server block came
   from mcp_server.wrap_envelope, whose _server_block() stamps the
   SNAPSHOT key (7F:D4:...) read from snapshot_pubkey_fingerprint.txt,
   while the envelope is actually signed with the RECEIPT key
   (A4:0F:...) recorded in proof.check_v09_signature.signer. A client
   pinning the key our own docs tell it to pin would reject a valid
   response -- or accept one against the wrong key. Confirmed live before
   the fix: server.public_key_fingerprint=7F:D4:F2:F4:D2:57:7C:BE with
   proof.signer=A4:0F:B1:0A:9D:33:64:03 on the same response.

3. openapi.json documented 12 paths and NONE of the seven live JSON
   read-API surfaces, while llms.txt calls it "an index of the LIVE free
   surface". An agent reading the spec concluded they did not exist.

Hermetic: pure helpers plus text reads of app.py. No DB, no network.
"""

import os
import re

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PY = os.path.join(HERE, "app.py")
ANCHORS_MD = os.path.join(HERE, "docs", "anchor_history.md")
RECEIPT_FP = os.path.join(HERE, "receipt_pubkey_fingerprint.txt")

READ_API_PATHS = (
    "/tokens.json",
    "/whales.json",
    "/pools.json",
    "/amendments.json",
    "/cold-storage/unlocks.json",
    "/claims/index.json",
    "/.well-known/verified-tokens.json",
)


@pytest.fixture(scope="module")
def src():
    return open(APP_PY, encoding="utf-8").read()


def _load_age_helper():
    """Exec just the helper against module-level imports.

    Importing app.py outright needs a Flask app + env; this keeps the
    test hermetic while still exercising the real source text, so a
    change to the helper is actually covered.
    """
    s = open(APP_PY, encoding="utf-8").read()
    start = s.index("def _amm_snapshot_age_seconds")
    end = s.index("_XRP_ESCROW_STALE_AFTER", start)
    ns = {}
    exec("from datetime import datetime, timezone\nimport time", ns)  # noqa: S102
    exec(s[start:end], ns)  # noqa: S102
    return ns["_amm_snapshot_age_seconds"]


class TestPoolsStampAge:
    def test_prefers_finished_at_over_snapshot_ts(self):
        """The regression: a stale ranking with a fresh touch time."""
        f = _load_age_helper()
        meta = {
            "finished_at": "2026-11-12T04:50:00+00:00",
            "snapshot_ts": 99999999999,   # absurdly "fresh" touch time
        }
        age = f(meta)
        assert age is not None and age > 3600, (
            "finished_at must win; a touch time must not make a 20h-old "
            "ranking read as fresh"
        )

    def test_falls_back_when_no_finish_recorded(self):
        f = _load_age_helper()
        assert f({"snapshot_ts": 1000000000}) is not None

    def test_malformed_finish_degrades_to_touch_time(self):
        """A bad stamp must not blank the chip entirely."""
        f = _load_age_helper()
        assert f({"finished_at": "not-a-date",
                  "snapshot_ts": 1000000000}) is not None

    @pytest.mark.parametrize("meta", [None, {}, {"finished_at": None}])
    def test_unknown_age_is_none_not_zero(self, meta):
        """None renders no chip. Zero would render 'TVL snapshot · 0s ago',
        which is the same class of lie this fix removes."""
        assert _load_age_helper()(meta) is None

    def test_naive_timestamp_is_treated_as_utc(self):
        f = _load_age_helper()
        assert f({"finished_at": "2020-01-01T00:00:00"}) > 0

    def test_route_uses_the_helper_not_raw_snapshot_ts(self, src):
        assert "age = _amm_snapshot_age_seconds(meta)" in src


class TestCheckJsonSigningKey:
    def test_fingerprint_is_read_from_the_receipt_file(self, src):
        assert "RECEIPT_PUBKEY_FP_PATH" in src
        assert "receipt_pubkey_fingerprint.txt" in src

    def test_receipt_file_exists_and_is_a_fingerprint(self):
        fp = open(RECEIPT_FP, encoding="utf-8").read().strip()
        assert re.fullmatch(r"(?:[0-9A-F]{2}:){7}[0-9A-F]{2}", fp), fp

    def test_server_block_is_corrected_BEFORE_signing(self, src):
        """ORDER IS THE WHOLE FIX, and the first version had it backwards.

        _check_v09_sign hashes `deepcopy(envelope)` with only
        proof.check_v09_signature popped -- so the canonical hash covers
        the `server` block. Correcting the fingerprint AFTER signing
        ships a payload whose hash no longer matches its signature, and
        every verifier that recomputes the hash rejects the response.
        The correction must therefore happen BEFORE the signature is
        taken, so the signature covers the corrected block.
        """
        i_fix = src.index('envelope["server"]["public_key_fingerprint"] = _fp')
        i_sign = src.index("envelope = _check_v09_sign(envelope)",
                           src.index("def _check_json_response"))
        assert i_fix < i_sign, "fingerprint must be set before signing"

    def test_mcp_name_corrected_on_a_non_mcp_endpoint(self, src):
        assert 'envelope["server"]["name"] = "xrpldashboard"' in src

    def test_name_fix_also_precedes_signing(self, src):
        i_name = src.index('envelope["server"]["name"] = "xrpldashboard"')
        i_sign = src.index("envelope = _check_v09_sign(envelope)",
                           src.index("def _check_json_response"))
        assert i_name < i_sign

    def test_signed_hash_covers_the_server_block(self, src):
        """Pin the property the ordering depends on. If a later change
        made _check_v09_sign exclude `server` from the hash, the ordering
        constraint would quietly stop mattering -- and if it started
        excluding MORE, this catches that too."""
        blk = src[src.index("def _check_v09_sign"):]
        blk = blk[:blk.index("canonical_hash = ")]
        assert "copy.deepcopy(envelope)" in blk
        # exactly one key is removed before hashing
        assert blk.count(".pop(") == 1
        assert 'pop("check_v09_signature"' in blk
        assert '"server"' not in blk, "server must NOT be excluded from the hash"

    def test_missing_file_does_not_break_the_endpoint(self, src):
        """A missing fingerprint file must degrade, not 500 /check.json."""
        block = src[src.index("def _receipt_pubkey_fingerprint"):]
        block = block[:block.index("def _check_json_response")]
        assert "except Exception" in block and "return None" in block

    def test_override_is_guarded_on_a_dict(self, src):
        assert 'isinstance(envelope.get("server"), dict)' in src


class TestMutationAfterSigningBreaksTheHash:
    """EXECUTABLE proof that the ordering is load-bearing.

    The source-text assertions above say the correction happens before
    signing. This one shows WHY that is not cosmetic: it reproduces the
    signer's own canonicalisation and demonstrates that editing `server`
    after the hash is taken yields a payload whose recomputed hash no
    longer matches the one in the signature block. Any verifier doing
    what we tell it to do in the docs would reject such a response.

    Uses signed_snapshot._canonical_json -- the exact function
    _check_v09_sign uses -- so this cannot drift from the real signer.
    """

    @staticmethod
    def _hash(envelope):
        import copy, hashlib
        from signed_snapshot import _canonical_json
        e = copy.deepcopy(envelope)
        e.get("proof", {}).pop("check_v09_signature", None)
        return hashlib.sha256(_canonical_json(e)).hexdigest()

    def _envelope(self, fp):
        return {
            "data": {"address": "rrrrrrrrrrrrrrrrrrrrrhoLvTp"},
            "proof": {"source": "xrpldashboard/check-endpoint"},
            "server": {"name": "xrpldashboard", "version": "2.0.0",
                       "public_key_fingerprint": fp,
                       "docs": "https://example.invalid/m"},
        }

    def test_server_block_changes_the_canonical_hash(self):
        """If this ever passes as EQUAL, `server` left the signed payload
        and the ordering constraint would no longer apply."""
        wrong = self._envelope("7F:D4:F2:F4:D2:57:7C:BE")
        right = self._envelope("A4:0F:B1:0A:9D:33:64:03")
        assert self._hash(wrong) != self._hash(right)

    def test_post_sign_mutation_invalidates_the_recorded_hash(self):
        """The bug the first implementation would have shipped."""
        env = self._envelope("7F:D4:F2:F4:D2:57:7C:BE")
        signed_hash = self._hash(env)                       # signer hashes here
        env["proof"]["check_v09_signature"] = {
            "canonical_hash": signed_hash,
            "signer": "A4:0F:B1:0A:9D:33:64:03",
        }
        # ...then the old code corrected the fingerprint AFTER signing:
        env["server"]["public_key_fingerprint"] = "A4:0F:B1:0A:9D:33:64:03"
        assert self._hash(env) != signed_hash, (
            "mutating server after signing must change the recomputed "
            "hash -- that is exactly why it breaks verification"
        )

    def test_pre_sign_correction_keeps_the_hash_agreeing(self):
        """The shipped order: correct first, then hash."""
        env = self._envelope("7F:D4:F2:F4:D2:57:7C:BE")
        env["server"]["public_key_fingerprint"] = "A4:0F:B1:0A:9D:33:64:03"
        env["server"]["name"] = "xrpldashboard"
        signed_hash = self._hash(env)
        env["proof"]["check_v09_signature"] = {
            "canonical_hash": signed_hash,
            "signer": "A4:0F:B1:0A:9D:33:64:03",
        }
        assert self._hash(env) == signed_hash


class TestOpenapiReadApiPaths:
    @pytest.mark.parametrize("path", READ_API_PATHS)
    def test_path_is_registered(self, src, path):
        assert f'("{path}"' in src or f'"{path}",' in src

    @pytest.mark.parametrize("path", READ_API_PATHS)
    def test_route_actually_exists(self, src, path):
        """Documenting a path that 404s is the /escrow-supply mistake in
        reverse. Each documented path must have a real @app.route."""
        assert f'@app.route("{path}")' in src

    def test_all_seven_registered(self, src):
        block = src[src.index("# ---- Live JSON read-API surfaces"):]
        block = block[:block.index("_XRP_ESCROW_STALE_AFTER")] \
            if "_XRP_ESCROW_STALE_AFTER" in block else block
        assert sum(1 for p in READ_API_PATHS if f'"{p}"' in block) == 7

    def test_envelope_is_described_not_overclaimed(self, src):
        """Describe the envelope; do not promise the signature is valid."""
        i = src.index("_read_api_note")
        note = src[i:i + 600]
        assert "Verify" in note
        assert "guaranteed" not in note.lower()


class TestAnchorHistoryScrub:
    def test_anchor_7_has_no_mirror_hostname(self):
        md = open(ANCHORS_MD, encoding="utf-8").read()
        seven = md.split("## Anchor #7")[1].split("## Anchor #8")[0]
        assert "DockVault" not in seven
        assert "dockvault" not in seven

    def test_the_recovery_fact_survives_the_scrub(self):
        """Scrub the hostname, keep the record: #7's leaf WAS restored
        from a backup, and that is the load-bearing fact."""
        md = open(ANCHORS_MD, encoding="utf-8").read()
        seven = md.split("## Anchor #7")[1].split("## Anchor #8")[0]
        assert "offline mirror" in seven
        assert "restored" in seven

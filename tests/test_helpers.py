"""Unit tests for pure helpers in app.py.

These functions don't touch the network or the filesystem (or when they
do, we feed them tmp_path), so they're cheap and deterministic — they
catch regressions in the small parsers and validators that the routes
depend on.
"""

import json
import time
from datetime import datetime, timezone, timedelta

import pytest

import app as app_module


# ---------- _is_xrpl_address ----------

class TestIsXrplAddress:
    def test_known_good(self):
        assert app_module._is_xrpl_address("rEhxGqkqPPSxQ3P25J66ft5TwpzV14k2de")

    def test_empty_string(self):
        assert not app_module._is_xrpl_address("")

    def test_none(self):
        assert not app_module._is_xrpl_address(None)

    def test_wrong_prefix(self):
        assert not app_module._is_xrpl_address("xEhxGqkqPPSxQ3P25J66ft5TwpzV14k2de")

    def test_too_short(self):
        assert not app_module._is_xrpl_address("rShort")

    def test_too_long(self):
        assert not app_module._is_xrpl_address("r" + "A" * 50)

    def test_invalid_chars(self):
        # 0, O, I, l are explicitly excluded from XRPL base58
        assert not app_module._is_xrpl_address("r" + "0" * 30)

    def test_non_string(self):
        assert not app_module._is_xrpl_address(12345)


# ---------- _is_valid_currency ----------

class TestIsValidCurrency:
    def test_three_char_ascii(self):
        assert app_module._is_valid_currency("USD")
        assert app_module._is_valid_currency("XRP")

    def test_forty_char_hex(self):
        assert app_module._is_valid_currency("A" * 40)
        assert app_module._is_valid_currency("0" * 40)

    def test_wrong_length(self):
        assert not app_module._is_valid_currency("US")
        assert not app_module._is_valid_currency("USDC")
        assert not app_module._is_valid_currency("A" * 39)

    def test_three_char_disallowed_chars(self):
        # _CURRENCY_ASCII_CHARS allows alphanumerics + a small punctuation
        # set. Whitespace and slash are not in the allowed set.
        assert not app_module._is_valid_currency("U D")
        assert not app_module._is_valid_currency("U/D")
        # Non-ASCII bytes likewise rejected
        assert not app_module._is_valid_currency("ÜSD")

    def test_empty(self):
        assert not app_module._is_valid_currency("")
        assert not app_module._is_valid_currency(None)


# ---------- _short_addr ----------

class TestShortAddr:
    def test_long_address(self):
        out = app_module._short_addr("rEhxGqkqPPSxQ3P25J66ft5TwpzV14k2de")
        assert out == "rEhxGq…k2de"

    def test_short_string_unchanged(self):
        # Strings 14 chars or shorter pass through untouched
        assert app_module._short_addr("rShort") == "rShort"

    def test_none(self):
        assert app_module._short_addr(None) is None

    def test_empty(self):
        assert app_module._short_addr("") is None


# ---------- _decode_currency_hex ----------

class TestDecodeCurrencyHex:
    def test_ascii_currency(self):
        # "RLUSD" padded with NUL to 40 chars hex
        hex_str = "RLUSD".encode().hex().upper().ljust(40, "0")
        assert app_module._decode_currency_hex(hex_str) == "RLUSD"

    def test_wrong_length(self):
        assert app_module._decode_currency_hex("DEAD") is None
        assert app_module._decode_currency_hex("X" * 41) is None

    def test_non_hex(self):
        assert app_module._decode_currency_hex("Z" * 40) is None

    def test_non_ascii_bytes(self):
        # All-zeros decodes to empty after rstrip -> None
        assert app_module._decode_currency_hex("0" * 40) is None

    def test_unprintable_bytes(self):
        # Control char 0x07 is not printable
        assert app_module._decode_currency_hex("07" + "0" * 38) is None


# ---------- _format_xrp ----------

class TestFormatXrp:
    def test_one_xrp(self):
        assert app_module._format_xrp(1_000_000) == "1.00 XRP"

    def test_thousands_separator(self):
        assert app_module._format_xrp(1_234_567_890) == "1,234.57 XRP"

    def test_zero(self):
        assert app_module._format_xrp(0) == "0.00 XRP"

    def test_none(self):
        assert app_module._format_xrp(None) is None


# ---------- _format_token_amount ----------

class TestFormatTokenAmount:
    def test_large_value(self):
        assert app_module._format_token_amount(1234.5678, "USD") == "1,234.57 USD"

    def test_mid_value(self):
        # Between 1 and 1000 -> 4 decimals
        assert app_module._format_token_amount(12.34, "USD") == "12.3400 USD"

    def test_small_value(self):
        # Below 1 -> 8 decimals (precision matters for low-value tokens)
        out = app_module._format_token_amount(0.00012345, "MEME")
        assert out == "0.00012345 MEME"

    def test_invalid_value(self):
        assert app_module._format_token_amount("garbage", "USD") == "? USD"

    def test_none_value(self):
        assert app_module._format_token_amount(None, "USD") == "? USD"


# ---------- _humanize_seconds ----------

class TestHumanizeSeconds:
    def test_none_returns_dash(self):
        assert app_module._humanize_seconds(None) == "—"

    def test_seconds(self):
        assert app_module._humanize_seconds(45) == "45s"

    def test_minutes_and_seconds(self):
        assert app_module._humanize_seconds(125) == "2m 5s"

    def test_hours_and_minutes(self):
        assert app_module._humanize_seconds(3 * 3600 + 17 * 60) == "3h 17m"

    def test_zero(self):
        assert app_module._humanize_seconds(0) == "0s"


# ---------- _iso_to_age_seconds ----------

class TestIsoToAgeSeconds:
    def test_none_input(self):
        assert app_module._iso_to_age_seconds(None) is None

    def test_empty_string(self):
        assert app_module._iso_to_age_seconds("") is None

    def test_garbage(self):
        assert app_module._iso_to_age_seconds("not a date") is None

    def test_recent_iso(self):
        ten_min_ago = datetime.now(timezone.utc) - timedelta(minutes=10)
        age = app_module._iso_to_age_seconds(ten_min_ago.isoformat())
        # ~600s, allow drift for execution time
        assert age is not None
        assert 590 <= age <= 610

    def test_handles_z_suffix(self):
        # "...Z" is a valid ISO format the helper normalizes
        five_min_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
        # Build "Z" form manually
        iso_z = five_min_ago.strftime("%Y-%m-%dT%H:%M:%SZ")
        age = app_module._iso_to_age_seconds(iso_z)
        assert age is not None
        assert 290 <= age <= 310


# ---------- _safe_load_json ----------

class TestSafeLoadJson:
    def test_missing_file(self, tmp_path):
        assert app_module._safe_load_json(str(tmp_path / "nope.json")) is None

    def test_valid_json(self, tmp_path):
        p = tmp_path / "ok.json"
        p.write_text(json.dumps({"k": 1}))
        assert app_module._safe_load_json(str(p)) == {"k": 1}

    def test_garbage_returns_none(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        assert app_module._safe_load_json(str(p)) is None


# ---------- _file_age_seconds ----------

class TestFileAgeSeconds:
    def test_missing_file(self, tmp_path):
        assert app_module._file_age_seconds(str(tmp_path / "nope")) is None

    def test_existing_file(self, tmp_path):
        p = tmp_path / "fresh"
        p.write_text("hi")
        age = app_module._file_age_seconds(str(p))
        assert age is not None and 0 <= age < 5


# ---------- _tail_lines ----------

class TestTailLines:
    def test_missing_file(self, tmp_path):
        assert app_module._tail_lines(str(tmp_path / "nope")) == []

    def test_returns_last_n(self, tmp_path):
        p = tmp_path / "log"
        p.write_text("\n".join(f"line{i}" for i in range(20)))
        out = app_module._tail_lines(str(p), n=3)
        assert out == ["line17", "line18", "line19"]

    def test_short_file(self, tmp_path):
        p = tmp_path / "log"
        p.write_text("only-line")
        out = app_module._tail_lines(str(p), n=10)
        assert out == ["only-line"]


# ---------- _pools_snapshot_label ----------

class TestPoolsSnapshotLabel:
    def test_returns_friendly_string_when_state_present(self):
        # The repo ships with amm_rank_state.json populated; helper should
        # produce a "Month Day, Year" string from finished_at.
        out = app_module._pools_snapshot_label()
        # Could be None in an empty checkout; if non-None, must look right.
        if out is not None:
            assert "," in out
            # Year should be 4 digits at the end
            assert out.split(",")[-1].strip().isdigit()
            assert len(out.split(",")[-1].strip()) == 4

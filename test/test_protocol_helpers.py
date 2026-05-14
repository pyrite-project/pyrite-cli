import pytest

from cli.utils.Flash import (
    _grep_size_after_ok,
    _grep_raw_start,
    _extract_raw_bytes,
    _strip_repl_trailer,
)

# ── _grep_size_after_ok ─────────────────────────────────────────────


class TestGrepSizeAfterOk:
    def test_normal_integer(self):
        assert _grep_size_after_ok(b"OK12345\nrest") == 12345

    def test_with_carriage_return(self):
        assert _grep_size_after_ok(b"OK12345\r\nrest") == 12345

    def test_negative_number(self):
        assert _grep_size_after_ok(b"OK-1\n") == -1

    def test_no_ok_marker(self):
        assert _grep_size_after_ok(b"garbage data") == -1

    def test_ok_but_no_newline(self):
        assert _grep_size_after_ok(b"OK55") == -1

    def test_non_numeric_size(self):
        assert _grep_size_after_ok(b"OKabc\n") == -1

    def test_first_newline_only(self):
        assert _grep_size_after_ok(b"OK123\n456\n") == 123

    def test_trailing_garbage_ignored(self):
        assert _grep_size_after_ok(b"OK42\ntrailing") == 42

    def test_empty_bytes(self):
        assert _grep_size_after_ok(b"") == -1


# ── _grep_raw_start ─────────────────────────────────────────────────


class TestGrepRawStart:
    def test_basic(self):
        # "h" is at index 4 in b"OK5\nhello"
        assert _grep_raw_start(b"OK5\nhello") == 4

    def test_with_carriage_return(self):
        # "h" is at index 5 in b"OK5\r\nhello"
        assert _grep_raw_start(b"OK5\r\nhello") == 5

    def test_no_ok_marker(self):
        assert _grep_raw_start(b"nope") == -1

    def test_ok_but_no_newline(self):
        assert _grep_raw_start(b"OK99") == -1

    def test_data_after_newline(self):
        # "a" is at index 4 in b"OK3\nabcdef"
        assert _grep_raw_start(b"OK3\nabcdef") == 4
        assert _grep_raw_start(b"OK3\nabc") == 4

    def test_empty_bytes(self):
        assert _grep_raw_start(b"") == -1


# ── _extract_raw_bytes ──────────────────────────────────────────────


class TestExtractRawBytes:
    def test_normal_with_trailers(self):
        buf = b"OK5\nhello\x04\x04>"
        result = _extract_raw_bytes(buf, 5)
        assert result == b"hello"

    def test_truncated_to_expected_size(self):
        buf = b"OK5\nhello_world\x04"
        result = _extract_raw_bytes(buf, 5)
        assert result == b"hello"

    def test_expected_size_from_protocol(self):
        buf = b"OK5\nhello\x04\x04>"
        result = _extract_raw_bytes(buf, -1)
        assert result == b"hello"

    def test_multiple_trailer_markers(self):
        buf = b"OK3\nabc\x04\x04"
        result = _extract_raw_bytes(buf, 3)
        assert result == b"abc"

    def test_raises_when_no_ok_marker(self):
        with pytest.raises(RuntimeError, match="响应格式错误"):
            _extract_raw_bytes(b"no_ok_here", 10)

    def test_raises_when_data_too_short(self):
        buf = b"OK100\nshort"
        with pytest.raises(RuntimeError, match="数据不完整"):
            _extract_raw_bytes(buf, 100)

    def test_raises_when_cannot_parse_size(self):
        buf = b"OK\n"
        with pytest.raises(RuntimeError, match="无法解析文件大小"):
            _extract_raw_bytes(buf, -1)

    def test_exact_size_no_trailer(self):
        buf = b"OK3\nabc"
        result = _extract_raw_bytes(buf, 3)
        assert result == b"abc"

    def test_larger_buffer_with_extra_data(self):
        buf = b"OK3\nabc\x04\x04>garbage"
        result = _extract_raw_bytes(buf, 3)
        assert result == b"abc"


# ── _strip_repl_trailer ─────────────────────────────────────────────


class TestStripReplTrailer:
    def test_double_execute_with_prompt(self):
        assert _strip_repl_trailer(b"data\x04\x04>") == b"data"

    def test_double_execute(self):
        assert _strip_repl_trailer(b"data\x04\x04") == b"data"

    def test_single_execute(self):
        assert _strip_repl_trailer(b"data\x04") == b"data"

    def test_no_trailer(self):
        assert _strip_repl_trailer(b"data") == b"data"

    def test_empty_data(self):
        assert _strip_repl_trailer(b"") == b""

    def test_only_trailer(self):
        assert _strip_repl_trailer(b"\x04\x04>") == b""

    def test_prefers_longest_match(self):
        # Should strip \x04\x04> (the longest matching trailer)
        assert _strip_repl_trailer(b"x\x04\x04>") == b"x"

    def test_binary_data_with_0x04_not_at_end(self):
        buf = bytes(range(256))
        result = _strip_repl_trailer(buf)
        # Should not touch data when 0x04 is not at the end
        assert result == buf

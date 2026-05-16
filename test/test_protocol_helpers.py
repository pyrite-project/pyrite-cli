import pytest

from cli.utils.flash import (
    _strip_repl_trailer,
)

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

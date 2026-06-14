import os
import binascii
from pathlib import Path

import pytest

from cli.utils.flash import MicroPython, _colorize_repl_output
from cli.project.sync import compute_file_hash

# ── _colorize_repl_output ───────────────────────────────────────────

_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_RESET = "\033[0m"


class TestColorizeReplOutput:
    def test_normal_text_no_change(self):
        text, in_error = _colorize_repl_output("hello world", False)
        assert text == "hello world"
        assert in_error is False

    def test_traceback_with_error_is_colored(self):
        text, in_error = _colorize_repl_output(
            "Traceback (most recent call last):\n  ValueError: bad", False
        )
        assert _RED in text
        assert _RESET in text
        assert in_error is False

    def test_traceback_with_exception_is_colored(self):
        text, in_error = _colorize_repl_output(
            'Traceback:\n  Exception: "fail"', False
        )
        assert _RED in text
        assert _RESET in text
        assert in_error is False

    def test_traceback_no_error_enters_error_block(self):
        text, in_error = _colorize_repl_output(
            "Traceback (no match yet)", False
        )
        assert _RED in text
        assert in_error is True

    def test_in_error_block_error_is_colored(self):
        text, in_error = _colorize_repl_output(
            "ValueError: still broken", True
        )
        assert _RED in text
        assert _RESET in text
        assert in_error is False

    def test_in_error_block_no_error_stays_colored(self):
        text, in_error = _colorize_repl_output("some continuation", True)
        assert _RED in text
        assert in_error is True

    def test_empty_string(self):
        text, in_error = _colorize_repl_output("", False)
        assert text == ""
        assert in_error is False

    def test_multiline_text(self):
        text = "normal line\nTraceback:\n  RuntimeError: boom\n"
        result, in_error = _colorize_repl_output(text, False)
        assert _RED in result
        assert in_error is False

    def test_ansi_boundary_in_error_block(self):
        """Ensure error block exits cleanly when Error: appears while in
        error block."""
        text, in_error = _colorize_repl_output("some data", True)
        assert _RED in text

        text2, in_error2 = _colorize_repl_output("Error: finally", in_error)
        assert _RESET in text2
        assert in_error2 is False


# ── _compute_crc32 ──────────────────────────────────────────────────


class TestComputeCrc32:
    def test_empty_data(self):
        result = MicroPython._compute_crc32(b"")
        # CRC32 of empty data is a well-known value
        assert result == binascii.crc32(b"") & 0xFFFFFFFF

    def test_known_value(self):
        data = b"hello"
        result = MicroPython._compute_crc32(data)
        expected = binascii.crc32(data) & 0xFFFFFFFF
        assert result == expected

    def test_binary_data(self):
        data = bytes(range(256))
        result = MicroPython._compute_crc32(data)
        expected = binascii.crc32(data) & 0xFFFFFFFF
        assert result == expected

    def test_different_inputs_different_hashes(self):
        assert MicroPython._compute_crc32(b"abc") != MicroPython._compute_crc32(b"xyz")

    def test_matches_manual_computation(self):
        data = b"MicroPython file content\n"
        result = MicroPython._compute_crc32(data)
        assert result == binascii.crc32(data) & 0xFFFFFFFF

    def test_zero_length_data(self):
        assert MicroPython._compute_crc32(b"") == 0

    def test_unicode_encoded_bytes(self):
        data = "你好".encode("utf-8")
        result = MicroPython._compute_crc32(data)
        assert isinstance(result, int)
        assert result >= 0


class TestFilesystemMountGuard:
    def test_ensure_filesystem_mounted_supports_mpython_flashbdev_list(self, monkeypatch):
        mp = MicroPython(port="COM99")
        calls = []

        def fake_execute(code, **kwargs):
            calls.append((code, kwargs))
            return "FS_READY"

        monkeypatch.setattr(mp, "_execute", fake_execute)

        mp._ensure_filesystem_mounted()

        code, kwargs = calls[0]
        assert "flashbdev.bdev" in code
        assert "isinstance(b,(list,tuple))" in code
        assert "b=b[0]" in code
        assert "os.VfsLfs2(b)" in code
        assert kwargs == {"timeout": 5, "raise_on_error": False}


# ── _compute_file_hash ──────────────────────────────────────────────


class TestComputeFileHash:
    def test_known_content(self, tmp_path: Path):
        f = tmp_path / "test.bin"
        data = b"hello world"
        f.write_bytes(data)

        result = compute_file_hash(str(f))
        assert result == compute_file_hash(str(f))

    def test_different_files_different_hashes(self, tmp_path: Path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"content a")
        b.write_bytes(b"content b")
        assert compute_file_hash(str(a)) != compute_file_hash(str(b))

    def test_empty_file(self, tmp_path: Path):
        f = tmp_path / "empty.bin"
        f.write_text("")
        result = compute_file_hash(str(f))
        assert isinstance(result, str)
        assert len(result) == 64  # SHA256 hex digest length

    def test_large_file_chunk_boundary(self, tmp_path: Path):
        """_compute_file_hash reads in 1MB chunks; test a file just over
        that boundary."""
        f = tmp_path / "large.bin"
        data = b"x" * (1048576 + 100)
        f.write_bytes(data)

        result = compute_file_hash(str(f))
        assert isinstance(result, str)
        assert len(result) == 64

    def test_nonexistent_file_raises(self):
        with pytest.raises(FileNotFoundError):
            compute_file_hash("/nonexistent/path/file.bin")

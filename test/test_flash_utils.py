import os
import binascii
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cli.utils.flash import MicroPython, _colorize_repl_output
from cli.project.sync import ProjectSyncManager, compute_file_hash
from cli.utils.compiler import _compile_to_mpy

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


class TestFlashBatchHelpers:
    def test_remote_dirs_expand_nested_parents(self):
        dirs = MicroPython._remote_dirs_for_paths([
            "/app/main.py",
            "/app/lib/drivers/sensor.py",
            "pkg/mod.py",
        ])

        assert dirs == [
            "/app",
            "/app/lib",
            "/app/lib/drivers",
            "pkg",
        ]

    def test_batch_verify_parses_single_repl_response(self, monkeypatch):
        mp = MicroPython(port="COM99")
        calls = []

        def fake_execute(code, **kwargs):
            calls.append((code, kwargs))
            return "OK 0\nBAD 1 3 -1\nERR 2 OSError"

        monkeypatch.setattr(mp, "_execute", fake_execute)

        result = mp._verify_files_on_device_batch(
            [
                ("/ok.py", 2),
                ("/bad.py", 4),
                ("/err.py", 1),
            ],
            "size",
            {},
        )

        assert result == {
            "/ok.py": True,
            "/bad.py": False,
            "/err.py": False,
        }
        assert len(calls) == 1
        assert "entries =" in calls[0][0]
        assert calls[0][1]["timeout"] >= 5


class TestCompilerCache:
    def test_compile_to_mpy_reuses_cache(self, tmp_path: Path, monkeypatch):
        source = tmp_path / "main.py"
        source.write_text("print('cached')\n", encoding="utf-8")
        cache_dir = tmp_path / "cache"
        calls = []

        class FakeRunResult:
            returncode = 0

            def __init__(self, out_path: str):
                self.stderr = MagicMock()
                self.stderr.read.return_value = b""
                self._out_path = out_path

            def wait(self, timeout: int):
                Path(self._out_path).write_bytes(b"compiled")

        class FakeMpyCross:
            __version__ = "9.9"

            @staticmethod
            def set_version(**_kwargs):
                return None

            @staticmethod
            def run(*args, **_kwargs):
                calls.append(args)
                out_path = args[args.index("-o") + 1]
                return FakeRunResult(out_path)

        monkeypatch.setitem(sys.modules, "mpy_cross", FakeMpyCross)

        first_path, first_tmp = _compile_to_mpy(
            str(source), bytecode_ver=6, arch="xtensa", cache_dir=str(cache_dir)
        )
        second_path, second_tmp = _compile_to_mpy(
            str(source), bytecode_ver=6, arch="xtensa", cache_dir=str(cache_dir)
        )

        assert Path(first_path).read_bytes() == b"compiled"
        assert first_path == second_path
        assert first_tmp is None
        assert second_tmp is None
        assert len(calls) == 1


class TestProjectFlashBatching:
    def test_project_flash_batches_changed_files(self, tmp_path: Path):
        (tmp_path / "main.py").write_text("print('new')\n", encoding="utf-8")
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "sensor.py").write_text("print('sensor')\n", encoding="utf-8")

        hash_config = tmp_path / "pyrite_file_config.json"
        hash_config.write_text(
            '{"version": 1, "hash_algorithm": "sha256", "files": {"main.py": "old"}}',
            encoding="utf-8",
        )

        mp = MagicMock()
        mp.flash_entries.return_value = [
            (str(tmp_path / "main.py"), "/app/main.py", True),
            (str(tmp_path / "lib" / "sensor.py"), "/app/lib/sensor.py", True),
        ]

        results = ProjectSyncManager(mp).flash(
            str(tmp_path),
            "/app",
            hash_config_path=str(hash_config),
            bytecode_ver=6,
            arch="xtensa",
        )

        assert results == mp.flash_entries.return_value
        mp.flash_entries.assert_called_once()
        entries = mp.flash_entries.call_args.args[0]
        assert entries == [
            (str(tmp_path / "main.py"), "/app/main.py"),
            (str(tmp_path / "lib" / "sensor.py"), "/app/lib/sensor.py"),
        ]
        assert mp.flash_entries.call_args.kwargs["bytecode_ver"] == 6
        assert mp.flash_entries.call_args.kwargs["arch"] == "xtensa"
        mp.flash_file.assert_not_called()


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

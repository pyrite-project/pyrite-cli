import os
import binascii
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cli.utils.flash import (
    BATCH_ACK_EVERY,
    FLASH,
    FLASH_PROGRAM,
    FLASH_SUFFIX,
    MicroPython,
    _build_inline_batch_verify_code,
    _build_inline_verify_code,
    _colorize_repl_output,
    _compute_block_crc32,
    _decide_delta_flash,
    _parse_remote_block_crc_output,
)
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


class TestDeltaFlashHelpers:
    def test_compute_block_crc32_splits_data(self):
        blocks = _compute_block_crc32(b"abcde", 2)

        assert blocks == [
            (binascii.crc32(b"ab") & 0xFFFFFFFF, 2),
            (binascii.crc32(b"cd") & 0xFFFFFFFF, 2),
            (binascii.crc32(b"e") & 0xFFFFFFFF, 1),
        ]

    def test_remote_missing_falls_back_to_full(self):
        decision = _decide_delta_flash(
            4,
            None,
            _compute_block_crc32(b"abcd", 2),
            None,
            2,
        )

        assert decision.action == "full"

    def test_equal_blocks_skip_flash(self):
        blocks = _compute_block_crc32(b"abcd", 2)

        decision = _decide_delta_flash(4, 4, blocks, blocks, 2)

        assert decision.action == "skip"
        assert decision.offset == 0

    def test_middle_difference_writes_suffix_from_first_bad_block(self):
        local = _compute_block_crc32(b"aabbcc", 2)
        remote = _compute_block_crc32(b"aaxxcc", 2)

        decision = _decide_delta_flash(6, 6, local, remote, 2)

        assert decision.action == "suffix"
        assert decision.offset == 2
        assert decision.truncate is False

    def test_matching_prefix_and_longer_local_appends(self):
        local = _compute_block_crc32(b"aabbcc", 2)
        remote = _compute_block_crc32(b"aabb", 2)

        decision = _decide_delta_flash(6, 4, local, remote, 2)

        assert decision.action == "append"
        assert decision.offset == 4

    def test_matching_prefix_and_shorter_local_truncates_without_rewrite(self):
        local = _compute_block_crc32(b"aabb", 2)
        remote = _compute_block_crc32(b"aabbcc", 2)

        decision = _decide_delta_flash(4, 6, local, remote, 2)

        assert decision.action == "truncate"
        assert decision.offset == 4
        assert decision.truncate is True

    def test_shorter_local_with_difference_writes_suffix_then_truncates(self):
        local = _compute_block_crc32(b"aaxx", 2)
        remote = _compute_block_crc32(b"aabbcc", 2)

        decision = _decide_delta_flash(4, 6, local, remote, 2)

        assert decision.action == "suffix"
        assert decision.offset == 2
        assert decision.truncate is True

    def test_suffix_script_rebuilds_file_when_truncate_is_unavailable(self):
        assert "f.truncate(final_size)" in FLASH_SUFFIX
        assert "except Exception:" in FLASH_SUFFIX
        assert "with open(tmp,'wb') as dst:" in FLASH_SUFFIX
        assert "os.rename(FILE,bak)" in FLASH_SUFFIX
        assert "os.rename(tmp,FILE)" in FLASH_SUFFIX

    def test_single_file_scripts_use_sparse_ack_and_batched_flush(self):
        for script in (FLASH, FLASH_SUFFIX):
            assert "ack_every" in script
            assert "ack_count" in script
            assert "ack_count" in script and "%ack_every" in script.replace(" ", "")
            assert "if f_size:" in script or "if remaining:" in script
            assert script.count("sys.stdout.write('+')") == 1

    def test_upload_scripts_use_small_serial_read_buffer(self):
        for script in (FLASH, FLASH_SUFFIX, FLASH_PROGRAM):
            assert "usb.read(min(64," in script.replace(" ", "")

    def test_inline_verify_code_checks_size(self):
        code = _build_inline_verify_code("/app.py", 123, "size", None, 4096)

        assert "_verify_path='/app.py'" in code
        assert "_expected_size=123" in code
        assert "os.stat(_verify_path)[6]" in code
        assert "VERIFY_SIZE" in code
        assert "ubinascii" not in code

    def test_inline_verify_code_checks_crc32_after_size(self):
        code = _build_inline_verify_code("/app.py", 123, "crc32", 0x1234, 8192)

        assert "_expected_size=123" in code
        assert "_expected_crc=4660" in code
        assert "import gc,ubinascii" in code
        assert "_vf.read(4096)" in code
        assert "VERIFY_CRC" in code

    def test_inline_batch_verify_code_checks_files(self):
        code = _build_inline_batch_verify_code(
            [("/a.py", 3), ("/b.py", 4)],
            "crc32",
            {"/a.py": 1, "/b.py": 2},
            2048,
        )

        assert "_verify_entries=[('/a.py', 3, 1), ('/b.py', 4, 2)]" in code
        assert "VERIFY_SIZE" in code
        assert "VERIFY_CRC" in code
        assert "_vf.read(2048)" in code

    def test_last_block_size_mismatch_is_a_suffix_difference(self):
        local = [(123, 2)]
        remote = [(123, 1)]

        decision = _decide_delta_flash(2, 1, local, remote, 2)

        assert decision.action == "suffix"
        assert decision.offset == 0

    def test_parse_remote_block_crc_output(self):
        parsed = _parse_remote_block_crc_output(
            "SIZE 5\nBLOCK 0 123 4\nBLOCK 1 45 1\nEND\n"
        )

        assert parsed.missing is False
        assert parsed.error is None
        assert parsed.size == 5
        assert parsed.blocks == [(123, 4), (45, 1)]

    @pytest.mark.parametrize("output", ["MISSING\n", "ERR OSError('bad')\n"])
    def test_parse_remote_block_crc_output_fallback_markers(self, output):
        parsed = _parse_remote_block_crc_output(output)

        assert parsed.blocks is None
        assert parsed.size is None

    @pytest.mark.parametrize(
        "output",
        [
            "",
            "SIZE 5\nBLOCK 1 123 4\nEND\n",
            "SIZE 5\nBLOCK 0 123 4\n",
            "BLOCK 0 123 4\nEND\n",
        ],
    )
    def test_parse_remote_block_crc_output_rejects_bad_format(self, output):
        parsed = _parse_remote_block_crc_output(output)

        assert parsed.error is not None
        assert parsed.blocks is None


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


class TestUploadAckWindow:
    def test_slow_serial_uploads_wait_for_every_chunk(self):
        assert MicroPython(port="COM99", baudrate=115200)._upload_ack_every() == 1
        assert MicroPython(port="COM99", baudrate=230400)._upload_ack_every() == 1

    def test_fast_serial_uploads_keep_sparse_ack(self):
        assert (
            MicroPython(port="COM99", baudrate=921600)._upload_ack_every()
            == BATCH_ACK_EVERY
        )

    def test_flash_file_passes_adaptive_ack_to_script_and_sender(
        self, tmp_path, monkeypatch
    ):
        local = tmp_path / "payload.bin"
        local.write_bytes(b"hello")
        mp = MicroPython(port="COM99", baudrate=115200)
        mp.config.auto_compile = False
        mp.config.delta_flash = "off"
        mp.config.verify = "off"
        mp.config.max_retries = 0
        calls = []

        monkeypatch.setattr(mp, "_enter_raw_repl", lambda: None)
        monkeypatch.setattr(mp, "_mkdirs_on_device", lambda paths: None)

        def fake_send(script, *args, **kwargs):
            calls.append((script, kwargs))

        monkeypatch.setattr(mp, "_send_flash_payload", fake_send)

        mp.flash_file(str(local), "/payload.bin", compile=False)

        assert len(calls) == 1
        script, kwargs = calls[0]
        assert "ack_every = 1" in script
        assert kwargs["ack_every"] == 1

    def test_flash_file_inlines_size_verify_without_second_repl_call(
        self, tmp_path, monkeypatch
    ):
        local = tmp_path / "payload.bin"
        local.write_bytes(b"hello")
        mp = MicroPython(port="COM99", baudrate=921600)
        mp.config.auto_compile = False
        mp.config.delta_flash = "off"
        mp.config.verify = "size"
        mp.config.max_retries = 0
        calls = []

        monkeypatch.setattr(mp, "_traffic_log_ctx", lambda: nullcontext())
        monkeypatch.setattr(mp, "_enter_raw_repl", lambda: None)
        monkeypatch.setattr(mp, "_mkdirs_on_device", lambda paths: None)
        monkeypatch.setattr(
            mp,
            "_verify_file_on_device",
            lambda *args, **kwargs: pytest.fail("verify should be inline"),
        )

        def fake_send(script, *args, **kwargs):
            calls.append((script, kwargs))

        monkeypatch.setattr(mp, "_send_flash_payload", fake_send)

        mp.flash_file(str(local), "/payload.bin", compile=False)

        script, kwargs = calls[0]
        assert "_expected_size=5" in script
        assert "VERIFY_SIZE" in script
        assert "VERIFY_CODE" not in script
        assert kwargs["confirm_timeout"] == 10

    def test_flash_file_inlines_crc32_verify(self, tmp_path, monkeypatch):
        local = tmp_path / "payload.bin"
        local.write_bytes(b"hello")
        expected_crc = binascii.crc32(b"hello") & 0xFFFFFFFF
        mp = MicroPython(port="COM99", baudrate=921600)
        mp.config.auto_compile = False
        mp.config.delta_flash = "off"
        mp.config.verify = "crc32"
        mp.config.max_retries = 0
        calls = []

        monkeypatch.setattr(mp, "_traffic_log_ctx", lambda: nullcontext())
        monkeypatch.setattr(mp, "_enter_raw_repl", lambda: None)
        monkeypatch.setattr(mp, "_mkdirs_on_device", lambda paths: None)
        monkeypatch.setattr(
            mp,
            "_verify_file_on_device",
            lambda *args, **kwargs: pytest.fail("verify should be inline"),
        )

        def fake_send(script, *args, **kwargs):
            calls.append((script, kwargs))

        monkeypatch.setattr(mp, "_send_flash_payload", fake_send)

        mp.flash_file(str(local), "/payload.bin", compile=False)

        script, kwargs = calls[0]
        assert f"_expected_crc={expected_crc}" in script
        assert "VERIFY_CRC" in script
        assert "VERIFY_CODE" not in script
        assert kwargs["confirm_timeout"] >= 15


class TestRawReplBaudFallback:
    def test_init_device_state_reconnects_with_common_baud_when_raw_repl_is_silent(
        self, monkeypatch
    ):
        mp = MicroPython(port="COM99", baudrate=921600)
        mp.config.max_retries = 0
        calls = []

        def fake_try_raw_sequence():
            calls.append(("try", mp.baudrate))
            if mp.baudrate == 115200:
                return True, b"raw REPL; CTRL-B to exit\r\n>"
            return False, b""

        monkeypatch.setattr(mp, "_try_raw_repl_sequence", fake_try_raw_sequence)
        monkeypatch.setattr(mp, "_ensure_filesystem_mounted", lambda: calls.append(("fs", mp.baudrate)))
        monkeypatch.setattr(
            mp,
            "_execute",
            lambda *args, **kwargs: calls.append(("execute", mp.baudrate)) or "",
        )

        reconnects = []

        def fake_reconnect_for_baud(baudrate):
            reconnects.append(baudrate)
            mp.baudrate = baudrate
            mp.transport.baudrate = baudrate

        monkeypatch.setattr(mp, "_reconnect_for_baud", fake_reconnect_for_baud)

        mp._init_device_state()

        assert reconnects == [115200]
        assert mp.baudrate == 115200
        assert ("fs", 115200) in calls
        assert ("execute", 115200) in calls


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

    def test_flash_entries_inlines_verify_without_second_repl_call(
        self, tmp_path, monkeypatch
    ):
        local = tmp_path / "main.py"
        local.write_text("print('hi')\n", encoding="utf-8")
        mp = MicroPython(port="COM99")
        mp.config.auto_compile = False
        mp.config.verify = "size"
        mp.config.max_retries = 0
        writes = []

        monkeypatch.setattr(mp, "_traffic_log_ctx", lambda: nullcontext())
        monkeypatch.setattr(mp, "_enter_raw_repl", lambda: None)
        monkeypatch.setattr(mp, "_mkdirs_on_device", lambda paths: None)
        monkeypatch.setattr(mp, "_write", lambda data: writes.append(data))
        monkeypatch.setattr(
            mp,
            "_read_until_marker",
            lambda marker, timeout=30: (
                (True, b"READY") if marker == b"READY" else (True, b"\x04\x04>")
            ),
        )
        monkeypatch.setattr(
            mp,
            "_send_data_with_sparse_ack",
            lambda data_iter, total, **kwargs: list(data_iter),
        )
        monkeypatch.setattr(
            mp,
            "_verify_files_on_device_batch",
            lambda *args, **kwargs: pytest.fail("verify should be inline"),
        )

        result = mp.flash_entries([(str(local), "/main.py")])

        assert result == [(str(local), "/main.py", True)]
        script = writes[0].decode("utf-8", errors="ignore")
        assert "_verify_entries=[('/main.py'," in script
        assert "VERIFY_SIZE" in script
        assert "VERIFY_CODE" not in script


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

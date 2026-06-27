import os
import binascii
import io
import queue
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cli.utils.flash import (
    BATCH_ACK_EVERY,
    FLASH,
    FLASH_DELTA,
    FLASH_PROGRAM,
    MicroPython,
    _build_inline_batch_verify_code,
    _build_inline_verify_code,
    _colorize_repl_output,
    _compute_block_crc32,
    _load_mp_script,
    _WindowsReplEchoFilter,
    _WindowsReplLineEditor,
    _windows_repl_input_reader,
    _windows_repl_key_to_bytes,
)
from cli.project.sync import ProjectSyncManager, compute_file_hash
from cli.utils.build import _compile_to_mpy

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


class _ReplStdout:
    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, text):
        self.buffer.write(text.encode("utf-8"))

    def flush(self):
        pass

    def getvalue(self):
        return self.buffer.getvalue().decode("utf-8", errors="replace")


class _FakeMsvcrt:
    def __init__(self, keys=None):
        self.keys = list(keys or [])

    def kbhit(self):
        return bool(self.keys)

    def getch(self):
        if not self.keys:
            raise OSError("closed")
        self.keys.pop(0)
        return b"\xd6"

    def getwch(self):
        if not self.keys:
            raise OSError("closed")
        return self.keys.pop(0)


class _FakeInput:
    def __init__(self, data, echo_as=None):
        self.data = data
        self.echo_as = echo_as


class _BlockingMsvcrt:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def getwch(self):
        self.started.set()
        self.release.wait(5)
        raise OSError("closed")


class _KeyboardReplTransport:
    def __init__(self):
        self.connected = True
        self.writes = []

    def write(self, data):
        self.writes.append(data)
        if data not in (b"\x03", b"\x02"):
            self.connected = False

    def read(self, _size):
        return b""

    @property
    def in_waiting(self):
        return 0

    def reset_input_buffer(self):
        pass

    @property
    def is_connected(self):
        return self.connected


class _OutputReplTransport:
    def __init__(self, chunks):
        self.connected = True
        self.chunks = list(chunks)
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    def read(self, _size):
        return self.chunks.pop(0)

    @property
    def in_waiting(self):
        if self.chunks:
            return len(self.chunks[0])
        self.connected = False
        return 0

    def reset_input_buffer(self):
        pass

    @property
    def is_connected(self):
        return self.connected


class TestInteractiveReplUnicode:
    def test_windows_repl_key_encoder_escapes_chinese_for_friendly_repl(self):
        assert _windows_repl_key_to_bytes("中", lambda: "") == b"\\u4e2d"

    def test_windows_repl_key_encoder_maps_arrow_key(self):
        assert _windows_repl_key_to_bytes("\xe0", lambda: "H") == b"\x1b[A"

    def test_windows_repl_reader_queues_ascii_escape_for_chinese(self):
        q = queue.Queue()
        stop = threading.Event()

        _windows_repl_input_reader(_FakeMsvcrt(["中"]), q, stop)

        item = q.get_nowait()
        assert item.data == b"\\u4e2d"
        assert item.echo_as == "中"

    def test_windows_repl_echo_filter_restores_split_chinese_echo(self):
        echo_filter = _WindowsReplEchoFilter()
        echo_filter.add(b"\\u4e2d", "中")

        assert echo_filter.feed(b"\\u") == b""
        assert echo_filter.feed(b"4e2d") == "中".encode("utf-8")

    def test_windows_line_editor_buffers_chinese_until_enter(self):
        stdout = _ReplStdout()
        editor = _WindowsReplLineEditor(stdout)

        data, should_exit = editor.handle(_FakeInput(b"\\u4e2d", "中"))

        assert data is None
        assert should_exit is False
        assert stdout.getvalue() == "中"

        data, should_exit = editor.handle(_FakeInput(b"\r"))

        assert data == b"\\u4e2d\r"
        assert should_exit is False

    def test_windows_line_editor_backspace_is_local(self):
        stdout = _ReplStdout()
        editor = _WindowsReplLineEditor(stdout)

        editor.handle(_FakeInput(b"\\u4e2d", "中"))
        data, should_exit = editor.handle(_FakeInput(b"\x08"))

        assert data is None
        assert should_exit is False
        assert stdout.buffer.getvalue().endswith(("\b \b" * 2).encode("utf-8"))

    def test_windows_line_editor_ctrl_c_requests_exit(self):
        editor = _WindowsReplLineEditor(_ReplStdout())

        data, should_exit = editor.handle(_FakeInput(b"\x03"))

        assert data is None
        assert should_exit is True

    def test_windows_repl_input_sends_ascii_escape_only_on_enter(self, monkeypatch):
        transport = _KeyboardReplTransport()
        mp = MicroPython(port="COM99", transport=transport)
        monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt(["中", "\r"]))
        monkeypatch.setattr(sys, "stdout", _ReplStdout())

        mp.repl_()

        assert b"\\u4e2d\r" in transport.writes
        assert b"\xd6" not in transport.writes

    def test_windows_repl_backspace_after_chinese_does_not_send_escape(self, monkeypatch):
        transport = _KeyboardReplTransport()
        mp = MicroPython(port="COM99", transport=transport)
        monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt(["中", "\b", "\r"]))
        monkeypatch.setattr(sys, "stdout", _ReplStdout())

        mp.repl_()

        assert b"\\u4e2d\r" not in transport.writes

    def test_windows_repl_ctrl_c_after_chinese_exits_without_commit(self, monkeypatch):
        transport = _KeyboardReplTransport()
        mp = MicroPython(port="COM99", transport=transport)
        monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt(["中", "\x03"]))
        monkeypatch.setattr(sys, "stdout", _ReplStdout())

        mp.repl_()

        assert b"\\u4e2d\r" not in transport.writes

    def test_windows_repl_blocked_ime_reader_does_not_block_output(self, monkeypatch):
        transport = _OutputReplTransport([b"ok"])
        mp = MicroPython(port="COM99", transport=transport)
        stdout = _ReplStdout()
        fake_msvcrt = _BlockingMsvcrt()
        monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
        monkeypatch.setattr(sys, "stdout", stdout)

        try:
            mp.repl_()
        finally:
            fake_msvcrt.release.set()

        assert "ok" in stdout.getvalue()

    def test_repl_output_decodes_split_utf8_chinese(self, monkeypatch):
        transport = _OutputReplTransport([b"\xe4", b"\xb8", b"\xad"])
        mp = MicroPython(port="COM99", transport=transport)
        stdout = _ReplStdout()
        monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt())
        monkeypatch.setattr(sys, "stdout", stdout)

        mp.repl_()

        assert "中" in stdout.getvalue()
        assert "\ufffd" not in stdout.getvalue()

    def test_repl_command_handler_can_intercept_entered_line(self, monkeypatch):
        transport = _KeyboardReplTransport()
        mp = MicroPython(port="COM99", transport=transport)
        monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt(["r", "u", "n", "\r"]))
        monkeypatch.setattr(sys, "stdout", _ReplStdout())
        handled = []

        def command_handler(data):
            handled.append(data)
            transport.connected = False
            return True

        mp.repl_(command_handler=command_handler)

        assert handled == [b"run\r"]
        assert b"run\r" not in transport.writes

    def test_repl_command_handler_decline_sends_entered_line(self, monkeypatch):
        transport = _KeyboardReplTransport()
        mp = MicroPython(port="COM99", transport=transport)
        monkeypatch.setitem(
            sys.modules,
            "msvcrt",
            _FakeMsvcrt(["p", "r", "i", "n", "t", "(", "1", ")", "\r"]),
        )
        monkeypatch.setattr(sys, "stdout", _ReplStdout())
        handled = []

        def command_handler(data):
            handled.append(data)
            return False

        mp.repl_(command_handler=command_handler)

        assert handled == [b"print(1)\r"]
        assert b"print(1)\r" in transport.writes


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
    def test_mp_scripts_are_loaded_from_external_files(self):
        assert FLASH == _load_mp_script("flash.py")
        assert FLASH_PROGRAM == _load_mp_script("flash_program.py")
        assert FLASH_DELTA == _load_mp_script("flash_delta.py")

    def test_compute_block_crc32_splits_data(self):
        blocks = _compute_block_crc32(b"abcde", 2)

        assert blocks == [
            (binascii.crc32(b"ab") & 0xFFFFFFFF, 2),
            (binascii.crc32(b"cd") & 0xFFFFFFFF, 2),
            (binascii.crc32(b"e") & 0xFFFFFFFF, 1),
        ]

    def test_single_file_scripts_use_sparse_ack_and_batched_flush(self):
        for script in (FLASH, FLASH_DELTA):
            assert "ack_every" in script
            assert "ack_count" in script
            assert "ack_count" in script and "%ack_every" in script.replace(" ", "")
            assert "if f_size:" in script or "if remaining:" in script
            assert script.count("sys.stdout.write('+')") == 1

    def test_upload_scripts_use_small_serial_read_buffer(self):
        for script in (FLASH, FLASH_DELTA, FLASH_PROGRAM):
            assert "usb.read(min(64," in script.replace(" ", "")

    def test_batch_script_flushes_on_ack_window(self):
        assert "f.write(d)\n                f.flush()\n                remaining" not in FLASH_PROGRAM
        assert "if ack_every and ack_count % ack_every == 0:" in FLASH_PROGRAM
        assert "f.flush()\n                    if total_left:" in FLASH_PROGRAM

    def test_delta_header_parser(self):
        action, offset, truncate, transfer_size = MicroPython._parse_delta_header(
            b"OKDELTA:suffix:4096:1:128\nREADY"
        )

        assert action == "suffix"
        assert offset == 4096
        assert truncate is True
        assert transfer_size == 128

    def test_flash_delta_script_embeds_host_block_crc_table(self):
        assert "local_blocks=LOCAL_BLOCKS" in FLASH_DELTA
        assert "sys.stdout.write('DELTA:%s:%d:%d:%d\\n'" in FLASH_DELTA
        assert "print('BLOCK'" not in FLASH_DELTA

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


class _SafeBreakTransport:
    def __init__(self):
        self.connected = True
        self.writes = []
        self.reset_count = 0

    def write(self, data):
        self.writes.append(data)

    def read(self, _size):
        return b""

    @property
    def in_waiting(self):
        return 0

    def reset_input_buffer(self):
        self.reset_count += 1

    @property
    def is_connected(self):
        return self.connected


class TestSafeMain:
    def test_safe_break_sends_ctrl_c_burst_then_clears_input(self, monkeypatch):
        transport = _SafeBreakTransport()
        mp = MicroPython(port="COM99", transport=transport)
        sleeps = []
        monkeypatch.setattr("cli.utils.flash.core.time.sleep", lambda value: sleeps.append(value))

        mp.safe_break(attempts=3, interval=0.01, settle=0.2)

        assert transport.writes == [b"\x03", b"\x03", b"\x03"]
        assert sleeps == [0.01, 0.01, 0.01, 0.2]
        assert transport.reset_count == 1

    def test_safe_main_backup_path_stays_next_to_root_main(self):
        assert (
            MicroPython.safe_main_backup_path(
                "/main.py", timestamp="20260627-010203"
            )
            == "/main.py.pyrite-bak-20260627-010203"
        )
        assert (
            MicroPython.safe_main_backup_path(
                "main.py", timestamp="20260627-010203"
            )
            == "main.py.pyrite-bak-20260627-010203"
        )

    def test_safe_main_plan_targets_only_root_main_and_can_be_disabled(self):
        plan = MicroPython.plan_safe_main_overwrites(
            ["/main.py", "/lib/main.py", "/boot.py", "main.py"],
            enabled=True,
            timestamp="20260627-010203",
        )

        assert [(item.remote_path, item.backup_path) for item in plan] == [
            ("/main.py", "/main.py.pyrite-bak-20260627-010203"),
            ("main.py", "main.py.pyrite-bak-20260627-010203"),
        ]
        assert MicroPython.plan_safe_main_overwrites(["/main.py"], enabled=False) == []

    def test_flash_file_safe_main_backs_up_root_main_before_payload(
        self, tmp_path, monkeypatch
    ):
        local = tmp_path / "main.py"
        local.write_text("print('safe')\n", encoding="utf-8")
        mp = MicroPython(port="COM99")
        mp.config.auto_compile = False
        mp.config.delta_flash = "off"
        mp.config.verify = "off"
        mp.config.max_retries = 0
        calls = []

        monkeypatch.setattr(mp, "_traffic_log_ctx", lambda: nullcontext())
        monkeypatch.setattr(mp, "safe_break", lambda **_kwargs: calls.append(("break", None)))
        monkeypatch.setattr(mp, "_enter_raw_repl", lambda: calls.append(("raw", None)))
        monkeypatch.setattr(mp, "_mkdirs_on_device", lambda paths: calls.append(("mkdirs", paths)))
        monkeypatch.setattr(
            MicroPython,
            "safe_main_backup_path",
            staticmethod(lambda path: f"{path}.pyrite-bak-test"),
        )

        def fake_execute(code, **kwargs):
            calls.append(("execute", code, kwargs))
            return "BACKUP:/main.py.pyrite-bak-test"

        def fake_send(script, *args, **kwargs):
            calls.append(("send", script, kwargs))

        monkeypatch.setattr(mp, "_execute", fake_execute)
        monkeypatch.setattr(mp, "_send_flash_payload", fake_send)

        mp.flash_file(str(local), "/main.py", compile=False, safe_main=True)

        assert [call[0] for call in calls] == [
            "break",
            "raw",
            "execute",
            "mkdirs",
            "send",
        ]
        backup_code = calls[2][1]
        assert "_src='/main.py'" in backup_code
        assert "_dst_base='/main.py.pyrite-bak-test'" in backup_code
        assert calls[2][2] == {"timeout": 10}

    def test_flash_file_safe_main_does_not_touch_non_root_main(
        self, tmp_path, monkeypatch
    ):
        local = tmp_path / "payload.py"
        local.write_text("print('payload')\n", encoding="utf-8")
        mp = MicroPython(port="COM99")
        mp.config.auto_compile = False
        mp.config.delta_flash = "off"
        mp.config.verify = "off"
        mp.config.max_retries = 0
        calls = []

        monkeypatch.setattr(mp, "_traffic_log_ctx", lambda: nullcontext())
        monkeypatch.setattr(mp, "safe_break", lambda **_kwargs: pytest.fail("no safe break"))
        monkeypatch.setattr(mp, "_enter_raw_repl", lambda: calls.append(("raw", None)))
        monkeypatch.setattr(mp, "_mkdirs_on_device", lambda paths: calls.append(("mkdirs", paths)))
        monkeypatch.setattr(mp, "_execute", lambda *args, **kwargs: pytest.fail("no backup"))
        monkeypatch.setattr(
            mp,
            "_send_flash_payload",
            lambda script, *args, **kwargs: calls.append(("send", script, kwargs)),
        )

        mp.flash_file(str(local), "/lib/main.py", compile=False, safe_main=True)

        assert [call[0] for call in calls] == ["raw", "mkdirs", "send"]

    def test_flash_entries_safe_main_backs_up_once_before_batch_payload(
        self, tmp_path, monkeypatch
    ):
        main = tmp_path / "main.py"
        helper = tmp_path / "helper.py"
        main.write_text("print('main')\n", encoding="utf-8")
        helper.write_text("print('helper')\n", encoding="utf-8")
        mp = MicroPython(port="COM99")
        mp.config.auto_compile = False
        mp.config.verify = "off"
        mp.config.max_retries = 0
        calls = []

        monkeypatch.setattr(mp, "_traffic_log_ctx", lambda: nullcontext())
        monkeypatch.setattr(mp, "safe_break", lambda **_kwargs: calls.append(("break", None)))
        monkeypatch.setattr(mp, "_enter_raw_repl", lambda: calls.append(("raw", None)))
        monkeypatch.setattr(mp, "_mkdirs_on_device", lambda paths: calls.append(("mkdirs", paths)))
        monkeypatch.setattr(
            MicroPython,
            "safe_main_backup_path",
            staticmethod(lambda path: f"{path}.pyrite-bak-test"),
        )

        def fake_execute(code, **kwargs):
            calls.append(("execute", code, kwargs))
            return "BACKUP:/main.py.pyrite-bak-test"

        def fake_write(data):
            calls.append(("write", data))

        monkeypatch.setattr(mp, "_execute", fake_execute)
        monkeypatch.setattr(mp, "_write", fake_write)
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
            lambda data_iter, total, **kwargs: calls.append(("payload", total)),
        )

        result = mp.flash_entries(
            [
                (str(main), "/main.py"),
                (str(helper), "/lib/helper.py"),
            ],
            safe_main=True,
        )

        assert result == [
            (str(main), "/main.py", True),
            (str(helper), "/lib/helper.py", True),
        ]
        assert [call[0] for call in calls[:5]] == [
            "break",
            "raw",
            "execute",
            "mkdirs",
            "write",
        ]
        assert calls[2][1].count("_src='/main.py'") == 1
        assert "_src='/lib/helper.py'" not in calls[2][1]


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

    def test_flash_file_delta_embeds_local_blocks_without_remote_crc_roundtrip(
        self, tmp_path, monkeypatch
    ):
        local = tmp_path / "payload.bin"
        local.write_bytes(b"abcdefgh")
        mp = MicroPython(port="COM99", baudrate=921600)
        mp.config.auto_compile = False
        mp.config.delta_flash = "on"
        mp.config.delta_min_size = 1
        mp.config.verify = "off"
        mp.config.max_retries = 0
        calls = []

        monkeypatch.setattr(mp, "_traffic_log_ctx", lambda: nullcontext())
        monkeypatch.setattr(mp, "_enter_raw_repl", lambda: None)
        monkeypatch.setattr(mp, "_mkdirs_on_device", lambda paths: None)

        def fake_delta_send(script, *args, **kwargs):
            calls.append((script, args, kwargs))
            return "suffix"

        monkeypatch.setattr(mp, "_send_delta_flash_payload", fake_delta_send)

        mp.flash_file(str(local), "/payload.bin", compile=False)

        assert len(calls) == 1
        script, args, kwargs = calls[0]
        assert "local_blocks=[" in script
        assert "DELTA:%s:%d:%d:%d" in script
        assert args[0] == str(local)
        assert kwargs["ack_every"] == BATCH_ACK_EVERY

    def test_flash_file_auto_delta_starts_above_chunk_size(
        self, tmp_path, monkeypatch
    ):
        local = tmp_path / "payload.bin"
        local.write_bytes(b"abcde")
        mp = MicroPython(port="COM99", baudrate=921600)
        mp.config.auto_compile = False
        mp.config.chunk_size = 4
        mp.config.delta_flash = "auto"
        mp.config.verify = "off"
        mp.config.max_retries = 0
        calls = []

        monkeypatch.setattr(mp, "_traffic_log_ctx", lambda: nullcontext())
        monkeypatch.setattr(mp, "_enter_raw_repl", lambda: None)
        monkeypatch.setattr(mp, "_mkdirs_on_device", lambda paths: None)
        monkeypatch.setattr(
            mp,
            "_send_flash_payload",
            lambda *args, **kwargs: pytest.fail("expected delta transfer"),
        )

        def fake_delta_send(script, *args, **kwargs):
            calls.append((script, args, kwargs))
            return "suffix"

        monkeypatch.setattr(mp, "_send_delta_flash_payload", fake_delta_send)

        mp.flash_file(str(local), "/payload.bin", compile=False)

        assert len(calls) == 1
        script, _args, kwargs = calls[0]
        assert "block_size=4" in script
        assert kwargs["ack_every"] == BATCH_ACK_EVERY

    def test_flash_file_auto_delta_skips_at_chunk_size(
        self, tmp_path, monkeypatch
    ):
        local = tmp_path / "payload.bin"
        local.write_bytes(b"abcd")
        mp = MicroPython(port="COM99", baudrate=921600)
        mp.config.auto_compile = False
        mp.config.chunk_size = 4
        mp.config.delta_flash = "auto"
        mp.config.verify = "off"
        mp.config.max_retries = 0
        calls = []

        monkeypatch.setattr(mp, "_traffic_log_ctx", lambda: nullcontext())
        monkeypatch.setattr(mp, "_enter_raw_repl", lambda: None)
        monkeypatch.setattr(mp, "_mkdirs_on_device", lambda paths: None)
        monkeypatch.setattr(
            mp,
            "_send_delta_flash_payload",
            lambda *args, **kwargs: pytest.fail("expected full transfer"),
        )

        def fake_send(script, *args, **kwargs):
            calls.append((script, args, kwargs))

        monkeypatch.setattr(mp, "_send_flash_payload", fake_send)

        mp.flash_file(str(local), "/payload.bin", compile=False)

        assert len(calls) == 1
        script, _args, kwargs = calls[0]
        assert "want = min(4, f_size)" in script
        assert kwargs["ack_every"] == BATCH_ACK_EVERY


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

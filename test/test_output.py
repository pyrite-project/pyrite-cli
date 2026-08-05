import json
import os
import base64
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner

from cli.main import app, _norm_path
from cli.project.sync import ProjectSyncManager, compute_file_hash
from cli.utils.errors import humanize_exception
from cli.utils.log import INFO, configure, shutdown

runner = CliRunner()


def _fake_ports():
    return [{"device": "/dev/ttyUSB0", "description": "USB Serial",
             "vid": 0x10C4, "pid": 0xEA60, "serial_number": "001"}]


# ── scan ────────────────────────────────────────────────────────────

class TestScanFormat:
    def test_json_valid(self):
        with patch("cli.main.MicroPython.scan_ports", return_value=_fake_ports()):
            result = runner.invoke(app, ["scan", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["count"] == 1
        assert data["devices"][0]["device"] == "/dev/ttyUSB0"
        assert data["devices"][0]["vid"] == "10C4"

    def test_json_alias_valid(self):
        with patch("cli.main.MicroPython.scan_ports", return_value=_fake_ports()):
            result = runner.invoke(app, ["scan", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["count"] == 1

    def test_json_with_info_includes_brief(self):
        with patch("cli.main.MicroPython.scan_ports", return_value=_fake_ports()), \
             patch("cli.main._fetch_brief", return_value="  micropython ESP32"):
            result = runner.invoke(app, ["scan", "--with-info", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["devices"][0]["brief"] == "micropython ESP32"

    def test_text_no_json(self):
        with patch("cli.main.MicroPython.scan_ports", return_value=_fake_ports()):
            result = runner.invoke(app, ["scan"])
        assert result.exit_code == 0
        assert "{" not in result.stdout

    def test_stderr_has_log_text(self):
        with patch("cli.main.MicroPython.scan_ports", return_value=_fake_ports()):
            result = runner.invoke(app, ["scan"])
        # 默认 CliRunner 将 stderr 混合到 stdout
        assert "ttyUSB0" in result.stdout

    def test_json_stdout_only(self):
        with patch("cli.main.MicroPython.scan_ports", return_value=_fake_ports()):
            result = runner.invoke(app, ["scan", "--format", "json"])
        assert result.stdout.strip().startswith("{")
        # 验证 stdout 的 JSON 行是可解析的（排除可能的 stderr 混合行）
        json_lines = [l for l in result.stdout.splitlines() if l.strip().startswith("{")]
        assert len(json_lines) == 1
        json.loads(json_lines[0])

    def test_env_var_format(self, monkeypatch):
        monkeypatch.setenv("PYRITE_FORMAT", "json")
        with patch("cli.main.MicroPython.scan_ports", return_value=_fake_ports()):
            result = runner.invoke(app, ["scan"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "devices" in data

    def test_empty_json_result(self):
        with patch("cli.main.MicroPython.scan_ports", return_value=[]):
            result = runner.invoke(app, ["scan", "--format", "json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"devices": [], "count": 0}

    def test_invalid_format_rejected(self):
        with patch("cli.main.MicroPython.scan_ports", return_value=_fake_ports()):
            result = runner.invoke(app, ["scan", "--format", "yaml"])
        assert result.exit_code != 0


# ── board-info ───────────────────────────────────────────────────────

class TestBoardInfoFormat:
    _RAW = "FW:micropython 1.22.0\nPLAT:esp32\nHW:ESP32\nREL:1.22.0\nCPU:240000000\nUID:aabbccdd\nRST:PWRON_RESET\nMF:200000\nMA:50000\nFS:2097152/1048576\nFLASH:4194304\nMAC:aa:bb:cc:dd:ee:ff\n"

    def _mp(self):
        mp = MagicMock()
        mp.run.return_value = self._RAW
        return mp

    def test_json_structure(self):
        with patch("cli.reg_commands.debug._mp_factory", return_value=self._mp()):
            result = runner.invoke(app, ["debug", "board-info", "/dev/ttyUSB0", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["firmware"]["platform"] == "esp32"
        assert data["device"]["cpu_hz"] == 240000000
        assert data["memory"]["flash_size"] == 4194304

    def test_json_alias_structure(self):
        with patch("cli.reg_commands.debug._mp_factory", return_value=self._mp()):
            result = runner.invoke(app, ["debug", "board-info", "/dev/ttyUSB0", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["firmware"]["platform"] == "esp32"

    def test_text_no_json(self):
        with patch("cli.reg_commands.debug._mp_factory", return_value=self._mp()):
            result = runner.invoke(app, ["debug", "board-info", "/dev/ttyUSB0"])
        assert result.exit_code == 0
        assert "{" not in result.stdout

    def test_empty_json_result_exits_with_error_payload(self):
        mp = MagicMock()
        mp.run.return_value = ""

        with patch("cli.reg_commands.debug._mp_factory", return_value=mp):
            result = runner.invoke(app, ["debug", "board-info", "/dev/ttyUSB0", "--format", "json"])

        assert result.exit_code == 1
        # JSON 在 stdout 首行（stderr 可能混入后续行）
        data = json.loads(result.stdout.splitlines()[0])
        assert data == {"error": "no_device_info"}


# ── project status ───────────────────────────────────────────────────

class TestProjectStatusFormat:
    def _setup_sync(self, has_diff=True):
        mgr = MagicMock()
        mgr.status.return_value = has_diff
        return mgr

    def test_exit_code_with_diff(self, tmp_path):
        mp = MagicMock()
        mp.detect_tags.return_value = {"ESP32"}
        with patch("cli.reg_commands.project._mp_factory", return_value=mp), \
             patch("cli.reg_commands.project.ProjectSyncManager", return_value=self._setup_sync(True)):
            result = runner.invoke(app, ["project", "status", "/dev/ttyUSB0",
                                         str(tmp_path), "/pyrite"])
        assert result.exit_code == 1

    def test_exit_code_no_diff(self, tmp_path):
        mp = MagicMock()
        mp.detect_tags.return_value = {"ESP32"}
        with patch("cli.reg_commands.project._mp_factory", return_value=mp), \
             patch("cli.reg_commands.project.ProjectSyncManager", return_value=self._setup_sync(False)):
            result = runner.invoke(app, ["project", "status", "/dev/ttyUSB0",
                                         str(tmp_path), "/pyrite"])
        assert result.exit_code == 0

    def test_fmt_passed_to_status(self, tmp_path):
        mp = MagicMock()
        mp.detect_tags.return_value = {"ESP32"}
        mgr = self._setup_sync(False)
        with patch("cli.reg_commands.project._mp_factory", return_value=mp), \
             patch("cli.reg_commands.project.ProjectSyncManager", return_value=mgr):
            runner.invoke(app, ["project", "status", "/dev/ttyUSB0",
                                str(tmp_path), "/pyrite", "--format", "json"])
        mgr.status.assert_called_once()
        assert mgr.status.call_args.kwargs.get("fmt") == "json"

    def test_json_alias_passed_to_status(self, tmp_path):
        mp = MagicMock()
        mp.detect_tags.return_value = {"ESP32"}
        mgr = self._setup_sync(False)
        with patch("cli.reg_commands.project._mp_factory", return_value=mp), \
             patch("cli.reg_commands.project.ProjectSyncManager", return_value=mgr):
            runner.invoke(app, ["project", "status", "/dev/ttyUSB0",
                                str(tmp_path), "/pyrite", "--json"])
        mgr.status.assert_called_once()
        assert mgr.status.call_args.kwargs.get("fmt") == "json"

    def test_diff_flag_passed_to_status(self, tmp_path):
        mp = MagicMock()
        mp.detect_tags.return_value = {"ESP32"}
        mgr = self._setup_sync(False)
        with patch("cli.reg_commands.project._mp_factory", return_value=mp), \
             patch("cli.reg_commands.project.ProjectSyncManager", return_value=mgr):
            runner.invoke(app, ["project", "status", "/dev/ttyUSB0",
                                str(tmp_path), "/pyrite", "--diff"])
        mgr.status.assert_called_once()
        assert mgr.status.call_args.kwargs.get("diff") is True

    def test_empty_project_status_outputs_json(self, tmp_path, capsys):
        mgr = ProjectSyncManager(MagicMock())

        has_diff = mgr.status(str(tmp_path), "/pyrite", fmt="json")

        captured = capsys.readouterr()
        assert has_diff is False
        assert json.loads(captured.out) == {
            "added": [],
            "changed": [],
            "removed": [],
            "ok_count": 0,
        }
        assert captured.err == ""

    def test_status_default_does_not_download_device_file(self, tmp_path, capsys):
        local = tmp_path / "main.py"
        local.write_text("print('new')\n", encoding="utf-8")
        (tmp_path / "pyrite_file_config.json").write_text(
            json.dumps({
                "version": 1,
                "hash_algorithm": "sha256",
                "files": {"main.py": compute_file_hash(str(local))},
            }),
            encoding="utf-8",
        )
        mp = MagicMock()
        mp.run.return_value = str(local.stat().st_size)
        mgr = ProjectSyncManager(mp)

        has_diff = mgr.status(str(tmp_path), "/app")

        captured = capsys.readouterr()
        assert has_diff is False
        assert "[MOD]" not in captured.err
        mp._read_device_file.assert_not_called()

    def test_status_reads_device_file_and_prints_unified_diff(self, tmp_path, capsys):
        local = tmp_path / "main.py"
        local.write_text("print('new')\n", encoding="utf-8")
        mp = MagicMock()
        mp.run.return_value = "13"
        mp._read_device_file.return_value = b"print('old')\n"
        mgr = ProjectSyncManager(mp)

        has_diff = mgr.status(str(tmp_path), "/app", diff=True)

        captured = capsys.readouterr()
        assert has_diff is True
        assert "[MOD]" in captured.err
        assert "--- /app/main.py" in captured.err
        assert "+++ main.py" in captured.err
        assert "-print('old')" in captured.err
        assert "+print('new')" in captured.err
        mp._read_device_file.assert_called_once_with("/app/main.py")

    def test_status_diff_escapes_device_control_chars(self, tmp_path, capsys):
        shutdown()
        configure(console_level=INFO, log_dir=str(tmp_path / "log"), file_enabled=False)
        try:
            local = tmp_path / "main.py"
            local.write_text("print('new')\n", encoding="utf-8")
            mp = MagicMock()
            mp.run.return_value = "4"
            mp._read_device_file.return_value = b"\x1b[2J\n"
            mgr = ProjectSyncManager(mp)

            has_diff = mgr.status(str(tmp_path), "/app", diff=True)

            captured = capsys.readouterr()
            assert has_diff is True
            assert "\x1b[2J" not in captured.err
            assert "\\x1b[2J" in captured.err
        finally:
            shutdown()


# ── project pull ─────────────────────────────────────────────────────

class TestProjectPullFormat:
    def test_device_path_target_rejects_host_escape_paths(self, tmp_path):
        safe, rel, reason = ProjectSyncManager._safe_local_target_for_device_path(
            str(tmp_path), "/", "/cfg.json",
        )
        assert Path(safe).resolve() == (tmp_path / "cfg.json").resolve()
        assert rel == "cfg.json"
        assert reason is None

        unsafe_paths = [
            "/flash/../../evil.txt",
            "C:/Users/x/evil.txt",
            "//server/share/evil.txt",
        ]
        for remote_path in unsafe_paths:
            safe, rel, reason = ProjectSyncManager._safe_local_target_for_device_path(
                str(tmp_path), "/", remote_path,
            )
            assert safe is None
            assert rel is None
            assert reason is not None

    def test_empty_pull_outputs_json(self, tmp_path, capsys):
        mp = MagicMock()
        mp.run.return_value = ""
        mgr = ProjectSyncManager(mp)

        mgr.pull(str(tmp_path), "/pyrite", fmt="json")

        captured = capsys.readouterr()
        assert json.loads(captured.out) == {
            "downloaded": [],
            "skipped": [],
            "failed": [],
        }
        assert captured.err == ""

    def test_backup_always_discovers_device_files(self, tmp_path):
        mp = MagicMock()
        mgr = ProjectSyncManager(mp)
        with patch.object(mgr, "_discover_device_files", return_value=[("/cfg.json", 2)]), \
             patch.object(mgr, "_download_device_files", return_value=True) as download:
            ok = mgr.backup(str(tmp_path), "/", fmt="json")

        assert ok is True
        download.assert_called_once()
        assert download.call_args.args[0] == ["/cfg.json"]
        assert download.call_args.args[1] == [str(tmp_path / "cfg.json").replace("\\", "/")]

    def test_backup_strips_relative_remote_prefix_from_discovered_paths(self, tmp_path):
        mp = MagicMock()
        mgr = ProjectSyncManager(mp)
        with patch.object(mgr, "_discover_device_files", return_value=[("app/main.py", 2)]), \
             patch.object(mgr, "_download_device_files", return_value=True) as download:
            ok = mgr.backup(str(tmp_path), "app", fmt="json")

        assert ok is True
        download.assert_called_once()
        assert download.call_args.args[0] == ["app/main.py"]
        assert download.call_args.args[1] == [str(tmp_path / "main.py").replace("\\", "/")]

    def test_backup_dry_run_json_skips_unsafe_device_paths(self, tmp_path, capsys):
        mp = MagicMock()
        mgr = ProjectSyncManager(mp)

        with patch.object(mgr, "_discover_device_files", return_value=[
            ("/cfg.json", 2),
            ("/flash/../../evil.txt", 4),
            ("C:/Users/x/evil.txt", 4),
            ("//server/share/evil.txt", 4),
        ]):
            ok = mgr.backup(str(tmp_path), "/", dry_run=True, fmt="json")

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert ok is True
        assert data["preview"] == [{
            "remote": "/cfg.json",
            "local": str(tmp_path / "cfg.json").replace("\\", "/"),
        }]
        assert {entry["remote"] for entry in data["skipped"]} == {
            "/flash/../../evil.txt",
            "C:/Users/x/evil.txt",
            "//server/share/evil.txt",
        }
        assert data["failed"] == []
        assert captured.err == ""

    def test_pull_dry_run_json_skips_unsafe_discovered_paths(self, tmp_path, capsys):
        mp = MagicMock()
        mgr = ProjectSyncManager(mp)

        with patch.object(mgr, "_discover_device_files", return_value=[
            ("/flash/cfg.json", 2),
            ("/flash/../../evil.txt", 4),
            ("C:/Users/x/evil.txt", 4),
            ("//server/share/evil.txt", 4),
        ]):
            ok = mgr.pull(str(tmp_path), "/flash", dry_run=True, fmt="json")

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert ok is True
        assert data["preview"] == [{
            "remote": "/flash/cfg.json",
            "local": str(tmp_path / "cfg.json").replace("\\", "/"),
        }]
        assert {entry["remote"] for entry in data["skipped"]} == {
            "/flash/../../evil.txt",
            "C:/Users/x/evil.txt",
            "//server/share/evil.txt",
        }
        assert data["failed"] == []
        assert captured.err == ""


class TestDeviceCommands:
    def test_device_backup_uses_project_manager_backup(self, tmp_path):
        mp = MagicMock()
        mgr = MagicMock()
        mgr.backup.return_value = True
        with patch("cli.reg_commands.device._mp_factory", return_value=mp), \
             patch("cli.reg_commands.device.ProjectSyncManager", return_value=mgr):
            result = runner.invoke(app, [
                "device", "backup", "/dev/ttyUSB0", str(tmp_path), "/",
                "--format", "json",
            ])

        assert result.exit_code == 0
        mgr.backup.assert_called_once()
        assert mgr.backup.call_args.args[:2] == (str(tmp_path), "/")
        assert mgr.backup.call_args.kwargs["fmt"] == "json"

    def test_device_restore_uses_project_manager_restore(self, tmp_path):
        mp = MagicMock()
        mgr = MagicMock()
        mgr.restore.return_value = [("a.txt", "/a.txt", True)]
        with patch("cli.reg_commands.device._mp_factory", return_value=mp), \
             patch("cli.reg_commands.device.ProjectSyncManager", return_value=mgr):
            result = runner.invoke(app, [
                "device", "restore", "/dev/ttyUSB0", str(tmp_path), "/",
                "--dry-run",
            ])

        assert result.exit_code == 0
        mgr.restore.assert_called_once()
        assert mgr.restore.call_args.args[:2] == (str(tmp_path), "/")
        assert mgr.restore.call_args.kwargs["dry_run"] is True


class TestHumanErrors:
    def test_timeout_has_actionable_hint(self):
        message = humanize_exception(TimeoutError("read timed out"))

        assert "操作超时" in message
        assert "--timeout" in message

    def test_raw_repl_no_response_has_device_hint(self):
        message = humanize_exception(RuntimeError("无法进入原始 REPL 模式，设备响应: b''"))

        assert "设备没有进入原始 REPL" in message
        assert "Ctrl+C" in message

    def test_empty_dry_run_pull_outputs_preview_json(self, tmp_path, capsys):
        mp = MagicMock()
        mp.run.return_value = ""
        mgr = ProjectSyncManager(mp)

        mgr.pull(str(tmp_path), "/pyrite", fmt="json", dry_run=True)

        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"preview": []}
        assert captured.err == ""

    def test_transfer_error_outputs_json_and_fails(self, tmp_path, capsys, monkeypatch):
        (tmp_path / "main.py").write_text("print('x')\n", encoding="utf-8")
        mp = MagicMock()
        mp.transport.in_waiting = 0
        mgr = ProjectSyncManager(mp)

        times = iter([0, 31])
        monkeypatch.setattr("cli.project.sync.time.time", lambda: next(times, 31))

        ok = mgr.pull(str(tmp_path), "/pyrite", fmt="json")

        captured = capsys.readouterr()
        assert ok is False
        assert json.loads(captured.out) == {
            "error": "size_info_missing",
            "message": "无法获取文件大小信息",
        }
        assert captured.err == ""


# ── fs ls ────────────────────────────────────────────────────────────

class TestFsLsFormat:
    def test_safe_text_escapes_ansi_and_osc_control_chars(self):
        from cli.utils.ui import safe_text

        text = safe_text("evil\x1b]52;c;AAAA\x07.py\nnext\x1b[2J")

        assert "\x1b" not in text
        assert "\x07" not in text
        assert "\\x1b]52;c;AAAA\\x07.py\nnext\\x1b[2J" == text

    def test_json_honors_sort_name(self):
        mp = MagicMock()
        mp.fs_ls.return_value = [
            {"name": "z.py", "type": "F", "size": "1"},
            {"name": "b", "type": "D", "size": "0"},
            {"name": "a.py", "type": "F", "size": "2"},
            {"name": "a", "type": "D", "size": "0"},
        ]

        with patch("cli.reg_commands.fs._mp_factory", return_value=mp):
            result = runner.invoke(app, [
                "fs", "ls", "/dev/ttyUSB0", "/", "--format", "json", "--sort", "name",
            ])

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert [entry["name"] for entry in data["entries"]] == ["a", "b", "a.py", "z.py"]

    def test_text_escapes_control_chars_in_device_names(self):
        mp = MagicMock()
        mp.fs_ls.return_value = [
            {"name": "clip\x1b]52;c;AAAA\x07.py", "type": "F", "size": "1"},
            {"name": "clear\x1b[2J.py", "type": "F", "size": "2"},
        ]
        mp.fs_df.return_value = {"total": 0, "used": 0, "free": 0}

        with patch("cli.reg_commands.fs._mp_factory", return_value=mp):
            result = runner.invoke(app, ["fs", "ls", "/dev/ttyUSB0", "/"])

        assert result.exit_code == 0
        assert "\x1b" not in result.stdout
        assert "\x07" not in result.stdout
        assert "\\x1b]52;c;AAAA\\x07.py" in result.stdout
        assert "\\x1b[2J.py" in result.stdout

    def test_json_reverse_size_keeps_directories_first(self):
        mp = MagicMock()
        mp.fs_ls.return_value = [
            {"name": "small.py", "type": "F", "size": "1"},
            {"name": "dir-small", "type": "D", "size": "0"},
            {"name": "large.py", "type": "F", "size": "100"},
            {"name": "dir-large", "type": "D", "size": "999"},
        ]

        with patch("cli.reg_commands.fs._mp_factory", return_value=mp):
            result = runner.invoke(app, [
                "fs", "ls", "/dev/ttyUSB0", "/", "--format", "json", "--sort", "-size",
            ])

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert [entry["name"] for entry in data["entries"]] == [
            "dir-large", "dir-small", "large.py", "small.py",
        ]


class TestFsTreeFormat:
    def test_text_escapes_control_chars_in_tree_output(self):
        mp = MagicMock()
        mp.fs_tree.return_value = "/\n  clear\x1b[2J.py\n"

        with patch("cli.reg_commands.fs._mp_factory", return_value=mp):
            result = runner.invoke(app, ["fs", "tree", "/dev/ttyUSB0", "/"])

        assert result.exit_code == 0
        assert "\x1b[2J" not in result.stdout
        assert "\\x1b[2J.py" in result.stdout


# ── path warnings ────────────────────────────────────────────────────

class TestPathWarnings:
    def test_norm_path_warning_uses_stderr(self, capsys):
        assert _norm_path("C:/") == "/"

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "MSYS2" in captured.err


class TestPipeFriendlyIo:
    def _upload_mp(self):
        mp = MagicMock()
        mp.config = SimpleNamespace(verify="off")
        mp.is_safe_main_path.return_value = False
        mp.run.side_effect = RuntimeError("missing")
        return mp

    def test_flash_accepts_stdin_source_and_cleans_tempfile(self):
        mp = self._upload_mp()
        captured = {}

        def capture(local_path, remote_path, **kwargs):
            captured["local_path"] = local_path
            captured["remote_path"] = remote_path
            captured["content"] = Path(local_path).read_bytes()
            captured["kwargs"] = kwargs

        mp.flash_file.side_effect = capture

        with patch("cli.main._mp_factory", return_value=mp), \
             patch("cli.main.prepare_device", return_value=SimpleNamespace(
                 active_tags=set(),
                 bytecode_ver=None,
                 arch=None,
                 precheck_mp_version=None,
             )), \
             patch("cli.main._run_precheck_or_exit"):
            result = runner.invoke(
                app,
                ["flash", "/dev/ttyUSB0", "-", "/app/main.py"],
                input="print('pipe flash')\n",
            )

        assert result.exit_code == 0
        assert captured["content"] == b"print('pipe flash')\n"
        assert captured["local_path"] != "-"
        assert not Path(captured["local_path"]).exists()
        assert captured["remote_path"] == "/app/main.py"

    def test_fs_put_accepts_stdin_source_and_cleans_tempfile(self):
        mp = self._upload_mp()
        captured = {}

        def capture(local_path, remote_path, **kwargs):
            captured["local_path"] = local_path
            captured["remote_path"] = remote_path
            captured["content"] = Path(local_path).read_bytes()
            captured["kwargs"] = kwargs

        mp.flash_file.side_effect = capture

        with patch("cli.reg_commands.fs._mp_factory", return_value=mp), \
             patch("cli.reg_commands.fs.prepare_device", return_value=SimpleNamespace(
                 active_tags=set(),
                 bytecode_ver=None,
                 arch=None,
                 precheck_mp_version=None,
             )):
            result = runner.invoke(
                app,
                ["fs", "put", "/dev/ttyUSB0", "-", "/app/data.py"],
                input="print('pipe put')\n",
            )

        assert result.exit_code == 0
        assert captured["content"] == b"print('pipe put')\n"
        assert captured["local_path"] != "-"
        assert not Path(captured["local_path"]).exists()
        assert captured["remote_path"] == "/app/data.py"

    def test_fs_get_dash_writes_raw_bytes_to_stdout(self):
        mp = MagicMock()
        mp.fs_get_bytes.return_value = b"bin\x00data\n"

        with patch("cli.reg_commands.fs._mp_factory", return_value=mp):
            result = runner.invoke(app, ["fs", "get", "/dev/ttyUSB0", "/dev/file.bin", "-"])

        assert result.exit_code == 0
        assert result.stdout == "bin\x00data\n"

    def test_flash_refuses_overwrite_in_non_tty(self, monkeypatch):
        mp = self._upload_mp()
        mp.run.side_effect = None
        mp.run.return_value = ""

        monkeypatch.setattr("cli.reg_commands.common.is_tty", lambda: False)

        with patch("cli.main._mp_factory", return_value=mp), \
             patch("cli.main.prepare_device", return_value=SimpleNamespace(
                 active_tags=set(),
                 bytecode_ver=None,
                 arch=None,
                 precheck_mp_version=None,
             )), \
             patch("cli.main._run_precheck_or_exit"):
            result = runner.invoke(
                app,
                ["flash", "/dev/ttyUSB0", "src.py", "/app/main.py"],
            )

        assert result.exit_code == 1
        mp.flash_file.assert_not_called()
        assert "非交互终端下请显式添加 --force" in result.output


class TestBatchPipeIo:
    def _prepared(self):
        return SimpleNamespace(
            active_tags=set(),
            bytecode_ver=None,
            arch=None,
            precheck_mp_version=None,
        )

    def _upload_mp(self):
        mp = MagicMock()
        mp.config = SimpleNamespace(verify="off")
        mp.is_safe_main_path.return_value = False
        mp.run.side_effect = RuntimeError("missing")
        return mp

    def test_fs_get_batch_outputs_content_jsonl(self):
        mp = MagicMock()
        mp.fs_get_bytes.return_value = b"hello\x00"

        with patch("cli.reg_commands.fs._mp_factory", return_value=mp):
            result = runner.invoke(
                app,
                ["fs", "get-batch", "/dev/ttyUSB0"],
                input='{"remote":"/data.bin"}\n',
            )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["remote"] == "/data.bin"
        assert base64.b64decode(payload["content_b64"]) == b"hello\x00"

    def test_fs_put_batch_accepts_content_b64(self):
        mp = self._upload_mp()
        captured = {}

        def capture(local_path, remote_path, **_kwargs):
            captured["content"] = Path(local_path).read_bytes()
            captured["exists_during_call"] = Path(local_path).exists()
            captured["remote"] = remote_path
            captured["local"] = local_path

        mp.flash_file.side_effect = capture
        line = json.dumps({
            "remote": "/main.py",
            "content_b64": base64.b64encode(b"print(1)\n").decode("ascii"),
        })

        with patch("cli.reg_commands.fs._mp_factory", return_value=mp), \
             patch("cli.reg_commands.fs.prepare_device", return_value=self._prepared()):
            result = runner.invoke(app, ["fs", "put-batch", "/dev/ttyUSB0"], input=line + "\n")

        assert result.exit_code == 0
        assert captured["content"] == b"print(1)\n"
        assert captured["exists_during_call"] is True
        assert not Path(captured["local"]).exists()
        assert json.loads(result.stdout)["ok"] is True

    def test_flash_batch_accepts_content_b64(self):
        mp = self._upload_mp()
        captured = {}

        def capture(local_path, remote_path, **_kwargs):
            captured["content"] = Path(local_path).read_bytes()
            captured["local"] = local_path
            captured["remote"] = remote_path

        mp.flash_file.side_effect = capture
        line = json.dumps({
            "remote": "/boot.py",
            "content_b64": base64.b64encode(b"print('boot')\n").decode("ascii"),
        })

        with patch("cli.main._mp_factory", return_value=mp), \
             patch("cli.main.prepare_device", return_value=self._prepared()), \
             patch("cli.main._run_precheck_or_exit"):
            result = runner.invoke(app, ["flash-batch", "/dev/ttyUSB0"], input=line + "\n")

        assert result.exit_code == 0
        assert captured["content"] == b"print('boot')\n"
        assert captured["remote"] == "/boot.py"
        assert not Path(captured["local"]).exists()

    def test_trace_summarize_reads_stdin(self):
        trace_line = json.dumps({
            "type": "trace_event",
            "schema_version": 1,
            "session_id": "s1",
            "ts": "2026-01-01T00:00:00.000Z",
            "time": 1.0,
            "operation": "flash",
            "event": "session_start",
            "phase": "session",
        })

        result = runner.invoke(app, ["trace", "summarize", "-", "--json"], input=trace_line + "\n")

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["path"] == "-"
        assert payload["session_id"] == "s1"

    def test_manifest_plan_reads_stdin(self, tmp_path: Path, monkeypatch):
        module = tmp_path / "main.py"
        module.write_text("print(1)\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            app,
            ["manifest", "plan", "--manifest", "-", "--json"],
            input="module('main.py')\n",
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["modules"][0]["remote"] == "main.py"

    def test_project_sync_backup_stdout_jsonl(self, capsys):
        mp = MagicMock()
        mp.fs_get_bytes.return_value = b"backup"
        manager = ProjectSyncManager(mp)
        manager._discover_device_files = MagicMock(return_value=[("/app/main.py", 6)])

        assert manager.backup_stdout_jsonl("/app") is True

        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["remote"] == "/app/main.py"
        assert base64.b64decode(payload["content_b64"]) == b"backup"

    def test_device_backup_stdout_jsonl_uses_streaming_mode(self):
        calls = []

        class Manager:
            def __init__(self, mp):
                self.mp = mp

            def backup_stdout_jsonl(self, remote_path):
                calls.append(("stdout", remote_path))
                return True

            def backup(self, *_args, **_kwargs):
                raise AssertionError("backup should not write local files")

        mp = MagicMock()

        with patch("cli.reg_commands.device._mp_factory", return_value=mp), \
             patch("cli.reg_commands.device.ProjectSyncManager", Manager):
            result = runner.invoke(
                app,
                ["device", "backup", "/dev/ttyUSB0", "ignored", "/app", "--stdout-jsonl"],
            )

        assert result.exit_code == 0
        assert calls == [("stdout", "/app")]

    def test_device_restore_accepts_stdin_jsonl(self):
        mp = MagicMock()
        captured = {}
        line = json.dumps({
            "remote": "/app/main.py",
            "content_b64": base64.b64encode(b"print('restore')\n").decode("ascii"),
        })

        def capture(local_path, remote_path, **kwargs):
            captured["content"] = Path(local_path).read_bytes()
            captured["exists_during_call"] = Path(local_path).exists()
            captured["local"] = local_path
            captured["remote"] = remote_path
            captured["kwargs"] = kwargs

        mp.flash_file.side_effect = capture

        with patch("cli.reg_commands.device._mp_factory", return_value=mp):
            result = runner.invoke(
                app,
                ["device", "restore", "/dev/ttyUSB0", "-", "/", "--stdin-jsonl", "-"],
                input=line + "\n",
            )

        assert result.exit_code == 0
        assert captured["content"] == b"print('restore')\n"
        assert captured["exists_during_call"] is True
        assert captured["remote"] == "/app/main.py"
        assert captured["kwargs"]["compile"] is False
        assert not Path(captured["local"]).exists()
        assert json.loads(result.stdout)["ok"] is True

    def test_device_restore_stdin_jsonl_dry_run_does_not_connect(self):
        mp = MagicMock()
        line = json.dumps({
            "path": "main.py",
            "content_b64": base64.b64encode(b"print(1)\n").decode("ascii"),
        })

        with patch("cli.reg_commands.device._mp_factory", return_value=mp):
            result = runner.invoke(
                app,
                ["device", "restore", "/dev/ttyUSB0", "-", "/app", "--dry-run"],
                input=line + "\n",
            )

        assert result.exit_code == 0
        mp.connect.assert_not_called()
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["dry_run"] is True
        assert payload["remote"] == "/app/main.py"


# ── isatty / color ───────────────────────────────────────────────────

class TestIsatty:
    def test_no_ansi_when_not_tty(self):
        with patch("cli.main.MicroPython.scan_ports", return_value=_fake_ports()):
            result = runner.invoke(app, ["scan"])
        # CliRunner 不是 tty，输出中不应包含 ANSI 转义码
        assert "\033[" not in result.stdout

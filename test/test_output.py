import json
import os
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner

from cli.main import app, _norm_path
from cli.project.sync import ProjectSyncManager
from cli.utils.errors import humanize_exception

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
        with patch("cli.main._mp_factory", return_value=self._mp()):
            result = runner.invoke(app, ["board-info", "/dev/ttyUSB0", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["firmware"]["platform"] == "esp32"
        assert data["device"]["cpu_hz"] == 240000000
        assert data["memory"]["flash_size"] == 4194304

    def test_json_alias_structure(self):
        with patch("cli.main._mp_factory", return_value=self._mp()):
            result = runner.invoke(app, ["board-info", "/dev/ttyUSB0", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["firmware"]["platform"] == "esp32"

    def test_text_no_json(self):
        with patch("cli.main._mp_factory", return_value=self._mp()):
            result = runner.invoke(app, ["board-info", "/dev/ttyUSB0"])
        assert result.exit_code == 0
        assert "{" not in result.stdout

    def test_empty_json_result_exits_with_error_payload(self):
        mp = MagicMock()
        mp.run.return_value = ""

        with patch("cli.main._mp_factory", return_value=mp):
            result = runner.invoke(app, ["board-info", "/dev/ttyUSB0", "--format", "json"])

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
        with patch("cli.main._mp_factory", return_value=mp), \
             patch("cli.main.ProjectSyncManager", return_value=self._setup_sync(True)):
            result = runner.invoke(app, ["project", "status", "/dev/ttyUSB0",
                                         str(tmp_path), "/pyrite"])
        assert result.exit_code == 1

    def test_exit_code_no_diff(self, tmp_path):
        mp = MagicMock()
        mp.detect_tags.return_value = {"ESP32"}
        with patch("cli.main._mp_factory", return_value=mp), \
             patch("cli.main.ProjectSyncManager", return_value=self._setup_sync(False)):
            result = runner.invoke(app, ["project", "status", "/dev/ttyUSB0",
                                         str(tmp_path), "/pyrite"])
        assert result.exit_code == 0

    def test_fmt_passed_to_status(self, tmp_path):
        mp = MagicMock()
        mp.detect_tags.return_value = {"ESP32"}
        mgr = self._setup_sync(False)
        with patch("cli.main._mp_factory", return_value=mp), \
             patch("cli.main.ProjectSyncManager", return_value=mgr):
            runner.invoke(app, ["project", "status", "/dev/ttyUSB0",
                                str(tmp_path), "/pyrite", "--format", "json"])
        mgr.status.assert_called_once()
        assert mgr.status.call_args.kwargs.get("fmt") == "json"

    def test_json_alias_passed_to_status(self, tmp_path):
        mp = MagicMock()
        mp.detect_tags.return_value = {"ESP32"}
        mgr = self._setup_sync(False)
        with patch("cli.main._mp_factory", return_value=mp), \
             patch("cli.main.ProjectSyncManager", return_value=mgr):
            runner.invoke(app, ["project", "status", "/dev/ttyUSB0",
                                str(tmp_path), "/pyrite", "--json"])
        mgr.status.assert_called_once()
        assert mgr.status.call_args.kwargs.get("fmt") == "json"

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

    def test_status_reads_device_file_and_prints_unified_diff(self, tmp_path, capsys):
        local = tmp_path / "main.py"
        local.write_text("print('new')\n", encoding="utf-8")
        mp = MagicMock()
        mp.run.return_value = "13"
        mp._read_device_file.return_value = b"print('old')\n"
        mgr = ProjectSyncManager(mp)

        has_diff = mgr.status(str(tmp_path), "/app")

        captured = capsys.readouterr()
        assert has_diff is True
        assert "[MOD]" in captured.err
        assert "--- /app/main.py" in captured.err
        assert "+++ main.py" in captured.err
        assert "-print('old')" in captured.err
        assert "+print('new')" in captured.err
        mp._read_device_file.assert_called_once_with("/app/main.py")


# ── project pull ─────────────────────────────────────────────────────

class TestProjectPullFormat:
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


class TestDeviceCommands:
    def test_device_backup_uses_project_manager_backup(self, tmp_path):
        mp = MagicMock()
        mgr = MagicMock()
        mgr.backup.return_value = True
        with patch("cli.main._mp_factory", return_value=mp), \
             patch("cli.main.ProjectSyncManager", return_value=mgr):
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
        with patch("cli.main._mp_factory", return_value=mp), \
             patch("cli.main.ProjectSyncManager", return_value=mgr):
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
    def test_json_honors_sort_name(self):
        mp = MagicMock()
        mp.fs_ls.return_value = [
            {"name": "z.py", "type": "F", "size": "1"},
            {"name": "b", "type": "D", "size": "0"},
            {"name": "a.py", "type": "F", "size": "2"},
            {"name": "a", "type": "D", "size": "0"},
        ]

        with patch("cli.main._mp_factory", return_value=mp):
            result = runner.invoke(app, [
                "fs", "ls", "/dev/ttyUSB0", "/", "--format", "json", "--sort", "name",
            ])

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert [entry["name"] for entry in data["entries"]] == ["a", "b", "a.py", "z.py"]

    def test_json_reverse_size_keeps_directories_first(self):
        mp = MagicMock()
        mp.fs_ls.return_value = [
            {"name": "small.py", "type": "F", "size": "1"},
            {"name": "dir-small", "type": "D", "size": "0"},
            {"name": "large.py", "type": "F", "size": "100"},
            {"name": "dir-large", "type": "D", "size": "999"},
        ]

        with patch("cli.main._mp_factory", return_value=mp):
            result = runner.invoke(app, [
                "fs", "ls", "/dev/ttyUSB0", "/", "--format", "json", "--sort", "-size",
            ])

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert [entry["name"] for entry in data["entries"]] == [
            "dir-large", "dir-small", "large.py", "small.py",
        ]


# ── path warnings ────────────────────────────────────────────────────

class TestPathWarnings:
    def test_norm_path_warning_uses_stderr(self, capsys):
        assert _norm_path("C:/") == "/"

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "MSYS2" in captured.err


# ── isatty / color ───────────────────────────────────────────────────

class TestIsatty:
    def test_no_ansi_when_not_tty(self):
        with patch("cli.main.MicroPython.scan_ports", return_value=_fake_ports()):
            result = runner.invoke(app, ["scan"])
        # CliRunner 不是 tty，输出中不应包含 ANSI 转义码
        assert "\033[" not in result.stdout

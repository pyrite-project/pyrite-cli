import json
import os
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner

from cli.main import app, _norm_path
from cli.project.sync import ProjectSyncManager

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
        assert "ttyUSB0" in result.stderr

    def test_json_stdout_only(self):
        with patch("cli.main.MicroPython.scan_ports", return_value=_fake_ports()):
            result = runner.invoke(app, ["scan", "--format", "json"])
        assert result.stdout.strip().startswith("{")
        assert result.stderr == ""

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
        assert result.stderr == ""

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
        assert json.loads(result.stdout) == {"error": "no_device_info"}


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


# ── plugin list ──────────────────────────────────────────────────────

class TestPluginListFormat:
    def _plugins(self):
        p = MagicMock()
        p.name, p.version, p.description = "myplugin", "1.0.0", "A plugin"
        return [p]

    def test_json_structure(self):
        with patch("cli.main.get_loaded_plugins", return_value=self._plugins()):
            result = runner.invoke(app, ["plugin", "list", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["plugins"][0]["name"] == "myplugin"

    def test_empty_json(self):
        with patch("cli.main.get_loaded_plugins", return_value=[]):
            result = runner.invoke(app, ["plugin", "list", "--format", "json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"plugins": []}


# ── plugin info ──────────────────────────────────────────────────────

class TestPluginInfoFormat:
    def test_missing_plugin_json_exits_with_error_payload(self):
        with patch("cli.main.get_loaded_plugins", return_value=[]):
            result = runner.invoke(app, ["plugin", "info", "missing", "--format", "json"])

        assert result.exit_code == 1
        assert json.loads(result.stdout) == {
            "error": "plugin_not_found",
            "name": "missing",
        }


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
        # CliRunner stdout is not a tty, so ANSI codes should not appear in stderr
        assert "\033[" not in result.stderr

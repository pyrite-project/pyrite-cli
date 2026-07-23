import json
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from cli.main import app
from cli.project.scaffold import detect_device_info


runner = CliRunner()


def test_project_new_help_distinguishes_platform_and_port():
    result = runner.invoke(app, ["project", "new", "--help"])

    assert result.exit_code == 0
    assert "--platform" in result.stdout
    assert "直接指定 MicroPython 平台" in result.stdout
    assert "--port" in result.stdout
    assert "串口号" in result.stdout


def test_project_new_rejects_platform_and_port_together():
    result = runner.invoke(
        app,
        [
            "project",
            "new",
            "demo",
            "--platform",
            "esp32",
            "--port",
            "COM3",
        ],
    )

    assert result.exit_code == 2
    assert "--platform 和 --port 不能同时使用" in result.stderr


def test_project_new_passes_explicit_connection_settings():
    with patch("cli.reg_commands.project.new_project_interactive") as create:
        result = runner.invoke(
            app,
            [
                "project",
                "new",
                "demo",
                "--platform",
                "esp32",
                "--baudrate",
                "115200",
                "--timeout",
                "7",
            ],
        )

    assert result.exit_code == 0, result.output
    create.assert_called_once_with(
        "demo",
        platform="esp32",
        port=None,
        baudrate=115200,
        timeout=7,
    )


def test_project_init_passes_environment_connection_settings():
    with patch("cli.reg_commands.project.init_stubs") as init:
        result = runner.invoke(
            app,
            ["project", "init", "esp32", "1.24.0", "--port", "COM3"],
            env={"PYRITE_BAUDRATE": "230400", "PYRITE_TIMEOUT": "9"},
        )

    assert result.exit_code == 0, result.output
    init.assert_called_once_with(
        "esp32",
        "1.24.0",
        None,
        "COM3",
        baudrate=230400,
        timeout=9,
    )


def test_project_device_detection_resolves_board_alias(tmp_path, monkeypatch):
    alias_file = tmp_path / "aliases.json"
    alias_file.write_text(
        json.dumps({"version": 1, "aliases": {"bench": "COM9"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PYRITE_BOARD_ALIAS_FILE", str(alias_file))
    captured = {}

    class FakeMicroPython:
        def __init__(self, *, port, baudrate, timeout):
            captured.update(port=port, baudrate=baudrate, timeout=timeout)

        def connect(self):
            pass

        def ensure_device_context(self):
            return SimpleNamespace(version="1.24.1", platform="esp32")

        def disconnect(self):
            pass

    monkeypatch.setattr("cli.utils.flash.MicroPython", FakeMicroPython)

    assert detect_device_info("@bench") == ("esp32", "1.24.1")
    assert captured["port"] == "COM9"

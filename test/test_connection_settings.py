from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from cli import main
from cli.main import app
from cli.reg_commands import common
from cli.project.scaffold import detect_device_info
from cli.utils.device_tests import DeviceTestPlan, DeviceTestSession
from cli.utils.config import CONFIG_FILE
from cli.utils.transport import serial as serial_module


runner = CliRunner()


def _write_connection_config(tmp_path, *, baudrate: int, timeout: int) -> None:
    (tmp_path / CONFIG_FILE).write_text(
        json.dumps({"baudrate": baudrate, "timeout": timeout}),
        encoding="utf-8",
    )


def test_common_factory_uses_project_connection_settings(
    tmp_path,
    monkeypatch,
):
    _write_connection_config(tmp_path, baudrate=460800, timeout=23)
    monkeypatch.chdir(tmp_path)
    factory = MagicMock(return_value=object())
    monkeypatch.setattr(common, "MicroPython", factory)

    common._mp_factory("COM3", None, None)

    factory.assert_called_once_with(port="COM3", baudrate=460800, timeout=23)


def test_common_factory_explicit_values_override_project_config(
    tmp_path,
    monkeypatch,
):
    _write_connection_config(tmp_path, baudrate=460800, timeout=23)
    monkeypatch.chdir(tmp_path)
    factory = MagicMock(return_value=object())
    monkeypatch.setattr(common, "MicroPython", factory)

    common._mp_factory("COM3", 115200, 4)

    factory.assert_called_once_with(port="COM3", baudrate=115200, timeout=4)


def test_main_factory_uses_project_connection_settings(
    tmp_path,
    monkeypatch,
):
    _write_connection_config(tmp_path, baudrate=460800, timeout=23)
    monkeypatch.chdir(tmp_path)
    factory = MagicMock(return_value=object())
    monkeypatch.setattr(main, "MicroPython", factory)

    main._mp_factory("COM3", None, None)

    factory.assert_called_once_with(port="COM3", baudrate=460800, timeout=23)


def test_common_webrepl_factory_receives_resolved_timeout(
    tmp_path,
    monkeypatch,
):
    _write_connection_config(tmp_path, baudrate=460800, timeout=19)
    monkeypatch.chdir(tmp_path)
    factory = MagicMock(return_value=object())
    monkeypatch.setattr(common, "WebREPLMicroPython", factory)

    common._mp_factory("ignored", None, None, "ws://board.local:8266", "secret")

    factory.assert_called_once_with(
        url="ws://board.local:8266",
        password="secret",
        timeout=19,
    )


def test_main_webrepl_factory_receives_resolved_timeout(
    tmp_path,
    monkeypatch,
):
    _write_connection_config(tmp_path, baudrate=460800, timeout=19)
    monkeypatch.chdir(tmp_path)
    factory = MagicMock(return_value=object())
    monkeypatch.setattr(main, "WebREPLMicroPython", factory)

    main._mp_factory("ignored", None, None, "ws://board.local:8266", "secret")

    factory.assert_called_once_with(
        url="ws://board.local:8266",
        password="secret",
        timeout=19,
    )


def test_uart_transport_factory_receives_resolved_settings(
    tmp_path,
    monkeypatch,
):
    _write_connection_config(tmp_path, baudrate=230400, timeout=12)
    monkeypatch.chdir(tmp_path)
    factory = MagicMock(return_value=object())
    monkeypatch.setattr(serial_module, "SerialTransport", factory)

    main._serial_transport_factory("COM4", None, None)

    factory.assert_called_once_with(port="COM4", baudrate=230400, timeout=12)


def test_project_device_detection_uses_project_connection_settings(
    tmp_path,
    monkeypatch,
):
    _write_connection_config(tmp_path, baudrate=115200, timeout=31)
    monkeypatch.chdir(tmp_path)
    mp = MagicMock()
    mp.ensure_device_context.return_value = SimpleNamespace(
        platform="esp32",
        version="1.24.0",
    )
    factory = MagicMock(return_value=mp)
    monkeypatch.setattr("cli.utils.flash.MicroPython", factory)

    assert detect_device_info("COM5") == ("esp32", "1.24.0")

    factory.assert_called_once_with(port="COM5", baudrate=115200, timeout=31)


def test_micropython_default_constructor_uses_project_connection_settings(
    tmp_path,
    monkeypatch,
):
    _write_connection_config(tmp_path, baudrate=576000, timeout=17)
    monkeypatch.chdir(tmp_path)
    transport = MagicMock()
    factory = MagicMock(return_value=transport)
    monkeypatch.setattr("cli.utils.flash.core.SerialTransport", factory)

    from cli.utils.flash import MicroPython

    mp = MicroPython(port="COM6")

    assert mp.baudrate == 576000
    assert mp.timeout == 17
    factory.assert_called_once_with("COM6", 576000, 17)


def test_cli_omitted_connection_options_remain_unspecified():
    mp = MagicMock()
    with patch("cli.main._mp_factory", return_value=mp) as factory:
        result = runner.invoke(app, ["reset", "COM3"])

    assert result.exit_code == 0, result.output
    factory.assert_called_once_with("COM3", None, None, None, None)


def test_cli_explicit_connection_options_override_config():
    mp = MagicMock()
    with patch("cli.main._mp_factory", return_value=mp) as factory:
        result = runner.invoke(
            app,
            ["reset", "COM3", "--baudrate", "115200", "--timeout", "4"],
        )

    assert result.exit_code == 0, result.output
    factory.assert_called_once_with("COM3", 115200, 4, None, None)


def test_cli_environment_connection_options_override_config():
    mp = MagicMock()
    with patch("cli.main._mp_factory", return_value=mp) as factory:
        result = runner.invoke(
            app,
            ["reset", "COM3"],
            env={"PYRITE_BAUDRATE": "230400", "PYRITE_TIMEOUT": "6"},
        )

    assert result.exit_code == 0, result.output
    factory.assert_called_once_with("COM3", 230400, 6, None, None)


def test_device_test_has_separate_connection_timeout(monkeypatch):
    from cli.reg_commands import device_test as command

    plan = DeviceTestPlan(files=[], remote_dir="/.pyrite_tests")
    session = DeviceTestSession(plan=plan, results=[], raw_output="")
    mp = MagicMock()
    factory = MagicMock(return_value=mp)
    monkeypatch.setattr(command, "discover_device_tests", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(command, "run_device_test_plan", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(command, "_mp_factory", factory)

    result = runner.invoke(
        app,
        ["test", "COM3", "--timeout", "15", "--connect-timeout", "4"],
    )

    assert result.exit_code == 0, result.output
    factory.assert_called_once_with("COM3", None, 4, None, None)

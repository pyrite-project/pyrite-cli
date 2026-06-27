import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from cli.main import app
from cli.utils.diagnostics import run_doctor


runner = CliRunner()


DOCTOR_OUTPUT = """\
PYRITE_DOCTOR_BEGIN
INFO|firmware.name|micropython
INFO|firmware.version|1.22.0
INFO|firmware.platform|esp32
INFO|firmware.machine|ESP32-S3 module
INFO|firmware.release|1.22.0
INFO|memory.free|186368
INFO|memory.allocated|52432
INFO|filesystem.total|2097152
INFO|filesystem.free|1048576
CHECK|raw_repl|ok|behaviour-probe|command execution succeeded
CHECK|filesystem_rw|ok|behaviour-probe|write/read/delete passed
FEATURE|sys.settrace|debug|unsupported|hasattr-probe|MICROPY_PY_SYS_SETTRACE|hasattr(sys, "settrace")
FEATURE|network|network|supported|import-probe|MICROPY_PY_NETWORK|import network
PYRITE_DOCTOR_END
"""


def _fake_mp():
    mp = MagicMock()
    mp.config = SimpleNamespace(chunk_size=4096, verify="size", max_retries=2)
    mp.run.return_value = DOCTOR_OUTPUT
    return mp


def test_run_doctor_reports_observable_firmware_features():
    mp = _fake_mp()

    report = run_doctor(mp, connect_ms=12)

    assert report["connection"]["connect_ms"] == 12
    assert report["board"]["platform"] == "esp32"
    assert report["memory"]["total"] == 238800
    assert report["filesystem"]["used"] == 1048576
    assert report["checks"][0]["id"] == "serial_connect"
    items = report["firmware_features"]["items"]
    assert items[0] == {
        "id": "sys.settrace",
        "category": "debug",
        "status": "unsupported",
        "confidence": "hasattr-probe",
        "macro_hint": "MICROPY_PY_SYS_SETTRACE",
        "probe": 'hasattr(sys, "settrace")',
    }
    assert "macro_value" not in items[0]


def test_debug_doctor_outputs_and_saves_json(tmp_path):
    mp = _fake_mp()
    save_path = tmp_path / "doctor.json"

    with patch("cli.reg_commands.debug._mp_factory", return_value=mp):
        result = runner.invoke(app, [
            "debug", "doctor", "COM3",
            "--format", "json",
            "--save", str(save_path),
        ])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    saved = json.loads(save_path.read_text(encoding="utf-8"))
    assert data["summary"]["ok"] is True
    assert saved["board"]["machine"] == "ESP32-S3 module"
    mp.connect.assert_called_once()
    mp.disconnect.assert_called_once()


def test_debug_doctor_text_uses_board_info_section_style():
    mp = _fake_mp()

    with patch("cli.reg_commands.debug._mp_factory", return_value=mp):
        result = runner.invoke(app, ["debug", "doctor", "COM3"])

    assert result.exit_code == 0
    assert "── 固件" in result.stdout
    assert "── 诊断" in result.stdout
    assert "── 内存" in result.stdout
    assert "── 特性" in result.stdout
    assert "Raw REPL" in result.stdout
    assert "sys.settrace" in result.stdout
    assert "Board:" not in result.stdout
    assert "Firmware:" not in result.stdout

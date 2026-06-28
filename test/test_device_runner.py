import base64
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from cli.main import app
from cli.utils.device_tests import (
    build_cleanup_plan,
    build_device_test_runner_script,
    discover_device_tests,
    parse_device_test_output,
)


runner = CliRunner()


def _result_line(index, status, path, stdout="", error="", duration_ms=3):
    def enc(value):
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    return "|".join([
        "PYRITE_TEST",
        str(index),
        status,
        str(duration_ms),
        enc(path),
        enc(stdout),
        enc(error),
    ])


def test_discovery_defaults_to_test_device_directory(tmp_path):
    root = tmp_path
    test_dir = root / "test_device"
    nested = test_dir / "drivers"
    nested.mkdir(parents=True)
    (test_dir / "test_gpio.py").write_text("assert True\n", encoding="utf-8")
    (nested / "test_bus.py").write_text("assert True\n", encoding="utf-8")
    (test_dir / "notes.txt").write_text("ignore\n", encoding="utf-8")

    plan = discover_device_tests(cwd=root)

    assert [item.relative_path for item in plan.files] == [
        "drivers/test_bus.py",
        "test_gpio.py",
    ]
    assert [item.remote_path for item in plan.files] == [
        "/.pyrite_tests/drivers/test_bus.py",
        "/.pyrite_tests/test_gpio.py",
    ]
    assert plan.remote_dir == "/.pyrite_tests"


def test_discovery_accepts_a_specific_file(tmp_path):
    test_file = tmp_path / "custom_case.py"
    test_file.write_text("assert True\n", encoding="utf-8")

    plan = discover_device_tests(test_file, cwd=tmp_path, remote_dir="/tmp/tests/")

    assert len(plan.files) == 1
    assert plan.files[0].local_path == test_file
    assert plan.files[0].relative_path == "custom_case.py"
    assert plan.files[0].remote_path == "/tmp/tests/custom_case.py"


def test_runner_script_captures_stdout_asserts_and_timeout():
    script = build_device_test_runner_script(
        ["/.pyrite_tests/test_gpio.py"],
        timeout=7,
    )

    assert "class _Capture" in script
    assert "AssertionError" in script
    assert "PYRITE_TEST" in script
    assert "_TIMEOUT_MS=7000" in script
    assert "/.pyrite_tests/test_gpio.py" in script


def test_result_parser_decodes_runner_lines():
    output = "\n".join([
        "boot noise",
        _result_line(
            0,
            "fail",
            "/.pyrite_tests/test_gpio.py",
            stdout="pin=0\n",
            error="AssertionError: bad pin\n",
            duration_ms=12,
        ),
    ])

    results = parse_device_test_output(output)

    assert len(results) == 1
    assert results[0].index == 0
    assert results[0].status == "fail"
    assert results[0].remote_path == "/.pyrite_tests/test_gpio.py"
    assert results[0].stdout == "pin=0\n"
    assert results[0].error == "AssertionError: bad pin\n"
    assert results[0].duration_ms == 12


def test_cleanup_plan_defaults_to_remote_temp_dir():
    plan = SimpleNamespace(remote_dir="/.pyrite_tests")

    cleanup = build_cleanup_plan(plan, keep_files=False)

    assert cleanup is not None
    assert cleanup.remote_dir == "/.pyrite_tests"
    assert cleanup.recursive is True
    assert cleanup.force is True
    assert build_cleanup_plan(plan, keep_files=True) is None


def test_cli_uploads_runs_and_cleans_default_test_files(tmp_path, monkeypatch):
    test_dir = tmp_path / "test_device"
    test_dir.mkdir()
    (test_dir / "test_gpio.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mp = MagicMock()
    mp.run.return_value = _result_line(
        0,
        "pass",
        "/.pyrite_tests/test_gpio.py",
        stdout="ok\n",
    )

    with patch("cli.reg_commands.device_test._mp_factory", return_value=mp):
        result = runner.invoke(app, ["test", "COM3"])

    assert result.exit_code == 0
    assert "CLEANED remote test files from /.pyrite_tests" in result.stdout
    mp.connect.assert_called_once()
    mp.flash_file.assert_called_once_with(
        str(test_dir / "test_gpio.py"),
        "/.pyrite_tests/test_gpio.py",
        compile=False,
    )
    mp.run.assert_called_once()
    mp.fs_rm.assert_called_once_with("/.pyrite_tests", recursive=True, force=True)
    mp.disconnect.assert_called_once()


def test_cli_keep_files_skips_cleanup_for_specific_file(tmp_path):
    test_file = tmp_path / "test_gpio.py"
    test_file.write_text("assert True\n", encoding="utf-8")
    mp = MagicMock()
    mp.run.return_value = _result_line(
        0,
        "pass",
        "/.pyrite_tests/test_gpio.py",
    )

    with patch("cli.reg_commands.device_test._mp_factory", return_value=mp):
        result = runner.invoke(app, [
            "test",
            "COM3",
            str(test_file),
            "--keep-files",
            "--timeout",
            "12",
        ])

    assert result.exit_code == 0
    assert "KEEP-FILES remote test files retained at /.pyrite_tests" in result.stdout
    mp.run.assert_called_once()
    assert mp.run.call_args.kwargs["timeout"] == 12
    mp.fs_rm.assert_not_called()

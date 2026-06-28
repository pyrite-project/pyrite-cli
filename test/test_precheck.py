import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app
from cli.utils.precheck import (
    PrecheckError,
    collect_directory_entries,
    collect_project_precheck_entries,
    run_precheck,
)


runner = CliRunner()


def _write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_basic_reports_syntax_error_with_path_and_location(tmp_path: Path):
    source = _write(tmp_path / "main.py", "def broken(:\n    pass\n")

    with pytest.raises(PrecheckError) as excinfo:
        run_precheck([(str(source), "/main.py")], mode="basic")

    message = str(excinfo.value)
    assert str(source) in message
    assert ":1:" in message
    assert "syntax" in message.lower()


def test_basic_parses_preprocessed_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = _write(tmp_path / "main.py", "print('ok')\n")
    monkeypatch.setattr("cli.utils.precheck.preprocess", lambda *_args: "def broken(:\n")

    with pytest.raises(PrecheckError) as excinfo:
        run_precheck([(str(source), "/main.py")], mode="basic", active_tags={"on"})

    assert str(source) in str(excinfo.value)
    assert "preprocessed" in str(excinfo.value)


def test_strict_can_warn_or_error_for_compat_findings(tmp_path: Path):
    source = _write(tmp_path / "main.py", "name = 'pyrite'\nprint(f'{name}')\n")

    warning_report = run_precheck(
        [(str(source), "/main.py")],
        mode="strict",
        compat="warn",
    )
    assert warning_report.ok is True
    assert any(item.severity == "warning" for item in warning_report.items)

    with pytest.raises(PrecheckError):
        run_precheck(
            [(str(source), "/main.py")],
            mode="strict",
            compat="error",
        )


def test_rejects_empty_file_and_remote_path_conflict(tmp_path: Path):
    a = _write(tmp_path / "a.py", "")
    b = _write(tmp_path / "b.py", "print('b')\n")

    with pytest.raises(PrecheckError) as excinfo:
        run_precheck([(str(a), "/app/main.py"), (str(b), "/app/main.py")])

    message = str(excinfo.value)
    assert str(a) in message
    assert str(b) in message
    assert "/app/main.py" in message


@pytest.mark.parametrize("remote", ["", "../main.py", r"C:\main.py", r"\main.py"])
def test_rejects_obviously_invalid_remote_paths(tmp_path: Path, remote: str):
    source = _write(tmp_path / "main.py", "print('ok')\n")

    with pytest.raises(PrecheckError):
        run_precheck([(str(source), remote)])


def test_flash_dry_run_precheck_runs_before_mp_factory(tmp_path: Path):
    source = _write(tmp_path / "main.py", "def broken(:\n    pass\n")

    with patch("cli.main._mp_factory", side_effect=AssertionError("must not connect")):
        result = runner.invoke(app, [
            "flash", "COM3", str(source), "/main.py", "--dry-run",
        ])

    assert result.exit_code == 1
    assert "precheck failed" in result.output.lower()


def test_flash_accepts_bare_check_option(tmp_path: Path):
    source = _write(tmp_path / "main.py", "def broken(:\n    pass\n")

    with patch("cli.main._mp_factory", side_effect=AssertionError("must not connect")):
        result = runner.invoke(app, [
            "flash", "COM3", str(source), "/main.py", "--dry-run", "--check",
        ])

    assert result.exit_code == 1
    assert "precheck failed" in result.output.lower()


def test_project_flash_accepts_check_equals_strict(tmp_path: Path):
    _write(tmp_path / "main.py", "def broken(:\n    pass\n")

    with patch("cli.reg_commands.project._mp_factory", side_effect=AssertionError("must not connect")):
        result = runner.invoke(app, [
            "project", "flash", "COM3", str(tmp_path), "/app", "--dry-run", "--check=strict",
        ])

    assert result.exit_code == 1
    assert "precheck failed" in result.output.lower()


def test_flash_no_check_skips_precheck(tmp_path: Path):
    source = _write(tmp_path / "main.py", "def broken(:\n    pass\n")
    mp = MagicMock()
    mp.get_mpy_version.return_value = (6, "xtensawin")
    mp.detect_tags.return_value = {"ESP32"}

    with patch("cli.main._mp_factory", return_value=mp):
        result = runner.invoke(app, [
            "flash", "COM3", str(source), "/main.py", "--dry-run", "--no-check",
        ])

    assert result.exit_code == 0
    mp.connect.assert_called_once()


def test_flash_program_dry_run_precheck_runs_before_mp_factory(tmp_path: Path):
    _write(tmp_path / "main.py", "def broken(:\n    pass\n")

    with patch("cli.main._mp_factory", side_effect=AssertionError("must not connect")):
        result = runner.invoke(app, [
            "flash-program", "COM3", str(tmp_path), "/app", "--dry-run",
        ])

    assert result.exit_code == 1
    assert "precheck failed" in result.output.lower()


def test_project_flash_dry_run_precheck_runs_before_mp_factory(tmp_path: Path):
    _write(tmp_path / "main.py", "def broken(:\n    pass\n")

    with patch("cli.reg_commands.project._mp_factory", side_effect=AssertionError("must not connect")):
        result = runner.invoke(app, [
            "project", "flash", "COM3", str(tmp_path), "/app", "--dry-run",
        ])

    assert result.exit_code == 1
    assert "precheck failed" in result.output.lower()


def test_project_precheck_filters_to_incremental_changed_entries(tmp_path: Path):
    unchanged_bad = _write(tmp_path / "unchanged_bad.py", "")
    changed_good = _write(tmp_path / "changed_good.py", "print('changed')\n")
    hash_config = tmp_path / "pyrite_file_config.json"
    hash_config.write_text(
        json.dumps({
            "version": 1,
            "hash_algorithm": "sha256",
            "files": {
                "unchanged_bad.py": _sha256(unchanged_bad),
                "changed_good.py": "old",
            },
        }),
        encoding="utf-8",
    )

    entries = collect_project_precheck_entries(
        str(tmp_path),
        "/app",
        hash_config_path=str(hash_config),
    )

    assert entries == [(str(changed_good), "/app/changed_good.py")]
    assert run_precheck(entries).ok is True


def test_project_flash_does_not_precheck_unchanged_bad_file(tmp_path: Path):
    unchanged_bad = _write(tmp_path / "unchanged_bad.py", "")
    _write(tmp_path / "changed_good.py", "print('changed')\n")
    (tmp_path / "pyrite_file_config.json").write_text(
        json.dumps({
            "version": 1,
            "hash_algorithm": "sha256",
            "files": {
                "unchanged_bad.py": _sha256(unchanged_bad),
                "changed_good.py": "old",
            },
        }),
        encoding="utf-8",
    )
    mp = MagicMock()
    mp.config.board_tags = {"ESP32": ["ESP32", "wifi"]}
    mp.get_mpy_version.return_value = (6, "xtensa")

    with patch("cli.reg_commands.project._mp_factory", return_value=mp):
        result = runner.invoke(app, [
            "project", "flash", "COM3", str(tmp_path), "/app",
            "--target", "ESP32", "--dry-run",
        ])

    assert result.exit_code == 0
    mp.connect.assert_called_once()


def test_manifest_precheck_includes_feature_entries_when_tags_unknown(tmp_path: Path):
    source = _write(tmp_path / "wifi_case.py", "def broken(:\n    pass\n")
    manifest = _write(
        tmp_path / "manifest.py",
        'module("wifi_case.py", features=["wifi"])\n',
    )

    entries = collect_project_precheck_entries(
        str(tmp_path),
        "/app",
        active_tags=None,
        manifest_path=str(manifest),
    )

    assert entries == [(str(source), "/app/wifi_case.py")]
    with pytest.raises(PrecheckError):
        run_precheck(entries)


def test_manifest_precheck_respects_known_empty_tags(tmp_path: Path):
    manifest = _write(
        tmp_path / "manifest.py",
        'module("wifi_case.py", features=["wifi"])\n',
    )
    _write(tmp_path / "wifi_case.py", "print('wifi')\n")

    assert collect_directory_entries(
        str(tmp_path),
        "/app",
        active_tags=set(),
        manifest_path=str(manifest),
    ) == []

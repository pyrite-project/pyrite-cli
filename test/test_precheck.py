import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app
from cli.utils.precheck import (
    PrecheckError,
    collect_directory_entries,
    collect_project_precheck_entries,
    normalize_micropython_version,
    run_precheck,
)


runner = CliRunner()


def _write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_flash_mp(version: str = "1.22.0") -> MagicMock:
    mp = MagicMock()
    mp.runtime_info = SimpleNamespace(version=version)
    mp.config.board_tags = {"ESP32": ["ESP32", "wifi"]}
    mp.get_mpy_version.return_value = (6, "xtensa")
    mp.detect_tags.return_value = {"ESP32"}
    return mp


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


def test_normalize_micropython_version_uses_known_tag_order():
    assert normalize_micropython_version("1.12.0") == "v1.12"
    assert normalize_micropython_version("1.12.1") == "v1.12"
    assert normalize_micropython_version("1.19.2") == "v1.19.1"
    assert normalize_micropython_version("1.20") == "v1.20.0"
    assert normalize_micropython_version("MicroPython v1.22.0 on ESP32") == "v1.22.0"
    assert normalize_micropython_version("MicroPython v1.22.3 on ESP32") == "v1.22.2"
    assert normalize_micropython_version("v1.24.0-preview") == "v1.24.0-preview"


def test_strict_version_errors_for_feature_before_target_runtime(tmp_path: Path):
    source = _write(tmp_path / "main.py", "name = 'pyrite'\nprint(f'{name}')\n")

    with pytest.raises(PrecheckError) as excinfo:
        run_precheck(
            [(str(source), "/main.py")],
            mode="strict",
            compat="warn",
            mp_version="1.16",
        )

    message = str(excinfo.value)
    assert "f-string requires MicroPython v1.17+" in message
    assert "target v1.16" in message


def test_basic_version_errors_for_hard_unsupported_feature(tmp_path: Path):
    source = _write(tmp_path / "main.py", "print(f'{1}')\n")

    with pytest.raises(PrecheckError) as excinfo:
        run_precheck(
            [(str(source), "/main.py")],
            mode="basic",
            mp_version="1.12.0",
        )

    assert "f-string requires MicroPython v1.17+" in str(excinfo.value)


def test_strict_version_warns_for_config_gated_feature(tmp_path: Path):
    source = _write(tmp_path / "main.py", "if (value := 1):\n    print(value)\n")

    report = run_precheck(
        [(str(source), "/main.py")],
        mode="strict",
        compat="warn",
        mp_version="1.20.0",
    )

    assert report.ok is True
    assert any("gated by firmware build options" in item.message for item in report.warnings)


def test_strict_version_reports_unsupported_modern_python_syntax(tmp_path: Path):
    source = _write(tmp_path / "main.py", "match value:\n    case 1:\n        pass\n")

    with pytest.raises(PrecheckError) as excinfo:
        run_precheck(
            [(str(source), "/main.py")],
            mode="strict",
            compat="warn",
            mp_version="1.29.0-preview",
        )

    assert "match/case is not supported" in str(excinfo.value)


def test_token_feature_detection_for_numeric_underscore_and_fstring_forms(tmp_path: Path):
    source = _write(
        tmp_path / "main.py",
        "value = 1_000\n"
        "name = 'pyrite'\n"
        "print(rf'{name}')\n"
        "print(f'a' f'{name}')\n",
    )

    with pytest.raises(PrecheckError) as excinfo:
        run_precheck(
            [(str(source), "/main.py")],
            mode="strict",
            compat="warn",
            mp_version="1.23.0",
        )

    message = str(excinfo.value)
    assert "raw f-string prefix requires MicroPython v1.24.0+" in message
    assert "adjacent f-string concatenation requires MicroPython v1.24.0+" in message
    assert "numeric literal underscores" not in message


def test_tstring_detection_requires_new_micropython(tmp_path: Path):
    source = _write(tmp_path / "main.py", "name = 'pyrite'\nprint(t'hello {name}')\n")

    with pytest.raises(PrecheckError) as excinfo:
        run_precheck(
            [(str(source), "/main.py")],
            mode="strict",
            compat="warn",
            mp_version="1.27.0",
        )

    assert "t-string requires MicroPython v1.28.0+" in str(excinfo.value)


def test_token_feature_detection_runs_when_host_ast_rejects_syntax(tmp_path: Path):
    source = _write(tmp_path / "main.py", "name = 'pyrite'\nprint(t'hello {name}')\n")

    with patch("cli.utils.precheck.ast.parse", side_effect=SyntaxError("host parser rejected t-string")):
        with pytest.raises(PrecheckError) as excinfo:
            run_precheck(
                [(str(source), "/main.py")],
                mode="strict",
                compat="warn",
                mp_version="1.27.0",
            )

    message = str(excinfo.value)
    assert "syntax error: host parser rejected t-string" in message
    assert "t-string requires MicroPython v1.28.0+" in message


def test_source_fallback_detects_except_star_when_ast_rejects_syntax(tmp_path: Path):
    source = _write(
        tmp_path / "main.py",
        "try:\n"
        "    risky()\n"
        "except* ValueError:\n"
        "    pass\n",
    )

    with patch("cli.utils.precheck.ast.parse", side_effect=SyntaxError("host parser rejected except star")):
        with pytest.raises(PrecheckError) as excinfo:
            run_precheck(
                [(str(source), "/main.py")],
                mode="strict",
                compat="warn",
                mp_version="1.29.0-preview",
            )

    assert "except* / ExceptionGroup is not supported" in str(excinfo.value)


def test_source_fallback_detects_pep695_type_params(tmp_path: Path):
    source = _write(tmp_path / "main.py", "type Box[T] = list[T]\n")

    with pytest.raises(PrecheckError) as excinfo:
        run_precheck(
            [(str(source), "/main.py")],
            mode="strict",
            compat="warn",
            mp_version="1.29.0-preview",
        )

    assert "PEP 695 type parameters is not supported" in str(excinfo.value)


def test_dict_union_flags_dict_like_operands_but_not_set_union(tmp_path: Path):
    source = _write(
        tmp_path / "main.py",
        "a = {'x': 1}\n"
        "b = {'y': 2}\n"
        "merged = a | b\n"
        "set_union = {1} | {2}\n",
    )

    with pytest.raises(PrecheckError) as excinfo:
        run_precheck(
            [(str(source), "/main.py")],
            mode="strict",
            compat="warn",
            mp_version="1.19.1",
        )

    message = str(excinfo.value)
    assert message.count("dict union operator | requires MicroPython v1.20.0+") == 1


def test_unknown_bitor_does_not_warn_when_target_supports_dict_union(tmp_path: Path):
    source = _write(tmp_path / "main.py", "merged = left | right\n")

    report = run_precheck(
        [(str(source), "/main.py")],
        mode="strict",
        compat="warn",
        mp_version="1.20.0",
    )

    assert not any("static detector confidence is low" in item.message for item in report.warnings)


def test_function_annotation_semantics_warns_only_when_runtime_accessed(tmp_path: Path):
    source = _write(
        tmp_path / "main.py",
        "def f(value: int) -> int:\n"
        "    return value\n"
        "print(f.__annotations__)\n",
    )

    report = run_precheck(
        [(str(source), "/main.py")],
        mode="strict",
        compat="warn",
        mp_version="1.29.0-preview",
    )

    assert any("runtime __annotations__ access" in item.message for item in report.warnings)


def test_function_annotation_semantics_detects_access_before_function(tmp_path: Path):
    source = _write(
        tmp_path / "main.py",
        "print(f.__annotations__)\n"
        "def f(value: int) -> int:\n"
        "    return value\n",
    )

    report = run_precheck(
        [(str(source), "/main.py")],
        mode="strict",
        compat="warn",
        mp_version="1.29.0-preview",
    )

    assert any("runtime __annotations__ access" in item.message for item in report.warnings)


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


def test_flash_dry_run_precheck_runs_after_device_probe_before_flash(tmp_path: Path):
    source = _write(tmp_path / "main.py", "def broken(:\n    pass\n")
    mp = _fake_flash_mp()

    with patch("cli.main._mp_factory", return_value=mp):
        result = runner.invoke(app, [
            "flash", "COM3", str(source), "/main.py", "--dry-run",
        ])

    assert result.exit_code == 1
    assert "precheck failed" in result.output.lower()
    mp.connect.assert_called_once()
    mp._enter_raw_repl.assert_called_once()
    mp.flash_file.assert_not_called()


def test_flash_accepts_bare_check_option(tmp_path: Path):
    source = _write(tmp_path / "main.py", "def broken(:\n    pass\n")
    mp = _fake_flash_mp()

    with patch("cli.main._mp_factory", return_value=mp):
        result = runner.invoke(app, [
            "flash", "COM3", str(source), "/main.py", "--dry-run", "--check",
        ])

    assert result.exit_code == 1
    assert "precheck failed" in result.output.lower()
    mp.connect.assert_called_once()
    mp.flash_file.assert_not_called()


def test_project_flash_accepts_check_equals_strict(tmp_path: Path):
    _write(tmp_path / "main.py", "def broken(:\n    pass\n")
    mp = _fake_flash_mp()

    with patch("cli.reg_commands.project._mp_factory", return_value=mp):
        result = runner.invoke(app, [
            "project", "flash", "COM3", str(tmp_path), "/app", "--dry-run", "--check=strict",
        ])

    assert result.exit_code == 1
    assert "precheck failed" in result.output.lower()
    mp.connect.assert_called_once()
    mp._enter_raw_repl.assert_called_once()


def test_flash_strict_uses_detected_device_mp_version_for_precheck(tmp_path: Path):
    source = _write(tmp_path / "main.py", "print(f'{1}')\n")
    mp = _fake_flash_mp(version="1.16")

    with patch("cli.main._mp_factory", return_value=mp):
        result = runner.invoke(app, [
            "flash", "COM3", str(source), "/main.py", "--dry-run", "--check=strict",
        ])

    assert result.exit_code == 1
    assert "f-string requires micropython v1.17+" in result.output.lower()
    mp.connect.assert_called_once()
    mp.flash_file.assert_not_called()


def test_flash_default_check_uses_detected_device_version_for_hard_errors(tmp_path: Path):
    source = _write(tmp_path / "main.py", "print(f'{1}')\n")
    mp = _fake_flash_mp(version="1.12.0")

    with patch("cli.main._mp_factory", return_value=mp):
        result = runner.invoke(app, [
            "flash", "COM3", str(source), "/main.py", "--dry-run",
        ])

    assert result.exit_code == 1
    assert "f-string requires micropython v1.17+" in result.output.lower()
    mp.connect.assert_called_once()
    mp.flash_file.assert_not_called()


def test_flash_strict_explicit_mp_version_overrides_detected_version(tmp_path: Path):
    source = _write(tmp_path / "main.py", "print(f'{1}')\n")
    mp = _fake_flash_mp(version="1.22.0")

    with patch("cli.main._mp_factory", return_value=mp):
        result = runner.invoke(app, [
            "flash", "COM3", str(source), "/main.py",
            "--dry-run", "--check=strict", "--mp-version", "1.16",
        ])

    assert result.exit_code == 1
    assert "f-string requires micropython v1.17+" in result.output.lower()
    mp.flash_file.assert_not_called()


def test_flash_program_manifest_precheck_uses_detected_device_tags(tmp_path: Path):
    _write(tmp_path / "wifi_case.py", "def broken(:\n    pass\n")
    manifest = _write(
        tmp_path / "manifest.py",
        'module("wifi_case.py", features=["wifi"])\n',
    )
    mp = _fake_flash_mp()
    mp.detect_tags.return_value = {"USB_ONLY"}

    with patch("cli.main._mp_factory", return_value=mp):
        result = runner.invoke(app, [
            "flash-program", "COM3", str(tmp_path), "/app",
            "--manifest", str(manifest), "--dry-run",
        ])

    assert result.exit_code == 0
    mp.flash_program.assert_called_once()
    assert mp.flash_program.call_args.kwargs["active_tags"] == {"USB_ONLY"}


def test_flash_program_manifest_precheck_uses_explicit_target_over_detected_tags(tmp_path: Path):
    _write(tmp_path / "wifi_case.py", "def broken(:\n    pass\n")
    manifest = _write(
        tmp_path / "manifest.py",
        'module("wifi_case.py", features=["wifi"])\n',
    )
    mp = _fake_flash_mp()
    mp.config.board_tags = {"ESP32": ["ESP32", "wifi"]}
    mp.detect_tags.return_value = {"USB_ONLY"}

    with patch("cli.main._mp_factory", return_value=mp):
        result = runner.invoke(app, [
            "flash-program", "COM3", str(tmp_path), "/app",
            "--manifest", str(manifest), "--dry-run", "--target", "ESP32",
        ])

    assert result.exit_code == 1
    assert "precheck failed" in result.output.lower()
    mp.detect_tags.assert_not_called()
    mp.flash_program.assert_not_called()


def test_flash_no_check_skips_precheck(tmp_path: Path):
    source = _write(tmp_path / "main.py", "def broken(:\n    pass\n")
    mp = _fake_flash_mp()

    with patch("cli.main._mp_factory", return_value=mp):
        result = runner.invoke(app, [
            "flash", "COM3", str(source), "/main.py", "--dry-run", "--no-check",
        ])

    assert result.exit_code == 0
    mp.connect.assert_called_once()


def test_flash_program_dry_run_precheck_runs_after_device_probe(tmp_path: Path):
    _write(tmp_path / "main.py", "def broken(:\n    pass\n")
    mp = _fake_flash_mp()

    with patch("cli.main._mp_factory", return_value=mp):
        result = runner.invoke(app, [
            "flash-program", "COM3", str(tmp_path), "/app", "--dry-run",
        ])

    assert result.exit_code == 1
    assert "precheck failed" in result.output.lower()
    mp.connect.assert_called_once()
    mp._enter_raw_repl.assert_called_once()
    mp.flash_program.assert_not_called()


def test_project_flash_dry_run_precheck_runs_after_device_probe(tmp_path: Path):
    _write(tmp_path / "main.py", "def broken(:\n    pass\n")
    mp = _fake_flash_mp()

    with patch("cli.reg_commands.project._mp_factory", return_value=mp):
        result = runner.invoke(app, [
            "project", "flash", "COM3", str(tmp_path), "/app", "--dry-run",
        ])

    assert result.exit_code == 1
    assert "precheck failed" in result.output.lower()
    mp.connect.assert_called_once()
    mp._enter_raw_repl.assert_called_once()


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

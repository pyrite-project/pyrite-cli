import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli.main import app
from cli.utils.build import (
    ManifestLockError,
    build_manifest_lock,
    check_manifest_lock_current,
    load_manifest_lock,
    write_manifest_lock,
)


runner = CliRunner()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_manifest_lock_generation_is_stable_and_json_readable(tmp_path: Path):
    _write(tmp_path / "main.py", "print('main')\n")
    _write(tmp_path / "lib" / "net.py", "print('net')\n")
    _write(
        tmp_path / "manifest.py",
        "\n".join([
            'module("main.py", remote="/main.py")',
            'module("lib/net.py", remote="/lib/net.py", features=["wifi"])',
        ]),
    )

    lock_a = build_manifest_lock(
        tmp_path / "manifest.py",
        active_tags={"ESP32", "wifi"},
        base_dir=tmp_path,
        target="esp32_s3",
        build_settings={"auto_compile": True, "mpy_arch": "xtensa"},
    )
    lock_b = build_manifest_lock(
        tmp_path / "manifest.py",
        active_tags={"wifi", "ESP32"},
        base_dir=tmp_path,
        target="esp32_s3",
        build_settings={"mpy_arch": "xtensa", "auto_compile": True},
    )

    assert lock_a.to_dict() == lock_b.to_dict()
    payload = json.dumps(lock_a.to_dict(), ensure_ascii=False, indent=2)
    parsed = json.loads(payload)
    assert parsed["version"] == 2
    assert parsed["target"] == "esp32_s3"
    assert "profile" not in parsed
    assert [entry["remote"] for entry in parsed["modules"]] == [
        "/main.py",
        "/lib/net.py",
    ]
    assert parsed["features"] == {
        "active_tags": ["ESP32", "wifi"],
        "included": ["wifi"],
        "excluded": [],
    }
    assert parsed["build"] == {
        "auto_compile": True,
        "mpy_arch": "xtensa",
    }


def test_locked_check_detects_manifest_changes(tmp_path: Path):
    _write(tmp_path / "main.py", "print('main')\n")
    manifest = tmp_path / "manifest.py"
    _write(manifest, 'module("main.py", remote="/main.py")\n')
    lock_path = write_manifest_lock(
        manifest,
        active_tags=set(),
        base_dir=tmp_path,
        lock_path=tmp_path / "pyrite.lock",
    )

    assert check_manifest_lock_current(
        manifest,
        active_tags=set(),
        base_dir=tmp_path,
        lock_path=lock_path,
    )

    _write(manifest, 'module("main.py", remote="/boot.py")\n')
    with pytest.raises(ManifestLockError, match="pyrite.lock is out of date"):
        check_manifest_lock_current(
            manifest,
            active_tags=set(),
            base_dir=tmp_path,
            lock_path=lock_path,
        )


def test_manifest_lock_target_features_filter_modules(tmp_path: Path):
    _write(tmp_path / "main.py", "print('main')\n")
    _write(tmp_path / "wifi.py", "print('wifi')\n")
    _write(tmp_path / "ble.py", "print('ble')\n")
    _write(
        tmp_path / "manifest.py",
        "\n".join([
            'module("main.py")',
            'module("wifi.py", features=["wifi"])',
            'module("ble.py", features=["ble"])',
        ]),
    )

    lock = build_manifest_lock(
        tmp_path / "manifest.py",
        active_tags={"ESP32", "wifi", "esp32_s3"},
        base_dir=tmp_path,
        target="esp32_s3",
    )

    assert [entry.remote for entry in lock.modules] == ["main.py", "wifi.py"]
    assert lock.features.included == ("wifi",)
    assert lock.features.excluded == ("ble",)
    assert lock.target == "esp32_s3"


def test_manifest_lock_round_trips_from_file(tmp_path: Path):
    _write(tmp_path / "main.py", "print('main')\n")
    manifest = tmp_path / "manifest.py"
    _write(manifest, 'module("main.py")\n')

    lock_path = write_manifest_lock(
        manifest,
        active_tags={"ESP32"},
        base_dir=tmp_path,
        lock_path=tmp_path / "pyrite.lock",
    )

    loaded = load_manifest_lock(lock_path)

    assert loaded.to_dict() == build_manifest_lock(
        manifest,
        active_tags={"ESP32"},
        base_dir=tmp_path,
    ).to_dict()


def test_manifest_lock_v1_profile_is_normalised_and_still_current(tmp_path: Path):
    _write(tmp_path / "main.py", "print('main')\n")
    manifest = tmp_path / "manifest.py"
    _write(manifest, 'module("main.py")\n')

    current = build_manifest_lock(
        manifest,
        active_tags={"ESP32"},
        base_dir=tmp_path,
        target="esp32_s3",
        build_settings={"auto_compile": True},
    ).to_dict()
    current["version"] = 1
    current["profile"] = current.pop("target")
    lock_path = tmp_path / "pyrite.lock"
    _write(lock_path, json.dumps(current))

    loaded = load_manifest_lock(lock_path)
    assert loaded.version == 2
    assert loaded.target == "esp32_s3"
    assert loaded.to_dict()["target"] == "esp32_s3"
    assert "profile" not in loaded.to_dict()
    assert check_manifest_lock_current(
        manifest,
        active_tags={"ESP32"},
        base_dir=tmp_path,
        lock_path=lock_path,
        target="esp32_s3",
        build_settings={"auto_compile": True},
    )


def test_manifest_lock_cli_lock_and_plan_emit_json(tmp_path: Path):
    _write(tmp_path / "main.py", "print('main')\n")
    _write(tmp_path / "wifi.py", "print('wifi')\n")
    _write(
        tmp_path / "manifest.py",
        "\n".join([
            'module("main.py")',
            'module("wifi.py", remote="/lib/wifi.py", features=["wifi"])',
        ]),
    )

    lock_result = runner.invoke(app, [
        "manifest", "lock",
        "--manifest", str(tmp_path / "manifest.py"),
        "--base-dir", str(tmp_path),
        "--target", "esp32_s3",
        "--feature", "wifi",
        "--lockfile", str(tmp_path / "pyrite.lock"),
        "--format", "json",
    ])
    assert lock_result.exit_code == 0
    lock_data = json.loads(lock_result.stdout)
    assert lock_data["lockfile"] == str(tmp_path / "pyrite.lock")
    assert lock_data["version"] == 2
    assert lock_data["target"] == "esp32_s3"
    assert "profile" not in lock_data
    assert lock_data["modules"][1]["remote"] == "/lib/wifi.py"

    plan_result = runner.invoke(app, [
        "manifest", "plan",
        "--manifest", str(tmp_path / "manifest.py"),
        "--base-dir", str(tmp_path),
        "--target", "esp32_s3",
        "--format", "json",
    ])
    assert plan_result.exit_code == 0
    plan_data = json.loads(plan_result.stdout)
    assert plan_data["target"] == "esp32_s3"
    assert "profile" not in plan_data
    assert [entry["remote"] for entry in plan_data["modules"]] == ["main.py"]


def test_manifest_cli_profile_is_hidden_deprecated_alias(tmp_path: Path):
    _write(tmp_path / "main.py", "print('main')\n")
    _write(tmp_path / "manifest.py", 'module("main.py")\n')

    help_result = runner.invoke(app, ["manifest", "plan", "--help"])
    assert help_result.exit_code == 0
    assert "--target" in help_result.stdout
    assert "--profile" not in help_result.stdout

    result = runner.invoke(app, [
        "manifest", "plan",
        "--manifest", str(tmp_path / "manifest.py"),
        "--base-dir", str(tmp_path),
        "--profile", "esp32_s3",
        "--format", "json",
    ])
    assert result.exit_code == 0
    assert "--profile" in result.output
    assert "--target" in result.output
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    assert payload["target"] == "esp32_s3"
    assert "profile" not in payload


def test_manifest_cli_rejects_conflicting_target_and_profile(tmp_path: Path):
    _write(tmp_path / "manifest.py", "")

    result = runner.invoke(app, [
        "manifest", "lock",
        "--manifest", str(tmp_path / "manifest.py"),
        "--base-dir", str(tmp_path),
        "--target", "esp32",
        "--profile", "rp2",
    ])

    assert result.exit_code == 2
    assert "--target" in result.output
    assert "--profile" in result.output


def test_manifest_plan_text_uses_target_label(tmp_path: Path):
    _write(tmp_path / "main.py", "print('main')\n")
    _write(tmp_path / "manifest.py", 'module("main.py")\n')

    result = runner.invoke(app, [
        "manifest", "plan",
        "--manifest", str(tmp_path / "manifest.py"),
        "--base-dir", str(tmp_path),
        "--target", "esp32_s3",
    ])

    assert result.exit_code == 0
    assert "target: esp32_s3" in result.stdout
    assert "profile:" not in result.stdout

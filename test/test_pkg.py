import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app
from cli.utils.pkg import (
    PkgDependencyError,
    build_cache_plan,
    build_install_offline_plan,
    build_install_plan,
    load_package_manifest,
    run_pkg_plan,
)


runner = CliRunner()


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class TestPkgInstallPlan:
    def test_install_dry_run_builds_mpremote_command_without_subprocess(self):
        plan = build_install_plan(
            "COM3",
            "aioble",
            target="/lib",
            mpremote="custom-mpremote",
            dry_run=True,
        )

        assert list(plan.command) == [
            "custom-mpremote", "connect", "COM3",
            "mip", "install", "aioble", "--target", "/lib",
        ]

        runner_mock = MagicMock()
        result = run_pkg_plan(plan, runner=runner_mock)

        assert result is plan
        runner_mock.assert_not_called()

    def test_install_non_dry_run_calls_runner(self):
        plan = build_install_plan("COM3", "aioble", target="/lib")
        completed = subprocess.CompletedProcess(list(plan.command), 0)
        runner_mock = MagicMock(return_value=completed)

        result = run_pkg_plan(plan, runner=runner_mock)

        assert result is completed
        runner_mock.assert_called_once_with(list(plan.command), check=False)


class TestPkgCachePlan:
    def test_cache_plan_sanitizes_package_key_and_keeps_audit_note(self, tmp_path: Path):
        plan = build_cache_plan(
            "github:micropython/micropython-lib@main",
            version="latest",
            cache_root=tmp_path / "cache",
            dry_run=True,
        )

        assert plan.cache_dir == (
            tmp_path / "cache" / "github_micropython_micropython-lib_main" / "latest"
        )
        assert plan.command == ()
        assert plan.dry_run is True
        assert any("mpremote mip install" in note for note in plan.notes)

    def test_cache_plan_parses_local_manifest_for_audit(self, tmp_path: Path):
        package_dir = tmp_path / "pkg"
        _write_json(
            package_dir / "package.json",
            {
                "deps": ["logging"],
                "urls": [["mod.py", "https://example.invalid/mod.py"]],
            },
        )

        plan = build_cache_plan(str(package_dir), cache_root=tmp_path / "cache")

        assert plan.manifest is not None
        assert plan.manifest.deps == ("logging",)
        assert plan.manifest.urls == (
            {"path": "mod.py", "url": "https://example.invalid/mod.py"},
        )

    def test_cache_plan_rejects_missing_local_source(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="本地包路径不存在"):
            build_cache_plan(str(tmp_path / "missing" / "package.json"))


class TestPkgOfflinePlan:
    def test_install_offline_dir_uses_local_source_and_manifest(self, tmp_path: Path):
        package_dir = tmp_path / "aioble"
        _write_json(package_dir / "package.json", {"deps": [], "urls": []})

        plan = build_install_offline_plan("COM3", str(package_dir), target="/lib")

        assert list(plan.command) == [
            "mpremote", "connect", "COM3",
            "mip", "install", str(package_dir.resolve()), "--target", "/lib",
        ]
        assert plan.manifest is not None
        assert plan.manifest.path == (package_dir / "package.json").resolve()

    def test_install_offline_rejects_missing_source(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="离线包路径不存在"):
            build_install_offline_plan("COM3", str(tmp_path / "missing"))

    def test_install_offline_rejects_dir_without_package_json(self, tmp_path: Path):
        package_dir = tmp_path / "empty"
        package_dir.mkdir()

        with pytest.raises(FileNotFoundError, match="缺少 package.json"):
            build_install_offline_plan("COM3", str(package_dir))


class TestPkgManifestAudit:
    def test_invalid_deps_reports_source_path(self, tmp_path: Path):
        package_json = tmp_path / "package.json"
        _write_json(package_json, {"deps": [{"name": "bad"}]})

        with pytest.raises(PkgDependencyError, match="无法解析 deps"):
            load_package_manifest(package_json)

    def test_missing_manifest_reports_file(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="package.json 不存在"):
            load_package_manifest(tmp_path / "missing.json")


class TestPkgCli:
    def test_pkg_install_resolves_board_alias_in_dry_run_plan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        alias_file = tmp_path / "aliases.json"
        _write_json(alias_file, {"version": 1, "aliases": {"bench": "COM7"}})
        monkeypatch.setenv("PYRITE_BOARD_ALIAS_FILE", str(alias_file))

        result = runner.invoke(app, [
            "pkg", "install", "@bench", "aioble",
            "--dry-run",
            "--format", "json",
        ])

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["port"] == "COM7"
        assert data["command"][:3] == ["mpremote", "connect", "COM7"]

    def test_pkg_install_dry_run_outputs_plan_json_without_subprocess(self):
        with patch("cli.utils.pkg.subprocess.run", side_effect=AssertionError("no subprocess")):
            result = runner.invoke(app, [
                "pkg", "install", "COM3", "aioble",
                "--target", "/lib",
                "--dry-run",
                "--format", "json",
            ])

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["action"] == "install"
        assert data["command"] == [
            "mpremote", "connect", "COM3",
            "mip", "install", "aioble", "--target", "/lib",
        ]

    def test_pkg_cache_outputs_cache_plan_json(self, tmp_path: Path):
        result = runner.invoke(app, [
            "pkg", "cache", "aioble",
            "--version", "latest",
            "--cache-root", str(tmp_path / "cache"),
            "--dry-run",
            "--format", "json",
        ])

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["action"] == "cache"
        assert data["cache_dir"] == str(tmp_path / "cache" / "aioble" / "latest")
        assert data["command"] == []

    def test_pkg_install_offline_missing_path_exits_cleanly(self, tmp_path: Path):
        result = runner.invoke(app, [
            "pkg", "install-offline", "COM3", str(tmp_path / "missing"),
        ])

        assert result.exit_code == 1
        assert "离线包路径不存在" in result.output

    def test_pkg_install_offline_resolves_board_alias_in_dry_run_plan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        alias_file = tmp_path / "aliases.json"
        _write_json(alias_file, {"version": 1, "aliases": {"bench": "COM8"}})
        monkeypatch.setenv("PYRITE_BOARD_ALIAS_FILE", str(alias_file))
        package_dir = tmp_path / "pkg"
        _write_json(package_dir / "package.json", {"deps": [], "urls": []})

        result = runner.invoke(app, [
            "pkg", "install-offline", "@bench", str(package_dir),
            "--dry-run",
            "--format", "json",
        ])

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["port"] == "COM8"
        assert data["command"][:3] == ["mpremote", "connect", "COM8"]

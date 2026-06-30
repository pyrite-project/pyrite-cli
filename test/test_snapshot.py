from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from cli.main import app
from cli.reg_commands.snapshot import save_device_snapshot
from cli.utils.snapshot import (
    SnapshotEntry,
    SnapshotManifest,
    build_diff_plan,
    build_restore_plan,
    filter_device_entries,
    load_snapshot_manifest,
    safe_snapshot_name,
    save_snapshot_files,
    sha256_file,
)


runner = CliRunner()


def test_snapshot_name_rejects_path_traversal():
    assert safe_snapshot_name("before-refactor") == "before-refactor"

    for value in ("../x", "a/b", "", ".hidden", "name with spaces"):
        try:
            safe_snapshot_name(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected invalid snapshot name: {value!r}")


def test_save_snapshot_files_writes_manifest_and_sha256(tmp_path: Path):
    data = {
        "/main.py": b"print('hi')\n",
        "/lib/sensor.py": b"VALUE = 42\n",
    }

    manifest = save_snapshot_files(
        "before",
        data,
        root=tmp_path,
        device="COM9",
        include=("/lib/**",),
        exclude=("*.tmp",),
    )

    assert (tmp_path / "before" / "files" / "main.py").read_bytes() == data["/main.py"]
    assert manifest.name == "before"
    assert manifest.device == "COM9"
    assert manifest.include == ["/lib/**"]
    assert manifest.exclude == ["*.tmp"]
    by_path = {entry.path: entry for entry in manifest.files}
    assert by_path["/main.py"].sha256 == sha256_file(
        tmp_path / "before" / "files" / "main.py"
    )

    loaded = load_snapshot_manifest(tmp_path / "before")
    assert loaded.files == manifest.files


def test_filter_device_entries_honors_include_exclude_and_defaults():
    entries = [
        {"name": "/main.py", "type": "F", "size": "1"},
        {"name": "/log/session.txt", "type": "F", "size": "2"},
        {"name": "/lib/cache.tmp", "type": "F", "size": "3"},
        {"name": "/lib", "type": "D", "size": "0"},
    ]

    selected = filter_device_entries(
        entries,
        include=("/lib/**",),
        exclude=("*.tmp",),
    )

    assert selected == []
    assert filter_device_entries(entries)[0]["name"] == "/main.py"


def test_diff_plan_classifies_added_changed_deleted_unchanged(tmp_path: Path):
    manifest = SnapshotManifest(
        name="before",
        created_at="2026-06-30T00:00:00Z",
        device="COM9",
        include=[],
        exclude=[],
        files=[
            SnapshotEntry("/same.py", "files/same.py", 4, "same"),
            SnapshotEntry("/changed.py", "files/changed.py", 7, "snapshot"),
            SnapshotEntry("/deleted.py", "files/deleted.py", 1, "deleted"),
        ],
    )
    current = {
        "/same.py": {"size": 4, "sha256": "same"},
        "/changed.py": {"size": 7, "sha256": "device"},
        "/added.py": {"size": 2, "sha256": "added"},
    }

    plan = build_diff_plan(manifest, current)

    assert [item.path for item in plan.unchanged] == ["/same.py"]
    assert [item.path for item in plan.overwrite] == ["/changed.py"]
    assert [item.path for item in plan.delete] == ["/added.py"]
    assert [item.path for item in plan.add] == ["/deleted.py"]


def test_restore_plan_defaults_to_dry_run_until_apply_requested():
    manifest = SnapshotManifest(
        name="before",
        created_at="2026-06-30T00:00:00Z",
        device="COM9",
        include=[],
        exclude=[],
        files=[SnapshotEntry("/main.py", "files/main.py", 1, "snapshot")],
    )
    current = {"/main.py": {"size": 1, "sha256": "device"}}

    dry = build_restore_plan(manifest, current)
    apply = build_restore_plan(manifest, current, apply=True)

    assert dry.dry_run is True
    assert apply.dry_run is False
    assert [item.path for item in dry.overwrite] == ["/main.py"]


def test_snapshot_cli_help_is_registered():
    for args in [
        ["snapshot", "--help"],
        ["snapshot", "save", "--help"],
        ["snapshot", "list", "--help"],
        ["snapshot", "diff", "--help"],
        ["snapshot", "restore", "--help"],
    ]:
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.stdout
        assert "snapshot" in result.stdout.lower()


def test_project_flash_help_exposes_snapshot_before():
    result = runner.invoke(app, ["project", "flash", "--help"])

    assert result.exit_code == 0
    assert "--snapshot-before" in result.stdout


def test_save_device_snapshot_uses_temp_download_area(tmp_path: Path):
    class MP:
        def fs_ls_recursive(self, _remote_path):
            return [{"name": "/main.py", "type": "F", "size": "11"}]

        def fs_get(self, _remote_path, local_path):
            Path(local_path).parent.mkdir(parents=True, exist_ok=True)
            Path(local_path).write_bytes(b"print(1)\n")
            return 9

    manifest = save_device_snapshot(
        MP(),
        name="before",
        port="COM9",
        output_dir=str(tmp_path),
    )

    assert manifest.files[0].path == "/main.py"
    assert (tmp_path / "before" / "files" / "main.py").exists()
    assert not (tmp_path / "before" / "download").exists()

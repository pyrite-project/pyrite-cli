from pathlib import Path

import pytest

from cli.utils.build import load_manifest


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_package_remote_prefix_is_applied(tmp_path: Path):
    _write(tmp_path / "pkg" / "a.py", "print('a')\n")
    _write(tmp_path / "pkg" / "config.json", "{}\n")
    _write(tmp_path / "pkg" / "sub" / "b.py", "print('b')\n")
    manifest = tmp_path / "manifest.py"
    _write(manifest, 'package("pkg", remote="/lib")\n')

    entries = load_manifest(str(manifest), active_tags=set(), base_dir=str(tmp_path))
    remotes = {rp for _, rp in entries}

    assert "/lib/pkg/a.py" in remotes
    assert "/lib/pkg/config.json" in remotes
    assert "/lib/pkg/sub/b.py" in remotes


@pytest.mark.parametrize(
    "filename",
    [
        "/etc/passwd",
        "C:/Users/alice/.ssh/id_rsa",
        r"\\server\share\secret.py",
        "../secret.py",
    ],
)
def test_reject_local_paths_outside_base_dir(tmp_path: Path, filename: str):
    manifest = tmp_path / "manifest.py"
    _write(manifest, f"module({filename!r}, remote='/x')\n")

    with pytest.raises(ValueError):
        load_manifest(str(manifest), active_tags=set(), base_dir=str(tmp_path))


def test_package_remote_absolute_device_path_is_allowed(tmp_path: Path):
    _write(tmp_path / "pkg" / "a.py", "print('a')\n")
    manifest = tmp_path / "manifest.py"
    _write(manifest, 'package("pkg", remote="/lib")\n')

    entries = load_manifest(str(manifest), active_tags=set(), base_dir=str(tmp_path))
    remotes = [rp for _, rp in entries]

    assert remotes == ["/lib/pkg/a.py"]


@pytest.mark.parametrize(
    "remote",
    [
        "../lib",
        "C:/lib",
        r"\\server\share\lib",
    ],
)
def test_reject_host_shaped_or_traversing_remote_paths(tmp_path: Path, remote: str):
    _write(tmp_path / "a.py", "print('a')\n")
    manifest = tmp_path / "manifest.py"
    _write(manifest, f"module('a.py', remote={remote!r})\n")

    with pytest.raises(ValueError):
        load_manifest(str(manifest), active_tags=set(), base_dir=str(tmp_path))


def test_reject_extra_positional_argument(tmp_path: Path):
    manifest = tmp_path / "manifest.py"
    _write(manifest, 'module("a.py", "b.py")\n')

    with pytest.raises(ValueError, match="only one positional argument"):
        load_manifest(str(manifest), active_tags=set(), base_dir=str(tmp_path))


def test_reject_duplicate_keyword(tmp_path: Path):
    manifest = tmp_path / "manifest.py"
    _write(manifest, 'module("a.py", remote="x", remote="y")\n')

    with pytest.raises(ValueError):
        load_manifest(str(manifest), active_tags=set(), base_dir=str(tmp_path))

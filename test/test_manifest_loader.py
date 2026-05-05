from pathlib import Path

import pytest

from cli.utils.manifest_loader import load_manifest


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_package_remote_prefix_is_applied(tmp_path: Path):
    _write(tmp_path / "pkg" / "a.py", "print('a')\n")
    _write(tmp_path / "pkg" / "sub" / "b.py", "print('b')\n")
    manifest = tmp_path / "manifest.py"
    _write(manifest, 'package("pkg", remote="/lib")\n')

    entries = load_manifest(str(manifest), active_tags=set(), base_dir=str(tmp_path))
    remotes = {rp for _, rp in entries}

    assert "/lib/pkg/a.py" in remotes
    assert "/lib/pkg/sub/b.py" in remotes


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

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

from .build import load_manifest
from .config import HASH_CONFIG_FILE


PROJECT_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".pyrite_cache",
    ".pyrite_snapshots",
    ".venv",
    "venv",
    "env",
    "build",
    "dist",
    "log",
    "node_modules",
}

PROJECT_IGNORED_FILENAMES = {
    HASH_CONFIG_FILE,
    ".pyrite_config.json",
    "pyrite.lock",
    "pyproject.toml",
    ".DS_Store",
    "Thumbs.db",
}


def is_project_upload_filename(filename: str) -> bool:
    name = Path(str(filename)).name
    if not name:
        return False
    if name == "manifest.py":
        return False
    if name in PROJECT_IGNORED_FILENAMES:
        return False
    if name.endswith((".pyi", ".pyc")):
        return False
    return True


def is_project_upload_entry(local_path: str, remote_path: str) -> bool:
    return (
        is_project_upload_filename(local_path)
        and is_project_upload_filename(remote_path)
    )


def collect_project_files(
    local_dir: str,
    active_tags: Optional[Set[str]] = None,
    manifest_path: Optional[str] = None,
    exclude_paths: Optional[Iterable[str]] = None,
) -> List[Tuple[str, str]]:
    excluded = {
        str(Path(path).resolve())
        for path in (exclude_paths or [])
        if path
    }
    if manifest_path:
        entries = load_manifest(
            manifest_path,
            active_tags,
            base_dir=local_dir,
        )
    else:
        entries = []
        for root, dirs, files in os.walk(local_dir):
            dirs[:] = [
                dirname for dirname in dirs
                if dirname not in PROJECT_IGNORED_DIRS
            ]
            for filename in files:
                if not is_project_upload_filename(filename):
                    continue
                local_path = os.path.join(root, filename)
                rel = os.path.relpath(local_path, local_dir).replace("\\", "/")
                entries.append((local_path, rel))

    result: List[Tuple[str, str]] = []
    for local_path, remote_path in entries:
        if str(Path(local_path).resolve()) in excluded:
            continue
        if not is_project_upload_entry(str(local_path), str(remote_path)):
            continue
        result.append((str(local_path), str(remote_path)))
    return result

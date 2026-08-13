from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def load_project_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read project config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("project config top level must be an object")
    return data


def _write_json(path: Path, data: dict[str, Any], *, sort_keys: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=sort_keys) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def update_firmware(path: Path, firmware: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(firmware, dict):
        raise ValueError("firmware config must be an object")
    data = load_project_data(path)
    data["firmware"] = firmware
    _write_json(path, data)
    return data


def _lock_path(path: Path) -> Path:
    return path.parent / "pyrite.lock"


def update_lock(path: Path, payload: dict[str, Any]) -> None:
    """Update the firmware section of the existing v2 pyrite.lock file."""
    if not isinstance(payload, dict):
        raise ValueError("firmware lock payload must be an object")
    lock_path = _lock_path(path)
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        lock = {"version": 2, "manifest": {"path": "", "sha256": ""}, "target": None, "features": {"active_tags": [], "included": [], "excluded": []}, "modules": [], "packages": []}
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"cannot read lockfile {lock_path}: {exc}") from exc
    if not isinstance(lock, dict) or lock.get("version") != 2:
        raise ValueError("firmware requires a version 2 pyrite.lock")
    build = lock.get("build")
    if not isinstance(build, dict):
        build = {}
    build["firmware"] = payload
    lock["build"] = build
    _write_json(lock_path, lock, sort_keys=True)


def load_lock(path: Path) -> dict[str, Any]:
    lock_path = _lock_path(path)
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read firmware lock {lock_path}: {exc}") from exc
    if not isinstance(lock, dict) or lock.get("version") != 2:
        raise ValueError("firmware requires a version 2 pyrite.lock")
    return lock


def assert_lock_current(path: Path, payload: dict[str, Any]) -> None:
    lock = load_lock(path)
    if not isinstance(lock.get("build"), dict) or lock["build"].get("firmware") != payload:
        raise ValueError("pyrite.lock firmware section is stale; run `pyrcli firmware generate`")

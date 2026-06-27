"""Manifest lockfile helpers.

The lockfile records the host-side manifest resolution plan, not file
contents. It is intended to catch changes in manifest directives, feature
selection, remote mapping, and build settings before a locked flash.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .manifest_loader import ManifestPlan, load_manifest_plan

LOCKFILE_NAME = "pyrite.lock"
LOCKFILE_VERSION = 1


class ManifestLockError(ValueError):
    """Raised when a lockfile is missing, invalid, or stale."""


@dataclass(frozen=True)
class ManifestLockModule:
    local: str
    remote: str
    directive: str
    source: str
    features: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "local": self.local,
            "remote": self.remote,
            "directive": self.directive,
            "source": self.source,
        }
        if self.features:
            data["features"] = list(self.features)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ManifestLockModule":
        features = data.get("features", [])
        if not isinstance(features, list) or not all(
            isinstance(item, str) for item in features
        ):
            raise ManifestLockError("pyrite.lock modules[].features must be a string list")
        return cls(
            local=_require_str(data, "local"),
            remote=_require_str(data, "remote"),
            directive=_require_str(data, "directive"),
            source=_require_str(data, "source"),
            features=tuple(features),
        )


@dataclass(frozen=True)
class ManifestFeatureSummary:
    active_tags: tuple[str, ...] = ()
    included: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "active_tags": list(self.active_tags),
            "included": list(self.included),
            "excluded": list(self.excluded),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ManifestFeatureSummary":
        return cls(
            active_tags=_require_str_list(data, "active_tags"),
            included=_require_str_list(data, "included"),
            excluded=_require_str_list(data, "excluded"),
        )


@dataclass(frozen=True)
class ManifestLock:
    version: int
    manifest_path: str
    manifest_sha256: str
    modules: tuple[ManifestLockModule, ...]
    features: ManifestFeatureSummary
    profile: str | None = None
    build: Mapping[str, object] | None = None
    packages: tuple[dict[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "version": self.version,
            "manifest": {
                "path": self.manifest_path,
                "sha256": self.manifest_sha256,
            },
            "profile": self.profile,
            "features": self.features.to_dict(),
            "build": dict(self.build or {}),
            "modules": [module.to_dict() for module in self.modules],
            "packages": [dict(package) for package in self.packages],
        }
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ManifestLock":
        manifest = data.get("manifest")
        if not isinstance(manifest, Mapping):
            raise ManifestLockError("pyrite.lock manifest must be an object")
        features = data.get("features")
        if not isinstance(features, Mapping):
            raise ManifestLockError("pyrite.lock features must be an object")
        modules = data.get("modules")
        if not isinstance(modules, list) or not all(
            isinstance(item, Mapping) for item in modules
        ):
            raise ManifestLockError("pyrite.lock modules must be a list")
        build = data.get("build", {})
        if not isinstance(build, Mapping):
            raise ManifestLockError("pyrite.lock build must be an object")
        packages = data.get("packages", [])
        if not isinstance(packages, list) or not all(
            isinstance(item, Mapping) for item in packages
        ):
            raise ManifestLockError("pyrite.lock packages must be an object list")

        version = data.get("version")
        if not isinstance(version, int):
            raise ManifestLockError("pyrite.lock version must be an integer")

        profile = data.get("profile")
        if profile is not None and not isinstance(profile, str):
            raise ManifestLockError("pyrite.lock profile must be a string or null")

        return cls(
            version=version,
            manifest_path=_require_str(manifest, "path"),
            manifest_sha256=_require_str(manifest, "sha256"),
            profile=profile,
            features=ManifestFeatureSummary.from_dict(features),
            build=dict(build),
            modules=tuple(
                ManifestLockModule.from_dict(module)
                for module in modules
            ),
            packages=tuple(dict(package) for package in packages),
        )


def build_manifest_lock(
    manifest_path: str | Path,
    active_tags: set[str] | None = None,
    *,
    base_dir: str | Path | None = None,
    profile: str | None = None,
    build_settings: Mapping[str, object] | None = None,
) -> ManifestLock:
    """Build a deterministic lockfile payload from manifest.py."""
    manifest, base = _resolve_manifest_and_base(manifest_path, base_dir)
    plan = load_manifest_plan(str(manifest), active_tags or set(), base_dir=str(base))
    modules = tuple(_lock_modules(plan, base))
    manifest_resolved = manifest.resolve()
    return ManifestLock(
        version=LOCKFILE_VERSION,
        manifest_path=_relative_path(manifest_resolved, base),
        manifest_sha256=_file_sha256(manifest_resolved),
        profile=profile,
        features=ManifestFeatureSummary(
            active_tags=tuple(sorted(active_tags or set())),
            included=plan.included_features,
            excluded=plan.excluded_features,
        ),
        build=_normalise_build_settings(build_settings or {}),
        modules=modules,
        packages=(),
    )


def save_manifest_lock(lock: ManifestLock, lock_path: str | Path) -> Path:
    """Write a lock payload as stable, readable JSON."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(lock.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def write_manifest_lock(
    manifest_path: str | Path,
    active_tags: set[str] | None = None,
    *,
    base_dir: str | Path | None = None,
    lock_path: str | Path | None = None,
    profile: str | None = None,
    build_settings: Mapping[str, object] | None = None,
) -> Path:
    """Build and write ``pyrite.lock``; returns the written path."""
    manifest, base = _resolve_manifest_and_base(manifest_path, base_dir)
    path = _resolve_lock_path(lock_path, base)
    lock = build_manifest_lock(
        manifest,
        active_tags,
        base_dir=base,
        profile=profile,
        build_settings=build_settings,
    )
    return save_manifest_lock(lock, path)


def load_manifest_lock(lock_path: str | Path) -> ManifestLock:
    """Read and validate a JSON lockfile."""
    path = Path(lock_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestLockError(f"lockfile not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestLockError(f"lockfile is not valid JSON: {path}: {exc.msg}") from exc
    if not isinstance(data, Mapping):
        raise ManifestLockError("pyrite.lock top-level value must be an object")
    lock = ManifestLock.from_dict(data)
    if lock.version != LOCKFILE_VERSION:
        raise ManifestLockError(
            f"unsupported pyrite.lock version {lock.version}; expected {LOCKFILE_VERSION}"
        )
    return lock


def check_manifest_lock_current(
    manifest_path: str | Path,
    active_tags: set[str] | None = None,
    *,
    base_dir: str | Path | None = None,
    lock_path: str | Path | None = None,
    profile: str | None = None,
    build_settings: Mapping[str, object] | None = None,
) -> bool:
    """Raise if the lockfile does not match the current manifest plan."""
    manifest = Path(manifest_path)
    base = Path(base_dir or manifest.parent).resolve()
    path = _resolve_lock_path(lock_path, base)
    expected = build_manifest_lock(
        manifest,
        active_tags,
        base_dir=base,
        profile=profile,
        build_settings=build_settings,
    )
    actual = load_manifest_lock(path)
    if actual.to_dict() != expected.to_dict():
        raise ManifestLockError(
            f"pyrite.lock is out of date for {expected.manifest_path}; "
            "run `pyrcli manifest lock`"
        )
    return True


def _lock_modules(plan: ManifestPlan, base: Path) -> tuple[ManifestLockModule, ...]:
    modules: list[ManifestLockModule] = []
    for entry in plan.entries:
        modules.append(ManifestLockModule(
            local=_relative_path(Path(entry.local_path), base),
            remote=entry.remote_path.replace("\\", "/"),
            directive=entry.directive,
            source=entry.source.replace("\\", "/"),
            features=tuple(entry.features),
        ))
    return tuple(modules)


def _resolve_manifest_and_base(
    manifest_path: str | Path,
    base_dir: str | Path | None,
) -> tuple[Path, Path]:
    raw_manifest = Path(manifest_path)
    if base_dir is not None:
        base = Path(base_dir).resolve()
        manifest = raw_manifest if raw_manifest.is_absolute() else base / raw_manifest
        return manifest, base

    manifest = raw_manifest.resolve()
    return manifest, manifest.parent.resolve()


def _resolve_lock_path(lock_path: str | Path | None, base: Path) -> Path:
    if lock_path is None:
        return base / LOCKFILE_NAME
    path = Path(lock_path)
    if path.is_absolute():
        return path
    return base / path


def _relative_path(path: Path, base: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(base.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalise_build_settings(settings: Mapping[str, object]) -> dict[str, object]:
    return {
        str(key): _jsonable_value(settings[key])
        for key in sorted(settings)
    }


def _jsonable_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return [_jsonable_value(item) for item in sorted(value)]
    if isinstance(value, (tuple, list)):
        return [_jsonable_value(item) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable_value(value[key])
            for key in sorted(value)
        }
    return str(value)


def _require_str(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ManifestLockError(f"pyrite.lock {key} must be a string")
    return value


def _require_str_list(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManifestLockError(f"pyrite.lock {key} must be a string list")
    return tuple(value)

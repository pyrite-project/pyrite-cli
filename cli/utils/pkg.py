"""Package install planning for MicroPython mip/upypi packages.

The implementation intentionally keeps ``mpremote mip install`` as the
primary path.  This module builds auditable plans and only shells out when a
caller explicitly executes a non-dry-run plan.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DEFAULT_CACHE_ROOT = ".pyrite/pkg-cache"


class PkgError(ValueError):
    """Raised when a package plan cannot be built safely."""


class PkgDependencyError(PkgError):
    """Raised when package.json dependency metadata cannot be parsed."""


@dataclass(frozen=True)
class PackageManifest:
    """Audited subset of a mip ``package.json`` file."""

    path: Path
    deps: tuple[str, ...] = ()
    urls: tuple[dict[str, str | None], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "deps": list(self.deps),
            "urls": list(self.urls),
        }


@dataclass(frozen=True)
class PkgPlan:
    """A dry-run friendly package operation plan."""

    action: str
    command: tuple[str, ...] = ()
    port: str | None = None
    package: str | None = None
    target: str | None = None
    cache_dir: Path | None = None
    dry_run: bool = False
    manifest: PackageManifest | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "action": self.action,
            "command": list(self.command),
            "port": self.port,
            "package": self.package,
            "target": self.target,
            "dry_run": self.dry_run,
            "notes": list(self.notes),
        }
        if self.cache_dir is not None:
            data["cache_dir"] = str(self.cache_dir)
        if self.manifest is not None:
            data["manifest"] = self.manifest.to_dict()
        return data


PlanRunner = Callable[..., subprocess.CompletedProcess]


def build_install_plan(
    port: str,
    package: str,
    *,
    target: str | None = None,
    mpremote: str = "mpremote",
    dry_run: bool = False,
) -> PkgPlan:
    """Build a plan for ``mpremote connect <port> mip install <package>``."""

    port = _require_text(port, "port")
    package = _require_text(package, "package")
    target = _normalise_target(target)

    command = _build_mpremote_mip_command(mpremote, port, package, target)
    return PkgPlan(
        action="install",
        command=tuple(command),
        port=port,
        package=package,
        target=target,
        dry_run=dry_run,
        notes=(
            "install uses mpremote mip install as the primary host-side path",
            "dry-run does not connect to the device or invoke subprocess",
        ),
    )


def build_cache_plan(
    package: str,
    *,
    version: str = "latest",
    cache_root: str | Path = DEFAULT_CACHE_ROOT,
    dry_run: bool = True,
) -> PkgPlan:
    """Plan the host-side cache location without downloading from the network."""

    package = _require_text(package, "package")
    version = _require_text(version, "version")
    cache_dir = Path(cache_root).expanduser() / _cache_key(package) / version

    manifest = None
    if _looks_like_local_source(package):
        source_path = Path(package).expanduser()
        try:
            source_exists = source_path.exists()
        except OSError as exc:
            raise FileNotFoundError(f"本地包路径不存在: {source_path}") from exc
        if source_exists:
            _source, manifest_path = resolve_offline_source(package)
            manifest = load_package_manifest(manifest_path)
        else:
            raise FileNotFoundError(f"本地包路径不存在: {source_path}")

    return PkgPlan(
        action="cache",
        package=package,
        cache_dir=cache_dir,
        dry_run=dry_run,
        manifest=manifest,
        notes=(
            "cache is currently a planning/audit step and does not download",
            "install uses mpremote mip install as the primary path",
        ),
    )


def build_install_offline_plan(
    port: str,
    source: str,
    *,
    target: str | None = None,
    mpremote: str = "mpremote",
    dry_run: bool = False,
) -> PkgPlan:
    """Build an install plan for a local package.json file or package dir."""

    port = _require_text(port, "port")
    source = _require_text(source, "source")
    target = _normalise_target(target)
    source_path, manifest_path = resolve_offline_source(source)
    manifest = load_package_manifest(manifest_path)

    command = _build_mpremote_mip_command(mpremote, port, str(source_path), target)
    return PkgPlan(
        action="install-offline",
        command=tuple(command),
        port=port,
        package=str(source_path),
        target=target,
        dry_run=dry_run,
        manifest=manifest,
        notes=(
            "offline install delegates local package handling to mpremote mip install",
            "dry-run does not connect to the device or invoke subprocess",
        ),
    )


def run_pkg_plan(
    plan: PkgPlan,
    *,
    runner: PlanRunner | None = None,
) -> PkgPlan | subprocess.CompletedProcess:
    """Execute a non-dry-run plan, or return the plan unchanged for dry-run."""

    if plan.dry_run:
        return plan
    if not plan.command:
        raise PkgError(f"{plan.action} plan has no executable command")

    if runner is None:
        runner = subprocess.run

    try:
        return runner(list(plan.command), check=False)
    except FileNotFoundError as exc:
        exe = plan.command[0]
        raise FileNotFoundError(
            f"未找到 mpremote 可执行文件: {exe}。请安装 mpremote 或使用 --mpremote 指定路径。"
        ) from exc


def resolve_offline_source(source: str | Path) -> tuple[Path, Path]:
    """Return ``(install_source, package_json)`` for a local package source."""

    source_path = Path(source).expanduser()
    try:
        source_exists = source_path.exists()
    except OSError as exc:
        raise FileNotFoundError(f"离线包路径不存在: {source_path}") from exc
    if not source_exists:
        raise FileNotFoundError(f"离线包路径不存在: {source_path}")

    resolved = source_path.resolve()
    if resolved.is_dir():
        manifest_path = resolved / "package.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"离线包目录缺少 package.json: {manifest_path}")
        return resolved, manifest_path

    if not resolved.is_file() or resolved.suffix.lower() != ".json":
        raise PkgError(f"离线包必须是 package.json 文件或包含 package.json 的目录: {resolved}")
    return resolved, resolved


def load_package_manifest(path: str | Path) -> PackageManifest:
    """Parse dependency and URL metadata from a local mip package manifest."""

    manifest_path = Path(path).expanduser()
    if not manifest_path.is_file():
        raise FileNotFoundError(f"package.json 不存在: {manifest_path}")

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PkgError(f"package.json 不是有效 JSON: {manifest_path}: {exc.msg}") from exc

    if not isinstance(data, Mapping):
        raise PkgError(f"package.json 顶层必须是对象: {manifest_path}")

    deps = _parse_deps(data.get("deps", []), manifest_path)
    urls = _parse_urls(data.get("urls", []), manifest_path)
    return PackageManifest(path=manifest_path.resolve(), deps=deps, urls=urls)


def _build_mpremote_mip_command(
    mpremote: str,
    port: str,
    package: str,
    target: str | None,
) -> list[str]:
    mpremote = _require_text(mpremote, "mpremote")
    command = [mpremote, "connect", port, "mip", "install", package]
    if target:
        command.extend(["--target", target])
    return command


def _parse_deps(raw: object, source: Path) -> tuple[str, ...]:
    if raw in (None, []):
        return ()
    if not isinstance(raw, list):
        raise PkgDependencyError(f"无法解析 deps: 必须是字符串列表，来源 {source}")

    deps: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise PkgDependencyError(
                f"无法解析 deps: 依赖项必须是非空字符串，缺失项 {item!r}，来源 {source}"
            )
        deps.append(item.strip())
    return tuple(deps)


def _parse_urls(raw: object, source: Path) -> tuple[dict[str, str | None], ...]:
    if raw in (None, []):
        return ()
    if not isinstance(raw, list):
        raise PkgError(f"无法解析 urls: 必须是列表，来源 {source}")

    urls: list[dict[str, str | None]] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            urls.append({"path": None, "url": item.strip()})
            continue
        if (
            isinstance(item, list)
            and len(item) == 2
            and all(isinstance(part, str) and part.strip() for part in item)
        ):
            urls.append({"path": item[0].strip(), "url": item[1].strip()})
            continue
        if isinstance(item, Mapping) and isinstance(item.get("url"), str):
            path = item.get("path")
            if path is not None and not isinstance(path, str):
                raise PkgError(f"无法解析 urls: path 必须是字符串，缺失项 {item!r}，来源 {source}")
            url = item["url"].strip()
            if not url:
                raise PkgError(f"无法解析 urls: url 不能为空，缺失项 {item!r}，来源 {source}")
            urls.append({"path": path.strip() if isinstance(path, str) else None, "url": url})
            continue
        raise PkgError(f"无法解析 urls: 缺失项 {item!r}，来源 {source}")
    return tuple(urls)


def _cache_key(package: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", package).strip("._-")
    return key or "package"


def _looks_like_local_source(package: str) -> bool:
    lower = package.lower()
    if re.match(r"^(https?|github|gitlab|codeberg):", lower):
        return False
    if re.match(r"^[a-z]:[\\/]", lower):
        return True
    return (
        package.endswith(".json")
        or package.startswith((".", "~", "/", "\\"))
        or "\\" in package
        or ("/" in package and ":" not in package)
    )


def _normalise_target(target: str | None) -> str | None:
    if target is None:
        return None
    target = target.strip()
    return target or None


def _require_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PkgError(f"{name} 不能为空")
    return value.strip()

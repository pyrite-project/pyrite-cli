from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Optional

import typer

from ..utils.config import _load_config
from ..utils.ui import print_json
from .common import (
    _FORMAT_OPTION,
    _JSON_OPTION,
    _resolve_format,
    log,
)


manifest_app = typer.Typer(help="manifest.py 解析计划与 lockfile", add_completion=False)


def register(app: typer.Typer) -> None:
    app.add_typer(manifest_app, name="manifest")


def _split_tags(value: Optional[str]) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def _active_tags_for_profile(
    profile: Optional[str],
    feature: Optional[str],
    no_feature: Optional[str],
) -> set[str]:
    active: set[str] = set()
    if profile:
        cfg = _load_config()
        key = profile.upper()
        active.update(cfg.board_tags.get(key, [key]))
        active.add(key)
    active.update(_split_tags(feature))
    active.difference_update(_split_tags(no_feature))
    return active


def _build_settings(no_compile: bool) -> dict[str, object]:
    cfg = _load_config()
    return {"auto_compile": bool(cfg.auto_compile and not no_compile)}


def _print_manifest_payload(payload: Mapping[str, object], fmt: str) -> None:
    if fmt == "json":
        print_json(dict(payload))
        return

    manifest = payload.get("manifest")
    if isinstance(manifest, Mapping):
        print(f"  manifest: {manifest.get('path')}")
    if payload.get("profile"):
        print(f"  profile: {payload['profile']}")

    features = payload.get("features")
    if isinstance(features, Mapping):
        active_tags = ", ".join(str(tag) for tag in features.get("active_tags", []))
        if active_tags:
            print(f"  tags: {active_tags}")

    modules = payload.get("modules")
    if isinstance(modules, list):
        print(f"  modules: {len(modules)}")
        for module in modules:
            if isinstance(module, Mapping):
                print(f"    {module.get('local')} -> {module.get('remote')}")

    if payload.get("lockfile"):
        print(f"  lockfile: {payload['lockfile']}")


@manifest_app.command("plan")
def manifest_plan(
    manifest: str = typer.Option("manifest.py", "--manifest", "-m", help="manifest.py 路径"),
    base_dir: Optional[str] = typer.Option(None, "--base-dir", help="项目根目录；默认使用 manifest 所在目录"),
    profile: Optional[str] = typer.Option(None, "--profile", help="板卡 profile/target，例如 esp32_s3"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags，逗号分隔"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags，逗号分隔"),
    no_compile: bool = typer.Option(False, "--no-compile", help="构建摘要中记录为不自动编译 .py"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """输出 manifest 解析后的刷入计划，不写入 lockfile。"""
    from ..utils.build import ManifestLockError, build_manifest_lock

    fmt = _resolve_format(fmt, json_output)
    active_tags = _active_tags_for_profile(profile, feature, no_feature)
    try:
        lock = build_manifest_lock(
            manifest,
            active_tags,
            base_dir=base_dir,
            profile=profile,
            build_settings=_build_settings(no_compile),
        )
    except (FileNotFoundError, OSError, ValueError, ManifestLockError) as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc

    _print_manifest_payload(lock.to_dict(), fmt)


@manifest_app.command("lock")
def manifest_lock(
    manifest: str = typer.Option("manifest.py", "--manifest", "-m", help="manifest.py 路径"),
    base_dir: Optional[str] = typer.Option(None, "--base-dir", help="项目根目录；默认使用 manifest 所在目录"),
    profile: Optional[str] = typer.Option(None, "--profile", help="板卡 profile/target，例如 esp32_s3"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags，逗号分隔"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags，逗号分隔"),
    lockfile: str = typer.Option("pyrite.lock", "--lockfile", help="输出 lockfile 路径；相对路径基于项目根目录"),
    no_compile: bool = typer.Option(False, "--no-compile", help="构建摘要中记录为不自动编译 .py"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """生成 pyrite.lock JSON lockfile。"""
    from ..utils.build import ManifestLockError, build_manifest_lock, save_manifest_lock

    fmt = _resolve_format(fmt, json_output)
    manifest_path = Path(manifest)
    base = Path(base_dir or manifest_path.parent).resolve()
    path = Path(lockfile)
    if not path.is_absolute():
        path = base / path
    active_tags = _active_tags_for_profile(profile, feature, no_feature)
    try:
        lock = build_manifest_lock(
            manifest_path,
            active_tags,
            base_dir=base,
            profile=profile,
            build_settings=_build_settings(no_compile),
        )
        written = save_manifest_lock(lock, path)
    except (FileNotFoundError, OSError, ValueError, ManifestLockError) as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc

    payload = lock.to_dict()
    payload["lockfile"] = str(written)
    _print_manifest_payload(payload, fmt)

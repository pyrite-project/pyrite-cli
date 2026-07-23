from __future__ import annotations

import subprocess
from typing import Optional

import typer

from ..utils.board_alias import BoardAliasError, resolve_port_alias
from ..utils.ui import print_json
from .common import (
    _complete_port,
    _FORMAT_OPTION,
    _JSON_OPTION,
    _resolve_format,
    log,
)

# pkg — MicroPython 包安装计划与 mpremote mip 封装
# ═══════════════════════════════════════════════════════════════════

pkg_app = typer.Typer(help="MicroPython 包安装与缓存计划", add_completion=False)


def register(app: typer.Typer) -> None:
    app.add_typer(pkg_app, name="pkg")


def _print_pkg_plan(plan, fmt: str) -> None:
    if fmt == "json":
        print_json(plan.to_dict())
        return

    data = plan.to_dict()
    print(f"  action: {data['action']}")
    if data.get("package"):
        print(f"  package: {data['package']}")
    if data.get("port"):
        print(f"  port: {data['port']}")
    if data.get("target"):
        print(f"  target: {data['target']}")
    if data.get("cache_dir"):
        print(f"  cache: {data['cache_dir']}")
    command = data.get("command") or []
    if command:
        print("  command: " + " ".join(str(part) for part in command))
    for note in data.get("notes", []):
        print(f"  note: {note}")


def _run_pkg_plan_or_exit(plan) -> None:
    from ..utils.pkg import PkgError, run_pkg_plan

    try:
        result = run_pkg_plan(plan)
    except (FileNotFoundError, OSError, PkgError) as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc

    if isinstance(result, subprocess.CompletedProcess) and result.returncode != 0:
        raise typer.Exit(result.returncode)


def _resolve_pkg_port(port: str) -> str:
    try:
        return resolve_port_alias(port)
    except BoardAliasError as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc


@pkg_app.command("install")
def pkg_install(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    package: str = typer.Argument(..., help="包名、URL 或 github:/gitlab:/codeberg: spec"),
    target: Optional[str] = typer.Option(None, "--target", help="设备端安装目录，例如 /lib"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只输出安装计划，不连接设备"),
    mpremote_cmd: str = typer.Option("mpremote", "--mpremote", help="mpremote 可执行文件名或路径"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """通过 mpremote mip install 安装包。"""
    from ..utils.pkg import PkgError, build_install_plan

    fmt = _resolve_format(fmt, json_output)
    port = _resolve_pkg_port(port)
    try:
        plan = build_install_plan(
            port, package, target=target, dry_run=dry_run, mpremote=mpremote_cmd,
        )
    except (FileNotFoundError, OSError, PkgError) as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc

    if dry_run:
        _print_pkg_plan(plan, fmt)
        return
    _run_pkg_plan_or_exit(plan)


@pkg_app.command("cache")
def pkg_cache(
    package: str = typer.Argument(..., help="包名、URL 或本地 package.json/目录"),
    version: str = typer.Option("latest", "--version", help="缓存版本目录名"),
    cache_root: str = typer.Option(".pyrite/pkg-cache", "--cache-root", help="上位机缓存根目录"),
    dry_run: bool = typer.Option(True, "--dry-run", help="只输出缓存计划"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """规划上位机包缓存目录；当前不执行网络下载。"""
    from ..utils.pkg import PkgError, build_cache_plan

    fmt = _resolve_format(fmt, json_output)
    try:
        plan = build_cache_plan(
            package, version=version, cache_root=cache_root, dry_run=dry_run,
        )
    except (FileNotFoundError, OSError, PkgError) as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc
    _print_pkg_plan(plan, fmt)


@pkg_app.command("install-offline")
def pkg_install_offline(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    package_source: str = typer.Argument(..., help="本地 package.json 或包含 package.json 的目录"),
    target: Optional[str] = typer.Option(None, "--target", help="设备端安装目录，例如 /lib"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只输出安装计划，不连接设备"),
    mpremote_cmd: str = typer.Option("mpremote", "--mpremote", help="mpremote 可执行文件名或路径"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """通过 mpremote mip install 安装本地包。"""
    from ..utils.pkg import PkgError, build_install_offline_plan

    fmt = _resolve_format(fmt, json_output)
    port = _resolve_pkg_port(port)
    try:
        plan = build_install_offline_plan(
            port, package_source, target=target, dry_run=dry_run, mpremote=mpremote_cmd,
        )
    except (FileNotFoundError, OSError, PkgError) as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc

    if dry_run:
        _print_pkg_plan(plan, fmt)
        return
    _run_pkg_plan_or_exit(plan)


# ═══════════════════════════════════════════════════════════════════

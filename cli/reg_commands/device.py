from __future__ import annotations

from typing import Optional

import typer

from .common import (
    DEFAULT_BAUDRATE,
    ProjectSyncManager,
    _complete_port,
    _FORMAT_OPTION,
    _JSON_OPTION,
    _mp_factory,
    _norm_path,
    _resolve_format,
)

# device — 设备备份与恢复
# ═══════════════════════════════════════════════════════════════════

device_app = typer.Typer(help="设备文件备份与恢复", add_completion=False)


def register(app: typer.Typer) -> None:
    app.add_typer(device_app, name="device")


@device_app.command("backup")
def device_backup(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument(..., help="本地备份目录"),
    remote_path: str = typer.Argument("/", help="设备上的备份根路径"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="预览模式"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """批量导出设备文件到本地目录。"""
    fmt = _resolve_format(fmt, json_output)
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        ok = ProjectSyncManager(mp).backup(
            directory, remote_path, dry_run=dry_run, fmt=fmt,
        )
    finally:
        mp.disconnect()
    if ok is False:
        raise typer.Exit(1)


@device_app.command("restore")
def device_restore(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument(..., help="本地待恢复目录"),
    remote_path: str = typer.Argument("/", help="设备上的恢复根路径"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="预览模式"),
    no_overwrite: bool = typer.Option(False, "--no-overwrite", help="跳过设备上已存在的文件"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
) -> None:
    """批量导入本地目录文件到设备。"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        results = ProjectSyncManager(mp).restore(
            directory, remote_path,
            dry_run=dry_run, overwrite=not no_overwrite,
        )
    finally:
        mp.disconnect()
    if any(not success for _lp, _rp, success in results):
        raise typer.Exit(1)


# ═══════════════════════════════════════════════════════════════════

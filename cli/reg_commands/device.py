from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

import typer

from ..utils.pipes import (
    b64decode_text,
    cleanup_paths,
    read_jsonl,
    record_text,
    write_jsonl,
)
from .common import (
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
    baudrate: Optional[int] = typer.Option(None, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: Optional[int] = typer.Option(None, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="预览模式"),
    stdout_jsonl: bool = typer.Option(False, "--stdout-jsonl", help="将设备文件内容作为 JSONL 输出到 stdout，不写本地目录"),
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
        manager = ProjectSyncManager(mp)
        if stdout_jsonl:
            ok = manager.backup_stdout_jsonl(remote_path)
        else:
            ok = manager.backup(
                directory, remote_path, dry_run=dry_run, fmt=fmt,
            )
    finally:
        mp.disconnect()
    if ok is False:
        raise typer.Exit(1)


@device_app.command("restore")
def device_restore(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument(..., help="本地待恢复目录，或 - 配合 --stdin-jsonl 从 stdin 读取"),
    remote_path: str = typer.Argument("/", help="设备上的恢复根路径"),
    baudrate: Optional[int] = typer.Option(None, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: Optional[int] = typer.Option(None, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="预览模式"),
    no_overwrite: bool = typer.Option(False, "--no-overwrite", help="跳过设备上已存在的文件"),
    stdin_jsonl: Optional[str] = typer.Option(None, "--stdin-jsonl", help="从 JSONL 内容流恢复；使用 - 从 stdin 读取"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
) -> None:
    """批量导入本地目录文件到设备。"""
    remote_path = _norm_path(remote_path)
    if stdin_jsonl is not None or directory == "-":
        ok = _restore_stdin_jsonl(
            port,
            stdin_jsonl or "-",
            remote_path,
            baudrate=baudrate,
            timeout=timeout,
            dry_run=dry_run,
            no_overwrite=no_overwrite,
            ws=ws,
            password=password,
        )
        if not ok:
            raise typer.Exit(1)
        return

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


def _restore_stdin_jsonl(
    port: str,
    input_path: str,
    remote_prefix: str,
    *,
    baudrate: Optional[int],
    timeout: Optional[int],
    dry_run: bool,
    no_overwrite: bool,
    ws: Optional[str],
    password: Optional[str],
) -> bool:
    records: list[tuple[int, str, bytes]] = []
    failed = False
    for item in read_jsonl(input_path):
        record = item.data
        if record.get("_invalid"):
            failed = True
            write_jsonl({"ok": False, "line": item.line, "error": record.get("error", "invalid record")})
            continue
        remote = record_text(record, "remote", "path")
        if not remote or "content_b64" not in record:
            failed = True
            write_jsonl({"ok": False, "line": item.line, "error": "missing remote or content_b64"})
            continue
        try:
            records.append((item.line, _resolve_jsonl_remote(remote_prefix, remote), b64decode_text(record["content_b64"])))
        except Exception as exc:
            failed = True
            write_jsonl({"ok": False, "line": item.line, "remote": remote, "error": str(exc)})

    if dry_run:
        for line, remote, data in records:
            write_jsonl({"ok": True, "line": line, "remote": remote, "size": len(data), "dry_run": True})
        return not failed

    temp_paths: list[str] = []
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        for line, remote, data in records:
            try:
                if no_overwrite and _device_path_exists(mp, remote):
                    write_jsonl({"ok": False, "line": line, "remote": remote, "skipped": True, "error": "remote exists"})
                    failed = True
                    continue
                fd, temp_path = tempfile.mkstemp(
                    prefix="pyrite-device-restore-",
                    suffix=Path(remote).suffix or ".bin",
                )
                temp_paths.append(temp_path)
                with open(fd, "wb", closefd=True) as handle:
                    handle.write(data)
                mp.flash_file(temp_path, remote, compile=False)
                write_jsonl({"ok": True, "line": line, "remote": remote, "size": len(data)})
            except Exception as exc:
                failed = True
                write_jsonl({"ok": False, "line": line, "remote": remote, "error": str(exc)})
    finally:
        try:
            mp.disconnect()
        finally:
            cleanup_paths(temp_paths)
    return not failed


def _resolve_jsonl_remote(remote_prefix: str, remote: str) -> str:
    value = remote.replace("\\", "/")
    if value.startswith("/"):
        return _norm_path(value)
    return _norm_path(os.path.join(remote_prefix, value).replace("\\", "/"))


def _device_path_exists(mp, remote: str) -> bool:
    try:
        mp.run(f"import os;os.stat({remote!r})")
        return True
    except RuntimeError:
        return False


# ═══════════════════════════════════════════════════════════════════

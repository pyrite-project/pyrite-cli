from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List, Optional

import typer

from ..utils.config import DEFAULT_BAUDRATE
from ..utils.snapshot import (
    SNAPSHOT_DIR,
    build_current_index,
    build_diff_plan,
    build_restore_plan,
    filter_device_entries,
    format_snapshot_plan,
    load_snapshot_manifest,
    manifest_common_remote_root,
    normalize_device_path,
    safe_snapshot_name,
    save_snapshot_files,
    sha256_file,
    snapshot_path,
)
from .common import _complete_port, _mp_factory, _norm_path, log


snapshot_app = typer.Typer(
    help="设备文件系统快照、差异预览与恢复",
    add_completion=False,
)


def register(app: typer.Typer) -> None:
    app.add_typer(snapshot_app, name="snapshot")


def save_device_snapshot(
    mp,
    *,
    name: str,
    port: str,
    remote_path: str = "/",
    include: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
    output_dir: str = SNAPSHOT_DIR,
    max_file_bytes: int = 1024 * 1024,
):
    entries = filter_device_entries(
        mp.fs_ls_recursive(_norm_path(remote_path)),
        include=tuple(include or ()),
        exclude=tuple(exclude or ()),
        max_file_bytes=max_file_bytes,
    )
    files: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory(prefix="pyrite-snapshot-") as temp_dir:
        temp_root = Path(temp_dir)
        for entry in entries:
            remote = normalize_device_path(str(entry["name"]))
            local_rel = temp_root / remote.strip("/")
            mp.fs_get(remote, str(local_rel))
            files[remote] = local_rel.read_bytes()
    return save_snapshot_files(
        name,
        files,
        root=output_dir,
        device=port,
        include=tuple(include or ()),
        exclude=tuple(exclude or ()),
    )


@snapshot_app.command("save")
def snapshot_save(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    name: str = typer.Argument(..., help="快照名称"),
    remote_path: str = typer.Option("/", "--remote-path", help="设备端快照根路径"),
    include: Optional[List[str]] = typer.Option(None, "--include", help="包含的设备路径 glob，可重复"),
    exclude: Optional[List[str]] = typer.Option(None, "--exclude", help="排除的设备路径 glob，可重复"),
    output_dir: str = typer.Option(SNAPSHOT_DIR, "--output-dir", help="本地快照根目录"),
    max_file_bytes: int = typer.Option(
        1024 * 1024,
        "--max-file-bytes",
        min=1,
        help="单个文件最大保存字节数",
    ),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
) -> None:
    """保存设备文件系统快照到 .pyrite_snapshots/<name>/。"""
    try:
        safe_snapshot_name(name)
    except ValueError as exc:
        log.error("%s", exc)
        raise typer.Exit(2) from None

    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        manifest = save_device_snapshot(
            mp,
            name=name,
            port=port,
            remote_path=remote_path,
            include=include,
            exclude=exclude,
            output_dir=output_dir,
            max_file_bytes=max_file_bytes,
        )
    finally:
        mp.disconnect()
    typer.echo(f"snapshot saved: {manifest.name} ({len(manifest.files)} files)")


@snapshot_app.command("list")
def snapshot_list(
    output_dir: str = typer.Option(SNAPSHOT_DIR, "--output-dir", help="本地快照根目录"),
) -> None:
    """列出本地快照。"""
    root = Path(output_dir)
    if not root.exists():
        typer.echo("no snapshots")
        return
    found = False
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / "manifest.json").exists():
            continue
        manifest = load_snapshot_manifest(child)
        typer.echo(f"{manifest.name}\t{manifest.created_at}\t{len(manifest.files)} files")
        found = True
    if not found:
        typer.echo("no snapshots")


@snapshot_app.command("diff")
def snapshot_diff(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    name: str = typer.Argument(..., help="快照名称"),
    remote_path: str = typer.Option("/", "--remote-path", help="设备端对比根路径"),
    output_dir: str = typer.Option(SNAPSHOT_DIR, "--output-dir", help="本地快照根目录"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
) -> None:
    """对比当前设备文件系统与本地快照。"""
    manifest = load_snapshot_manifest(snapshot_path(name, root=output_dir))
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        current = _current_index_from_device(mp, _norm_path(remote_path))
    finally:
        mp.disconnect()
    typer.echo(format_snapshot_plan(build_diff_plan(manifest, current)))


@snapshot_app.command("restore")
def snapshot_restore(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    name: str = typer.Argument(..., help="快照名称"),
    output_dir: str = typer.Option(SNAPSHOT_DIR, "--output-dir", help="本地快照根目录"),
    remote_path: Optional[str] = typer.Option(
        None,
        "--remote-path",
        help="设备端恢复对比根路径；默认使用快照文件共同父目录",
    ),
    apply: bool = typer.Option(False, "--apply", help="执行恢复；默认只 dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认，与 --apply 一起使用"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
) -> None:
    """恢复快照；默认只展示 dry-run 计划。"""
    snap_dir = snapshot_path(name, root=output_dir)
    manifest = load_snapshot_manifest(snap_dir)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        scan_root = _norm_path(remote_path) if remote_path else manifest_common_remote_root(manifest)
        current = _current_index_from_device(mp, scan_root)
        plan = build_restore_plan(manifest, current, apply=apply)
        typer.echo(format_snapshot_plan(plan))
        if plan.dry_run:
            typer.echo("dry-run only; rerun with --apply --yes to restore")
            return
        if not yes and not typer.confirm("Apply restore plan?"):
            typer.echo("restore cancelled")
            return
        _apply_restore_plan(mp, snap_dir, plan)
    finally:
        mp.disconnect()
    typer.echo("restore complete")


def _current_index_from_device(mp, remote_path: str):
    entries = mp.fs_ls_recursive(remote_path)
    current = []
    with tempfile.TemporaryDirectory(prefix="pyrite-snapshot-diff-") as temp_dir:
        temp_root = Path(temp_dir)
        for entry in entries:
            if entry.get("type") != "F":
                continue
            remote = normalize_device_path(str(entry["name"]))
            local_path = temp_root / remote.strip("/")
            mp.fs_get(remote, str(local_path))
            current.append({
                "path": remote,
                "size": int(str(entry.get("size") or "0")),
                "sha256": sha256_file(local_path),
            })
    return build_current_index(current)


def _apply_restore_plan(mp, snap_dir: Path, plan) -> None:
    for item in plan.add + plan.overwrite:
        mp.flash_file(str(snap_dir / item.local_path), item.path, compile=False)
    for item in plan.delete:
        mp.fs_rm(item.path, recursive=True, force=True)

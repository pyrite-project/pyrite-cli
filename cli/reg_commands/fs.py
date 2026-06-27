from __future__ import annotations

import os
import sys
from typing import List, Optional

import click
import typer

from ..utils.ui import print_json, safe_text
from .common import (
    DEFAULT_BAUDRATE,
    MicroPython,
    _complete_port,
    _FORMAT_OPTION,
    _JSON_OPTION,
    _mp_factory,
    _norm_path,
    _resolve_format,
    _sort_fs_items,
    log,
)

# fs — 设备文件浏览器
# ═══════════════════════════════════════════════════════════════════

fs_app = typer.Typer(help="MicroPython 设备文件浏览器", add_completion=False)


def register(app: typer.Typer) -> None:
    app.add_typer(fs_app, name="fs")


def _display_paged(
    lines_with_color: List[tuple[str, bool]], page_size: int = 20,
) -> None:
    """分页显示文件列表。"""
    total = len(lines_with_color)
    start = 0
    while start < total:
        end = min(start + page_size, total)
        for i in range(start, end):
            line, is_dir = lines_with_color[i]
            if is_dir:
                typer.secho(line, fg=typer.colors.YELLOW)
            else:
                typer.secho(line, fg=typer.colors.CYAN)
        start = end
        if start < total:
            typer.secho(
                f"\n  -- 更多 ({start}/{total} 行, Enter 继续, q 退出) -- ",
                fg=typer.colors.BRIGHT_BLACK, nl=False,
            )
            ch = _read_one_key()
            print()
            if ch == "q":
                break


def _read_one_key() -> str:
    """读取单键输入，跨平台。"""
    try:
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b"q", b"Q"):
            return "q"
        return "enter"
    except ImportError:
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if ch in ("q", "Q"):
            return "q"
        return "enter"


def _build_tag_args(
    mp: MicroPython, target: Optional[str],
    feature: Optional[str], no_feature: Optional[str],
) -> set[str]:
    """构建 active_tags 公共逻辑。"""
    if target:
        active_tags: set[str] = set(
            mp.config.board_tags.get(target.upper(), [target.upper()])
        )
        active_tags.add(target.upper())
    else:
        active_tags = mp.detect_tags()
    if feature:
        active_tags.update(t.strip() for t in feature.split(","))
    if no_feature:
        active_tags.difference_update(t.strip() for t in no_feature.split(","))
    return active_tags


@fs_app.command("ls")
def fs_ls(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    path: str = typer.Argument("/", help="设备上的目录路径"),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="递归列出"),
    sort: Optional[str] = typer.Option(None, "--sort", help="排序: name/size"),
    paginate: bool = typer.Option(False, "--paginate", "-p", help="分页显示"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """连接设备并列出指定目录的内容。"""
    fmt = _resolve_format(fmt, json_output)
    path = _norm_path(path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if recursive:
            items = mp.fs_ls_recursive(path)
        else:
            items = mp.fs_ls(path)

        if items:
            _sort_fs_items(items, sort)

        if fmt == "json":
            print_json({
                "path": path,
                "entries": [
                    {
                        "name": item["name"],
                        "type": item["type"],
                        "size": int(item["size"]) if item["size"].isdigit() else None,
                    }
                    for item in items
                ],
            })
            return

        if not items:
            print("  (空目录)")
        else:
            output_lines = []
            for item in items:
                is_dir = item["type"] == "D"
                name = safe_text(item["name"], preserve_newlines=False)
                name = name + "/" if is_dir else name
                sz = item["size"] if item["size"].isdigit() else "?"
                if sz.isdigit():
                    sz_int = int(sz)
                    if sz_int < 1024:
                        num_str = f"{sz_int:>8}"
                        unit_str = "bytes"
                    else:
                        num_str = f"{sz_int / 1024:>8.2f}"
                        unit_str = "KB"
                else:
                    num_str = "       --"
                    unit_str = ""
                line = f"  {'[D]' if is_dir else '[F]'} {name:<31} {num_str} {unit_str}"
                output_lines.append((line, is_dir))
            if paginate and len(output_lines) > 20:
                _display_paged(output_lines, page_size=20)
            else:
                for line, is_dir in output_lines:
                    if is_dir:
                        typer.secho(line, fg=typer.colors.YELLOW)
                    else:
                        print(line)

        # Flash 占用进度条
        if not recursive and path.strip() in ("", ".", "./", "/"):
            df = mp.fs_df()
            if df["total"] > 0:
                pct = df["used"] / df["total"]
                bar_w = 30
                filled = int(bar_w * pct)
                bar = "█" * filled + "░" * (bar_w - filled)
                total_mb = df["total"] / 1024 / 1024
                used_mb = df["used"] / 1024 / 1024
                free_mb = df["free"] / 1024 / 1024
                typer.secho(
                    f"\n  Flash: [{bar}] {pct * 100:.1f}%",
                    fg=typer.colors.BRIGHT_BLACK,
                )
                print(
                    f"         {used_mb:.1f} MB used / {free_mb:.1f} MB free "
                    f"/ {total_mb:.1f} MB total"
                )
    finally:
        mp.disconnect()


@fs_app.command("rm")
def fs_rm(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    path: str = typer.Argument(..., help="设备上要删除的文件或目录路径"),
    recursive: bool = typer.Option(False, "-r", "--recursive", help="递归删除"),
    force: bool = typer.Option(False, "-f", "--force", help="忽略错误"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
) -> None:
    """连接设备并删除文件或递归删除目录。"""
    path = _norm_path(path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        mp.fs_rm(path, recursive=recursive, force=force)
        log.info("已删除: %s", path)
    except RuntimeError as e:
        msg = str(e)
        if msg.startswith("设备执行错误:\n"):
            msg = msg[len("设备执行错误:\n"):]
        log.error("删除失败: %s", path)
        for line in msg.strip().split("\n"):
            log.error("  %s", line)
    finally:
        mp.disconnect()


@fs_app.command("cat")
def fs_cat(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    path: str = typer.Argument(..., help="设备上的文件路径"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
) -> None:
    """连接设备并打印指定文本文件的内容。"""
    path = _norm_path(path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        print(mp.fs_cat(path))
    finally:
        mp.disconnect()


@fs_app.command("put")
def fs_put(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    local_path: str = typer.Argument(..., help="本地文件路径"),
    remote_path: str = typer.Argument(..., help="设备上的目标路径"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    no_compile: bool = typer.Option(False, "--no-compile", help="跳过 mpy 编译"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    force: bool = typer.Option(False, "--force", "-F", help="强制覆盖"),
    dry_run: bool = typer.Option(False, "--dry-run", help="预览模式"),
    safe_main: bool = typer.Option(
        True,
        "--safe-main/--no-safe-main",
        help="上传根 /main.py 前先 Ctrl+C 打断并备份原文件",
    ),
) -> None:
    """连接设备并上传本地文件。"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if safe_main and not dry_run and mp.is_safe_main_path(remote_path):
            mp.safe_break()
        if not force:
            try:
                mp.run(f"import os;os.stat({repr(remote_path)})")
                log.warning("文件 '%s' 已存在于设备，使用 --force 覆盖或先删除", remote_path)
                click.confirm("  继续覆盖?", default=False, abort=True)
            except RuntimeError:
                pass

        ver, arch = mp.get_mpy_version() if not no_compile else (None, None)
        active_tags = _build_tag_args(mp, target, feature, no_feature)
        if not active_tags and not target:
            log.error("无法识别设备 target，请使用 --target 手动指定")
            raise typer.Exit(1)
        mp.flash_file(
            local_path, remote_path, compile=not no_compile,
            bytecode_ver=ver, arch=arch,
            active_tags=active_tags or None, dry_run=dry_run,
            safe_main=safe_main,
        )
    finally:
        mp.disconnect()


@fs_app.command("get")
def fs_get(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    remote_path: str = typer.Argument(..., help="设备上的文件路径"),
    local_path: str = typer.Argument(None, help="本地保存路径"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
) -> None:
    """连接设备并下载指定文件到本地。"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        dst = local_path or os.path.basename(remote_path)
        sz = mp.fs_get(remote_path, dst)
        log.info("已下载: %s → %s (%d 字节)", remote_path, dst, sz)
    finally:
        mp.disconnect()


@fs_app.command("tree")
def fs_tree(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    path: str = typer.Argument("/", help="设备上的目录路径"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """以树形结构显示设备目录内容。"""
    fmt = _resolve_format(fmt, json_output)
    path = _norm_path(path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        tree_str = mp.fs_tree(path)
        if fmt == "json":
            print_json({"tree": tree_str})
        else:
            print(safe_text(tree_str, preserve_newlines=True))
    finally:
        mp.disconnect()


@fs_app.command("mv")
def fs_mv(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    src: str = typer.Argument(..., help="源路径"),
    dst: str = typer.Argument(..., help="目标路径"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
) -> None:
    """重命名/移动设备上的文件或目录。"""
    src = _norm_path(src)
    dst = _norm_path(dst)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if mp.fs_mv(src, dst):
            log.info("已移动: %s → %s", src, dst)
        else:
            log.warning("移动失败: %s → %s", src, dst)
    finally:
        mp.disconnect()


@fs_app.command("cp")
def fs_cp(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    src: str = typer.Argument(..., help="源路径"),
    dst: str = typer.Argument(..., help="目标路径"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
) -> None:
    """复制设备上的文件或目录。"""
    src = _norm_path(src)
    dst = _norm_path(dst)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if mp.fs_cp(src, dst):
            log.info("已复制: %s → %s", src, dst)
        else:
            log.warning("复制失败: %s → %s", src, dst)
    finally:
        mp.disconnect()


# ═══════════════════════════════════════════════════════════════════

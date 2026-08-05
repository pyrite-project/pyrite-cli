"""Shared helpers for Typer command modules."""

from __future__ import annotations

import os
import re
import sys
import tempfile
from importlib import import_module
from pathlib import Path
from typing import List, Optional

import click
import typer

from ..utils.board_alias import (
    BoardAliasError,
    BoardAliasStore,
    resolve_port_alias,
)
from ..utils.config import (
    DEFAULT_BAUDRATE as DEFAULT_BAUDRATE,
    _load_config,
    resolve_connection_settings,
)
from ..utils.log import get_logger
from ..utils.ui import is_tty

log = get_logger(__name__)


class _LazyObject:
    def __init__(self, module_name: str, attr_name: str) -> None:
        self._module_name = module_name
        self._attr_name = attr_name
        self._target = None

    def _load(self):
        if self._target is None:
            self._target = getattr(import_module(self._module_name), self._attr_name)
        return self._target

    def __call__(self, *args, **kwargs):
        return self._load()(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._load(), name)


MicroPython = _LazyObject("cli.utils.flash", "MicroPython")
WebREPLMicroPython = _LazyObject("cli.utils.webrepl", "WebREPLMicroPython")
ProjectSyncManager = _LazyObject("cli.project.sync", "ProjectSyncManager")
init_stubs = _LazyObject("cli.project.scaffold", "init_stubs")
new_project_interactive = _LazyObject("cli.project.scaffold", "new_project_interactive")


def _validate_format(value: str) -> str:
    if value not in {"text", "json"}:
        raise click.BadParameter("输出格式必须是 text 或 json")
    return value


_FORMAT_OPTION = typer.Option(
    "text", "--format", envvar="PYRITE_FORMAT",
    help="输出格式: text | json", callback=_validate_format,
)
_JSON_OPTION = typer.Option(False, "--json", help="等同于 --format json")


def _resolve_format(fmt: str, json_output: bool) -> str:
    return "json" if json_output else fmt


def _materialize_stdin_source(local_path: str, remote_path: str) -> tuple[str, Optional[str]]:
    """将 ``-`` 形式的 stdin 输入落盘为临时文件。"""
    if local_path != "-":
        return local_path, None

    suffix = Path(remote_path).suffix
    if suffix not in {".py", ".mpy", ".bin"}:
        suffix = ".bin"

    fd, temp_path = tempfile.mkstemp(prefix="pyrite-stdin-", suffix=suffix)
    os.close(fd)
    try:
        stdin_stream = getattr(sys.stdin, "buffer", sys.stdin)
        data = stdin_stream.read()
        if isinstance(data, str):
            data = data.encode("utf-8")
        Path(temp_path).write_bytes(data or b"")
    except Exception:
        Path(temp_path).unlink(missing_ok=True)
        raise
    return temp_path, temp_path


def _confirm_overwrite(remote_path: str) -> None:
    """在需要交互确认时提示覆盖；非 TTY 直接失败。"""
    if not is_tty():
        log.error("文件 '%s' 已存在于设备，使用 --force 覆盖或先删除", remote_path)
        log.error("非交互终端下请显式添加 --force")
        raise typer.Exit(1)
    click.confirm("  继续覆盖?", default=False, abort=True)


def _norm_path(p: str) -> str:
    """修复 MSYS2（Git Bash）路径转换问题。"""
    if not isinstance(p, str) or not re.match(r"^[A-Za-z]:[/\\]", p):
        return p

    msys_root = None
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        norm = entry.replace("/", os.sep)
        if norm.rstrip("\\").endswith("mingw64\\bin") or norm.rstrip("\\").endswith("usr\\bin"):
            parent = os.path.dirname(os.path.dirname(norm))
            if re.match(r"^[A-Za-z]:[/\\]", parent):
                msys_root = parent
                break

    if msys_root is None:
        if re.match(r"^[A-Za-z]:[/\\]$", p):
            log.warning("路径 '%s' 被 MSYS2 转换，已恢复为 '/'", p)
            return "/"
        return p

    p_norm = p.replace("/", "\\")
    prefix = msys_root.rstrip("\\") + "\\"
    if p_norm.startswith(prefix):
        rest = p_norm[len(prefix):].replace("\\", "/")
        recovered = "/" + rest
        if recovered != p:
            log.warning("路径 '%s' 被 MSYS2 转换，已恢复为 '%s'", p, recovered)
        return recovered

    return p


def _complete_port(ctx: click.Context, args: List[str], incomplete: str) -> List[str]:
    """Shell 补全回调：自动补全可用串口号。"""
    matches: list[str] = []
    try:
        ports = MicroPython.scan_ports(require_vid=False)
        matches.extend(p["device"] for p in ports if incomplete in p["device"])
    except Exception:
        pass
    try:
        aliases = [f"@{alias.name}" for alias in BoardAliasStore().list()]
        matches.extend(alias for alias in aliases if incomplete in alias)
    except Exception:
        pass
    return matches


def _mp_factory(
    port: str,
    baudrate: Optional[int],
    timeout: Optional[int],
    webrepl: Optional[str] = None,
    password: Optional[str] = None,
):
    """创建 MicroPython 实例，支持串口和 WebREPL。"""
    baudrate, timeout = resolve_connection_settings(
        baudrate,
        timeout,
        _load_config(),
    )
    if webrepl:
        return WebREPLMicroPython(url=webrepl, password=password, timeout=timeout)
    try:
        port = resolve_port_alias(port)
    except BoardAliasError as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc
    return MicroPython(port=port, baudrate=baudrate, timeout=timeout)


def _sort_fs_items(items: List[dict], sort: Optional[str]) -> None:
    """排序文件系统条目：目录优先，再按名称或体积排序。"""
    reverse = False
    sort_key = sort or "name"
    if sort_key.startswith("-"):
        reverse = True
        sort_key = sort_key[1:]

    if sort_key == "size":
        items.sort(key=lambda x: (
            int(x["size"]) if x["size"].isdigit() else 0,
            x["name"],
        ), reverse=reverse)
    else:
        items.sort(key=lambda x: x["name"], reverse=reverse)
    items.sort(key=lambda x: 0 if x["type"] == "D" else 1)

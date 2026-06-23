"""
pyrite-cli CLI 入口 — MicroPython 设备串口工具。

通过 Typer 提供 scan、flash、repl、run、reset、board-info、
monitor、pkg、project、fs、mount、remount 等子命令。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import http.client
from importlib import import_module
from typing import List, Optional
from urllib.parse import urlencode

import click
import typer

from . import __version__

from .utils.config import DEFAULT_BAUDRATE, create_default_config
from .utils.errors import humanize_exception
from .utils.log import configure_from_verbosity, get_logger
from .utils.output import is_tty, log as output_log, print_json

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
WebREPLMicroPython = _LazyObject("cli.utils.webrepl_micropython", "WebREPLMicroPython")
ProjectSyncManager = _LazyObject("cli.project.sync", "ProjectSyncManager")
init_stubs = _LazyObject("cli.project.project", "init_stubs")
new_project_interactive = _LazyObject("cli.project.project", "new_project_interactive")


# ═══════════════════════════════════════════════════════════════════
# 选项/校验辅助
# ═══════════════════════════════════════════════════════════════════

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
    try:
        ports = MicroPython.scan_ports(require_vid=False)
        return [p["device"] for p in ports if incomplete in p["device"]]
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════
# 应用入口
# ═══════════════════════════════════════════════════════════════════

app = typer.Typer(
    name="pyrite-cli",
    help="## PYRITE-CLI ## - MicroPython 设备刷入工具",
    add_completion=True,
)


@app.callback(invoke_without_command=True)
def _global_options(
    verbose: int = typer.Option(
        0, "--verbose", "-v", count=True,
        help="增加日志详细度 (-v=INFO, -vv=DEBUG, -vvv=TRACE)",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="静默模式，仅输出错误",
    ),
) -> None:
    """pyrite-cli: MicroPython 设备刷入工具链"""
    configure_from_verbosity(verbose, quiet)


# ── 设备信息查询 ──────────────────────────────────────────────────

_BRIEF_CODE = """\

import sys,os,machine
u=os.uname()
print(sys.implementation.name+' '+'.'.join(str(x) for x in sys.implementation.version))
print(u.machine)
print(str(machine.freq()//1000000)+' MHz')
"""


def _fetch_brief(port: str) -> str:
    mp = MicroPython(port=port)
    try:
        mp.connect()
        out = mp.run(_BRIEF_CODE)
    except Exception:
        return ""
    finally:
        mp.disconnect()
    lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
    return "  " + "  ".join(lines) if lines else ""


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"pyrite-cli {__version__}")
        raise typer.Exit()


def _mp_factory(
    port: str,
    baudrate: int,
    timeout: int,
    webrepl: Optional[str] = None,
    password: Optional[str] = None,
) -> MicroPython:
    """创建 MicroPython 实例，支持串口和 WebREPL。"""
    if webrepl:
        return WebREPLMicroPython(url=webrepl, password=password, timeout=timeout)
    return MicroPython(port=port, baudrate=baudrate, timeout=timeout)


def _serial_transport_factory(port: str, baudrate: int, timeout: int):
    from .utils.transport.serial import SerialTransport

    return SerialTransport(port=port, baudrate=baudrate, timeout=timeout)


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


# ═══════════════════════════════════════════════════════════════════
# scan — 扫描串口设备
# ═══════════════════════════════════════════════════════════════════

@app.command()
def scan(
    vid: Optional[int] = typer.Option(None, "--vid", help="按 VID 过滤（十进制）"),
    pid: Optional[int] = typer.Option(None, "--pid", help="按 PID 过滤（十进制）"),
    keyword: Optional[str] = typer.Option(None, "--keyword", "-k", help="按描述关键字过滤"),
    all: bool = typer.Option(False, "--all", "-a", help="显示所有设备（包括无 VID/PID 的）"),
    with_info: bool = typer.Option(False, "--with-info", "-i", help="连接设备并显示简略板子信息"),
    version: Optional[bool] = typer.Option(
        None, "--version", "-V",
        help="显示版本号并退出",
        callback=_version_callback,
        is_eager=True,
    ),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """扫描可用串口设备。"""
    fmt = _resolve_format(fmt, json_output)
    ports = MicroPython.scan_ports(
        vid=vid, pid=pid, keyword=keyword, require_vid=not all,
    )
    if not ports:
        if fmt == "json":
            print_json({"devices": [], "count": 0})
            return
        log.info("未检测到串口设备")
        raise typer.Exit()

    if fmt == "json":
        devices = []
        for p in ports:
            device = {
                "device": p["device"],
                "description": p["description"],
                "vid": f"{p['vid']:04X}" if p["vid"] is not None else None,
                "pid": f"{p['pid']:04X}" if p["pid"] is not None else None,
                "serial_number": p["serial_number"],
            }
            if with_info:
                device["brief"] = _fetch_brief(p["device"]).strip()
            devices.append(device)
        print_json({"devices": devices, "count": len(ports)})
        return

    # 用户可见的设备列表（文本模式）
    print(f"  发现 {len(ports)} 个串口设备:\n")
    for p in ports:
        tags = []
        if p["vid"] is not None:
            tags.append(f"VID={p['vid']:04X}")
        if p["pid"] is not None:
            tags.append(f"PID={p['pid']:04X}")
        sn = f" S/N={p['serial_number']}" if p["serial_number"] else ""
        tag_str = f" ({', '.join(tags)}{sn})" if tags else ""
        print(f"  {p['device']}{tag_str}")
        print(f"    {p['description']}")
        if with_info:
            brief = _fetch_brief(p["device"])
            if brief:
                typer.secho(brief, fg=typer.colors.BRIGHT_BLACK, err=True)
    print()


# ═══════════════════════════════════════════════════════════════════
# flash — 单文件刷入
# ═══════════════════════════════════════════════════════════════════

@app.command()
def flash(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    file: str = typer.Argument(..., help="待刷入的本地文件路径"),
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
) -> None:
    """连接设备并通过原始 REPL 刷入单个文件。"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if not force and remote_path:
            try:
                mp.run(f"import os;os.stat({remote_path!r})")
                log.warning("文件 '%s' 已存在于设备，使用 --force 覆盖或先删除", remote_path)
                click.confirm("  继续覆盖?", default=False, abort=True)
            except RuntimeError:
                pass

        ver, arch = mp.get_mpy_version() if not no_compile else (None, None)
        if target:
            active_tags = set(mp.config.board_tags.get(target.upper(), [target.upper()]))
            active_tags.add(target.upper())
        else:
            active_tags = mp.detect_tags()
            if not active_tags:
                log.error("无法识别设备 target，请使用 --target 手动指定")
                raise typer.Exit(1)
        if feature:
            active_tags.update(t.strip() for t in feature.split(","))
        if no_feature:
            active_tags.difference_update(t.strip() for t in no_feature.split(","))
        mp.flash_file(
            file, remote_path, compile=not no_compile,
            bytecode_ver=ver, arch=arch,
            active_tags=active_tags or None, dry_run=dry_run,
        )
    finally:
        mp.disconnect()


# ═══════════════════════════════════════════════════════════════════
# repl — 交互式 REPL
# ═══════════════════════════════════════════════════════════════════

@app.command()
def repl(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
) -> None:
    """连接设备并进入交互式 REPL 终端。"""
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        mp.repl_()
    finally:
        mp.disconnect()


# ═══════════════════════════════════════════════════════════════════
# flash-program — 批量刷入
# ═══════════════════════════════════════════════════════════════════

@app.command()
def flash_program(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument(..., help="本地目录路径"),
    remote_path: str = typer.Argument(..., help="设备上的远程路径前缀"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    no_compile: bool = typer.Option(False, "--no-compile", help="跳过 mpy 编译"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    dry_run: bool = typer.Option(False, "--dry-run", help="预览模式"),
) -> None:
    """连接设备并递归刷入整个本地目录。"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if no_compile:
            mp.config.auto_compile = False
        ver, arch = mp.get_mpy_version() if not no_compile else (None, None)
        if target:
            active_tags = set(mp.config.board_tags.get(target.upper(), [target.upper()]))
            active_tags.add(target.upper())
        else:
            active_tags = mp.detect_tags()
            if not active_tags:
                log.error("无法识别设备 target，请使用 --target 手动指定")
                raise typer.Exit(1)
        if feature:
            active_tags.update(t.strip() for t in feature.split(","))
        if no_feature:
            active_tags.difference_update(t.strip() for t in no_feature.split(","))
        results = mp.flash_program(
            directory, remote_path, bytecode_ver=ver, arch=arch,
            active_tags=active_tags or None,
            manifest_path=manifest, dry_run=dry_run,
        )
        ok = sum(1 for _, _, s in results if s)
        fail = sum(1 for _, _, s in results if not s)
        if ok or fail:
            parts = []
            if ok:
                parts.append(f"\033[32m{ok} 成功\033[0m")
            if fail:
                parts.append(f"\033[31m{fail} 失败\033[0m")
            log.info("完成: %s", ", ".join(parts))
    finally:
        mp.disconnect()


# ═══════════════════════════════════════════════════════════════════
# 注意: run 已移至 project run 子命令
# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════
# reset — 软重启
# ═══════════════════════════════════════════════════════════════════

@app.command()
def reset(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
) -> None:
    """连接设备并通过原始 REPL 软重启。"""
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        mp.reset()
        log.info("设备已重启")
    finally:
        mp.disconnect()


# ═══════════════════════════════════════════════════════════════════
# config — 生成默认配置
# ═══════════════════════════════════════════════════════════════════

@app.command()
def config() -> None:
    """在当前目录生成默认 .pyrite_config.json 配置文件。"""
    create_default_config()


# ═══════════════════════════════════════════════════════════════════
# board-info — 设备信息
# ═══════════════════════════════════════════════════════════════════

@app.command()
def board_info(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """连接设备并获取详细板级信息（固件、CPU、内存、Flash 等）。"""
    fmt = _resolve_format(fmt, json_output)
    code = """\
import sys,os,gc,machine,ubinascii
u=os.uname()
st=os.statvfs('/')
print('FW:'+sys.implementation.name+' '+'.'.join(str(x) for x in sys.implementation.version))
print('PLAT:'+sys.platform)
print('HW:'+u.machine)
print('REL:'+u.release)
print('CPU:'+str(machine.freq()))
print('UID:'+ubinascii.hexlify(machine.unique_id()).decode())
rc=machine.reset_cause()
_RC={getattr(machine,n):n for n in('PWRON_RESET','HARD_RESET','WDT_RESET','DEEPSLEEP_RESET','SOFT_RESET')if hasattr(machine,n)}
print('RST:'+_RC.get(rc,str(rc)))
gc.collect()
print('MF:'+str(gc.mem_free()))
print('MA:'+str(gc.mem_alloc()))
print('FS:'+str(st[0]*st[2])+'/'+str(st[0]*st[3]))
try:
 import esp
 print('FLASH:'+str(esp.flash_size()))
except:pass
try:
 import network
 w=network.WLAN(network.STA_IF)
 w.active(True)
 print('MAC:'+':'.join('%02x'%b for b in w.config('mac')))
except:pass
"""
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        output = mp.run(code)
    finally:
        mp.disconnect()

    if not output:
        if fmt == "json":
            print_json({"error": "no_device_info"})
            raise typer.Exit(1)
        log.error("未获取到设备信息")
        raise typer.Exit(1)

    info: dict[str, str] = {}
    for line in output.strip().splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            info[k] = v

    if fmt == "json":
        fs_used = fs_total = None
        if "FS" in info:
            _total, _free = info["FS"].split("/")
            fs_total = int(_total)
            fs_used = fs_total - int(_free)
        print_json({
            "firmware": {
                "name": info.get("FW"),
                "platform": info.get("PLAT"),
                "machine": info.get("HW"),
                "release": info.get("REL"),
            },
            "device": {
                "cpu_hz": int(info["CPU"]) if "CPU" in info else None,
                "uid": info.get("UID"),
                "reset_cause": info.get("RST"),
                "mac": info.get("MAC"),
            },
            "memory": {
                "ram_used": int(info["MA"]) if "MA" in info else None,
                "ram_total": (
                    int(info["MF"]) + int(info["MA"])
                    if "MF" in info and "MA" in info else None
                ),
                "fs_used": fs_used,
                "fs_total": fs_total,
                "flash_size": int(info["FLASH"]) if "FLASH" in info else None,
            },
        })
        return

    # ── 用户可见的格式化输出（命令产物） ──
    def row(label: str, value: str) -> None:
        pad = 10 - sum(2 if ord(c) > 127 else 1 for c in label)
        typer.secho(f"  {label}{' ' * pad}", fg=typer.colors.BRIGHT_BLACK, nl=False)
        typer.echo(value)

    def section(title: str) -> None:
        typer.echo()
        typer.secho(f"── {title} ", fg=typer.colors.BRIGHT_CYAN, bold=True)

    section("固件")
    row("名称", info.get("FW", "?"))
    row("平台", info.get("PLAT", "?"))
    row("硬件", info.get("HW", "?"))
    row("版本", info.get("REL", "?"))

    section("设备")
    if "CPU" in info:
        row("CPU", f"{int(info['CPU']) // 1_000_000} MHz")
    row("唯一ID", info.get("UID", "?"))
    row("复位原因", info.get("RST", "?"))
    if "MAC" in info:
        row("MAC", info["MAC"])

    section("内存")
    if "MF" in info and "MA" in info:
        mf, ma = int(info["MF"]), int(info["MA"])
        row("RAM", f"{ma // 1024} KB used / {(mf + ma) // 1024} KB total")
    if "FS" in info:
        total, free = info["FS"].split("/")
        row("Flash FS", f"{(int(total) - int(free)) // 1024} KB used / {int(total) // 1024} KB total")
    if "FLASH" in info:
        row("Flash", f"{int(info['FLASH']) // 1024} KB")
    typer.echo()


# ═══════════════════════════════════════════════════════════════════
# monitor — GPIO 只读监控
# ═══════════════════════════════════════════════════════════════════

@app.command("monitor")
def monitor(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    uart: bool = typer.Option(False, "--uart", help="Read raw UART bytes instead of GPIO state"),
    pins: Optional[str] = typer.Option(
        None, "--pins",
        help="逗号分隔的 GPIO 引脚列表，例如 0,2,4,5；不指定时保守探测",
    ),
    interval: Optional[float] = typer.Option(
        None,
        "--interval",
        "-i",
        help="采样间隔秒数；GPIO 默认 0.5，UART 默认 0.05",
    ),
    duration: Optional[float] = typer.Option(None, "--duration", help="监控持续秒数"),
    count: Optional[int] = typer.Option(None, "--count", help="采样次数"),
    edge: Optional[str] = typer.Option(None, "--edge", help="仅支持 changed，状态变化时输出"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """只读监控 MicroPython GPIO 输入状态。"""
    from .utils.monitor import (
        MonitorError,
        format_monitor_header,
        format_uart_monitor_header,
        format_uart_monitor_rows,
        parse_uart_ports,
        run_monitor_session,
        run_uart_monitor_session,
    )

    fmt = _resolve_format(fmt, json_output)
    text_refresh = fmt == "text" and is_tty()
    monitor_interval = interval if interval is not None else 0.5

    if uart:
        uart_interval = interval if interval is not None else 0.05
        transports = []
        printed_inline = False
        printed_uart_block = False
        uart_ports: list[str] = []

        def emit_uart_header(options) -> None:
            nonlocal printed_uart_block
            header = format_uart_monitor_header(options)
            if header:
                print(header, flush=True)
            if text_refresh:
                initial_rows = format_uart_monitor_rows(
                    [(uart_port, None) for uart_port in options.ports],
                    fmt=options.fmt,
                    encoding=options.encoding,
                )
                if initial_rows:
                    print(initial_rows, flush=True)
                    printed_uart_block = True

        def emit_uart(block: str) -> None:
            nonlocal printed_uart_block
            if text_refresh:
                if printed_uart_block:
                    print(f"\033[{len(uart_ports)}F", end="")
                for line in block.splitlines():
                    print("\033[K" + line)
                printed_uart_block = True
                sys.stdout.flush()
            else:
                print(block, flush=True)

        try:
            if pins is not None:
                raise MonitorError("--pins cannot be used with --uart")
            if edge is not None:
                raise MonitorError("--edge cannot be used with --uart")
            if ws or password:
                raise MonitorError("--uart does not support WebREPL options")

            uart_ports = parse_uart_ports(port)
            for uart_port in uart_ports:
                transport = _serial_transport_factory(uart_port, baudrate, timeout)
                transport.connect()
                transports.append((uart_port, transport))

            emitted = run_uart_monitor_session(
                transports,
                fmt=fmt,
                interval=uart_interval,
                duration=duration,
                count=count,
                on_start=emit_uart_header if fmt == "text" else None,
                refresh=text_refresh,
                write=emit_uart,
            )
            printed_inline = text_refresh and (emitted > 0 or printed_uart_block)
        except KeyboardInterrupt:
            printed_inline = text_refresh
            log.info("用户中断")
        except MonitorError as exc:
            log.error("%s", exc)
            raise typer.Exit(1) from exc
        finally:
            if printed_inline:
                print()
            for _uart_port, transport in reversed(transports):
                transport.disconnect()
        return

    def emit_header(options) -> None:
        header = format_monitor_header(options, port=port)
        if header:
            print(header, flush=True)

    def emit(line: str) -> None:
        if text_refresh:
            print("\r\033[K" + line, end="", flush=True)
        else:
            print(line, flush=True)

    mp = _mp_factory(port, baudrate, timeout, ws, password)
    printed_inline = False
    try:
        mp.connect()
        emitted = run_monitor_session(
            mp,
            pins=pins,
            fmt=fmt,
            interval=monitor_interval,
            duration=duration,
            count=count,
            edge=edge,
            on_sample_error=lambda exc, output: log.debug(
                "monitor sample skipped: %s; output=%r",
                exc,
                output,
            ),
            on_start=emit_header if fmt == "text" else None,
            sample_style="modern" if fmt == "text" else "compact",
            write=emit,
            stream=True,
        )
        printed_inline = text_refresh and emitted > 0
    except KeyboardInterrupt:
        printed_inline = text_refresh
        log.info("用户中断")
    except MonitorError as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc
    finally:
        if printed_inline:
            print()
        mp.disconnect()


# ═══════════════════════════════════════════════════════════════════
# mount — PC 侧 WebDAV 挂载
# ═══════════════════════════════════════════════════════════════════

@app.command()
def mount(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    root: str = typer.Option(
        "/", "--root", "-r",
        help="暴露给 WebDAV 的设备端根目录",
    ),
    host: str = typer.Option(
        "127.0.0.1", "--host",
        help="WebDAV 监听地址",
    ),
    port_http: int = typer.Option(
        8765, "--http-port", "-p",
        help="WebDAV 监听端口",
    ),
    drive: Optional[str] = typer.Option(
        None, "--drive", "-d",
        help="Windows 驱动器盘符，如 P 或 P:（Linux/macOS 忽略）",
    ),
    readonly: bool = typer.Option(
        False, "--readonly",
        help="以只读模式提供 WebDAV 服务",
    ),
    no_map: bool = typer.Option(
        False, "--no-map",
        help="只启动 WebDAV 服务，不自动连接默认文件管理器",
    ),
    load_all: bool = typer.Option(
        False, "--load-all",
        help="挂载前先递归读取并缓存完整目录结构",
    ),
    baudrate: int = typer.Option(
        DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE",
    ),
    timeout: int = typer.Option(
        10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT",
    ),
    run_timeout: int = typer.Option(
        300, "--run-timeout", help="mount-run 执行脚本的超时秒数",
    ),
    run_queue_max: int = typer.Option(
        64, "--run-queue-max", help="脚本运行期间最多排队的 WebDAV 写请求数",
    ),
    run_queue_max_bytes: int = typer.Option(
        64 * 1024 * 1024,
        "--run-queue-max-bytes",
        help="脚本运行期间排队 PUT 临时文件总字节上限",
    ),
    startup_empty_list_grace: float = typer.Option(
        5.0,
        "--startup-empty-list-grace",
        help="mount 启动期根目录为空时返回重试而不是发布空目录的秒数",
    ),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
) -> None:
    """通过 PC 侧 WebDAV 桥把设备文件系统挂到默认文件管理器。

    设备端不需要固件级 MTP 支持。本命令在 PC 上启动 WebDAV 服务，
    将文件管理器请求转换为现有 UART/Raw REPL 文件操作。Windows 下
    默认会用 net use 映射驱动器盘符；Linux/macOS 下会尝试打开系统
    默认文件管理器的 WebDAV 位置。按 Ctrl+C 停止并清理挂载。
    """
    from .utils.webdav_mount import WebDavConfig, serve_webdav

    config = WebDavConfig(
        host=host,
        port=port_http,
        root=_norm_path(root),
        readonly=readonly,
        drive=drive,
        map_drive=not no_map,
        load_all=load_all,
        run_timeout=run_timeout,
        run_queue_max_operations=run_queue_max,
        run_queue_max_bytes=run_queue_max_bytes,
        startup_empty_list_grace=startup_empty_list_grace,
    )

    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        log.info("已连接到 %s", port)
        serve_webdav(mp, config)

    except KeyboardInterrupt:
        log.info("用户中断")
    except (click.exceptions.Exit, typer.Exit):
        raise
    except Exception as e:
        log.error("挂载服务异常: %s", e)
        raise
    finally:
        try:
            mp.disconnect()
        except Exception:
            pass
        log.info("已断开连接")


@app.command("mount-run")
def mount_run(
    path: str = typer.Option(
        "/main.py", "--path",
        help="让当前 mount 会话在设备端 execfile() 的路径",
    ),
    host: str = typer.Option(
        "127.0.0.1", "--host",
        help="mount WebDAV 监听地址",
    ),
    port_http: int = typer.Option(
        8765, "--http-port", "-p",
        help="mount WebDAV 监听端口",
    ),
    timeout: int = typer.Option(
        300, "--timeout", "-t",
        help="等待脚本执行完成的秒数",
    ),
) -> None:
    """请求正在运行的 pyrcli mount 会话执行设备端脚本。"""
    from .utils.webdav_mount import mount_run_executable_for_system

    run_executable = mount_run_executable_for_system()
    if run_executable is None:
        log.error("mount-run 仅支持 Windows/macOS/Linux")
        raise typer.Exit(1)
    target = "/" + run_executable.name + "?" + urlencode({"path": _norm_path(path)})
    conn = http.client.HTTPConnection(host, port_http, timeout=timeout)
    try:
        conn.request("GET", target)
        resp = conn.getresponse()
        body = resp.read()
    except OSError as exc:
        log.error("无法连接 mount 会话: %s", exc)
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    text = body.decode("utf-8", errors="replace")
    if resp.status >= 400:
        log.error("mount-run 失败: HTTP %d %s", resp.status, text.strip())
        raise typer.Exit(1)


# ═══════════════════════════════════════════════════════════════════
# remount — 设备侧反向挂载主机目录
# ═══════════════════════════════════════════════════════════════════

def _resolve_mpremote_command(mpremote: str) -> str:
    resolved = shutil.which(mpremote)
    if resolved:
        return resolved
    raise FileNotFoundError("未找到 mpremote。请安装：pip install mpremote")


def _build_remount_command(
    port: str,
    local_dir: str,
    mpremote: str = "mpremote",
    unsafe_links: bool = False,
) -> List[str]:
    cmd = [
        _resolve_mpremote_command(mpremote),
        "connect",
        port,
        "mount",
    ]
    if unsafe_links:
        cmd.append("--unsafe-links")
    cmd.append(local_dir)
    return cmd


@app.command("remount")
def remount(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    local_dir: str = typer.Argument(".", help="暴露给设备端 /remote 的上位机目录"),
    unsafe_links: bool = typer.Option(
        False,
        "--unsafe-links",
        "-l",
        help="允许设备端通过符号链接访问挂载目录外的路径",
    ),
    mpremote_cmd: str = typer.Option(
        "mpremote",
        "--mpremote",
        help="mpremote 可执行文件名或路径",
    ),
) -> None:
    """用 mpremote 把上位机目录反向挂载到设备端 /remote。"""
    local_dir = os.path.abspath(local_dir)
    if not os.path.isdir(local_dir):
        log.error("本地目录不存在: %s", local_dir)
        raise typer.Exit(1)

    try:
        cmd = _build_remount_command(
            port,
            local_dir,
            mpremote=mpremote_cmd,
            unsafe_links=unsafe_links,
        )
    except FileNotFoundError as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc

    log.info("反向挂载 %s 到设备 /remote，按 Ctrl+] 或 Ctrl+x 退出 mpremote REPL", local_dir)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise typer.Exit(result.returncode)


# ═══════════════════════════════════════════════════════════════════
# pkg — MicroPython 包安装计划与 mpremote mip 封装
# ═══════════════════════════════════════════════════════════════════

pkg_app = typer.Typer(help="MicroPython 包安装与缓存计划", add_completion=False)
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
    from .utils.pkg import PkgError, run_pkg_plan

    try:
        result = run_pkg_plan(plan)
    except (FileNotFoundError, OSError, PkgError) as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc

    if isinstance(result, subprocess.CompletedProcess) and result.returncode != 0:
        raise typer.Exit(result.returncode)


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
    from .utils.pkg import PkgError, build_install_plan

    fmt = _resolve_format(fmt, json_output)
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
    from .utils.pkg import PkgError, build_cache_plan

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
    from .utils.pkg import PkgError, build_install_offline_plan

    fmt = _resolve_format(fmt, json_output)
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
# project 子命令组
# ═══════════════════════════════════════════════════════════════════

project_app = typer.Typer(help="项目脚手架、存根、文件哈希与增量刷入", add_completion=False)
app.add_typer(project_app, name="project")


@project_app.command("new")
def project_new(
    project_name: str = typer.Argument(..., help="新项目名称"),
    platform: Optional[str] = typer.Option(
        None, "--platform",
        help="串口号，用于自动检测硬件并下载匹配的 stubs",
    ),
) -> None:
    """创建新 MicroPython 项目目录及脚手架。"""
    new_project_interactive(project_name, platform=platform)


@project_app.command("init")
def project_init(
    hardware: Optional[str] = typer.Argument(None, help="MicroPython 硬件名称"),
    version: Optional[str] = typer.Argument(None, help="固件版本，如 '1.20.0'"),
    variant: Optional[str] = typer.Option(None, "--variant", "-V", help="硬件变体"),
    platform: Optional[str] = typer.Option(
        None, "--platform",
        help="串口号，用于自动检测硬件并下载匹配的 stubs",
    ),
) -> None:
    """在已有项目中下载 MicroPython 类型存根。"""
    init_stubs(hardware, version, variant, platform)


@project_app.command("hash")
def project_hash(
    directory: str = typer.Argument(".", help="项目目录路径"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="哈希配置文件输出路径"),
) -> None:
    """离线扫描项目目录，计算 SHA256 哈希并保存到哈希配置文件。"""
    mp = MicroPython()
    active_tags: set[str] = set()
    if feature:
        active_tags.update(t.strip() for t in feature.split(","))
    if no_feature:
        active_tags.difference_update(t.strip() for t in no_feature.split(","))
    ProjectSyncManager(mp).scan(
        directory, hash_config_path=output,
        active_tags=active_tags or None, manifest_path=manifest,
    )


@project_app.command("scan")
def project_scan(
    directory: str = typer.Argument(".", help="项目目录路径"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="哈希配置文件输出路径"),
) -> None:
    """扫描项目目录，计算 SHA256 哈希并保存到哈希配置文件。"""
    mp = MicroPython()
    active_tags = set()
    if feature:
        active_tags.update(t.strip() for t in feature.split(","))
    if no_feature:
        active_tags.difference_update(t.strip() for t in no_feature.split(","))
    ProjectSyncManager(mp).scan(
        directory, hash_config_path=output,
        active_tags=active_tags or None, manifest_path=manifest,
    )


@project_app.command("flash")
def project_flash(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument("./", help="本地项目目录路径"),
    remote_path: str = typer.Argument("./", help="设备上的远程路径前缀"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    no_compile: bool = typer.Option(False, "--no-compile", help="跳过 mpy 编译"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    hash_config: Optional[str] = typer.Option(None, "--config", "-c", help="哈希配置文件路径"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    dry_run: bool = typer.Option(False, "--dry-run", help="预览模式"),
) -> None:
    """连接设备并根据哈希配置增量刷入新增或变更的文件。"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if no_compile:
            mp.config.auto_compile = False
        ver, arch = mp.get_mpy_version() if not no_compile else (None, None)
        if target:
            active_tags = set(mp.config.board_tags.get(target.upper(), [target.upper()]))
            active_tags.add(target.upper())
        else:
            active_tags = mp.detect_tags()
            if not active_tags:
                log.error("无法识别设备 target，请使用 --target 手动指定")
                raise typer.Exit(1)
        if feature:
            active_tags.update(t.strip() for t in feature.split(","))
        if no_feature:
            active_tags.difference_update(t.strip() for t in no_feature.split(","))
        ProjectSyncManager(mp).flash(
            directory, remote_path, hash_config_path=hash_config,
            bytecode_ver=ver, arch=arch,
            active_tags=active_tags or None,
            manifest_path=manifest, dry_run=dry_run,
        )
    finally:
        mp.disconnect()


@project_app.command("status")
def project_status(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument(..., help="本地项目目录路径"),
    remote_path: str = typer.Argument(..., help="设备上的远程路径前缀"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    hash_config: Optional[str] = typer.Option(None, "--config", "-c", help="哈希配置文件路径"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    diff: bool = typer.Option(False, "--diff", help="download device files and print unified diff"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """连接设备并比对本地哈希与设备文件，显示差异清单（不刷入）。"""
    fmt = _resolve_format(fmt, json_output)
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if target:
            active_tags = set(mp.config.board_tags.get(target.upper(), [target.upper()]))
            active_tags.add(target.upper())
        elif (feature or no_feature):
            active_tags = mp.detect_tags() if not target else set()
        else:
            active_tags = mp.detect_tags()
        if feature:
            active_tags.update(t.strip() for t in feature.split(","))
        if no_feature:
            active_tags.difference_update(t.strip() for t in no_feature.split(","))
        has_diff = ProjectSyncManager(mp).status(
            directory, remote_path, hash_config_path=hash_config,
            active_tags=active_tags or None,
            manifest_path=manifest, fmt=fmt, diff=diff,
        )
    finally:
        mp.disconnect()
    if has_diff:
        raise typer.Exit(1)


@project_app.command("pull")
def project_pull(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument(help="本地项目目录路径（如 . 或 ./bak）"),
    remote_path: str = typer.Argument("/", help="设备上的远程路径前缀", show_default=False),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="预览模式"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """连接设备并按项目配置拉取文件到本地目录。"""
    fmt = _resolve_format(fmt, json_output)
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if target:
            active_tags: Optional[set[str]] = set(
                mp.config.board_tags.get(target.upper(), [target.upper()])
            )
            active_tags.add(target.upper())
        elif feature or no_feature:
            active_tags = set()
        else:
            active_tags = None
        if feature:
            if active_tags is None:
                active_tags = set()
            active_tags.update(t.strip() for t in feature.split(","))
        if no_feature:
            if active_tags is None:
                active_tags = set()
            active_tags.difference_update(t.strip() for t in no_feature.split(","))
        ok = ProjectSyncManager(mp).pull(
            directory, remote_path,
            active_tags=active_tags, manifest_path=manifest,
            dry_run=dry_run, fmt=fmt,
        )
    finally:
        mp.disconnect()
    if ok is False:
        raise typer.Exit(1)


@project_app.command("run")
def project_run(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument("./", help="本地项目目录路径"),
    remote_path: str = typer.Argument("./", help="设备上的远程路径前缀"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    no_compile: bool = typer.Option(False, "--no-compile", help="跳过 mpy 编译"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    hash_config: Optional[str] = typer.Option(None, "--config", "-c", help="哈希配置文件路径"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    dry_run: bool = typer.Option(False, "--dry-run", help="预览模式（仅显示差异，不刷入不进入 REPL）"),
) -> None:
    """增量刷入项目文件后进入交互式 REPL 监控。"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if no_compile:
            mp.config.auto_compile = False
        ver, arch = mp.get_mpy_version() if not no_compile else (None, None)
        if target:
            active_tags = set(mp.config.board_tags.get(target.upper(), [target.upper()]))
            active_tags.add(target.upper())
        else:
            active_tags = mp.detect_tags()
            if not active_tags:
                log.error("无法识别设备 target，请使用 --target 手动指定")
                raise typer.Exit(1)
        if feature:
            active_tags.update(t.strip() for t in feature.split(","))
        if no_feature:
            active_tags.difference_update(t.strip() for t in no_feature.split(","))

        ProjectSyncManager(mp).flash(
            directory, remote_path, hash_config_path=hash_config,
            bytecode_ver=ver, arch=arch,
            active_tags=active_tags or None,
            manifest_path=manifest, dry_run=dry_run,
        )

        if not dry_run:
            log.info("刷入完成，进入 REPL 监控...")
            mp.repl_()
    finally:
        mp.disconnect()


# ═══════════════════════════════════════════════════════════════════
# device — 设备备份与恢复
# ═══════════════════════════════════════════════════════════════════

device_app = typer.Typer(help="设备文件备份与恢复", add_completion=False)
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
# fs — 设备文件浏览器
# ═══════════════════════════════════════════════════════════════════

fs_app = typer.Typer(help="MicroPython 设备文件浏览器", add_completion=False)
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
                name = item["name"] + "/" if is_dir else item["name"]
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
) -> None:
    """连接设备并上传本地文件。"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
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
            print(tree_str)
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
# main
# ═══════════════════════════════════════════════════════════════════


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        app()
    except BrokenPipeError:
        sys.exit(0)
    except Exception as exc:
        log.error("%s", humanize_exception(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()

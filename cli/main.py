"""
pyrite-cli CLI 入口 — MicroPython 设备串口工具。

通过 Typer 提供 scan、flash、repl、reset、debug、monitor、pkg、project、fs、mount、remount 等子命令。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import http.client
from importlib import import_module
from typing import List, Optional
from urllib.parse import urlencode

import click
import typer

from . import __version__
from .reg_commands import register_command_groups

from .utils.board_profile import (
    BoardProfileError,
    BoardProfileStore,
    resolve_port_alias,
)
from .utils.config import DEFAULT_BAUDRATE, create_default_config, _load_config
from .utils.errors import humanize_exception
from .utils.log import configure_from_verbosity, get_logger
from .utils.ui import is_tty, print_json

log = get_logger(__name__)


def _device_precheck_mp_version(mp, explicit_version: Optional[str]) -> Optional[str]:
    if explicit_version is not None:
        return explicit_version
    version = getattr(getattr(mp, "runtime_info", None), "version", None)
    return version if isinstance(version, str) and version.strip() else None


def _resolve_flash_tags(
    mp,
    target: Optional[str],
    feature: Optional[str],
    no_feature: Optional[str],
) -> set[str]:
    if target:
        active_tags = set(mp.config.board_tags.get(target.upper(), [target.upper()]))
        active_tags.add(target.upper())
    else:
        active_tags = set(mp.detect_tags())
        if not active_tags:
            log.error("无法识别设备 target，请使用 --target 手动指定")
            raise typer.Exit(1)
    if feature:
        active_tags.update(t.strip() for t in feature.split(",") if t.strip())
    if no_feature:
        active_tags.difference_update(t.strip() for t in no_feature.split(",") if t.strip())
    return active_tags


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


def _run_precheck_or_exit(
    entries,
    check: Optional[str],
    no_check: bool,
    active_tags: Optional[set[str]] = None,
    mp_version: Optional[str] = None,
) -> None:
    if no_check:
        return
    from .utils.precheck import PrecheckError, run_precheck, validate_precheck_mode

    cfg = _load_config()
    try:
        mode = validate_precheck_mode(check if check is not None else cfg.precheck)
        report = run_precheck(
            entries,
            mode=mode,
            compat=cfg.precheck_compat,
            active_tags=active_tags,
            mp_version=mp_version if mp_version is not None else cfg.precheck_mp_version,
        )
    except ValueError as exc:
        log.error("%s", exc, exc_info=False)
        raise typer.Exit(2) from None
    except PrecheckError as exc:
        log.error("precheck failed:\n%s", exc, exc_info=False)
        raise typer.Exit(1) from None
    for item in report.warnings:
        log.warning("%s", item.format())


def _consume_check_option(args: list[str]) -> Optional[str]:
    check: Optional[str] = None
    leftovers: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--check":
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                check = args[i + 1]
                i += 2
            else:
                check = "basic"
                i += 1
            continue
        if arg.startswith("--check="):
            check = arg.split("=", 1)[1] or "basic"
            i += 1
            continue
        leftovers.append(arg)
        i += 1
    if leftovers:
        log.error("unknown option(s): %s", " ".join(leftovers))
        raise typer.Exit(2)
    return check


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
        aliases = [f"@{profile.name}" for profile in BoardProfileStore().list()]
        matches.extend(alias for alias in aliases if incomplete in alias)
    except Exception:
        pass
    return matches


# ═══════════════════════════════════════════════════════════════════
# 应用入口
# ═══════════════════════════════════════════════════════════════════

app = typer.Typer(
    name="pyrite-cli",
    help="## PYRITE-CLI ## - MicroPython 设备刷入工具",
    add_completion=True,
    pretty_exceptions_enable=False,
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
    lines = [line.strip() for line in out.strip().splitlines() if line.strip()]
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
    try:
        port = resolve_port_alias(port)
    except BoardProfileError as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc
    return MicroPython(port=port, baudrate=baudrate, timeout=timeout)


def _serial_transport_factory(port: str, baudrate: int, timeout: int):
    from .utils.transport.serial import SerialTransport

    return SerialTransport(port=port, baudrate=baudrate, timeout=timeout)


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

@app.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def flash(
    ctx: typer.Context,
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
    mp_version: Optional[str] = typer.Option(None, "--mp-version", help="目标 MicroPython 固件版本，用于 strict 兼容性预检查"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    force: bool = typer.Option(False, "--force", "-F", help="强制覆盖"),
    dry_run: bool = typer.Option(False, "--dry-run", help="预览模式"),
    safe_main: bool = typer.Option(
        True,
        "--safe-main/--no-safe-main",
        help="刷入根 /main.py 前先 Ctrl+C 打断并备份原文件",
    ),
    trace: bool = typer.Option(False, "--trace", help="记录本次 flash 的 Flight Recorder trace"),
    trace_path: Optional[str] = typer.Option(None, "--trace-path", help="指定 trace 输出文件路径"),
    no_check: bool = typer.Option(False, "--no-check", help="跳过刷入前预检查"),
) -> None:
    """连接设备并通过原始 REPL 刷入单个文件。

    预检查可用 --check、--check=basic|strict 或 --no-check 控制。
    """
    remote_path = _norm_path(remote_path)
    check = _consume_check_option(list(ctx.args))
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    recorder = None
    trace_status = "ok"
    try:
        if trace:
            from .utils.trace import TraceRecorder, default_trace_path, make_session_id

            session_id = make_session_id()
            output_path = trace_path or default_trace_path(
                operation="flash",
                session_id=session_id,
            )
            recorder = TraceRecorder(
                output_path,
                operation="flash",
                port=ws or port,
                session_id=session_id,
                metadata={
                    "baudrate": baudrate,
                    "timeout": timeout,
                    "webrepl": ws,
                    "password": password,
                    "local_file": file,
                    "remote_path": remote_path,
                    "no_compile": no_compile,
                    "target": target,
                    "feature": feature,
                    "no_feature": no_feature,
                    "dry_run": dry_run,
                    "safe_main": safe_main,
                },
            )
            mp.set_trace_recorder(recorder)
            log.info("trace 文件: %s", recorder.path)

        mp.connect()
        with mp._trace_phase_ctx("raw_repl"):
            mp._enter_raw_repl()
        active_tags = _resolve_flash_tags(mp, target, feature, no_feature)
        _run_precheck_or_exit(
            [(file, remote_path)],
            check,
            no_check,
            active_tags=active_tags,
            mp_version=_device_precheck_mp_version(mp, mp_version),
        )
        if safe_main and not dry_run and mp.is_safe_main_path(remote_path):
            mp.safe_break()
        if not force and remote_path:
            try:
                mp.run(f"import os;os.stat({remote_path!r})")
                log.warning("文件 '%s' 已存在于设备，使用 --force 覆盖或先删除", remote_path)
                click.confirm("  继续覆盖?", default=False, abort=True)
            except RuntimeError:
                pass

        ver, arch = mp.get_mpy_version() if not no_compile else (None, None)
        mp.flash_file(
            file, remote_path, compile=not no_compile,
            bytecode_ver=ver, arch=arch,
            active_tags=active_tags or None, dry_run=dry_run,
            safe_main=safe_main,
        )
    except Exception as exc:
        trace_status = "error"
        if recorder is not None:
            recorder.failure(exc, phase="flash")
        raise
    finally:
        try:
            mp.disconnect()
        finally:
            if recorder is not None:
                recorder.close(status=trace_status)


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
    map_traceback: bool = typer.Option(
        False,
        "--map-traceback",
        help="将 MicroPython traceback 的设备路径映射到本地源码",
    ),
) -> None:
    """连接设备并进入交互式 REPL 终端。"""
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    output_mapper = None
    if map_traceback:
        from .utils.traceback_map import create_traceback_output_mapper

        output_mapper = create_traceback_output_mapper(
            local_dir=".",
            remote_prefix="/",
            auto_manifest=True,
        )
    try:
        mp.connect()
        mp.repl_(output_mapper=output_mapper)
    finally:
        mp.disconnect()


# ═══════════════════════════════════════════════════════════════════
# flash-program — 批量刷入
# ═══════════════════════════════════════════════════════════════════

@app.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def flash_program(
    ctx: typer.Context,
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument(..., help="本地目录路径"),
    remote_path: str = typer.Argument(..., help="设备上的远程路径前缀"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    no_compile: bool = typer.Option(False, "--no-compile", help="跳过 mpy 编译"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    mp_version: Optional[str] = typer.Option(None, "--mp-version", help="目标 MicroPython 固件版本，用于 strict 兼容性预检查"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    dry_run: bool = typer.Option(False, "--dry-run", help="预览模式"),
    safe_main: bool = typer.Option(
        True,
        "--safe-main/--no-safe-main",
        help="批量刷入根 /main.py 前先 Ctrl+C 打断并备份原文件",
    ),
    no_check: bool = typer.Option(False, "--no-check", help="跳过刷入前预检查"),
) -> None:
    """连接设备并递归刷入整个本地目录。

    预检查可用 --check、--check=basic|strict 或 --no-check 控制。
    """
    remote_path = _norm_path(remote_path)
    check = _consume_check_option(list(ctx.args))
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        mp._enter_raw_repl()
        active_tags = _resolve_flash_tags(mp, target, feature, no_feature)
        if not no_check:
            from .utils.precheck import collect_directory_entries

            _run_precheck_or_exit(
                collect_directory_entries(
                    directory,
                    remote_path,
                    active_tags=active_tags,
                    manifest_path=manifest,
                ),
                check,
                no_check,
                active_tags=active_tags,
                mp_version=_device_precheck_mp_version(mp, mp_version),
            )
        if no_compile:
            mp.config.auto_compile = False
        ver, arch = mp.get_mpy_version() if not no_compile else (None, None)
        results = mp.flash_program(
            directory, remote_path, bytecode_ver=ver, arch=arch,
            active_tags=active_tags or None,
            manifest_path=manifest, dry_run=dry_run,
            safe_main=safe_main,
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
    from .utils.diagnostics import (
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

            try:
                port = resolve_port_alias(port)
            except BoardProfileError as exc:
                raise MonitorError(str(exc)) from exc
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
    max_upload_bytes: int = typer.Option(
        64 * 1024 * 1024,
        "--max-upload-bytes",
        help="单个 WebDAV PUT 请求的 Content-Length 字节上限",
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
        max_upload_bytes=max_upload_bytes,
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
        port = resolve_port_alias(port)
        cmd = _build_remount_command(
            port,
            local_dir,
            mpremote=mpremote_cmd,
            unsafe_links=unsafe_links,
        )
    except BoardProfileError as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc
    except FileNotFoundError as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc

    log.info("反向挂载 %s 到设备 /remote，按 Ctrl+] 或 Ctrl+x 退出 mpremote REPL", local_dir)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise typer.Exit(result.returncode)


register_command_groups(app)

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

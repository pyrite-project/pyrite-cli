from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import typer

from ..utils.diagnostics import run_doctor
from ..utils.device_context import CommandNeeds, command_needs, prepare_device
from ..utils.ui import print_json
from .common import (
    _complete_port,
    _FORMAT_OPTION,
    _JSON_OPTION,
    _mp_factory,
    _resolve_format,
    log,
)


debug_app = typer.Typer(help="设备诊断与调试信息", add_completion=False)

BOARD_INFO_NEEDS = CommandNeeds(
    connection=True,
    raw_repl=True,
    device_context=True,
    board_extra_info=True,
)

DOCTOR_NEEDS = CommandNeeds(
    connection=True,
    raw_repl=True,
    device_context=True,
)


def register(app: typer.Typer) -> None:
    app.add_typer(debug_app, name="debug")


def _row(label: str, value: object) -> None:
    pad = 10 - sum(2 if ord(c) > 127 else 1 for c in label)
    typer.secho(f"  {label}{' ' * max(pad, 1)}", fg=typer.colors.BRIGHT_BLACK, nl=False)
    typer.echo(str(value))


def _section(title: str) -> None:
    typer.echo()
    typer.secho(f"── {title} ", fg=typer.colors.BRIGHT_CYAN, bold=True)


@debug_app.command("board-info")
@command_needs(BOARD_INFO_NEEDS)
def board_info(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    baudrate: Optional[int] = typer.Option(None, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: Optional[int] = typer.Option(None, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """连接设备并获取详细板级信息（固件、CPU、内存、Flash 等）。"""
    fmt = _resolve_format(fmt, json_output)
    code = """\
import os,gc,machine,ubinascii
st=os.statvfs('/')
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
        prepared = prepare_device(mp, BOARD_INFO_NEEDS)
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
    context = prepared.device_context
    if context is not None:
        firmware = " ".join(
            part for part in (context.implementation, context.version) if part
        )
        if firmware:
            info["FW"] = firmware
        if context.platform:
            info["PLAT"] = context.platform
        if context.machine:
            info["HW"] = context.machine
        if context.release:
            info["REL"] = context.release
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

    _section("固件")
    _row("名称", info.get("FW", "?"))
    _row("平台", info.get("PLAT", "?"))
    _row("硬件", info.get("HW", "?"))
    _row("版本", info.get("REL", "?"))

    _section("设备")
    if "CPU" in info:
        _row("CPU", f"{int(info['CPU']) // 1_000_000} MHz")
    _row("唯一ID", info.get("UID", "?"))
    _row("复位原因", info.get("RST", "?"))
    if "MAC" in info:
        _row("MAC", info["MAC"])

    _section("内存")
    if "MF" in info and "MA" in info:
        mf, ma = int(info["MF"]), int(info["MA"])
        _row("RAM", f"{ma // 1024} KB used / {(mf + ma) // 1024} KB total")
    if "FS" in info:
        total, free = info["FS"].split("/")
        _row("Flash FS", f"{(int(total) - int(free)) // 1024} KB used / {int(total) // 1024} KB total")
    if "FLASH" in info:
        _row("Flash", f"{int(info['FLASH']) // 1024} KB")
    typer.echo()


@debug_app.command("doctor")
@command_needs(DOCTOR_NEEDS)
def doctor(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    baudrate: Optional[int] = typer.Option(None, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: Optional[int] = typer.Option(None, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    save: Optional[str] = typer.Option(None, "--save", help="保存 JSON 诊断报告到文件"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """运行设备诊断，报告可观测的运行时能力和固件特性。"""
    fmt = _resolve_format(fmt, json_output)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        start = time.perf_counter()
        prepare_device(mp, DOCTOR_NEEDS)
        connect_ms = int((time.perf_counter() - start) * 1000)
        report = run_doctor(mp, connect_ms=connect_ms)
    finally:
        mp.disconnect()

    if save:
        Path(save).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if fmt == "json":
        print_json(report)
        return
    _print_doctor_text(report)


def _print_doctor_text(report: dict) -> None:
    board = report.get("board", {})
    memory = report.get("memory", {})
    filesystem = report.get("filesystem", {})
    connection = report.get("connection", {})
    features = report.get("firmware_features", {}).get("items", [])

    fw = " ".join(
        part for part in (board.get("implementation"), board.get("version"))
        if part
    ) or "?"

    _section("固件")
    _row("名称", fw)
    _row("平台", board.get("platform") or "?")
    _row("硬件", board.get("machine") or "?")
    _row("版本", board.get("release") or board.get("version") or "?")

    _section("诊断")
    connect_ms = connection.get("connect_ms")
    if connect_ms is not None:
        _row("连接", f"OK, connect={connect_ms}ms")
    if connection.get("raw_repl_ms") is not None:
        _row("Raw REPL", f"OK, run={connection.get('raw_repl_ms')}ms")

    fs_check = _find_check(report, "filesystem_rw")
    if fs_check:
        status = "OK" if fs_check.get("status") == "ok" else fs_check.get("status", "?").upper()
        _row("文件系统", f"{status}, {fs_check.get('message', '')}")

    raw_check = _find_check(report, "raw_repl")
    if raw_check and connection.get("raw_repl_ms") is None:
        status = "OK" if raw_check.get("status") == "ok" else raw_check.get("status", "?").upper()
        _row("Raw REPL", f"{status}, {raw_check.get('message', '')}")

    _section("内存")
    if isinstance(memory.get("free"), int):
        ram = f"{memory['free'] // 1024} KB free"
        if isinstance(memory.get("total"), int):
            ram += f" / {memory['total'] // 1024} KB total"
        _row("RAM", ram)
    if isinstance(filesystem.get("total"), int) and isinstance(filesystem.get("used"), int):
        _row(
            "Flash FS",
            f"{filesystem['used'] // 1024} KB used / "
            f"{filesystem['total'] // 1024} KB total",
        )

    _section("特性")
    _row("supported", report.get("summary", {}).get("features_supported", 0))
    _row("unsupported", report.get("summary", {}).get("features_unsupported", 0))
    for feature_id in ("sys.settrace", "micropython.kbd_intr", "network", "webrepl"):
        item = _find_feature(features, feature_id)
        if item:
            _row(feature_id, _feature_status(item))

    if report.get("recommendations"):
        _section("Recommendations")
        for item in report.get("recommendations", []):
            _row(item.get("category", "info"), item.get("message", ""))

    _section("配置")
    config = report.get("configuration", {})
    if config.get("verify") is not None:
        _row("verify", config.get("verify"))
    if config.get("max_retries") is not None:
        _row("max_retries", config.get("max_retries"))
    if config.get("chunk_size") is not None:
        _row("chunk_size", config.get("chunk_size"))
    for rec in report.get("configuration", {}).get("recommendations", [])[:2]:
        _row("建议", rec)
    typer.echo()


def _find_check(report: dict, check_id: str) -> Optional[dict]:
    for item in report.get("checks", []):
        if item.get("id") == check_id:
            return item
    return None


def _find_feature(features: list[dict], feature_id: str) -> Optional[dict]:
    for item in features:
        if item.get("id") == feature_id:
            return item
    return None


def _feature_status(item: dict) -> str:
    status = str(item.get("status") or "?")
    confidence = item.get("confidence")
    return f"{status} ({confidence})" if confidence else status

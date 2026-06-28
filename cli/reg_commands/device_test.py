from __future__ import annotations

from typing import Optional

import typer

from ..utils.config import DEFAULT_BAUDRATE
from ..utils.device_tests import (
    DEFAULT_REMOTE_DIR,
    DeviceTestResult,
    DeviceTestSession,
    discover_device_tests,
    run_device_test_plan,
)
from ..utils.ui import safe_text
from .common import _complete_port, _mp_factory, _norm_path, log


def register(app: typer.Typer) -> None:
    app.command("test")(device_test)


def _result_label(result: DeviceTestResult) -> tuple[str, Optional[str]]:
    if result.status == "pass":
        return "PASS", typer.colors.GREEN
    if result.status == "fail":
        return "FAIL", typer.colors.RED
    if result.status == "timeout":
        return "TIMEOUT", typer.colors.YELLOW
    return "ERROR", typer.colors.RED


def _print_block(text: str) -> None:
    for line in safe_text(text, preserve_newlines=True).splitlines():
        print(f"    {line}")


def _print_session(session: DeviceTestSession, *, keep_files: bool = False) -> None:
    by_path = {result.remote_path: result for result in session.results}
    for item in session.plan.files:
        result = by_path.get(item.remote_path)
        if result is None:
            typer.secho(
                f"MISS {item.relative_path} (no runner result)",
                fg=typer.colors.RED,
            )
            continue
        label, color = _result_label(result)
        typer.secho(
            f"{label} {item.relative_path} ({result.duration_ms} ms)",
            fg=color,
        )
        if result.stdout:
            _print_block(result.stdout)
        if result.error:
            _print_block(result.error)

    passed = sum(1 for result in session.results if result.passed)
    failed = len(session.plan.files) - passed
    if keep_files:
        typer.echo(f"KEEP-FILES remote test files retained at {session.plan.remote_dir}")
    else:
        typer.echo(f"CLEANED remote test files from {session.plan.remote_dir}")
    log.info("测试完成: %d 通过, %d 失败", passed, failed)


def device_test(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    path: Optional[str] = typer.Argument(
        None,
        help="本地测试文件或目录，默认 test_device/",
    ),
    keep_files: bool = typer.Option(
        False,
        "--keep-files",
        help="测试结束后保留设备端临时文件",
    ),
    timeout: int = typer.Option(
        10,
        "--timeout",
        "-t",
        min=1,
        help="设备端测试执行超时秒数",
        envvar="PYRITE_TIMEOUT",
    ),
    remote_dir: str = typer.Option(
        DEFAULT_REMOTE_DIR,
        "--remote-dir",
        help="设备端临时测试目录",
    ),
    baudrate: int = typer.Option(
        DEFAULT_BAUDRATE,
        "--baudrate",
        "-b",
        help="波特率",
        envvar="PYRITE_BAUDRATE",
    ),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
) -> None:
    """上传并在设备端运行 MicroPython 测试。"""
    try:
        plan = discover_device_tests(path, remote_dir=_norm_path(remote_dir))
    except (FileNotFoundError, ValueError) as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc

    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        session = run_device_test_plan(
            mp,
            plan,
            timeout=timeout,
            keep_files=keep_files,
        )
        _print_session(session, keep_files=keep_files)
        if not session.ok:
            raise typer.Exit(1)
    finally:
        mp.disconnect()

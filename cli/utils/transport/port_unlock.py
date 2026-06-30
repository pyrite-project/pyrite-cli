"""Detect and release processes that are likely holding a serial port."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Callable, Iterable, Optional, Sequence

import click

from ..log import get_logger

log = get_logger(__name__)

ConfirmFn = Callable[..., bool]
EchoFn = Callable[[str], None]
ScannerFn = Callable[[str], tuple[list["PortProcess"], str, list[str]]]
TerminatorFn = Callable[[Sequence["PortProcess"]], list[str]]
RunFn = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class PortProcess:
    pid: int
    name: str = ""
    user: str = ""
    command: str = ""
    source: str = ""


_BUSY_HINTS = (
    "permission denied",
    "access is denied",
    "access denied",
    "resource busy",
    "device or resource busy",
    "busy",
    "already open",
    "in use",
    "errno 13",
    "errno 16",
    "拒绝访问",
    "权限",
    "被占用",
)
_OPEN_HINTS = (
    "could not open port",
    "failed to open",
    "cannot open",
)
_MISSING_PORT_HINTS = (
    "no such file",
    "cannot find",
    "not found",
    "找不到",
    "不存在",
)


def is_probable_port_busy_error(exc: BaseException) -> bool:
    """Return True when a serial-open exception looks like a port lock."""
    raw = str(exc)
    lower = raw.lower()
    if any(hint in lower or hint in raw for hint in _MISSING_PORT_HINTS):
        return False
    if isinstance(exc, PermissionError):
        return True
    if any(hint in lower or hint in raw for hint in _BUSY_HINTS):
        return True
    return any(hint in lower for hint in _OPEN_HINTS) and "permission" in lower


def parse_lsof_output(output: str) -> list[PortProcess]:
    processes: list[PortProcess] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.upper().startswith("COMMAND "):
            continue
        parts = line.split(None, 8)
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        processes.append(
            PortProcess(
                pid=int(parts[1]),
                name=parts[0],
                user=parts[2] if len(parts) > 2 else "",
                command=parts[8] if len(parts) > 8 else "",
                source="lsof",
            )
        )
    return _unique_processes(processes)


_HANDLE_LINE_RE = re.compile(
    r"^\s*(?P<name>.+?)\s+pid:\s*(?P<pid>\d+)\s+type:\s*(?P<type>\S+)"
    r"\s+(?P<handle>[0-9A-Fa-f]+):\s*(?P<object>.+?)\s*$",
    re.IGNORECASE,
)


def parse_handle_output(output: str) -> list[PortProcess]:
    processes: list[PortProcess] = []
    for raw_line in output.splitlines():
        match = _HANDLE_LINE_RE.match(raw_line)
        if not match:
            continue
        processes.append(
            PortProcess(
                pid=int(match.group("pid")),
                name=match.group("name").strip(),
                command=match.group("object").strip(),
                source="handle",
            )
        )
    return _unique_processes(processes)


def maybe_unlock_occupied_port(
    port: str,
    exc: Optional[BaseException] = None,
    *,
    confirm: Optional[ConfirmFn] = None,
    echo: Optional[EchoFn] = None,
    scanner: Optional[ScannerFn] = None,
    terminator: Optional[TerminatorFn] = None,
    interactive: Optional[bool] = None,
) -> bool:
    """Ask the user whether to release a busy serial port, then do it."""
    if exc is not None and not is_probable_port_busy_error(exc):
        return False

    if interactive is None:
        interactive = _is_interactive()
    if not interactive:
        log.debug("跳过串口占用解除：当前不是交互式终端")
        return False

    confirm_fn = confirm or _default_confirm
    echo_fn = echo or _default_echo
    scanner_fn = scanner or find_port_processes

    if not confirm_fn(
        f"检测到串口 {port} 疑似被占用，是否扫描占用进程并尝试解除占用?",
        default=False,
    ):
        return False

    try:
        processes, backend, warnings = scanner_fn(port)
    except Exception as scan_exc:
        log.warning("扫描串口占用进程失败: %s", scan_exc)
        echo_fn(f"扫描串口占用进程失败: {scan_exc}")
        return False

    for warning in warnings:
        echo_fn(warning)

    processes = _unique_processes(processes)
    if not processes:
        echo_fn(f"未扫描到正在占用 {port} 的进程，请手动关闭 IDE/串口监视器后重试。")
        if sys.platform.startswith("win") and backend != "handle.exe":
            echo_fn("Windows 精确扫描建议安装 Sysinternals handle.exe 并加入 PATH。")
        return False

    echo_fn(f"扫描后端: {backend}")
    echo_fn(format_process_table(processes))

    prompt = (
        "结束上述进程可能导致未保存数据丢失。"
        f"请先保存 IDE、终端或串口监视器中的工作。是否结束这些进程以释放 {port}?"
    )
    if not confirm_fn(prompt, default=False):
        return False

    if terminator is None:
        messages = terminate_processes(processes, confirm_force=confirm_fn)
    else:
        messages = terminator(processes)
    for message in messages:
        echo_fn(message)
    return True


def find_port_processes(
    port: str,
    *,
    platform_name: Optional[str] = None,
    run: RunFn = subprocess.run,
) -> tuple[list[PortProcess], str, list[str]]:
    platform_value = (platform_name or sys.platform).lower()
    if platform_value.startswith("win"):
        return _find_windows_port_processes(port, run=run)
    if platform_value.startswith("linux") or platform_value == "darwin":
        return _find_unix_port_processes(port, run=run)
    return [], "unsupported", [f"当前平台暂不支持扫描串口占用进程: {platform_value}"]


def format_process_table(processes: Sequence[PortProcess]) -> str:
    rows = ["PID      USER         PROCESS              COMMAND/SOURCE"]
    for proc in processes:
        user = _clip(proc.user or "-", 12)
        name = _clip(proc.name or "-", 20)
        detail = proc.command or proc.source or "-"
        rows.append(f"{proc.pid:<8} {user:<12} {name:<20} {detail}")
    return "\n".join(rows)


def terminate_processes(
    processes: Sequence[PortProcess],
    *,
    platform_name: Optional[str] = None,
    run: RunFn = subprocess.run,
    confirm_force: Optional[ConfirmFn] = None,
) -> list[str]:
    messages: list[str] = []
    targets = [proc for proc in _unique_processes(processes) if proc.pid != os.getpid()]
    skipped = [proc for proc in processes if proc.pid == os.getpid()]
    for proc in skipped:
        messages.append(f"跳过当前 pyrcli 进程 PID {proc.pid}")
    if not targets:
        return messages

    platform_value = (platform_name or sys.platform).lower()
    if platform_value.startswith("win"):
        messages.extend(_terminate_windows_processes(targets, force=False, run=run))
    else:
        messages.extend(_terminate_unix_processes(targets, force=False))

    time.sleep(0.8)
    remaining = [proc for proc in targets if _pid_exists(proc.pid, platform_name=platform_value)]
    if remaining and confirm_force is not None:
        if confirm_force(
            "仍有进程未退出。是否强制结束这些进程? 这可能导致未保存数据丢失。",
            default=False,
        ):
            if platform_value.startswith("win"):
                messages.extend(_terminate_windows_processes(remaining, force=True, run=run))
            else:
                messages.extend(_terminate_unix_processes(remaining, force=True))
            time.sleep(0.4)
    return messages


def _find_unix_port_processes(
    port: str,
    *,
    run: RunFn,
) -> tuple[list[PortProcess], str, list[str]]:
    warnings: list[str] = []
    processes: list[PortProcess] = []
    lsof = shutil.which("lsof")
    if lsof:
        result = _run_command([lsof, "-nP", port], run=run, timeout=5)
        if result is not None:
            output = (result.stdout or "") + "\n" + (result.stderr or "")
            processes.extend(parse_lsof_output(output))
        if processes:
            return _unique_processes(processes), "lsof", warnings
    else:
        warnings.append("未找到 lsof，尝试使用 fuser 扫描。")

    fuser = shutil.which("fuser")
    if fuser:
        result = _run_command([fuser, port], run=run, timeout=5)
        if result is not None:
            output = (result.stdout or "") + "\n" + (result.stderr or "")
            pids = _parse_fuser_pids(output, port)
            processes.extend(_describe_unix_pid(pid, run=run, source="fuser") for pid in pids)
    else:
        warnings.append("未找到 fuser，无法继续扫描占用进程。")
    return _unique_processes(processes), "fuser" if processes else "unix", warnings


def _find_windows_port_processes(
    port: str,
    *,
    run: RunFn,
) -> tuple[list[PortProcess], str, list[str]]:
    warnings: list[str] = []
    processes: list[PortProcess] = []
    port_name = _normalize_windows_port_name(port)
    search_terms = [port_name]
    device_name = _query_dos_device(port_name)
    if device_name:
        search_terms.insert(0, device_name)

    handle_exe = _find_handle_executable()
    if handle_exe:
        for term in search_terms:
            result = _run_command(
                [handle_exe, "-nobanner", "-accepteula", term],
                run=run,
                timeout=8,
            )
            if result is None:
                continue
            output = (result.stdout or "") + "\n" + (result.stderr or "")
            processes.extend(parse_handle_output(output))
        if processes:
            return _unique_processes(processes), "handle.exe", warnings
        warnings.append("handle.exe 未找到匹配句柄，改用命令行启发式扫描。")
    else:
        warnings.append("未找到 Sysinternals handle.exe，Windows 将使用命令行启发式扫描。")

    processes.extend(_scan_windows_command_lines(port_name, run=run))
    return _unique_processes(processes), "windows-commandline", warnings


def _scan_windows_command_lines(port_name: str, *, run: RunFn) -> list[PortProcess]:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return []
    command = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    result = _run_command(
        [powershell, "-NoProfile", "-Command", command],
        run=run,
        timeout=10,
    )
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    processes: list[PortProcess] = []
    token = port_name.lower()
    for item in items:
        if not isinstance(item, dict):
            continue
        command_line = str(item.get("CommandLine") or "")
        if token not in command_line.lower():
            continue
        pid = item.get("ProcessId")
        if not isinstance(pid, int):
            continue
        if pid == os.getpid():
            continue
        processes.append(
            PortProcess(
                pid=pid,
                name=str(item.get("Name") or ""),
                command=command_line,
                source="commandline",
            )
        )
    return _unique_processes(processes)


def _terminate_unix_processes(
    processes: Sequence[PortProcess],
    *,
    force: bool,
) -> list[str]:
    messages: list[str] = []
    sig = signal.SIGKILL if force else signal.SIGTERM
    label = "SIGKILL" if force else "SIGTERM"
    for proc in processes:
        try:
            os.kill(proc.pid, sig)
            messages.append(f"已向 PID {proc.pid} ({proc.name or 'unknown'}) 发送 {label}")
        except ProcessLookupError:
            messages.append(f"PID {proc.pid} 已退出")
        except PermissionError as exc:
            messages.append(f"无法结束 PID {proc.pid}: 权限不足 ({exc})")
        except OSError as exc:
            messages.append(f"无法结束 PID {proc.pid}: {exc}")
    return messages


def _terminate_windows_processes(
    processes: Sequence[PortProcess],
    *,
    force: bool,
    run: RunFn,
) -> list[str]:
    messages: list[str] = []
    for proc in processes:
        args = ["taskkill"]
        if force:
            args.append("/F")
        args.extend(["/PID", str(proc.pid), "/T"])
        result = _run_command(args, run=run, timeout=8)
        if result is None:
            messages.append(f"无法结束 PID {proc.pid}: taskkill 不可用")
            continue
        output = ((result.stdout or "") + " " + (result.stderr or "")).strip()
        if result.returncode == 0:
            mode = "强制结束" if force else "请求结束"
            messages.append(f"已{mode} PID {proc.pid} ({proc.name or 'unknown'})")
        else:
            messages.append(f"无法结束 PID {proc.pid}: {output or 'taskkill failed'}")
    return messages


def _describe_unix_pid(pid: int, *, run: RunFn, source: str) -> PortProcess:
    name = ""
    command = ""
    ps = shutil.which("ps")
    if ps:
        name_result = _run_command([ps, "-p", str(pid), "-o", "comm="], run=run, timeout=3)
        if name_result is not None and name_result.returncode == 0:
            name = name_result.stdout.strip().splitlines()[0] if name_result.stdout.strip() else ""
        command_result = _run_command([ps, "-p", str(pid), "-o", "args="], run=run, timeout=3)
        if command_result is not None and command_result.returncode == 0:
            command = command_result.stdout.strip().splitlines()[0] if command_result.stdout.strip() else ""
    return PortProcess(pid=pid, name=name, command=command, source=source)


def _parse_fuser_pids(output: str, port: str) -> list[int]:
    cleaned = output.replace(port, "")
    base = os.path.basename(port)
    if base:
        cleaned = cleaned.replace(base, "")
    pids: list[int] = []
    for token in re.findall(r"\b\d+\b", cleaned):
        pid = int(token)
        if pid > 0:
            pids.append(pid)
    return sorted(set(pids))


def _run_command(
    args: Sequence[str],
    *,
    run: RunFn,
    timeout: float,
) -> Optional[subprocess.CompletedProcess[str]]:
    try:
        return run(
            list(args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        log.trace("命令执行失败 %s: %s", args, exc)
        return None


def _normalize_windows_port_name(port: str) -> str:
    value = port.strip().strip('"').replace("/", "\\")
    if value.startswith("\\\\.\\"):
        value = value[4:]
    return value.upper()


def _query_dos_device(port_name: str) -> Optional[str]:
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    buffer = ctypes.create_unicode_buffer(4096)
    query_dos_device = ctypes.windll.kernel32.QueryDosDeviceW
    query_dos_device.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    query_dos_device.restype = wintypes.DWORD
    result = query_dos_device(port_name, buffer, len(buffer))
    if result == 0:
        return None
    return buffer.value or None


def _find_handle_executable() -> Optional[str]:
    return (
        shutil.which("handle.exe")
        or shutil.which("handle64.exe")
        or shutil.which("handle")
    )


def _pid_exists(pid: int, *, platform_name: Optional[str] = None) -> bool:
    if pid <= 0:
        return False
    platform_value = (platform_name or sys.platform).lower()
    if platform_value.startswith("win"):
        return _windows_pid_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


def _windows_pid_exists(pid: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    process_query_limited_information = 0x1000
    error_access_denied = 5
    still_active = 259
    try:
        kernel32 = ctypes.windll.kernel32
    except AttributeError:
        return False
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        return kernel32.GetLastError() == error_access_denied
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _unique_processes(processes: Iterable[PortProcess]) -> list[PortProcess]:
    seen: set[int] = set()
    result: list[PortProcess] = []
    for proc in processes:
        if proc.pid in seen:
            continue
        seen.add(proc.pid)
        result.append(proc)
    return result


def _clip(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 1] + "…"


def _is_interactive() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)())


def _default_confirm(message: str, *, default: bool = False) -> bool:
    return click.confirm(message, default=default, err=True)


def _default_echo(message: str) -> None:
    click.echo(message, err=True)

"""Human-friendly error messages for common device failures."""

from __future__ import annotations

import socket


def humanize_exception(exc: BaseException) -> str:
    """Return an actionable CLI error message for a low-level exception."""
    raw = str(exc).strip()
    lower = raw.lower()

    if isinstance(exc, TimeoutError) or "timeout" in lower or "timed out" in lower or "超时" in raw:
        return (
            "操作超时：设备没有在限定时间内响应。\n"
            "建议：确认 USB/串口连接稳定，设备没有停在用户代码死循环中；"
            "必要时增大 --timeout，或按住 BOOT/RESET 后重试。"
        )

    if isinstance(exc, (ConnectionError, OSError, socket.error)) and (
        "could not open" in lower
        or "permission" in lower
        or "access is denied" in lower
        or "拒绝访问" in raw
    ):
        return (
            "无法打开设备端口：串口不存在、被占用或权限不足。\n"
            "建议：运行 pyrcli scan 确认端口号，关闭串口监视器/REPL/IDE，"
            "并检查当前用户是否有串口访问权限。"
        )

    if "无法进入原始 repl" in lower or "raw repl" in lower or "ctrl-b" in lower:
        return (
            "设备没有进入原始 REPL：pyrite-cli 未收到 MicroPython 的 Raw REPL 握手。\n"
            "建议：确认设备正在运行 MicroPython，尝试按 Ctrl+C 中断正在运行的程序，"
            "再按 RESET 后重试；如果是新固件，先用 pyrcli board-info 检查连接。"
        )

    if "mpy" in lower and (
        "version" in lower or "版本" in raw or "incompatible" in lower or "不兼容" in raw
    ):
        return (
            "MPY 协议/字节码版本不匹配：编译出的 .mpy 可能不能在当前固件上运行。\n"
            "建议：升级/重装匹配的 mpy-cross，或使用 --no-compile 先刷入 .py 源码验证。"
        )

    if "设备未就绪" in raw or "no response" in lower or "无响应" in raw:
        return (
            "设备无响应：刷入脚本已发送，但设备没有返回就绪标记。\n"
            "建议：检查串口波特率、数据线和供电，重置设备后重试；"
            "若用户程序占用 REPL，请先 Ctrl+C 中断。"
        )

    return f"操作失败：{raw or exc.__class__.__name__}"

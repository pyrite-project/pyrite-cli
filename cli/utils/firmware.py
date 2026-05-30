"""
固件刷写模块 — 通过 esptool 烧录 MicroPython/ESP 固件。

esptool 为可选依赖，未安装时会给出安装提示。
通过子进程调用 esptool，避免强制依赖和版本冲突。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import List, Optional

from .log import get_logger

log = get_logger(__name__)


def _find_esptool_cmd() -> Optional[List[str]]:
    """查找可用的 esptool 命令行路径。"""
    # 1. 优先尝试 python -m esptool
    try:
        import esptool  # noqa: F401
        log.trace("找到 esptool (python -m esptool)")
        return [sys.executable, "-m", "esptool"]
    except ImportError:
        pass
    # 2. 回退: PATH 中的 standalone esptool.py
    path = shutil.which("esptool.py")
    if path:
        log.trace("找到 esptool.py: %s", path)
        return [path]
    return None


_ESDTOOL_INSTALL_HINT = (
    "未找到 esptool。请安装：\n"
    "  pip install esptool\n"
    "或从 https://github.com/espressif/esptool 下载 standalone 版本并加入 PATH。"
)


def _ensure_esptool() -> List[str]:
    cmd = _find_esptool_cmd()
    if cmd:
        return cmd
    raise FileNotFoundError(_ESDTOOL_INSTALL_HINT)


def flash_firmware(
    port: str,
    firmware: str,
    baudrate: int = 460800,
    address: str = "0x0",
    flash_mode: str = "keep",
    flash_size: str = "keep",
    erase_first: bool = False,
    before: str = "default_reset",
    after: str = "hard_reset",
) -> subprocess.CompletedProcess:
    """烧录固件到设备。"""
    esptool = _ensure_esptool()
    cmd: List[str] = [
        *esptool,
        "--port", port,
        "--baud", str(baudrate),
        "write_flash",
        "--flash_mode", flash_mode,
        "--flash_size", flash_size,
        "--before", before,
        "--after", after,
    ]
    if erase_first:
        cmd.append("--erase-all")
    cmd += [address, firmware]
    log.info("烧录固件: %s → %s (端口=%s, 波特率=%d)", firmware, port, baudrate)
    log.debug("esptool 命令: %s", " ".join(cmd))
    return subprocess.run(cmd, check=True)


def erase_flash(
    port: str,
    baudrate: int = 460800,
) -> subprocess.CompletedProcess:
    """擦除设备整个 Flash。"""
    esptool = _ensure_esptool()
    cmd = [*esptool, "--port", port, "--baud", str(baudrate), "erase_flash"]
    log.info("擦除 Flash (端口=%s)", port)
    return subprocess.run(cmd, check=True)


def chip_info(
    port: str,
    baudrate: int = 460800,
) -> str:
    """读取芯片和 Flash 信息。"""
    esptool = _ensure_esptool()
    cmd = [*esptool, "--port", port, "--baud", str(baudrate), "flash_id"]
    log.debug("读取芯片信息 (端口=%s)", port)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"读取芯片信息失败 (exit {result.returncode}):\n{result.stderr}"
        )
    return result.stdout + result.stderr


def verify_firmware(
    port: str,
    firmware: str,
    baudrate: int = 460800,
    address: str = "0x0",
) -> subprocess.CompletedProcess:
    """验证固件已正确烧录到设备。"""
    esptool = _ensure_esptool()
    cmd = [
        *esptool, "--port", port, "--baud", str(baudrate),
        "verify_flash", address, firmware,
    ]
    log.info("验证固件: %s (端口=%s)", firmware, port)
    return subprocess.run(cmd, check=True)


def read_flash(
    port: str,
    size: str,
    address: str = "0x0",
    output: Optional[str] = None,
    baudrate: int = 460800,
) -> str:
    """从设备 Flash 读取内容到本地文件。"""
    dst = output or f"flash_dump_{address}.bin"
    esptool = _ensure_esptool()
    cmd = [
        *esptool, "--port", port, "--baud", str(baudrate),
        "read_flash", address, size, dst,
    ]
    log.info("读取 Flash: 地址=%s 大小=%s → %s (端口=%s)", address, size, dst, port)
    subprocess.run(cmd, check=True)
    return dst

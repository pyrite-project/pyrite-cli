"""固件刷写模块 — 通过 esptool 烧录 MicroPython/ESP 固件。

esptool 为可选依赖，未安装时会给出安装提示。
通过子进程调用 esptool，避免强制依赖和版本冲突。
"""

import subprocess
import sys
import shutil
from typing import List, Optional


def _find_esptool_cmd() -> Optional[List[str]]:
    """查找可用的 esptool 命令行路径。"""
    # 1. 优先尝试 python -m esptool（安装为 Python 包时）
    try:
        import esptool  # noqa: F401
        return [sys.executable, "-m", "esptool"]
    except ImportError:
        pass
    # 2. 回退: PATH 中的 standalone esptool.py
    path = shutil.which("esptool.py")
    if path:
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
    """烧录固件到设备。

    Args:
        port: 串口号（如 COM3, /dev/ttyUSB0）
        firmware: 固件 .bin 文件路径
        baudrate: 波特率
        address: 烧录起始地址（默认 0x0）
        flash_mode: Flash 模式 (qio/qout/dio/dout/keep)
        flash_size: Flash 大小 (1MB/2MB/4MB/8MB/16MB/detect/keep)
        erase_first: 烧录前是否全片擦除
        before: 连接前操作 (default_reset/no_reset/no_reset_no_sync)
        after: 烧录后操作 (hard_reset/soft_reset/no_reset)

    Returns:
        subprocess.CompletedProcess
    """
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
    return subprocess.run(cmd, check=True)


def erase_flash(
    port: str,
    baudrate: int = 460800,
) -> subprocess.CompletedProcess:
    """擦除设备整个 Flash。

    Args:
        port: 串口号
        baudrate: 波特率

    Returns:
        subprocess.CompletedProcess
    """
    esptool = _ensure_esptool()
    cmd = [*esptool, "--port", port, "--baud", str(baudrate), "erase_flash"]
    return subprocess.run(cmd, check=True)


def chip_info(
    port: str,
    baudrate: int = 460800,
) -> str:
    """读取芯片和 Flash 信息。

    Args:
        port: 串口号
        baudrate: 波特率

    Returns:
        标准输出+标准错误文本
    """
    esptool = _ensure_esptool()
    cmd = [*esptool, "--port", port, "--baud", str(baudrate), "flash_id"]
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
    """验证固件已正确烧录到设备。

    通过 esptool verify_flash 比对本地文件与 Flash 内容。

    Args:
        port: 串口号
        firmware: 固件 .bin 文件路径
        baudrate: 波特率
        address: 起始地址

    Returns:
        subprocess.CompletedProcess
    """
    esptool = _ensure_esptool()
    cmd = [*esptool, "--port", port, "--baud", str(baudrate), "verify_flash", address, firmware]
    return subprocess.run(cmd, check=True)


def read_flash(
    port: str,
    size: str,
    address: str = "0x0",
    output: Optional[str] = None,
    baudrate: int = 460800,
) -> str:
    """从设备 Flash 读取内容到本地文件。

    Args:
        port: 串口号
        size: 读取字节数（支持 0x100000 等 hex 格式）
        address: 起始地址
        output: 输出文件路径，默认 auto 生成
        baudrate: 波特率

    Returns:
        输出文件路径
    """
    dst = output or f"flash_dump_{address}.bin"
    esptool = _ensure_esptool()
    cmd = [*esptool, "--port", port, "--baud", str(baudrate), "read_flash", address, size, dst]
    subprocess.run(cmd, check=True)
    return dst

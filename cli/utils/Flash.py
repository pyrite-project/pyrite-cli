"""
MicroPython 设备刷入工具 - 通过串口原始 REPL 上传文件到设备
依赖: pyserial (pip install pyserial)
"""

import os
import sys
import time
import json
from pathlib import Path
import serial
import serial.tools.list_ports


# ── 配置管理 ────────────────────────────────────────────────────────────────

CONFIG_FILE = ".pyrite_config.json"
DEFAULT_CHUNK_SIZE = 4096  # 字节


def _load_config():
    """从当前或上级目录加载配置文件，未找到则使用默认值。"""
    cfg = {"chunk_size": DEFAULT_CHUNK_SIZE}
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        p = parent / CONFIG_FILE
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data.get("chunk_size"), int) and data["chunk_size"] > 0:
                    cfg["chunk_size"] = data["chunk_size"]
            except (json.JSONDecodeError, OSError):
                pass
            break
    return cfg


# ── MicroPython 设备操作类 ─────────────────────────────────────────────────

class MicroPython:
    """通过串口原始 REPL 与 MicroPython 设备交互。

    提供扫描串口、连接、断开、执行代码、上传文件功能。
    上传时自动设置 kbd_intr(-1) 防止数据流中的 0x03 字节重启设备。
    """

    def __init__(self, port=None, baudrate=115200, timeout=10):
        self.config = _load_config()
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None          # pySerial 对象
        self._in_raw = False     # 是否处于原始 REPL 模式
        self._kbd_set = False    # 是否已设置 kbd_intr(-1)

    # ── 串口扫描 ────────────────────────────────────────────────────────────

    @staticmethod
    def scan_ports():
        """扫描所有可用串口，返回设备信息列表。"""
        ports = []
        for p in serial.tools.list_ports.comports():
            ports.append({
                "device": p.device,
                "description": p.description,
                "hwid": p.hwid,
                "vid": p.vid,
                "pid": p.pid,
                "serial_number": p.serial_number,
            })
        return ports

    @staticmethod
    def scan_micropython_ports():
        """返回疑似 MicroPython 设备的串口列表（基于常见 USB-Serial 芯片 VID）。"""
        mp_vids = {0x10C4, 0x1A86, 0x0403, 0x2E8A, 0x303A, 0x16D0}  # CP210x, CH340, FTDI, RP2040, ESP32-S3, MCP2221
        candidates = set()
        for p in serial.tools.list_ports.comports():
            if p.vid in mp_vids:
                candidates.add(p.device)
            desc = (p.description or "").lower()
            if any(k in desc for k in ("cp210", "ch340", "ft232", "usb serial",
                                       "uart", "micropython")):
                candidates.add(p.device)
        return [{"device": d} for d in sorted(candidates)]

    # ── 连接管理 ────────────────────────────────────────────────────────────

    def connect(self, port=None, baudrate=None):
        """打开串口连接到设备。

        Args:
            port: 串口号，如 'COM3' 或 '/dev/ttyUSB0'
            baudrate: 波特率，默认 115200

        Returns:
            True 表示连接成功
        """
        if self.ser and self.ser.is_open:
            self.disconnect()

        self.port = port or self.port
        if not self.port:
            raise ValueError("未提供串口号，请先调用 scan_ports() 或指定 port")

        baud = baudrate or self.baudrate

        self.ser = serial.Serial(
            port=self.port,
            baudrate=baud,
            timeout=self.timeout,
            write_timeout=self.timeout,
        )
        # 等待设备串口就绪
        time.sleep(0.3)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        return True

    def disconnect(self):
        """断开串口连接，恢复设备 kbd_intr 设置并退出原始 REPL。"""
        self._restore_kbd_intr()
        self._exit_raw_repl()
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    @property
    def is_connected(self):
        """是否已连接。"""
        return self.ser is not None and self.ser.is_open

    # ── 原始 REPL ───────────────────────────────────────────────────────────

    def _enter_raw_repl(self):
        """切换到原始 REPL 模式（Ctrl+A）。"""
        if self._in_raw:
            return

        # 先发 Ctrl+C 中断可能正在运行的程序
        self._write(b"\x03")
        time.sleep(0.15)
        self.ser.reset_input_buffer()

        # Ctrl+A 进入原始 REPL
        self._write(b"\x01")
        data = self._read_until(b">", timeout=2)

        if b">" not in data:
            raise RuntimeError(f"无法进入原始 REPL 模式，设备响应: {data[:100]!r}")

        self._in_raw = True

    def _exit_raw_repl(self):
        """退出原始 REPL 回到普通 REPL（Ctrl+B）。"""
        if not self._in_raw:
            return
        try:
            self._write(b"\x02")
            time.sleep(0.1)
            self.ser.reset_input_buffer()
        except Exception:
            pass
        finally:
            self._in_raw = False

    def _write(self, data):
        """写入数据到串口。"""
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.ser.write(data)

    def _read_until(self, terminator=b"\x04", timeout=None):
        """从串口读取直到遇到终止符。

        Returns:
            包含终止符在内的全部读取数据
        """
        timeout = timeout or self.timeout
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ser.in_waiting:
                chunk = self.ser.read(self.ser.in_waiting)
                buf += chunk
                if terminator in buf:
                    break
            time.sleep(0.02)
        return buf

    def _execute(self, code, timeout=10):
        """在原始 REPL 中执行 Python 代码并返回设备输出。

        Args:
            code: 要执行的 Python 代码（字符串或 bytes）
            timeout: 等待响应超时（秒）

        Returns:
            设备输出的 stdout 文本
        """
        if not self._in_raw:
            raise RuntimeError("不在原始 REPL 模式，请先调用 _enter_raw_repl()")

        if isinstance(code, str):
            code = code.encode("utf-8")

        self._write(code)
        self._write(b"\x04")  # Ctrl+D 执行

        resp = self._read_until(b"\x04", timeout=timeout)
        # 去掉尾部的 \x04
        resp = resp.rstrip(b"\x04")
        text = resp.decode("utf-8", errors="replace").strip()

        if "Traceback" in text:
            raise RuntimeError(f"设备执行错误:\n{text}")

        return text

    # ── kbd_intr 保护 ───────────────────────────────────────────────────────

    def _setup_kbd_intr(self):
        """设置 kbd_intr(-1)，禁用 Ctrl+C 中断（防止数据中 0x03 重启设备）。"""
        self._execute("import micropython; micropython.kbd_intr(-1)")
        self._kbd_set = True

    def _restore_kbd_intr(self):
        """恢复 kbd_intr(3)，重新启用 Ctrl+C 中断。"""
        if not self._kbd_set:
            return
        try:
            if self.is_connected and self._in_raw:
                self._execute("import micropython; micropython.kbd_intr(3)", timeout=3)
        except Exception:
            pass
        finally:
            self._kbd_set = False

    # ── 文件刷入 ────────────────────────────────────────────────────────────

    def _write_raw_chunk(self, data):
        """通过系统标准输入的缓冲区直接将原始字节写入设备已打开的文件。

        协议:
          1. 发送 Python 代码，通知设备从 stdin 读取 N 个原始字节
          2. 发送 Ctrl+D 触发编译与执行
          3. 等待设备开始执行、阻塞在 read() 上
          4. 发送原始字节
          5. 等待 \x04（执行完成信号）
        """
        code = (
            "import sys\n"
            f"b=sys.stdin.buffer.read({len(data)})\n"
            "f.write(b)\n"
        )
        self._write(code.encode() + b"\x04")
        time.sleep(0.05)  # 等待设备开始执行并阻塞在 stdin 上
        self._write(data)
        resp = self._read_until(b"\x04", timeout=self.timeout)
        text = resp.decode("utf-8", errors="replace").strip()
        if "Traceback" in text:
            raise RuntimeError(f"设备写入错误:\n{text}")

    def flash_file(self, local_path, remote_path=None):
        """将本地文件刷入 MicroPython 设备。

        流程: 进入原始 REPL → 设置 kbd_intr(-1)
              → 分块原始字节传输（sys.stdin.buffer.read）→ 恢复 kbd_intr

        Args:
            local_path: 本地文件路径
            remote_path: 设备上的目标路径（默认使用文件名）

        Returns:
            True 表示成功
        """
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"本地文件不存在: {local_path}")

        remote_path = (remote_path or os.path.basename(local_path)).replace("\\", "/")
        file_size = os.path.getsize(local_path)
        chunk_size = self.config["chunk_size"]

        if not self._in_raw:
            self._enter_raw_repl()
        if not self._kbd_set:
            self._setup_kbd_intr()

        print(f"  刷入: {local_path} -> {remote_path} ({file_size} 字节, "
              f"块大小={chunk_size})")

        try:
            self._execute(f"f=open({repr(remote_path)},'wb')")

            with open(local_path, "rb") as lf:
                while True:
                    chunk = lf.read(chunk_size)
                    if not chunk:
                        break
                    self._write_raw_chunk(chunk)

            self._execute("f.close()\ndel f")
            print(f"  ✓ {remote_path}")
            return True

        except Exception:
            try:
                self._execute("f.close()", timeout=2)
            except Exception:
                pass
            raise

    def flash_program(self, local_dir, remote_prefix=""):
        """刷入整个目录树到设备。

        Args:
            local_dir: 本地目录路径
            remote_prefix: 设备上的远程路径前缀

        Returns:
            [(本地路径, 远程路径, 成功与否), ...] 列表
        """
        if not os.path.isdir(local_dir):
            raise NotADirectoryError(f"不是有效目录: {local_dir}")

        if not self._in_raw:
            self._enter_raw_repl()

        # 收集所有文件
        entries = []
        for root, _dirs, files in os.walk(local_dir):
            for fn in files:
                lp = os.path.join(root, fn)
                rp = os.path.join(
                    remote_prefix, os.path.relpath(lp, local_dir)
                ).replace("\\", "/")
                entries.append((lp, rp))

        if not entries:
            print("  没有需要刷入的文件。")
            return []

        # 在设备上创建远程目录结构
        dirs = sorted({
            os.path.dirname(rp) for _, rp in entries if os.path.dirname(rp)
        })
        for d in dirs:
            try:
                self._execute(
                    "import os\n"
                    "try:\n"
                    f" os.mkdir({repr(d)})\n"
                    "except OSError:\n"
                    " pass"
                )
            except Exception:
                pass

        # 逐个刷入
        results = []
        for lp, rp in entries:
            try:
                self.flash_file(lp, rp)
                results.append((lp, rp, True))
            except Exception as e:
                print(f"  ✗ {rp}: {e}")
                results.append((lp, rp, False))

        return results

    # ── 工具方法 ────────────────────────────────────────────────────────────

    def run(self, code):
        """在设备上执行任意 Python 代码并返回输出。"""
        if not self._in_raw:
            self._enter_raw_repl()
        return self._execute(code)

    def reset(self):
        """软重启设备 (machine.reset())。"""
        if not self._in_raw:
            self._enter_raw_repl()
        self._restore_kbd_intr()
        try:
            self._execute("import machine; machine.reset()", timeout=2)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.disconnect()


# ── 辅助函数 ────────────────────────────────────────────────────────────────

def create_default_config():
    """在工作目录创建默认配置文件。"""
    cfg_path = Path.cwd() / CONFIG_FILE
    cfg_path.write_text(
        json.dumps({"chunk_size": DEFAULT_CHUNK_SIZE}, indent=2),
        encoding="utf-8",
    )
    print(f"默认配置文件已创建: {cfg_path}")
    print(f"  chunk_size = {DEFAULT_CHUNK_SIZE} 字节（修改后需重启本工具）")
    return cfg_path

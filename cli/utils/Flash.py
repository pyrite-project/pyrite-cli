import os
import time
import json
import binascii
import hashlib
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import tempfile
import re
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
import serial
import serial.tools.list_ports

# ANSI 颜色
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_RESET = "\033[0m"

try:
    import tomllib # type: ignore
except ImportError:
    import tomli as tomllib  # type: ignore

CONFIG_FILE = ".pyrite_config.json"
DEFAULT_CHUNK_SIZE = 4096  # 单位:字节

HASH_CONFIG_FILE = "pyrite_file_config.json"
_HASH_VERSION = 1

_DEFAULT_BOARD_TAGS = {
    "ESP32":  ["ESP32", "wifi"],
    "ESP8266": ["ESP8266"],
    "RP2040": ["RP2040"],
    "PICO":   ["RP2040"],
    "STM32":  ["STM32"],
}

ENTER_RAW_REPL = b'\x01'
EXIT_RAW_REPL = b'\x02'
SET_RESET = b'\x03'
SET_EXECUTE = b'\x04'
ENTER_RAW_PASTE = b'\x05'

FLASH = """import sys,micropython

micropython.kbd_intr(-1)
usb = sys.stdin.buffer

f_size = FSIZE
with open(FILE, 'wb') as f:
    while f_size:
        ln = usb.read(min(BFSIZE, f_size))
        if ln:
            f.flush()
            f.write(ln)
            f_size -= len(ln)
micropython.kbd_intr(3)
"""

FLASH_PROGRAM = """import sys, micropython

# 禁用 Ctrl+C 中断，防止数据流中的 0x03 字节误触发设备重启
micropython.kbd_intr(-1)
usb = sys.stdin.buffer

# FILES 格式: [(size, remote_path), ...]，运行前由 PC 端替换
entries = FILES
for file_size, file_path in entries:
    with open(file_path, 'wb') as f:
        remaining = file_size
        while remaining:
            chunk = usb.read(min(remaining, BFSIZE))
            if chunk:
                f.write(chunk)
                remaining -= len(chunk)
# 恢复 Ctrl+C 中断
micropython.kbd_intr(3)
"""

def _load_config():
    """从当前或上级目录加载配置文件，未找到则使用默认值。"""
    cfg = {
        "chunk_size": DEFAULT_CHUNK_SIZE,
        "download_threads": 4,
        "auto_compile": True,
        "verify": "size",
        "max_retries": 2,
        "board_tags": dict(_DEFAULT_BOARD_TAGS),
    }
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        p = parent / CONFIG_FILE
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data.get("chunk_size"), int) and data["chunk_size"] > 0:
                    cfg["chunk_size"] = data["chunk_size"]
                t = data.get("download_threads", 4)
                if isinstance(t, int) and t > 0:
                    cfg["download_threads"] = min(t, 12)
                if isinstance(data.get("auto_compile"), bool):
                    cfg["auto_compile"] = data["auto_compile"]
                v = data.get("verify", "size")
                if v in ("off", "size", "crc32"):
                    cfg["verify"] = v
                r = data.get("max_retries", 2)
                if isinstance(r, int) and r >= 0:
                    cfg["max_retries"] = r
            except (json.JSONDecodeError, OSError):
                pass
            break
    for parent in [cwd] + list(cwd.parents):
        p = parent / "pyproject.toml"
        if p.exists():
            try:
                data = tomllib.loads(p.read_text(encoding="utf-8"))
                bt = data.get("tool", {}).get("pyrite", {}).get("board_tags", {})
                cfg["board_tags"].update({k.upper(): v for k, v in bt.items()})
            except Exception:
                pass
            break
    return cfg


def _compile_to_mpy(local_path: str, bytecode_ver: int = None, arch: str = None): # type: ignore
    """编译 .py -> .mpy，返回 (tmp_mpy_path, tmp_dir)；失败返回 (None, None)。"""
    tmp_dir = tempfile.mkdtemp()
    out_path = os.path.join(tmp_dir, Path(local_path).stem + ".mpy")
    args = [local_path, "-o", out_path]
    if arch is not None:
        args += [f"-march={arch}"]
    try:
        import mpy_cross
        if bytecode_ver is not None:
            mpy_cross.set_version(micropython=None, bytecode=str(bytecode_ver))
        r = mpy_cross.run(*args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        r.wait(timeout=30)
        if r.returncode == 0:
            return out_path, tmp_dir
        print(f"  {_YELLOW}[WARN]{_RESET} mpy-cross 编译失败，回退到 .py\n"
              f"         {r.stderr.read().decode(errors='replace').strip()}") # type: ignore
    except ImportError:
        print(f"  {_YELLOW}[INFO]{_RESET} 未找到 mpy-cross，跳过编译")
    except Exception as e:
        print(f"  {_YELLOW}[WARN]{_RESET} 编译异常: {e}，回退到 .py")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None, None


def _compile_files_parallel(local_paths: list, bytecode_ver: int = None,
                            arch: str = None, max_workers: int = 4):
    """并行编译多个 .py → .mpy。

    Args:
        local_paths: 本地 .py 路径列表
        bytecode_ver: mpy 字节码版本
        arch: 目标架构
        max_workers: 最大并行数

    Returns:
        dict: {local_path: (mpy_path, tmp_dir)}，编译失败则值为 (None, None)
    """
    if not local_paths:
        return {}
    results = {}
    workers = min(max_workers, len(local_paths))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_compile_to_mpy, lp, bytecode_ver, arch): lp
                   for lp in local_paths}
        for future in as_completed(futures):
            lp = futures[future]
            try:
                results[lp] = future.result()
            except Exception:
                results[lp] = (None, None)
    return results


# ── bytes 协议辅助函数（模块级，供 _read_device_file 使用） ─────

def _grep_size_after_ok(buf: bytes) -> int:
    """在 buf 中查找 OK<size>\\r?\\n 并解析文件大小。"""
    ok = buf.find(b"OK")
    if ok < 0:
        return -1
    after = buf[ok + 2:]
    nl = after.find(b"\n")
    if nl < 0:
        return -1
    try:
        return int(after[:nl].decode().strip())
    except ValueError:
        return -1


def _grep_raw_start(buf: bytes) -> int:
    """返回原始数据在 buf 中的起始下标（跳过 OK<size>\\n）。"""
    ok = buf.find(b"OK")
    if ok < 0:
        return -1
    after = buf[ok + 2:]
    nl = after.find(b"\n")
    if nl < 0:
        return -1
    return ok + 2 + nl + 1


def _extract_raw_bytes(buf: bytes, expected_size: int) -> bytes:
    """从 buf 中提取原始文件数据，去除协议前缀和尾部标记。"""
    raw_start = _grep_raw_start(buf)
    if raw_start < 0:
        raise RuntimeError(f"响应格式错误（未找到 OK）: {buf[:200]!r}")
    if expected_size < 0:
        expected_size = _grep_size_after_ok(buf)
        if expected_size < 0:
            raise RuntimeError(f"无法解析文件大小: {buf[:200]!r}")
    data = buf[raw_start:]
    for trailer in (SET_EXECUTE + b">", SET_EXECUTE + SET_EXECUTE, SET_EXECUTE):
        if data.endswith(trailer):
            data = data[:-len(trailer)]
    if len(data) < expected_size:
        raise RuntimeError(
            f"数据不完整: 期望 {expected_size} 字节, 收到 {len(data)} 字节"
        )
    return data[:expected_size]


def _colorize_repl_output(text, in_error):
    """给 REPL 输出中的 Traceback/Error 添加红色高亮。

    Returns:
        (处理后文本, 是否仍在错误块中)
    """
    if "Traceback" in text:
        idx = text.index("Traceback")
        after = text[idx:]
        m = re.search(r"(?:Error|Exception):[^\r\n]*", after)
        if m:
            colored = after[: m.end()]
            rest = after[m.end() :]
            return text[:idx] + _RED + colored + _RESET + rest, False
        else:
            return text[:idx] + _RED + after, True
    if in_error:
        m = re.search(r"(?:Error|Exception):[^\r\n]*", text)
        if m:
            colored = text[: m.end()]
            rest = text[m.end() :]
            return _RED + colored + _RESET + rest, False
        else:
            return _RED + text + _RESET, True
    return text, False


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
        self.ser = serial.Serial()  # 未打开的空串口对象
        self._in_raw = False     # 是否处于原始 REPL 模式
        self._kbd_set = False    # 是否已设置 kbd_intr(-1)
        self._repl_log_file = None   # REPL 原始数据日志文件

    @staticmethod
    def scan_ports(vid=None, pid=None, keyword=None, require_vid=True):
        """扫描可用串口，可按 VID/PID/描述关键字过滤。默认过滤掉无 VID 的设备。"""
        ports = []
        for p in serial.tools.list_ports.comports():
            if require_vid and p.vid is None:
                continue
            if vid is not None and p.vid != vid:
                continue
            if pid is not None and p.pid != pid:
                continue
            if keyword and keyword.lower() not in (p.description or "").lower():
                continue
            ports.append({
                "device": p.device,
                "description": p.description,
                "hwid": p.hwid,
                "vid": p.vid,
                "pid": p.pid,
                "serial_number": p.serial_number,
            })
        return ports

    def connect(self, port=None, baudrate=None):
        """打开串口连接到设备。

        Args:
            port: 串口号，如 'COM3' 或 '/dev/ttyUSB0'
            baudrate: 波特率，默认 115200

        Returns:
            True 表示连接成功
        """
        if self.ser.is_open:
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
        if self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass

    @property
    def is_connected(self):
        """是否已连接。"""
        return self.ser.is_open

    def _enter_raw_repl(self):
        """切换到原始 REPL 模式（Ctrl+A）。"""
        if self._in_raw:
            return

        # 连发两次 Ctrl+C 中断正在运行的程序
        for _ in range(2):
            self._write(SET_RESET)
            time.sleep(0.1)
        self.ser.reset_input_buffer()

        # Ctrl+A 进入原始 REPL
        self._write(ENTER_RAW_REPL)
        data = self._read_until(b">", timeout=2)

        if b">" not in data:
            # Ctrl+C 未能中断，尝试 Ctrl+D 软重启后再进入
            self._write(SET_EXECUTE)  # Ctrl+D
            time.sleep(0.8)
            self.ser.reset_input_buffer()
            self._write(ENTER_RAW_REPL)
            data = self._read_until(b">", timeout=3)

        if b">" not in data:
            raise RuntimeError(f"无法进入原始 REPL 模式，设备响应: {data[:100]!r}")

        self._in_raw = True

    def _exit_raw_repl(self):
        """退出原始 REPL 回到普通 REPL（Ctrl+B）。"""
        if not self._in_raw:
            return
        try:
            self._write(EXIT_RAW_REPL)
            time.sleep(0.1)
            self.ser.reset_input_buffer()
        except Exception:
            pass
        finally:
            self._in_raw = False

    # ── REPL 原始日志 ──────────────────────────────────────────────
    def _open_repl_log(self):
        """在 ./log/ 下创建 REPL 原始数据日志文件。"""
        log_dir = Path.cwd() / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        log_path = log_dir / f"flash_{ts}.log"
        self._repl_log_file = open(log_path, "w", encoding="utf-8")
        return log_path

    def _close_repl_log(self):
        if self._repl_log_file:
            self._repl_log_file.close()
            self._repl_log_file = None

    @contextmanager
    def _repl_log_ctx(self):
        """日志上下文：无日志文件时自动创建，退出时自动关闭。"""
        if self._repl_log_file:
            yield
        else:
            log_path = self._open_repl_log()
            print(f"  {_GREEN}日志:{_RESET} {log_path}")
            try:
                yield
            finally:
                self._close_repl_log()

    def _drain_rx_log(self):
        """非阻塞读取串口 RX 缓冲中所有数据并记录到日志。"""
        if not self.ser.is_open:
            return
        while self.ser.in_waiting:
            chunk = self.ser.read(self.ser.in_waiting)
            if chunk:
                self._log_repl_data("rx", chunk)

    def _log_repl_data(self, direction, data):
        if not self._repl_log_file:
            return
        ts = time.strftime("%H:%M:%S")
        marker = ">>" if direction == "tx" else "<<"
        text = data.decode("utf-8", errors="replace")
        for c, name in [
            ("\x01", "<RAW>"), ("\x02", "<B>"), ("\x03", "<C>"),
            ("\x04", "<D>"), ("\x05", "<E>"),
        ]:
            text = text.replace(c, name)
        self._repl_log_file.write(f"[{ts}] {marker} {text}\n")
        stripped = text.replace("\n", "").replace("\r", "").replace("\t", "")
        if stripped.strip() == "" and data:
            self._repl_log_file.write(f"[{ts}] {marker} [hex] {data.hex(' ')}\n")
        self._repl_log_file.flush()

    def _write(self, data):
        """写入数据到串口。"""
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._log_repl_data("tx", data)
        self.ser.write(data)

    def _read_until(self, terminator=b"\x04", timeout=None):
        timeout = timeout or self.timeout
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ser.in_waiting:
                chunk = self.ser.read(self.ser.in_waiting)
                buf += chunk
                self._log_repl_data("rx", chunk)
                idx = buf.find(terminator)
                if idx >= 0:
                    buf = buf[:idx + len(terminator)]
                    break
            time.sleep(0.02)
        return buf

    def repl_(self):
        """交互式 MicroPython REPL（串口透传模式）。"""
        # 跨平台非阻塞键盘输入
        try:
            import msvcrt
            win = True
        except ImportError:
            import select
            import termios
            import tty
            win = False

        # 中断运行程序，切换到普通 REPL
        for _ in range(2):
            self._write(SET_RESET)
            time.sleep(0.1)
        self.ser.reset_input_buffer()
        self._write(EXIT_RAW_REPL)
        time.sleep(0.3)
        self.ser.reset_input_buffer()

        print("=== MicroPython REPL ===")
        print()

        old_tty = None
        if not win:
            fd = sys.stdin.fileno()
            old_tty = termios.tcgetattr(fd)
            mode = old_tty[:]
            mode[tty.CC] = mode[tty.CC][:]  # 拷贝 cc 列表避免共享修改
            mode[tty.LFLAG] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
            mode[tty.CC][termios.VMIN] = 1
            mode[tty.CC][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSAFLUSH, mode)

        in_error = False

        try:
            while self.is_connected:
                # ── 串口 → 终端 ──
                while self.ser.in_waiting:
                    chunk = self.ser.read(self.ser.in_waiting)
                    if not chunk:
                        continue
                    text = chunk.decode("utf-8", errors="replace")
                    if not text:
                        continue
                    output, in_error = _colorize_repl_output(text, in_error)
                    sys.stdout.write(output)
                    sys.stdout.flush()

                # ── 键盘 → 串口 ──
                if win:
                    _EXT_KEYS = {
                        b'H': b'\x1b[A',   # Up
                        b'P': b'\x1b[B',   # Down
                        b'M': b'\x1b[C',   # Right
                        b'K': b'\x1b[D',   # Left
                        b'G': b'\x1b[H',   # Home
                        b'O': b'\x1b[F',   # End
                        b'S': b'\x1b[3~',  # Delete
                        b'R': b'\x1b[2~',  # Insert
                        b'I': b'\x1b[5~',  # Page Up
                        b'Q': b'\x1b[6~',  # Page Down
                    }
                    if msvcrt.kbhit():
                        ch = msvcrt.getch()
                        if ch == b'\xe0':  # 扩展键（方向键/编辑键）
                            ch2 = msvcrt.getch()
                            seq = _EXT_KEYS.get(ch2)
                            if seq:
                                self._write(seq)
                        elif ch == b'\x00':  # 功能键（F1-F12 等）
                            msvcrt.getch()
                        else:
                            self._write(ch)
                else:
                    if select.select([sys.stdin], [], [], 0)[0]:
                        buf = os.read(sys.stdin.fileno(), 1)
                        if not buf:
                            break
                        if buf == b'\x03':  # Ctrl+C 退出 REPL
                            break
                        if buf == b'\x1b':  # ESC 开头可能为转义序列
                            for _ in range(16):
                                if select.select([sys.stdin], [], [], 0.02)[0]:
                                    b = os.read(sys.stdin.fileno(), 1)
                                    if not b:
                                        break
                                    buf += b
                                    if b in (b'~',) or (len(buf) >= 2 and b in b'ABCDHPQRS'):
                                        break
                                else:
                                    break
                        self._write(buf)

                time.sleep(0.01)
        except KeyboardInterrupt:
            pass
        finally:
            sys.stdout.buffer.write(b'\r\n')
            sys.stdout.buffer.flush()
            if old_tty is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_tty)
                except Exception:
                    pass

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
        self._write(SET_EXECUTE)  # Ctrl+D 执行

        resp = self._read_until(SET_EXECUTE, timeout=timeout)
        # 去掉尾部的 \x04
        resp = resp.rstrip(SET_EXECUTE)
        text = resp.decode("utf-8", errors="replace")
        if text.startswith("OK"):
            text = text[2:]
        text = text.strip()

        if "Traceback" in text:
            raise RuntimeError(f"设备执行错误:\n{text}")

        return text

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

    @staticmethod
    def _compute_crc32(data: bytes) -> int:
        """计算数据的 CRC32 校验值。"""
        import binascii
        return binascii.crc32(data) & 0xffffffff

    def _verify_file_on_device(self, remote_path: str, expected_size: int,
                                verify_mode: str = "size",
                                expected_crc: int | None = None) -> bool:
        """验证设备上的文件与预期一致。

        在原始 REPL 中执行验证命令，比较文件大小和可选的 CRC32。

        Args:
            remote_path: 设备上的文件路径
            expected_size: 期望的文件大小（字节）
            verify_mode: 校验模式 — "size" 或 "crc32"
            expected_crc: 期望的 CRC32 值（verify_mode="crc32" 时必需）

        Returns:
            True 表示校验通过，False 表示失败
        """
        try:
            remote_lit = repr(remote_path)
            out = self._execute(
                f"import os; print(os.stat({remote_lit})[6])", timeout=5
            )
            actual_size = int(out.strip().splitlines()[-1])
        except Exception as e:
            print(f"  {_RED}文件大小校验失败: {e}{_RESET}")
            return False

        if actual_size != expected_size:
            print(f"  {_RED}大小不匹配: 期望 {expected_size} 字节, 实际 {actual_size} 字节{_RESET}")
            return False

        if verify_mode == "crc32" and expected_crc is not None:
            try:
                crc_out = self._execute(
                    "import ubinascii\n"
                    f"with open({remote_lit},'rb') as f:\n"
                    " print(ubinascii.crc32(f.read()))",
                    timeout=15,
                )
                actual_crc = int(crc_out.strip().splitlines()[-1]) & 0xffffffff
                if actual_crc != expected_crc:
                    print(f"  {_RED}CRC32 不匹配: 期望 {expected_crc:#x}, 实际 {actual_crc:#x}{_RESET}")
                    return False
                print(f"  {_GREEN}CRC32 校验通过{_RESET}")
            except Exception as e:
                print(f"  {_YELLOW}[WARN]{_RESET} CRC32 校验不可用 ({e})，仅验证文件大小")

        return True

    def flash_file(self, local_path, remote_path=None, compile=None, bytecode_ver=None, arch=None, active_tags=None):
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"本地文件不存在: {local_path}")

        should_compile = self.config["auto_compile"] if compile is None else compile
        tmp_dirs = []
        actual_local = local_path
        actual_remote = (remote_path or os.path.basename(local_path)).replace("\\", "/")

        if active_tags and local_path.endswith(".py"):
            from .preprocessor import preprocess
            pp_dir = tempfile.mkdtemp()
            tmp_dirs.append(pp_dir)
            pp_path = os.path.join(pp_dir, Path(local_path).name)
            Path(pp_path).write_text(
                preprocess(Path(local_path).read_text(encoding="utf-8"), active_tags, local_path),
                encoding="utf-8",
            )
            actual_local = pp_path

        # .pyi、main.py、boot.py 不编译；manifest.py 不上传到设备
        remote_basename = Path(actual_remote).name
        if remote_basename == "manifest.py":
            print(f"  {_YELLOW}[WARN]{_RESET} '{remote_basename}' 是编译所需文件，已跳过刷入")
            return
        if remote_basename in ("main.py", "boot.py"):
            should_compile = False

        if should_compile and actual_local.endswith(".py"):
            mpy_path, tmp_dir = _compile_to_mpy(actual_local, bytecode_ver, arch) # type: ignore
            if mpy_path:
                tmp_dirs.append(tmp_dir)
                actual_local = mpy_path
                actual_remote = actual_remote[:-3] + ".mpy"

        file_size = os.path.getsize(actual_local)
        chunk_size = self.config["chunk_size"]
        verify_mode = self.config.get("verify", "size")
        max_retries = self.config.get("max_retries", 2)

        # 预计算 CRC32（如果需要）
        expected_crc = None
        if verify_mode == "crc32":
            with open(actual_local, "rb") as f:
                expected_crc = self._compute_crc32(f.read())

        print(f"  {_GREEN}刷入:{_RESET} {local_path} -> {actual_remote} ({file_size} 字节, 块大小={chunk_size})")

        try:
            with self._repl_log_ctx():
                for attempt in range(max_retries + 1):
                    self._enter_raw_repl()

                    if attempt > 0:
                        print(f"  {_YELLOW}[RETRY {attempt}/{max_retries}]{_RESET}")

                    try:
                        self._drain_rx_log()
                        self._write(
                            FLASH.replace("FILE", repr(actual_remote))
                            .replace("BFSIZE", str(DEFAULT_CHUNK_SIZE))
                            .replace("FSIZE", str(file_size))
                        )
                        self._write(SET_EXECUTE)
                        time.sleep(0.3)
                        self._drain_rx_log()

                        with open(actual_local, 'rb') as f:
                            for _ in range(0, file_size, DEFAULT_CHUNK_SIZE):
                                self._write(f.read(DEFAULT_CHUNK_SIZE))

                        self._drain_rx_log()

                        # 等待设备返回原始 REPL 提示符，确保刷入完成
                        self._read_until(b">", timeout=5)

                        # ── 刷入后校验 ──
                        if verify_mode != "off":
                            ok = self._verify_file_on_device(
                                actual_remote, file_size, verify_mode, expected_crc
                            )
                            if not ok:
                                raise RuntimeError("校验失败")
                            print(f"  {_GREEN}✓ 刷入成功{_RESET}")
                        else:
                            print(f"  {_GREEN}✓ 刷入成功 (校验已关闭){_RESET}")
                        return  # 成功

                    except Exception as e:
                        if attempt >= max_retries:
                            raise
                        print(f"  {_YELLOW}{e}，准备重试 ({attempt+1}/{max_retries})...{_RESET}")
                        try:
                            self._execute("f.close()", timeout=2)
                        except Exception:
                            pass
        finally:
            for d in tmp_dirs:
                shutil.rmtree(d, ignore_errors=True)

    def flash_program(self, local_dir, remote_prefix="", bytecode_ver=None, arch=None, active_tags=None, manifest_path=None):
        if not os.path.isdir(local_dir):
            raise NotADirectoryError(f"不是有效目录: {local_dir}")

        # ── 收集文件清单 ──
        if manifest_path:
            from .manifest_loader import load_manifest
            entries = load_manifest(manifest_path, active_tags or set(), base_dir=local_dir)
        else:
            entries = []
            for root, _dirs, files in os.walk(local_dir):
                for fn in files:
                    if not fn.endswith(".py"):
                        continue
                    lp = os.path.join(root, fn)
                    rp = os.path.join(remote_prefix, os.path.relpath(lp, local_dir)).replace("\\", "/")
                    entries.append((lp, rp))

        # ── 预处理、编译、读取内容 ──
        tmp_dirs = []
        file_list = []    # [(remote_path, local_compiled_path, size)]
        file_meta = []    # [(size, remote_path), ...] → 替换到 FLASH_PROGRAM
        all_data = b""
        verify_mode = self.config.get("verify", "size")
        expected_crcs = {}   # {remote_path: crc32} 用于校验

        # 第一轮：预处理，收集编译候选
        prep = []  # [(actual_local, actual_remote, needs_compile)]
        for lp, rp in entries:
            if Path(rp).name == "manifest.py" or lp.endswith(".pyi"):
                continue
            actual_local = lp
            actual_remote = rp
            # 条件编译预处理
            if active_tags and actual_local.endswith(".py"):
                from .preprocessor import preprocess
                pp_dir = tempfile.mkdtemp()
                tmp_dirs.append(pp_dir)
                pp_path = os.path.join(pp_dir, Path(actual_local).name)
                Path(pp_path).write_text(
                    preprocess(Path(actual_local).read_text(encoding="utf-8"), active_tags, actual_local),
                    encoding="utf-8",
                )
                actual_local = pp_path
            basename = Path(actual_remote).name
            needs_compile = (
                self.config["auto_compile"]
                and basename not in ("main.py", "boot.py")
                and actual_local.endswith(".py")
            )
            prep.append((actual_local, actual_remote, needs_compile))

        # 第二轮：并行编译
        compile_jobs = [al for al, _, needs in prep if needs]
        compiled = _compile_files_parallel(compile_jobs, bytecode_ver, arch)

        # 第三轮：读取编译结果并构建文件列表
        for actual_local, actual_remote, needs_compile in prep:
            if needs_compile and actual_local in compiled:
                mpy_path, mpy_tmp_dir = compiled[actual_local]
                if mpy_path:
                    tmp_dirs.append(mpy_tmp_dir)
                    actual_local = mpy_path
                    actual_remote = actual_remote[:-3] + ".mpy"

            with open(actual_local, "rb") as f:
                content = f.read()
            file_list.append((actual_remote, actual_local, len(content)))
            if verify_mode == "crc32":
                expected_crcs[actual_remote] = self._compute_crc32(content)
            all_data += content
            file_meta.append((len(content), actual_remote))

        if not file_list:
            print("  没有需要刷入的文件。")
            return []

        max_retries = self.config.get("max_retries", 2)

        try:
            for attempt in range(max_retries + 1):
                self._enter_raw_repl()

                if attempt > 0:
                    print(f"  {_YELLOW}[RETRY {attempt}/{max_retries}]{_RESET}")

                # ── 批量创建设备目录（一次执行，N-1 次往返节省） ──
                dirs = sorted({os.path.dirname(rp) for rp, _, _ in file_list if os.path.dirname(rp)})
                if dirs:
                    mkdir_cmds = (
                        "import os\n"
                        f"for d in {dirs!r}:\n"
                        "    try:\n"
                        "        os.mkdir(d)\n"
                        "    except OSError:\n"
                        "        pass\n"
                    )
                    self._execute(mkdir_cmds)

                # ── 发送刷入脚本 ──
                script = FLASH_PROGRAM.replace("FILES", repr(file_meta)).replace("BFSIZE", str(DEFAULT_CHUNK_SIZE))

                with self._repl_log_ctx():
                    print(f"  {_GREEN}刷入 {len(file_list)} 个文件:{_RESET}")
                    for rp, lp, sz in file_list:
                        print(f"    {lp} -> {rp} ({sz} 字节)")

                    try:
                        self._write(script.encode() + SET_EXECUTE)
                        time.sleep(0.3)
                        self._drain_rx_log()

                        for offset in range(0, len(all_data), DEFAULT_CHUNK_SIZE):
                            self._write(all_data[offset:offset + DEFAULT_CHUNK_SIZE])

                        self._drain_rx_log()

                        # 等待设备返回原始 REPL 提示符
                        self._read_until(b">", timeout=10)

                        # ── 校验 ──
                        if verify_mode != "off":
                            failed_files = []
                            for rp, lp, sz in file_list:
                                ok = self._verify_file_on_device(
                                    rp, sz, verify_mode, expected_crcs.get(rp)
                                )
                                if not ok:
                                    failed_files.append((lp, rp))

                            if failed_files:
                                if attempt >= max_retries:
                                    print(f"  {_RED}✗ {len(failed_files)} 个文件校验失败，重试耗尽{_RESET}")
                                    return [(lp, rp, False) for (rp, lp, sz) in file_list]

                                print(f"  {_YELLOW}{len(failed_files)} 个文件校验失败，"
                                      f"使用单文件模式逐文件重试...{_RESET}")
                                retry_ok = 0
                                for lp, rp in failed_files:
                                    try:
                                        self.flash_file(
                                            lp, rp, compile=None,
                                            bytecode_ver=bytecode_ver, arch=arch,
                                            active_tags=active_tags,
                                        )
                                        retry_ok += 1
                                    except Exception as e2:
                                        print(f"  {_RED}重试失败 {rp}: {e2}{_RESET}")
                                if retry_ok == len(failed_files):
                                    print(f"  {_GREEN}✓ 全部重试成功{_RESET}")
                                    break  # 成功，退出重试循环
                                # 仍有失败，继续外层重试
                                continue

                        total_size = len(all_data)
                        print(f"  {_GREEN}✓ 刷入成功 ({total_size} 字节, {len(file_list)} 个文件){_RESET}")
                        return [(lp, rp, True) for (rp, lp, sz) in file_list]

                    except Exception as e:
                        if attempt >= max_retries:
                            print(f"  {_RED}✗ 刷入失败: {e}{_RESET}")
                            return [(lp, rp, False) for (rp, lp, sz) in file_list]
                        print(f"  {_YELLOW}刷入过程异常，准备重试 ({attempt+1}/{max_retries})...{_RESET}")
        finally:
            for d in tmp_dirs:
                shutil.rmtree(d, ignore_errors=True)

    def get_mpy_version(self) -> tuple[int, str] | tuple[None, None]:
        """从设备读取 mpy 字节码版本号和架构，返回 (ver, arch) 或 (None, None)。"""
        try:
            out = self.run(
                "import sys\n"
                "m=sys.implementation.mpy\n"
                "a=[None,'x86','x64','armv6','armv6m','armv7m','armv7em','armv7emsp','armv7emdp','xtensa','xtensawin'][m>>10]\n"
                "print(m&0xff,a or '')"
            )
            parts = out.strip().split()
            ver = int(parts[0])
            arch = parts[1] if len(parts) > 1 and parts[1] else None
            return ver, arch # type: ignore
        except Exception:
            return None, None

    def detect_tags(self) -> set:
        """从设备读取 board 信息，返回 active_tags 集合。"""
        try:
            out = self.run("import os,sys\nprint(os.uname().machine)\nprint(sys.platform)")
        except Exception:
            return set()
        lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
        combined = " ".join(lines).upper()
        board_tags = self.config["board_tags"]
        tags = set()
        for kw, tag_list in board_tags.items():
            if kw in combined:
                tags.update(tag_list)
                break
        if len(lines) > 1:
            tags.add(lines[1].upper())
        return tags

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

    # ── 哈希与增量刷入 ──────────────────────────────────────────

    @staticmethod
    def _compute_file_hash(filepath: str) -> str:
        """计算文件的 SHA256 哈希值。"""
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(1048576)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _collect_project_files(self, local_dir: str, active_tags: set = None,
                                manifest_path: str = None):
        """收集项目中可刷入的文件列表（与 flash_program 规则一致）。

        Returns:
            list of (local_abs_path, local_rel_remote)
            无 manifest 时第二项为相对于 local_dir 的路径；
            有 manifest 时第二项为 manifest 中指定的 remote 路径（或无 remote 时取文件名）。
        """
        if manifest_path:
            from .manifest_loader import load_manifest
            entries = load_manifest(manifest_path, active_tags or set(), base_dir=local_dir)
        else:
            entries = []
            for root, _dirs, files in os.walk(local_dir):
                for fn in files:
                    if not fn.endswith(".py"):
                        continue
                    lp = os.path.join(root, fn)
                    rp = os.path.relpath(lp, local_dir).replace("\\", "/")
                    entries.append((lp, rp))
        # 过滤 manifest.py 和 .pyi
        return [(lp, rp) for lp, rp in entries
                if Path(rp).name != "manifest.py" and not lp.endswith(".pyi")]

    def project_scan(self, local_dir: str, hash_config_path: str = None,
                     active_tags: set = None, manifest_path: str = None):
        """扫描项目，计算所有可刷入文件的 SHA256 哈希并保存到配置文件。

        Args:
            local_dir: 项目目录路径
            hash_config_path: 哈希配置文件路径，默认 local_dir/pyrite_file_config.json
            active_tags: 条件编译 tags
            manifest_path: manifest.py 路径

        Returns:
            配置文件路径
        """
        if hash_config_path is None:
            hash_config_path = os.path.join(local_dir, HASH_CONFIG_FILE)

        entries = self._collect_project_files(local_dir, active_tags, manifest_path)

        file_hashes = {}
        for lp, _rp in entries:
            rel_path = os.path.relpath(lp, local_dir).replace("\\", "/")
            file_hashes[rel_path] = self._compute_file_hash(lp)

        config = {
            "version": _HASH_VERSION,
            "hash_algorithm": "sha256",
            "files": file_hashes,
        }
        with open(hash_config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

        print(f"  {_GREEN}项目文件哈希已保存:{_RESET} {hash_config_path}")
        print(f"  {_GREEN}共 {len(file_hashes)} 个文件{_RESET}")
        for rel_path in sorted(file_hashes):
            print(f"    {rel_path}")
        return hash_config_path

    def project_flash(self, local_dir: str, remote_prefix: str,
                      hash_config_path: str = None,
                      bytecode_ver: int = None, arch: str = None,
                      active_tags: set = None, manifest_path: str = None):
        """根据哈希配置，仅刷入新增或已更改的文件。

        需先调用 connect() 建立串口连接。

        Args:
            local_dir: 本地项目目录
            remote_prefix: 设备上的远程路径前缀
            hash_config_path: 哈希配置路径，默认 local_dir/pyrite_file_config.json
            bytecode_ver: mpy 字节码版本（自动检测）
            arch: 目标架构（自动检测）
            active_tags: 条件编译 tags
            manifest_path: manifest.py 路径

        Returns:
            list of (local_path, remote_path, success)
        """
        if hash_config_path is None:
            hash_config_path = os.path.join(local_dir, HASH_CONFIG_FILE)

        # 加载已有哈希配置
        if os.path.exists(hash_config_path):
            with open(hash_config_path, "r", encoding="utf-8") as f:
                stored_config = json.load(f)
            stored_hashes = stored_config.get("files", {})
        else:
            print(f"  {_YELLOW}[WARN]{_RESET} 未找到哈希配置文件，将全量刷入")
            stored_hashes = {}

        # 扫描当前项目文件
        entries = self._collect_project_files(local_dir, active_tags, manifest_path)
        if not entries:
            print("  没有需要刷入的文件。")
            return []

        # 计算当前哈希并比对
        changed = []        # [(local_abs_path, remote_path)]
        unchanged_count = 0
        current_hashes = {}

        for lp, rp_part in entries:
            rel_path = os.path.relpath(lp, local_dir).replace("\\", "/")
            cur_hash = self._compute_file_hash(lp)
            current_hashes[rel_path] = cur_hash

            remote_path = os.path.join(remote_prefix, rp_part).replace("\\", "/")

            stored = stored_hashes.get(rel_path)
            if stored is None:
                changed.append((lp, remote_path, "新增"))
            elif stored != cur_hash:
                changed.append((lp, remote_path, "已更改"))
            else:
                unchanged_count += 1

        # 报告已删除文件
        removed = [k for k in stored_hashes if k not in current_hashes]
        if removed:
            print(f"  {_YELLOW}[INFO]{_RESET} {len(removed)} 个文件已从项目中移除（将从配置中清除）")
            for rf in sorted(removed):
                print(f"    - {rf}")

        if not changed:
            print(f"  {_GREEN}所有文件均未更改 ({unchanged_count} 个文件)，无需刷入{_RESET}")
            return [(lp, os.path.join(remote_prefix, rp_part).replace("\\", "/"), True)
                    for lp, rp_part in entries]

        print(f"  {_GREEN}需要刷入 {len(changed)} 个文件:{_RESET}")
        for lp, rp, reason in changed:
            print(f"    [{reason}] {os.path.relpath(lp, local_dir)} -> {rp}")
        if unchanged_count:
            print(f"  {_GREEN}{unchanged_count} 个文件未更改，跳过{_RESET}")

        # 逐个刷入变更文件
        results = []
        ok = 0
        fail = 0

        for lp, remote_path, _reason in changed:
            print("")
            try:
                self.flash_file(
                    lp, remote_path,
                    compile=None,
                    bytecode_ver=bytecode_ver,
                    arch=arch,
                    active_tags=active_tags,
                )
                results.append((lp, remote_path, True))
                ok += 1
            except Exception as e:
                print(f"  {_RED}刷入失败: {e}{_RESET}")
                results.append((lp, remote_path, False))
                fail += 1

        # 更新哈希配置（仅成功刷入的文件）
        if ok > 0:
            updated = {}
            for lp, rp_part in entries:
                rel_path = os.path.relpath(lp, local_dir).replace("\\", "/")
                # 仅当文件成功刷入或用的是旧哈希（未变更文件）时保留
                was_flashed_ok = any(
                    lp == flp and success
                    for flp, _frp, success in results
                )
                if was_flashed_ok:
                    updated[rel_path] = current_hashes[rel_path]
                elif rel_path in stored_hashes:
                    updated[rel_path] = stored_hashes[rel_path]
                elif rel_path in current_hashes:
                    updated[rel_path] = current_hashes[rel_path]

            with open(hash_config_path, "w", encoding="utf-8") as f:
                json.dump({
                    "version": _HASH_VERSION,
                    "hash_algorithm": "sha256",
                    "files": updated,
                }, f, indent=2, ensure_ascii=False)

            print(f"\n  {_GREEN}哈希配置已更新:{_RESET} {hash_config_path}")

        parts = []
        if ok:
            parts.append(f"\033[32m{ok} 成功\033[0m")
        if fail:
            parts.append(f"\033[31m{fail} 失败\033[0m")
        print(f"\n增量刷入完成: {', '.join(parts)}")
        return results

    # ── 设备文件读取（bytes 协议） ───────────────────────────────
    def _read_device_file(self, remote_path: str) -> bytes:
        """从设备读取文件内容（原始字节传输，兼容二进制）。

        协议：设备先输出文件大小（文本行），再通过 stdout.buffer 输出原始字节。
        PC 端读取全部串口数据后解析，不受 \\x04 等字节干扰。
        """
        self._enter_raw_repl()
        # 用 sys.stdout.buffer 直接输出字节（避免 TextIOWrapper 兼容问题）
        # _out 缓存引用跳过 sys.stdout 的 TextIOWrapper
        script = (
            "import os,sys\n"
            f"p={remote_path!r}\n"
            "sz=os.stat(p)[6]\n"
            "_out=sys.stdout.buffer\n"
            "_out.write(str(sz).encode()+b'\\n')\n"
            "with open(p,'rb') as f:\n"
            " while True:\n"
            "  c=f.read(512)\n"
            "  if not c:break\n"
            "  _out.write(c)\n"
        )
        self._write(script.encode() + SET_EXECUTE)
        time.sleep(0.3)

        # MicroPython 原始 REPL 输出格式: "OK<size>\\r\\n<raw_bytes>\\x04\\x04>"
        # OK 后没有换行，直接跟 size 数字
        buf = b""
        deadline = time.time() + 30
        size = -1
        while time.time() < deadline:
            if self.ser.in_waiting:
                buf += self.ser.read(self.ser.in_waiting)
                if size < 0:
                    size = _grep_size_after_ok(buf)
                if size >= 0:
                    raw_start = _grep_raw_start(buf)
                    if raw_start >= 0 and len(buf) - raw_start >= size:
                        time.sleep(0.05)
                        buf += self.ser.read(self.ser.in_waiting)
                        break
            else:
                time.sleep(0.02)

        return _extract_raw_bytes(buf, size)

    def _check_device_files(self, remote_paths: list) -> dict:
        """批量检查设备文件存在性和大小。

        Returns:
            dict: {remote_path: size}，不存在的文件 size 为 -1
        """
        if not remote_paths:
            return {}
        paths_repr = repr(remote_paths)
        script = (
            "import os\n"
            "r=[]\n"
            f"for p in {paths_repr}:\n"
            " try:\n"
            "  r.append(str(os.stat(p)[6]))\n"
            " except OSError:\n"
            "  r.append('-')\n"
            "print(','.join(r))\n"
        )
        out = self.run(script)
        sizes = out.strip().split(',')
        result = {}
        for i, rp in enumerate(remote_paths):
            if i < len(sizes) and sizes[i] != '-':
                result[rp] = int(sizes[i])
            else:
                result[rp] = -1
        return result

    # ── project status / pull ────────────────────────────────────

    def project_status(self, local_dir: str, remote_prefix: str,
                       hash_config_path: str = None,
                       active_tags: set = None, manifest_path: str = None):
        """比对本地哈希和设备端文件，显示差异清单（不刷入）。

        需先调用 connect() 建立串口连接。
        """
        if hash_config_path is None:
            hash_config_path = os.path.join(local_dir, HASH_CONFIG_FILE)

        # 加载哈希配置
        if os.path.exists(hash_config_path):
            with open(hash_config_path, "r", encoding="utf-8") as f:
                stored = json.load(f).get("files", {})
        else:
            stored = {}

        # 扫描本地文件
        entries = self._collect_project_files(local_dir, active_tags, manifest_path)
        if not entries:
            print("  没有可刷入的文件。")
            return

        current_hashes = {}
        remote_paths = []
        local_map = {}  # {remote_path: local_rel_path}
        for lp, rp_part in entries:
            rel = os.path.relpath(lp, local_dir).replace("\\", "/")
            remote = os.path.join(remote_prefix, rp_part).replace("\\", "/")
            current_hashes[remote] = self._compute_file_hash(lp)
            remote_paths.append(remote)
            local_map[remote] = rel

        # 查询设备端文件
        dev_sizes = self._check_device_files(remote_paths)

        # 构建差异列表
        added = []       # 本地有，设备无
        changed = []     # 哈希不同
        removed = []     # 配置有，本地无
        ok_count = 0

        for rp in remote_paths:
            rel = local_map[rp]
            cur_hash = current_hashes.get(rp)
            old_hash = stored.get(rel)
            dev_size = dev_sizes.get(rp, -1)
            if dev_size < 0:
                added.append((rel, rp))
            elif old_hash is not None and cur_hash != old_hash:
                changed.append((rel, rp))
            elif old_hash is None:
                added.append((rel, rp))
            else:
                ok_count += 1

        for rel in stored:
            if rel not in current_hashes.values() and rel not in [local_map[r] for r in remote_paths]:
                # Actually check local_map by rel
                pass
        for rel in stored:
            if rel not in [local_map[r] for r in remote_paths]:
                removed.append(rel)

        # 打印差异清单
        header = f"{'状态':6}  {'本地文件':40}  {'设备路径':40}"
        sep = f"{'──':6}  {'─'*40}  {'─'*40}"
        print(f"\n  {header}")
        print(f"  {sep}")

        for rel, rp in added:
            print(f"  {_YELLOW}[ADD]{_RESET}  {rel:<40}  {rp:<40}")
        for rel, rp in changed:
            print(f"  {_YELLOW}[MOD]{_RESET}  {rel:<40}  {rp:<40}")
        for rel in removed:
            print(f"  {_RED}[DEL]{_RESET}  {rel:<40}  {'(不在项目中)':40}")

        if not added and not changed and not removed:
            print(f"  {_GREEN}所有文件一致 ({ok_count} 个文件){_RESET}")
        else:
            print(f"  {_GREEN}一致: {ok_count}{_RESET}  "
                  f"{_YELLOW}新增: {len(added)}{_RESET}  "
                  f"{_YELLOW}变更: {len(changed)}{_RESET}  "
                  f"{_RED}删除: {len(removed)}{_RESET}")
        print()

    def _discover_device_files(self, remote_prefix: str) -> list:
        """递归发现设备上的所有文件，返回 [(full_remote_path, size), ...]。

        设备端逐行输出 size|path，主机端按行解析，避免 eval。
        """
        script = (
            "import os\n"
            "def _walk(d):\n"
            " for n in os.listdir(d):\n"
            "  fp=(d+'/'+n).replace('//','/')\n"
            "  try:s=os.stat(fp)\n"
            "  except:continue\n"
            "  if s[0]&0x4000:\n"
            "   _walk(fp)\n"
            "  else:\n"
            "   print(str(s[6])+'|'+fp)\n"
            f"_walk({remote_prefix!r})\n"
        )
        out = self.run(script)
        files = []
        for line in out.strip().splitlines():
            line = line.strip()
            if '|' in line:
                sz, _, fp = line.partition('|')
                if sz.isdigit():
                    files.append((fp, int(sz)))
        return files

    def project_pull(self, local_dir: str, remote_prefix: str,
                     hash_config_path: str = None,
                     active_tags: set = None, manifest_path: str = None,
                     dry_run: bool = False):
        """从设备下载文件到本地（批量传输）。

        类似 flash_program 的批处理逻辑：
        一次收集所有文件大小，设备将全部文件内容拼接输出，
        主机端按文件大小分割并写入本地文件。

        如果本地目录为空或不存在，自动从设备递归发现文件。
        需先调用 connect() 建立串口连接。
        """
        # 尝试从本地项目收集文件清单
        entries = self._collect_project_files(local_dir, active_tags, manifest_path)
        from_device = False

        if not entries:
            # 本地无文件清单 → 从设备递归发现
            print(f"  {_YELLOW}[INFO]{_RESET} 本地目录为空，从设备发现文件...")
            dev_files = self._discover_device_files(remote_prefix)
            if not dev_files:
                print(f"  {_YELLOW}[INFO]{_RESET} 设备上未发现文件。")
                return
            from_device = True
            entries = []
            for rp, sz in dev_files:
                # 计算本地相对路径：去掉 remote_prefix 前缀
                rel = rp[len(remote_prefix):].lstrip('/') if rp.startswith(remote_prefix) else rp.lstrip('/')
                lp = os.path.join(local_dir, rel).replace("\\", "/")
                entries.append((lp, rel))

        # 构建远程文件路径列表
        remote_files = []
        local_paths = []
        for lp, rp_part in entries:
            remote = os.path.join(remote_prefix, rp_part).replace("\\", "/")
            remote_files.append(remote)
            local_paths.append(lp)

        if dry_run:
            print(f"  {_YELLOW}[PREVIEW]{_RESET} 将下载 {len(remote_files)} 个文件:")
            for rp, lp in zip(remote_files, local_paths):
                print(f"    {rp} -> {lp}")
            return

        # ── 批量获取：一次性发送脚本，设备输出所有文件大小 + 拼接内容 ──
        self._enter_raw_repl()
        script = (
            "import os,sys\n"
            "_out=sys.stdout.buffer\n"
            f"files={remote_files!r}\n"
            "sizes=[]\n"
            "for f in files:\n"
            " try:\n"
            "  sizes.append(os.stat(f)[6])\n"
            " except:\n"
            "  sizes.append(-1)\n"
            "_out.write(b'SZ:'+repr(sizes).encode()+b'\\n')\n"
            "for i,f in enumerate(files):\n"
            " if sizes[i]>=0:\n"
            "  with open(f,'rb') as fp:\n"
            "   while True:\n"
            "    c=fp.read(512)\n"
            "    if not c:break\n"
            "    _out.write(c)\n"
        )
        self._write(script.encode() + SET_EXECUTE)
        time.sleep(0.3)

        # ── 读取设备返回 ──
        buf = b""
        deadline = time.time() + max(30, len(remote_files) * 8)
        sizes = []
        expected_total = -1
        raw_start = -1

        while time.time() < deadline:
            if self.ser.in_waiting:
                buf += self.ser.read(self.ser.in_waiting)
                if expected_total < 0:
                    sz_marker = buf.find(b"SZ:")
                    if sz_marker >= 0:
                        nl = buf.find(b"\n", sz_marker)
                        if nl >= 0:
                            try:
                                sizes = eval(buf[sz_marker + 3:nl])
                                expected_total = sum(s for s in sizes if s >= 0)
                                raw_start = nl + 1
                            except Exception:
                                pass
                if expected_total >= 0:
                    raw_len = len(buf) - raw_start
                    # 去除尾部协议标记后判断是否收足
                    raw = buf[raw_start:]
                    for trailer in (SET_EXECUTE + b">", SET_EXECUTE + SET_EXECUTE, SET_EXECUTE):
                        if raw.endswith(trailer):
                            raw = raw[:-len(trailer)]
                    if len(raw) >= expected_total:
                        time.sleep(0.05)
                        buf += self.ser.read(self.ser.in_waiting)
                        break
            else:
                time.sleep(0.02)

        if expected_total < 0:
            print(f"  {_RED}[ERROR]{_RESET} 无法获取文件大小信息")
            return

        # ── 解析原始数据 ──
        raw = buf[raw_start:]
        for trailer in (SET_EXECUTE + b">", SET_EXECUTE + SET_EXECUTE, SET_EXECUTE):
            if raw.endswith(trailer):
                raw = raw[:-len(trailer)]

        if len(raw) < expected_total:
            print(f"  {_RED}[ERROR]{_RESET} 数据不完整: 期望 {expected_total} 字节, 收到 {len(raw)} 字节")
            return

        raw = raw[:expected_total]

        # ── 按大小分割并写入本地文件 ──
        ok = fail = 0
        offset = 0
        for i, (lp, size) in enumerate(zip(local_paths, sizes)):
            if size < 0:
                print(f"  {_YELLOW}[SKIP]{_RESET} {remote_files[i]} (设备上不存在)")
                fail += 1
                continue
            file_data = raw[offset:offset + size]
            offset += size
            try:
                os.makedirs(os.path.dirname(lp) or '.', exist_ok=True)
                with open(lp, "wb") as f:
                    f.write(file_data)
                print(f"  {_GREEN}✓{_RESET} {remote_files[i]} -> {lp} ({size} 字节)")
                ok += 1
            except Exception as e:
                print(f"  {_RED}✗{_RESET} {remote_files[i]} -> {lp}: {e}")
                fail += 1

        print(f"\n  {_GREEN}下载完成: {ok} 成功{_RESET}", end="")
        if fail:
            print(f"  {_RED}{fail} 失败{_RESET}", end="")
        print()

    # ── 设备文件管理 (fs) ─────────────────────────────────────────

    def fs_ls(self, remote_path: str = "/") -> list:
        """列出设备目录下的文件和子目录。"""
        script = (
            "import os\n"
            "def _st(p):\n"
            " s=os.stat(p); s=os.stat(p)\n"
            " return s\n"
            f"p={remote_path!r}\n"
            "for n in sorted(os.listdir(p or '/')):\n"
            " try:\n"
            "  s=_st((p+'/'+n).replace('//','/'))\n"
            "  print(str(s[6])+'|'+('D' if s[0]&0x4000 else 'F')+'|'+n)\n"
            " except OSError:\n"
            "  print('?|?|'+n)\n"
        )
        out = self.run(script)
        items = []
        for line in out.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split('|', 2)
            if len(parts) == 3:
                items.append({'size': parts[0], 'type': parts[1], 'name': parts[2]})
        return items

    def fs_df(self) -> dict:
        """获取设备文件系统使用情况。"""
        script = (
            "import os\n"
            "s=os.statvfs('/')\n"
            "print(str(s[0]*s[2])+'|'+str(s[0]*(s[2]-s[3]))+'|'+str(s[0]*s[3]))\n"
        )
        out = self.run(script)
        for line in out.strip().splitlines():
            line = line.strip()
            parts = line.split('|')
            if len(parts) == 3:
                return {
                    'total': int(parts[0]),
                    'used': int(parts[1]),
                    'free': int(parts[2]),
                }
        return {'total': 0, 'used': 0, 'free': 0}

    def fs_rm(self, remote_path: str) -> bool:
        """删除设备上的文件或空目录。"""
        script = (
            "import os\n"
            f"os.remove({remote_path!r})\n"
            "print('OK')\n"
        )
        out = self.run(script)
        return 'OK' in out

    def fs_cat(self, remote_path: str) -> str:
        """读取设备上文本文件的内容。"""
        script = f"print(open({remote_path!r}).read())\n"
        return self.run(script)

    def fs_get(self, remote_path: str, local_path: str) -> int:
        """从设备下载文件到本地路径。返回文件大小（字节）。"""
        data = self._read_device_file(remote_path)
        os.makedirs(os.path.dirname(local_path) or '.', exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(data)
        return len(data)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.disconnect()

def create_default_config():
    """在工作目录创建默认配置文件。"""
    cfg_path = Path.cwd() / CONFIG_FILE
    cfg_path.write_text(
        json.dumps({
            "chunk_size": DEFAULT_CHUNK_SIZE,
            "download_threads": 4,
            "auto_compile": True,
            "verify": "size",
            "max_retries": 2,
        }, indent=2),
        encoding="utf-8",
    )
    print(f"默认配置文件已创建: {cfg_path}")
    print(f"  chunk_size = {DEFAULT_CHUNK_SIZE} 字节（修改后需重启本工具）")
    print("  download_threads = 4（存根下载线程数，范围 1~12）")
    print("  auto_compile = true（自动编译 .py -> .mpy，设为 false 可关闭）")
    print('  verify = "size"（校验模式：off=不校验, size=文件大小, crc32=文件大小+CRC32）')
    print("  max_retries = 2（校验失败时最大重试次数，设为 0 关闭重试）")
    return cfg_path

import os
import time
import json
import subprocess
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
with open("FILE", 'wb') as f:
    while f_size:
        ln = usb.read(BFSIZE)
        if ln:
            f.flush()
            f.write(ln)
            f_size -= len(ln)
            print(f_size)
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
        print(f"  {_YELLOW}[警告]{_RESET} mpy-cross 编译失败，回退到 .py\n"
              f"         {r.stderr.read().decode(errors='replace').strip()}") # type: ignore
    except ImportError:
        print(f"  {_YELLOW}[提示]{_RESET} 未找到 mpy-cross，跳过编译")
    except Exception as e:
        print(f"  {_YELLOW}[警告]{_RESET} 编译异常: {e}，回退到 .py")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None, None


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
        self.ser = None          # pySerial 对象
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

    def _enter_raw_repl(self):
        """切换到原始 REPL 模式（Ctrl+A）。"""
        if self._in_raw:
            return

        # 连发两次 Ctrl+C 中断正在运行的程序
        for _ in range(2):
            self._write(SET_RESET)
            time.sleep(0.1)
        self.ser.reset_input_buffer() # type: ignore

        # Ctrl+A 进入原始 REPL
        self._write(ENTER_RAW_REPL)
        data = self._read_until(b">", timeout=2)

        if b">" not in data:
            # Ctrl+C 未能中断，尝试 Ctrl+D 软重启后再进入
            self._write(SET_EXECUTE)  # Ctrl+D
            time.sleep(0.8)
            self.ser.reset_input_buffer() # type: ignore
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
            self.ser.reset_input_buffer() # type: ignore
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
        if not self.ser or not self.ser.is_open:
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
        self.ser.write(data) # type: ignore

    def _read_until(self, terminator=b"\x04", timeout=None):
        timeout = timeout or self.timeout
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ser.in_waiting: # type: ignore
                chunk = self.ser.read(self.ser.in_waiting) # type: ignore
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

        print(f"  {_GREEN}刷入:{_RESET} {local_path} -> {actual_remote} ({file_size} 字节, 块大小={chunk_size})")

        with self._repl_log_ctx():
            try:
                self._enter_raw_repl()

                self._drain_rx_log()
                self._write(FLASH.replace("FILE", actual_remote).replace("BFSIZE", str(DEFAULT_CHUNK_SIZE)).replace("FSIZE", str(file_size)))
                self._write(SET_EXECUTE)
                time.sleep(0.3)
                self._drain_rx_log()

                with open(actual_local, 'rb') as f:
                    for _ in range(0, file_size, DEFAULT_CHUNK_SIZE):
                        self._write(f.read(DEFAULT_CHUNK_SIZE))

                self._drain_rx_log()
                print(f"  {_GREEN}✓ 刷入成功{_RESET}")

            except Exception:
                try:
                    self._execute("f.close()", timeout=2)
                except Exception:
                    pass
                raise
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

        # ── 预处理、预编译、读取内容（一轮遍历） ──
        tmp_dirs = []
        file_list = []    # [(remote_path, local_compiled_path, size)]
        file_meta = []    # [(size, remote_path), ...] → 替换到 FLASH_PROGRAM
        all_data = b""

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

            # 编译 .py → .mpy（跳过启动文件）
            basename = Path(actual_remote).name
            should_compile = self.config["auto_compile"] and basename not in ("main.py", "boot.py")
            if should_compile and actual_local.endswith(".py"):
                mpy_path, mpy_tmp_dir = _compile_to_mpy(actual_local, bytecode_ver, arch) # type: ignore
                if mpy_path:
                    tmp_dirs.append(mpy_tmp_dir)
                    actual_local = mpy_path
                    actual_remote = actual_remote[:-3] + ".mpy"

            with open(actual_local, "rb") as f:
                content = f.read()
            file_list.append((actual_remote, actual_local, len(content)))
            all_data += content
            file_meta.append((len(content), actual_remote))

        if not file_list:
            print("  没有需要刷入的文件。")
            return []

        # ── 进入原始 REPL ──
        if not self._in_raw:
            self._enter_raw_repl()

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

                total_size = len(all_data)
                print(f"  {_GREEN}✓ 刷入成功 ({total_size} 字节, {len(file_list)} 个文件){_RESET}")
                return [(lp, rp, True) for (rp, lp, sz) in file_list]

            except Exception as e:
                print(f"  {_RED}✗ 刷入失败: {e}{_RESET}")
                return [(lp, rp, False) for (rp, lp, sz) in file_list]
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
        }, indent=2),
        encoding="utf-8",
    )
    print(f"默认配置文件已创建: {cfg_path}")
    print(f"  chunk_size = {DEFAULT_CHUNK_SIZE} 字节（修改后需重启本工具）")
    print("  download_threads = 4（存根下载线程数，范围 1~12）")
    print("  auto_compile = true（自动编译 .py -> .mpy，设为 false 可关闭）")
    return cfg_path

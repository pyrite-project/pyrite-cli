import os
import time
import binascii
import tempfile
import re
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import serial
import serial.tools.list_ports

from .ansi import _GREEN, _YELLOW, _RED, _RESET
from .config import _load_config, DEFAULT_CHUNK_SIZE
from .types import PyriteConfig
from .transport import Transport
from .serial_transport import SerialTransport
from .compiler import _compile_to_mpy, _compile_files_parallel

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

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

# ── 协议辅助函数（模块级） ──────────────────────────────────

def _strip_repl_trailer(buf: bytes) -> bytes:
    """去除原始 REPL 响应尾部的 \\x04\\x04>、\\x04\\x04、\\x04 等协议标记。"""
    for trailer in (SET_EXECUTE + b">", SET_EXECUTE + SET_EXECUTE, SET_EXECUTE):
        if buf.endswith(trailer):
            buf = buf[:-len(trailer)]
    return buf


def _colorize_repl_output(text, in_error):
    """给 REPL 输出中的 Traceback/Error 添加红色高亮。

    Returns:
        (处理后文本, 是否仍在错误块中)
    """
    if "Traceback" in text:
        idx = text.index("Traceback")
        prefix, search_in = text[:idx], text[idx:]
        m = re.search(r"(?:Error|Exception):[^\r\n]*", search_in)
        if m:
            return prefix + _RED + search_in[:m.end()] + _RESET + search_in[m.end():], False
        return prefix + _RED + search_in, True
    if in_error:
        m = re.search(r"(?:Error|Exception):[^\r\n]*", text)
        if m:
            return _RED + text[:m.end()] + _RESET + text[m.end():], False
        return _RED + text + _RESET, True
    return text, False


class MicroPython:
    """通过串口原始 REPL 与 MicroPython 设备交互。

    提供扫描串口、连接、断开、执行代码、上传文件功能。
    上传时自动设置 kbd_intr(-1) 防止数据流中的 0x03 字节重启设备。
    """

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = 115200,
        timeout: int = 10,
        transport: Optional['Transport'] = None,
    ) -> None:
        self.config = _load_config()  # type: PyriteConfig
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.transport = transport or SerialTransport(port, baudrate, timeout)  # type: ignore
        self._repl_log_file: Optional[Any] = None   # REPL 原始数据日志文件

    @staticmethod
    def scan_ports(vid: Optional[int] = None, pid: Optional[int] = None,
                   keyword: Optional[str] = None, require_vid: bool = True) -> List[Dict[str, Any]]:
        """扫描可用串口，可按 VID/PID/描述关键字过滤。默认过滤掉无 VID 的设备。"""
        ports: List[Dict[str, Any]] = []
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

    def connect(self, port: Optional[str] = None, baudrate: Optional[int] = None) -> bool:
        """打开串口连接到设备。

        Args:
            port: 串口号，如 'COM3' 或 '/dev/ttyUSB0'
            baudrate: 波特率，默认 115200

        Returns:
            True 表示连接成功
        """
        if self.is_connected:
            self.disconnect()

        if port:
            self.port = port
        if baudrate:
            self.baudrate = baudrate
        if not self.port:
            raise ValueError("未提供串口号，请先调用 scan_ports() 或指定 port")

        self.transport.connect()

        # 串口传输：自动 DTR/RTS 硬件复位设备，确保设备处于已知启动状态
        if isinstance(self.transport, SerialTransport):
            try:
                self.transport.dtr_rts_reset()
            except Exception:
                pass

        return True

    def _ensure_connected(self) -> None:
        """确保串口已连接，断线时自动重连并初始化设备状态。"""
        if self.is_connected:
            return
        max_retries = self.config.max_retries
        for attempt in range(max_retries + 1):
            try:
                print(f"  {_YELLOW}[RECONNECT]{_RESET} 串口已断开，尝试重新连接 ({attempt + 1}/{max_retries + 1})...")
                self.connect()
                return
            except Exception as e:
                if attempt >= max_retries:
                    raise ConnectionError(f"重连失败 ({max_retries + 1} 次): {e}")
                time.sleep(1)

    def _init_device_state(self) -> None:
        """初始化设备到原始 REPL 模式并设置 kbd_intr(-1)。

        在原始 REPL 模式中执行完整序列：
        Ctrl+C × 2 → Ctrl+A → fallback Ctrl+D → Ctrl+A → kbd_intr(-1)
        """
        for _ in range(2):
            self._write(SET_RESET)
            time.sleep(0.1)
        self.transport.reset_input_buffer()

        self._write(ENTER_RAW_REPL)
        data = self._read_until(b">", timeout=2)

        if b">" not in data:
            self._write(SET_EXECUTE)
            time.sleep(0.8)
            self.transport.reset_input_buffer()
            self._write(ENTER_RAW_REPL)
            data = self._read_until(b">", timeout=3)

        if b">" not in data:
            raise RuntimeError(f"无法进入原始 REPL 模式，设备响应: {data[:100]!r}")

        self._execute("import micropython; micropython.kbd_intr(-1)", timeout=3)

    def disconnect(self) -> None:
        """断开串口连接，退出原始 REPL 并关闭串口。"""
        self._exit_raw_repl()
        if self.transport.is_connected:
            try:
                self.transport.disconnect()
            except Exception:
                pass

    @property
    def is_connected(self) -> bool:
        """是否已连接。"""
        return self.transport.is_connected

    def _enter_raw_repl(self) -> None:
        """确保连接并进入原始 REPL 模式。"""
        self._ensure_connected()
        self._init_device_state()

    def _exit_raw_repl(self):
        """退出原始 REPL 回到普通 REPL（Ctrl+B）。"""
        try:
            self._write(EXIT_RAW_REPL)
            time.sleep(0.1)
            self.transport.reset_input_buffer()
        except Exception:
            pass

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
        if not self.transport.is_connected:
            return
        while self.transport.in_waiting:
            chunk = self.transport.read(self.transport.in_waiting)
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

    def _write(self, data: bytes | str) -> None:
        """写入数据到串口。"""
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._log_repl_data("tx", data)
        self.transport.write(data)

    def _read_until(self, terminator: bytes = b"\x04", timeout: Optional[int] = None) -> bytes:
        timeout = timeout or self.timeout
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.transport.in_waiting:
                chunk = self.transport.read(self.transport.in_waiting)
                buf += chunk
                self._log_repl_data("rx", chunk)
                idx = buf.find(terminator)
                if idx >= 0:
                    buf = buf[:idx + len(terminator)]
                    break
            time.sleep(0.02)
        return buf

    def repl_(self) -> None:
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
        self.transport.reset_input_buffer()
        self._write(EXIT_RAW_REPL)
        time.sleep(0.3)
        self.transport.reset_input_buffer()

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
                while self.transport.in_waiting:
                    chunk = self.transport.read(self.transport.in_waiting)
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

    def _execute(self, code: str | bytes, timeout: int = 10) -> str:
        """在原始 REPL 中执行 Python 代码并返回设备输出。

        Args:
            code: 要执行的 Python 代码（字符串或 bytes）
            timeout: 等待响应超时（秒）

        Returns:
            设备输出的 stdout 文本
        """
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

    @staticmethod
    def _compute_crc32(data: bytes) -> int:
        """计算数据的 CRC32 校验值。"""
        return binascii.crc32(data) & 0xffffffff

    def _verify_file_on_device(self, remote_path: str, expected_size: int,
                                verify_mode: str = "size",
                                expected_crc: Optional[int] = None) -> bool:
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

    def flash_file(self, local_path: str, remote_path: Optional[str] = None,
                   compile: Optional[bool] = None,
                   bytecode_ver: Optional[int] = None,
                   arch: Optional[str] = None,
                   active_tags: Optional[Set[str]] = None) -> None:
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"本地文件不存在: {local_path}")

        should_compile = self.config.auto_compile if compile is None else compile
        tmp_dirs = []
        actual_local = local_path
        actual_remote = (remote_path or os.path.basename(local_path)).replace("\\", "/")

        if active_tags and local_path.endswith(".py"):
            from .preprocessor import preprocess
            pp_dir = tempfile.mkdtemp()
            os.chmod(pp_dir, 0o700)
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
        verify_mode = self.config.verify
        max_retries = self.config.max_retries

        # 预计算 CRC32（如果需要）
        expected_crc = None
        if verify_mode == "crc32":
            with open(actual_local, "rb") as f:
                expected_crc = self._compute_crc32(f.read())

        print(f"  {_GREEN}刷入:{_RESET} {local_path} -> {actual_remote} ({file_size} 字节, 块大小={DEFAULT_CHUNK_SIZE})")

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

                        with open(actual_local, 'rb') as f:
                            if tqdm:
                                with tqdm(total=file_size, desc="传输中", unit="B",
                                          unit_scale=True, leave=False) as pbar:
                                    for _ in range(0, file_size, DEFAULT_CHUNK_SIZE):
                                        chunk = f.read(DEFAULT_CHUNK_SIZE)
                                        self._write(chunk)
                                        pbar.update(len(chunk))
                            else:
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

                    except (serial.SerialException, Exception) as e:
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

    def flash_program(self, local_dir: str, remote_prefix: str = "",
                      bytecode_ver: Optional[int] = None, arch: Optional[str] = None,
                      active_tags: Optional[Set[str]] = None,
                      manifest_path: Optional[str] = None) -> List[Tuple[str, str, bool]]:
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
        verify_mode = self.config.verify
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
                os.chmod(pp_dir, 0o700)
                tmp_dirs.append(pp_dir)
                pp_path = os.path.join(pp_dir, Path(actual_local).name)
                Path(pp_path).write_text(
                    preprocess(Path(actual_local).read_text(encoding="utf-8"), active_tags, actual_local),
                    encoding="utf-8",
                )
                actual_local = pp_path
            basename = Path(actual_remote).name
            needs_compile = (
                self.config.auto_compile
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

        max_retries = self.config.max_retries

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

                        total_data_size = len(all_data)
                        if tqdm:
                            with tqdm(total=total_data_size, desc="传输中", unit="B",
                                      unit_scale=True, leave=False) as pbar:
                                for offset in range(0, total_data_size, DEFAULT_CHUNK_SIZE):
                                    chunk = all_data[offset:offset + DEFAULT_CHUNK_SIZE]
                                    self._write(chunk)
                                    pbar.update(len(chunk))
                        else:
                            for offset in range(0, total_data_size, DEFAULT_CHUNK_SIZE):
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

                    except (serial.SerialException, Exception) as e:
                        if attempt >= max_retries:
                            print(f"  {_RED}✗ 刷入失败: {e}{_RESET}")
                            return [(lp, rp, False) for (rp, lp, sz) in file_list]
                        print(f"  {_YELLOW}刷入过程异常，准备重试 ({attempt+1}/{max_retries})...{_RESET}")
        finally:
            for d in tmp_dirs:
                shutil.rmtree(d, ignore_errors=True)

    def get_mpy_version(self) -> Tuple[Optional[int], Optional[str]]:
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

    def detect_tags(self) -> Set[str]:
        """从设备读取 board 信息，返回 active_tags 集合。"""
        try:
            out = self.run("import os,sys\nprint(os.uname().machine)\nprint(sys.platform)")
        except Exception:
            return set()
        lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
        combined = " ".join(lines).upper()
        board_tags = self.config.board_tags
        tags = set()
        for kw, tag_list in board_tags.items():
            if kw in combined:
                tags.update(tag_list)
                break
        if len(lines) > 1:
            tags.add(lines[1].upper())
        return tags

    def run(self, code: str, timeout: int = 10) -> str:
        """在设备上执行任意 Python 代码并返回输出。"""
        self._enter_raw_repl()
        return self._execute(code, timeout=timeout)

    def reset(self) -> None:
        """复位设备。优先使用 DTR/RTS 硬件复位，否则使用软重启。"""
        if isinstance(self.transport, SerialTransport):
            try:
                self.transport.dtr_rts_reset()
                return
            except Exception:
                pass
        # 兜底：设备端软重启
        self._enter_raw_repl()
        try:
            self._execute("import machine; machine.reset()", timeout=2)
        except Exception:
            pass


    # ── 设备文件读取（bytes 协议） ───────────────────────────────
    def _read_device_file(self, remote_path: str) -> bytes:
        """从设备读取文件内容（原始字节传输，兼容二进制）。

        两阶段协议：
        1. 通过标准 execute 获取文件大小（纯文本输出，无二进制干扰）
        2. 在 Raw REPL 中直接执行脚本输出原始字节，
           PC 端按已知大小准确接收，无需在上位机过滤协议头尾。

        原始 REPL 输出格式: "OK<raw_bytes>\\x04\\x04>"
        """
        # ── 阶段 1：获取文件大小 ──
        remote_lit = repr(remote_path)
        out = self.run(
            f"import os; print(os.stat({remote_lit})[6])", timeout=5
        )
        expected_size = int(out.strip().splitlines()[-1])

        # ── 阶段 2：读取原始字节 ──
        # run() 后设备仍在 Raw REPL 模式，直接发送输出脚本
        self.transport.reset_input_buffer()

        script = (
            "import os,sys\n"
            "_out=sys.stdout.buffer\n"
            f"p={remote_path!r}\n"
            "with open(p,'rb') as f:\n"
            " while True:\n"
            "  c=f.read(512)\n"
            "  if not c:break\n"
            "  _out.write(c)\n"
        )
        self._write(script.encode() + SET_EXECUTE)
        time.sleep(0.2)

        buf = b""
        need = 2 + expected_size + 3  # OK(2) + data(N) + trailer(\x04\x04> = 3)
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.transport.in_waiting:
                buf += self.transport.read(self.transport.in_waiting)
                if len(buf) >= need:
                    time.sleep(0.05)
                    buf += self.transport.read(self.transport.in_waiting)
                    break
            else:
                time.sleep(0.02)

        if len(buf) < need:
            raise RuntimeError(
                f"数据不完整: 期望 {expected_size} 字节, "
                f"收到 {max(0, len(buf) - 5)} 字节"
            )

        # 跳过 "OK" 前缀，去除尾部 \x04\x04> 等协议标记
        raw = buf[2:] if buf.startswith(b"OK") else buf
        raw = _strip_repl_trailer(raw)
        return raw[:expected_size]

    # ── 设备文件管理 (fs) ─────────────────────────────────────────

    def fs_ls(self, remote_path: str = "/") -> List[Dict[str, str]]:
        """列出设备目录下的文件和子目录。目录体积为递归计算的文件大小总和（最大递归深度 32）。

        使用 os.ilistdir() 取代 os.listdir() + os.stat()，将设备端文件系统调用减半。
        """
        script = (
            "import os\n"
            "def _ds(p,_d=0):\n"
            " t=0\n"
            " if _d>32:\n"
            "  return 0\n"
            " try:\n"
            "  for n,fl,_,sz in os.ilistdir(p):\n"
            "   if fl&0x4000:\n"
            "    fp='/'+n if p=='/' else p+'/'+n\n"
            "    t+=_ds(fp,_d+1)\n"
            "   else:\n"
            "    t+=sz\n"
            " except:\n"
            "  pass\n"
            " return t\n"
            f"p={remote_path!r}\n"
            "for n,fl,_,sz in os.ilistdir(p or '/'):\n"
            " try:\n"
            "  if fl&0x4000:\n"
            "   fp='/'+n if p=='/' else p+'/'+n\n"
            "   print(str(_ds(fp))+'|D|'+n)\n"
            "  else:\n"
            "   print(str(sz)+'|F|'+n)\n"
            " except OSError:\n"
            "  print('?|?|'+n)\n"
        )
        out = self.run(script, timeout=30)
        items = []
        for line in out.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split('|', 2)
            if len(parts) == 3:
                items.append({'size': parts[0], 'type': parts[1], 'name': parts[2]})
        return items

    def fs_ls_recursive(self, remote_path: str = "/") -> List[Dict[str, str]]:
        """递归列出设备目录下的所有文件和子目录。

        在设备端运行递归遍历脚本，一次性返回完整树状结果。
        设备端输出格式与 fs_ls 一致：size|type|path
        """
        script = (
            "import os\n"
            "def _st(p):\n"
            " s=os.stat(p); s=os.stat(p)\n"
            " return s\n"
            "def _walk(d):\n"
            " for n in os.listdir(d):\n"
            "  if d=='/':\n"
            "   fp='/'+n\n"
            "  else:\n"
            "   fp=d+'/'+n\n"
            "  try:\n"
            "   s=_st(fp)\n"
            "   is_dir=bool(s[0]&0x4000)\n"
            "   print(str(s[6])+'|'+('D' if is_dir else 'F')+'|'+fp)\n"
            "   if is_dir:\n"
            "    _walk(fp)\n"
            "  except OSError:\n"
            "   print('?|?|'+fp)\n"
            f"_walk({remote_path!r})\n"
        )
        out = self.run(script, timeout=30)
        items = []
        for line in out.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split('|', 2)
            if len(parts) == 3:
                items.append({'size': parts[0], 'type': parts[1], 'name': parts[2]})
        return items

    def fs_df(self) -> Dict[str, int]:
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

    def __enter__(self) -> 'MicroPython':
        return self

    def __exit__(self, *args: Any) -> None:
        self.disconnect()

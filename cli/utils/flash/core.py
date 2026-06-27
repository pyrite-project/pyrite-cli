"""Low-level MicroPython transport and raw REPL primitives."""

from __future__ import annotations

import binascii
from dataclasses import dataclass
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import serial
import serial.tools.list_ports

from ..config import DEFAULT_BAUDRATE, _load_config
from ..log import TrafficMonitor, get_logger
from ..transport import SerialTransport, Transport
from ..config import PyriteConfig

# ── 模块日志器 ──
log = get_logger(__name__)
BATCH_ACK_EVERY = 8
RAW_REPL_BAUD_FALLBACKS = (115200, DEFAULT_BAUDRATE, 460800, 230400)



@dataclass
class _WindowsReplInput:
    data: bytes
    echo_as: Optional[str] = None


# ── 原始 REPL 协议常量 ──
ENTER_RAW_REPL = b"\x01"
EXIT_RAW_REPL = b"\x02"
SET_RESET = b"\x03"
SET_EXECUTE = b"\x04"
ENTER_RAW_PASTE = b"\x05"

# ── 设备端刷入脚本 ──

MP_SCRIPTS_DIR = Path(__file__).with_name("mp_scripts")


def _load_mp_script(name: str) -> str:
    return (MP_SCRIPTS_DIR / name).read_text(encoding="utf-8")


FLASH = _load_mp_script("flash.py")

FLASH_PROGRAM = _load_mp_script("flash_program.py")

FLASH_DELTA = _load_mp_script("flash_delta.py")


def _compute_block_crc32(data: bytes, block_size: int) -> List[Tuple[int, int]]:
    """Return ``(crc32, size)`` for each block in ``data``."""
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    view = memoryview(data)
    blocks: List[Tuple[int, int]] = []
    for i in range(0, len(view), block_size):
        chunk = view[i:i + block_size]
        blocks.append((binascii.crc32(chunk) & 0xFFFFFFFF, len(chunk)))
    return blocks


def _build_inline_verify_code(
    remote_path: str,
    expected_size: int,
    verify_mode: str,
    expected_crc: Optional[int],
    chunk_size: int,
) -> str:
    if verify_mode == "off":
        return ""

    read_size = max(64, min(chunk_size, 4096))
    lines = [
        "    import os",
        f"    _verify_path={remote_path!r}",
        f"    _expected_size={expected_size}",
        "    _actual_size=os.stat(_verify_path)[6]",
        "    if _actual_size!=_expected_size:",
        "        raise OSError('VERIFY_SIZE:%d:%d'%(_expected_size,_actual_size))",
    ]
    if verify_mode == "crc32" and expected_crc is not None:
        lines.extend([
            f"    _expected_crc={expected_crc & 0xFFFFFFFF}",
            "    try:",
            "        import gc,ubinascii",
            "    except Exception:",
            "        ubinascii=None",
            "    if ubinascii is None:",
            "        sys.stdout.write('VERIFY_WARN:crc32 unavailable\\n')",
            "    else:",
            "        _crc=0",
            "        with open(_verify_path,'rb') as _vf:",
            "            while True:",
            "                gc.collect()",
            f"                _chunk=_vf.read({read_size})",
            "                if not _chunk:",
            "                    break",
            "                _crc=ubinascii.crc32(_chunk,_crc)",
            "        _crc=_crc&0xffffffff",
            "        if _crc!=_expected_crc:",
            "            raise OSError('VERIFY_CRC:%d:%d'%(_expected_crc,_crc))",
        ])
    return "\n".join(lines) + "\n"


def _build_inline_batch_verify_code(
    file_meta: Sequence[Tuple[str, int]],
    verify_mode: str,
    expected_crcs: Optional[Dict[str, int]],
    chunk_size: int,
) -> str:
    if verify_mode == "off":
        return ""

    expected_crcs = expected_crcs or {}
    entries = [
        (remote_path, size, expected_crcs.get(remote_path))
        for remote_path, size in file_meta
    ]
    read_size = max(64, min(chunk_size, 4096))
    lines = [
        "    import os",
        f"    _verify_entries={entries!r}",
        "    for _verify_path,_expected_size,_expected_crc in _verify_entries:",
        "        _actual_size=os.stat(_verify_path)[6]",
        "        if _actual_size!=_expected_size:",
        "            raise OSError('VERIFY_SIZE:%s:%d:%d'%(_verify_path,_expected_size,_actual_size))",
    ]
    if verify_mode == "crc32":
        lines.extend([
            "    try:",
            "        import gc,ubinascii",
            "    except Exception:",
            "        ubinascii=None",
            "    if ubinascii is None:",
            "        sys.stdout.write('VERIFY_WARN:crc32 unavailable\\n')",
            "    else:",
            "        for _verify_path,_expected_size,_expected_crc in _verify_entries:",
            "            if _expected_crc is None:",
            "                continue",
            "            _crc=0",
            "            with open(_verify_path,'rb') as _vf:",
            "                while True:",
            "                    gc.collect()",
            f"                    _chunk=_vf.read({read_size})",
            "                    if not _chunk:",
            "                        break",
            "                    _crc=ubinascii.crc32(_chunk,_crc)",
            "            _crc=_crc&0xffffffff",
            "            if _crc!=_expected_crc:",
            "                raise OSError('VERIFY_CRC:%s:%d:%d'%(_verify_path,_expected_crc,_crc))",
        ])
    return "\n".join(lines) + "\n"


def _strip_repl_trailer(buf: bytes) -> bytes:
    """去除原始 REPL 响应尾部的协议标记。"""
    for trailer in (SET_EXECUTE + b">", SET_EXECUTE + SET_EXECUTE, SET_EXECUTE):
        if buf.endswith(trailer):
            buf = buf[:-len(trailer)]
    return buf


def _colorize_repl_output(text: str, in_error: bool) -> Tuple[str, bool]:
    """给 REPL 输出中的 Traceback/Error 添加红色高亮。"""
    if "Traceback" in text:
        idx = text.index("Traceback")
        prefix, search_in = text[:idx], text[idx:]
        m = re.search(r"(?:Error|Exception):[^\r\n]*", search_in)
        if m:
            return prefix + "\033[31m" + search_in[:m.end()] + "\033[0m" + search_in[m.end():], False
        return prefix + "\033[31m" + search_in, True
    if in_error:
        m = re.search(r"(?:Error|Exception):[^\r\n]*", text)
        if m:
            return "\033[31m" + text[:m.end()] + "\033[0m" + text[m.end():], False
        return "\033[31m" + text + "\033[0m", True
    return text, False


_WINDOWS_EXT_KEYS = {
    "H": b"\x1b[A",
    "P": b"\x1b[B",
    "M": b"\x1b[C",
    "K": b"\x1b[D",
    "G": b"\x1b[H",
    "O": b"\x1b[F",
    "S": b"\x1b[3~",
    "R": b"\x1b[2~",
    "I": b"\x1b[5~",
    "Q": b"\x1b[6~",
}


class _WindowsReplEchoFilter:
    def __init__(self) -> None:
        self._pending: List[Tuple[bytes, bytes]] = []
        self._current: Optional[Tuple[bytes, bytes]] = None
        self._index = 0
        self._buffer = bytearray()

    def add(self, expected: bytes, replacement: str | bytes) -> None:
        if not expected:
            return
        if isinstance(replacement, str):
            replacement = replacement.encode("utf-8")
        self._pending.append((expected, replacement))

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        for byte in data:
            while True:
                if self._current is None and self._pending:
                    self._current = self._pending[0]
                    self._index = 0
                    self._buffer = bytearray()

                if self._current is None:
                    out.append(byte)
                    break

                expected, replacement = self._current
                if byte == expected[self._index]:
                    self._buffer.append(byte)
                    self._index += 1
                    if self._index == len(expected):
                        out.extend(replacement)
                        self._pending.pop(0)
                        self._current = None
                        self._index = 0
                        self._buffer = bytearray()
                    break

                if self._index:
                    out.extend(self._buffer)
                    self._pending.pop(0)
                    self._current = None
                    self._index = 0
                    self._buffer = bytearray()
                    continue

                out.append(byte)
                break
        return bytes(out)


def _windows_repl_key_to_input(
    ch: str,
    read_next: Callable[[], str],
) -> Optional[_WindowsReplInput]:
    if ch in ("\x00", "\xe0"):
        data = _WINDOWS_EXT_KEYS.get(read_next())
        return _WindowsReplInput(data) if data else None
    if not ch.isascii():
        return _WindowsReplInput(ch.encode("unicode_escape"), echo_as=ch)
    try:
        return _WindowsReplInput(ch.encode("utf-8"))
    except UnicodeEncodeError:
        return _WindowsReplInput(ch.encode("utf-8", errors="replace"))


def _windows_repl_key_to_bytes(
    ch: str,
    read_next: Callable[[], str],
) -> Optional[bytes]:
    item = _windows_repl_key_to_input(ch, read_next)
    return item.data if item else None


def _windows_repl_input_reader(
    msvcrt_module: Any,
    out_queue: Any,
    stop_event: Any,
) -> None:
    while not stop_event.is_set():
        try:
            ch = msvcrt_module.getwch()
            item = _windows_repl_key_to_input(ch, msvcrt_module.getwch)
        except (EOFError, OSError):
            break
        except KeyboardInterrupt:
            item = _WindowsReplInput(b"\x03")
        if item:
            out_queue.put(item)


def _repl_display_width(text: str) -> int:
    import unicodedata

    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1
    return max(width, 1 if text else 0)


class _WindowsReplLineEditor:
    def __init__(self, stdout: Any) -> None:
        self._stdout = stdout
        self._chars: List[str] = []

    def handle(self, item: _WindowsReplInput) -> Tuple[Optional[bytes], bool]:
        data = item.data
        if data == b"\x03":
            return None, True
        if data in (b"\r", b"\n"):
            line = self._encoded_line() + b"\r"
            self._chars.clear()
            return line, False
        if data in (b"\x08", b"\x7f"):
            self._backspace()
            return None, False
        if data.startswith(b"\x1b["):
            return (data, False) if not self._chars else (None, False)

        text = item.echo_as
        if text is None:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return data, False
        if not text:
            return None, False
        if any(ord(ch) < 32 and ch not in "\t" for ch in text):
            return data, False

        self._chars.extend(text)
        self._stdout.write(text)
        self._stdout.flush()
        return None, False

    def _backspace(self) -> None:
        if not self._chars:
            return
        ch = self._chars.pop()
        self._stdout.write("\b \b" * _repl_display_width(ch))
        self._stdout.flush()

    def _encoded_line(self) -> bytes:
        parts = []
        for ch in self._chars:
            if ch.isascii():
                parts.append(ch)
            else:
                parts.append(ch.encode("unicode_escape").decode("ascii"))
        return "".join(parts).encode("ascii", errors="replace")


class MicroPythonBase:
    """通过串口原始 REPL 与 MicroPython 设备交互。

    提供扫描串口、连接、断开、执行代码、上传文件功能。
    """

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: int = 10,
        transport: Optional["Transport"] = None,
    ) -> None:
        self.config: PyriteConfig = _load_config()
        self.port = port
        self.baudrate = baudrate or self.config.baudrate or DEFAULT_BAUDRATE
        self.timeout = timeout or self.config.timeout or 10
        self.transport = transport or SerialTransport(port, self.baudrate, self.timeout)  # type: ignore[arg-type]
        self._traffic_monitor: Optional[TrafficMonitor] = None
        self._suppress_traffic = False

    # ── 静态/工具方法 ──

    @staticmethod
    def scan_ports(
        vid: Optional[int] = None,
        pid: Optional[int] = None,
        keyword: Optional[str] = None,
        require_vid: bool = True,
    ) -> List[Dict[str, Any]]:
        """扫描可用串口，可按 VID/PID/描述关键字过滤。"""
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
        log.debug("扫描到 %d 个串口设备", len(ports))
        return ports

    # ── 连接管理 ──

    def connect(self, port: Optional[str] = None, baudrate: Optional[int] = None) -> bool:
        """打开串口连接到设备。"""
        if self.is_connected:
            self.disconnect()

        if port:
            self.port = port
        if baudrate:
            self.baudrate = baudrate
        if isinstance(self.transport, SerialTransport):
            self.transport.port = self.port
            self.transport.baudrate = self.baudrate
            self.transport.timeout = self.timeout
        if not self.port:
            raise ValueError("未提供串口号，请先调用 scan_ports() 或指定 port")

        log.debug("连接设备 %s (波特率=%d)", self.port, self.baudrate)
        self.transport.connect()

        # 串口传输：自动 DTR/RTS 硬件复位设备
        if isinstance(self.transport, SerialTransport):
            try:
                self.transport.dtr_rts_reset()
                log.trace("DTR/RTS 硬件复位完成")
            except Exception as e:
                log.trace("DTR/RTS 复位跳过: %s", e)

        return True

    def disconnect(self) -> None:
        """断开串口连接。"""
        log.debug("断开设备连接 %s", self.port)
        self._exit_raw_repl()
        if self.transport.is_connected:
            try:
                self.transport.disconnect()
            except Exception as e:
                log.trace("断开连接时忽略异常: %s", e)

    @property
    def is_connected(self) -> bool:
        return self.transport.is_connected

    def _ensure_connected(self) -> None:
        """确保串口已连接，断线时自动重连。"""
        if self.is_connected:
            return
        max_retries = self.config.max_retries
        for attempt in range(max_retries + 1):
            try:
                log.warning("串口已断开，尝试重新连接 (%d/%d)...", attempt + 1, max_retries + 1)
                self.connect()
                return
            except Exception as e:
                if attempt >= max_retries:
                    raise ConnectionError(f"重连失败 ({max_retries + 1} 次): {e}")
                time.sleep(1)

    # ── 原始 REPL 状态机 ──

    def _enter_raw_repl(self) -> None:
        """确保连接并进入原始 REPL 模式。"""
        self._ensure_connected()
        self._init_device_state()

    def _exit_raw_repl(self) -> None:
        """退出原始 REPL 回到普通 REPL。"""
        try:
            self._write(EXIT_RAW_REPL)
            time.sleep(0.1)
            self.transport.reset_input_buffer()
        except Exception as e:
            log.trace("退出原始 REPL 时忽略异常: %s", e)

    def _read_until_raw_repl(self, timeout: int = 3) -> Tuple[bool, bytes]:
        """读取串口数据直到检测到原始 REPL 确认消息或超时。"""
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            if self.transport.in_waiting:
                chunk = self.transport.read(self.transport.in_waiting)
                buf += chunk
                self._record_rx(chunk)
                if b"CTRL-B" in buf:
                    return True, buf
            time.sleep(0.02)
        return False, buf

    def _interrupt_running_program(
        self,
        attempts: int = 12,
        interval: float = 0.03,
        settle: float = 0.15,
    ) -> None:
        """持续发送 Ctrl+C，让正在运行的 main.py 有时间退出到普通 REPL。"""
        for _ in range(attempts):
            self._write(SET_RESET)
            time.sleep(interval)
        time.sleep(settle)
        self.transport.reset_input_buffer()

    def _reset_and_interrupt_boot(self) -> bool:
        """硬复位后抢占启动窗口，避免 main.py 先禁用 Ctrl+C。"""
        if not isinstance(self.transport, SerialTransport):
            return False
        try:
            self.transport.set_rts(True)
            self.transport.set_dtr(False)
            time.sleep(0.1)
            self.transport.set_rts(False)
            deadline = time.time() + 1.2
            while time.time() < deadline:
                self._write(SET_RESET)
                time.sleep(0.03)
            self.transport.reset_input_buffer()
            return True
        except Exception as exc:
            log.trace("硬复位启动中断失败: %s", exc)
            return False

    def _try_raw_repl_sequence(self) -> Tuple[bool, bytes]:
        """Try the normal Ctrl+C → Ctrl+A Raw REPL entry sequence once."""
        self._interrupt_running_program()

        self._write(ENTER_RAW_REPL)
        ok, data = self._read_until_raw_repl(timeout=2)
        if ok:
            return True, data

        log.debug("首次进入原始 REPL 失败，尝试兜底...")
        self._write(SET_EXECUTE)
        time.sleep(0.8)
        self._interrupt_running_program()
        self._write(ENTER_RAW_REPL)
        return self._read_until_raw_repl(timeout=3)

    def _reconnect_for_baud(self, baudrate: int) -> None:
        """Reconnect the current serial transport at a different baud rate."""
        if not isinstance(self.transport, SerialTransport):
            return
        log.debug("切换串口波特率并重连: %d", baudrate)
        self.connect(baudrate=baudrate)

    def _try_common_baud_raw_repl(self) -> Tuple[bool, bytes]:
        """Try common MicroPython baud rates when the configured rate is silent."""
        if not isinstance(self.transport, SerialTransport):
            return False, b""

        original_baudrate = self.baudrate
        last_data = b""
        candidates: List[int] = []
        for baudrate in RAW_REPL_BAUD_FALLBACKS:
            if baudrate != original_baudrate and baudrate not in candidates:
                candidates.append(baudrate)

        for baudrate in candidates:
            try:
                log.warning(
                    "原始 REPL 在 %d 波特率无响应，尝试 %d...",
                    original_baudrate,
                    baudrate,
                )
                self._reconnect_for_baud(baudrate)
                ok, data = self._try_raw_repl_sequence()
                last_data = data
                if ok:
                    log.info("已自动切换到可用波特率: %d", baudrate)
                    return True, data
            except Exception as exc:
                log.debug("尝试波特率 %d 失败: %s", baudrate, exc)

        try:
            self._reconnect_for_baud(original_baudrate)
        except Exception:
            pass
        return False, last_data

    def _ensure_filesystem_mounted(self) -> None:
        """确保标准 VFS 已挂载。

        某些 mPython 固件在启动窗口被打断后会进入 Raw REPL，但根 VFS 还未挂载。
        这时 os.listdir('/') 返回空，statvfs('/') 也全 0，需要手动挂载 flashbdev。
        """
        script = (
            "import os\n"
            "def _fs_ready():\n"
            " try:\n"
            "  s=os.statvfs('/')\n"
            "  return bool(s[0] and s[2])\n"
            " except Exception:\n"
            "  return False\n"
            "def _mount_flashbdev():\n"
            " try:\n"
            "  import flashbdev\n"
            "  b=flashbdev.bdev\n"
            "  if isinstance(b,(list,tuple)):\n"
            "   b=b[0]\n"
            "  for candidate in (b, os.VfsLfs2(b)):\n"
            "   try:\n"
            "    os.mount(candidate,'/')\n"
            "    return\n"
            "   except Exception:\n"
            "    pass\n"
            " except Exception:\n"
            "  pass\n"
            "if not _fs_ready():\n"
            " _mount_flashbdev()\n"
            "print('FS_READY' if _fs_ready() else 'FS_NOT_READY')\n"
        )
        out = self._execute(script, timeout=5, raise_on_error=False)
        if "FS_NOT_READY" in out:
            log.debug("设备文件系统未就绪，后续文件操作可能返回空目录")

    def _init_device_state(self) -> None:
        """初始化设备到原始 REPL 模式并设置 kbd_intr(-1)。

        序列：持续 Ctrl+C → Ctrl+A → 兜底 Ctrl+D → 持续 Ctrl+C → Ctrl+A → kbd_intr(-1)
        """
        log.trace("初始化设备状态 → 原始 REPL")
        ok, data = self._try_raw_repl_sequence()

        if not ok:
            ok, data = self._try_common_baud_raw_repl()

        if not ok:
            # 尝试 DTR/RTS 硬件复位兜底
            if isinstance(self.transport, SerialTransport):
                log.debug("尝试 DTR/RTS 硬件复位并抢占启动中断窗口...")
                try:
                    if self._reset_and_interrupt_boot():
                        self._write(ENTER_RAW_REPL)
                        ok, data = self._read_until_raw_repl(timeout=3)
                        if ok:
                            self._ensure_filesystem_mounted()
                            self._execute(
                                "import micropython; micropython.kbd_intr(-1)",
                                timeout=3, raise_on_error=False,
                            )
                            return
                except Exception:
                    pass
            raise RuntimeError(
                f"无法进入原始 REPL 模式，设备响应: {data[:100]!r}"
            )

        self._ensure_filesystem_mounted()
        self._execute(
            "import micropython; micropython.kbd_intr(-1)",
            timeout=3, raise_on_error=False,
        )
        log.trace("设备状态初始化完成")

    # ── 流量记录（内部） ──

    def _record_rx(self, data: bytes) -> None:
        """非阻塞记录 RX 数据（如果监控器激活）。"""
        if self._traffic_monitor and not self._suppress_traffic:
            self._traffic_monitor.rx(data)

    def _drain_rx(self) -> None:
        """排空串口 RX 缓冲并记录。"""
        if not self.transport.is_connected:
            return
        while self.transport.in_waiting:
            chunk = self.transport.read(self.transport.in_waiting)
            if chunk:
                self._record_rx(chunk)

    # ── 底层 I/O ──

    def _write(self, data: bytes | str) -> None:
        """写入数据到串口（自动记录流量）。"""
        if isinstance(data, str):
            data = data.encode("utf-8")
        if self._traffic_monitor and not self._suppress_traffic:
            self._traffic_monitor.tx(data)
        self.transport.write(data)

    def _read_until(
        self,
        terminator: bytes = b"\x04",
        timeout: Optional[int] = None,
    ) -> Tuple[bool, bytes]:
        """读取串口数据直到遇到终止符或超时。"""
        timeout = timeout or self.timeout
        buf = b""
        deadline = time.time() + timeout
        sleep_time = 0.001
        idle_count = 0
        while time.time() < deadline:
            if self.transport.in_waiting:
                chunk = self.transport.read(self.transport.in_waiting)
                buf += chunk
                self._record_rx(chunk)
                idle_count = 0
                sleep_time = 0.001
                idx = buf.find(terminator)
                if idx >= 0:
                    buf = buf[:idx + len(terminator)]
                    return True, buf
            else:
                idle_count += 1
                if idle_count > 10:
                    sleep_time = 0.02
            time.sleep(sleep_time)
        return False, buf

    def _read_until_marker(
        self, marker: bytes, timeout: int = 30,
    ) -> Tuple[bool, bytes]:
        """等待标记出现在串口数据流中。"""
        deadline = time.time() + timeout
        buf = b""
        sleep_time = 0.001
        idle_count = 0
        while time.time() < deadline:
            if self.transport.in_waiting:
                chunk = self.transport.read(self.transport.in_waiting)
                buf += chunk
                self._record_rx(chunk)
                if marker in buf:
                    return True, buf
                idle_count = 0
                sleep_time = 0.001
            else:
                idle_count += 1
                if idle_count > 10:
                    sleep_time = 0.02
            time.sleep(sleep_time)
        return False, buf

    # ═══════════════════════════════════════════════════════════════
    # 流量监控上下文管理器
    # ═══════════════════════════════════════════════════════════════

    @contextmanager
    def _traffic_log_ctx(self) -> Iterator[None]:
        """为关键操作开启流量监控。

        替代旧 ``_repl_log_ctx``，使用 TrafficMonitor 统一记录。
        """
        had_previous = self._traffic_monitor is not None
        if not had_previous:
            self._traffic_monitor = TrafficMonitor(log, port=self.port)
            from ..log import _mgr
            log_path = _mgr.jsonl_path
            if log_path:
                log.info("流量日志: %s", log_path)
        try:
            yield
        finally:
            if not had_previous:
                self._traffic_monitor.close()
                self._traffic_monitor = None

    # ═══════════════════════════════════════════════════════════════
    # REPL 执行
    # ═══════════════════════════════════════════════════════════════

    def _execute(
        self,
        code: str | bytes,
        timeout: int = 10,
        raise_on_error: bool = True,
    ) -> str:
        """在原始 REPL 中执行 Python 代码并返回设备输出。"""
        if isinstance(code, str):
            code = code.encode("utf-8")

        self._write(code)
        self._write(SET_EXECUTE)

        _, resp = self._read_until(SET_EXECUTE, timeout=timeout)
        resp = resp.rstrip(SET_EXECUTE)
        text = resp.decode("utf-8", errors="replace")
        if text.startswith("OK"):
            text = text[2:]
        text = text.strip()

        if raise_on_error and "Traceback" in text:
            log.trace("设备执行错误:\n%s", text)
            raise RuntimeError(f"设备执行错误:\n{text}")

        return text

    def _exec_raw(self, code: str | bytes, timeout: int = 10) -> str:
        """在原始 REPL 中执行代码，忽略设备启动阶段的 Traceback。"""
        return self._execute(code, timeout=timeout, raise_on_error=False)

    def __enter__(self) -> "MicroPythonBase":
        return self

    def __exit__(self, *args: Any) -> None:
        self.disconnect()

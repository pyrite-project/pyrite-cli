"""Public facade for MicroPython flash operations."""

from __future__ import annotations

import binascii
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import serial

from ..build import _compile_files_parallel, _compile_to_mpy
from ..config import DEFAULT_CHUNK_SIZE
from ..log import get_logger
from ..transport import SerialTransport
from . import core as _core

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

_core_export_names = [
    _name
    for _name in dir(_core)
    if not (_name.startswith("__") and _name.endswith("__"))
]

for _name in _core_export_names:
    globals()[_name] = getattr(_core, _name)

log = get_logger(__name__)


@dataclass
class _PreparedFlashFile:
    source_path: str
    remote_path: str
    local_path: str
    content: bytes
    size: int


@dataclass(frozen=True)
class SafeMainOverwritePlan:
    remote_path: str
    backup_path: str


def _safe_main_timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _normalize_remote_for_safe_main(remote_path: str) -> str:
    path = remote_path.replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    return path


def _build_safe_main_backup_script(remote_path: str, backup_path: str) -> str:
    return (
        "import os\n"
        f"_src={remote_path!r}\n"
        f"_dst_base={backup_path!r}\n"
        "_exists=True\n"
        "try:\n"
        " os.stat(_src)\n"
        "except OSError:\n"
        " _exists=False\n"
        "if not _exists:\n"
        " print('NO_BACKUP')\n"
        "else:\n"
        " _dst=_dst_base\n"
        " _i=1\n"
        " while True:\n"
        "  try:\n"
        "   os.stat(_dst)\n"
        "   _dst=_dst_base+'.'+str(_i)\n"
        "   _i+=1\n"
        "  except OSError:\n"
        "   break\n"
        " with open(_src,'rb') as _s:\n"
        "  with open(_dst,'wb') as _d:\n"
        "   while True:\n"
        "    _b=_s.read(512)\n"
        "    if not _b:\n"
        "     break\n"
        "    _d.write(_b)\n"
        " print('BACKUP:'+_dst)\n"
    )



class MicroPython(MicroPythonBase):
    """High-level MicroPython operations built on ``MicroPythonBase``."""

    @staticmethod
    def is_safe_main_path(remote_path: str) -> bool:
        normalized = _normalize_remote_for_safe_main(remote_path)
        return normalized in {"main.py", "/main.py"}

    @staticmethod
    def safe_main_backup_path(
        remote_path: str,
        timestamp: Optional[str] = None,
    ) -> str:
        normalized = _normalize_remote_for_safe_main(remote_path)
        stamp = timestamp or _safe_main_timestamp()
        return f"{normalized}.pyrite-bak-{stamp}"

    @classmethod
    def plan_safe_main_overwrites(
        cls,
        remote_paths: Sequence[str],
        enabled: bool = True,
        timestamp: Optional[str] = None,
    ) -> List[SafeMainOverwritePlan]:
        if not enabled:
            return []
        plans: List[SafeMainOverwritePlan] = []
        seen: Set[str] = set()
        for remote_path in remote_paths:
            normalized = _normalize_remote_for_safe_main(remote_path)
            if normalized in seen or not cls.is_safe_main_path(normalized):
                continue
            seen.add(normalized)
            if timestamp is None:
                backup_path = cls.safe_main_backup_path(normalized)
            else:
                backup_path = cls.safe_main_backup_path(
                    normalized,
                    timestamp=timestamp,
                )
            plans.append(SafeMainOverwritePlan(normalized, backup_path))
        return plans

    def safe_break(
        self,
        attempts: int = 12,
        interval: float = 0.03,
        settle: float = 0.15,
    ) -> None:
        """Send a Ctrl+C burst before Raw REPL entry to stop a bad main.py."""
        self._ensure_connected()
        self._interrupt_running_program(
            attempts=attempts,
            interval=interval,
            settle=settle,
        )

    def _backup_remote_file_if_exists(
        self,
        remote_path: str,
        backup_path: str,
    ) -> Optional[str]:
        script = _build_safe_main_backup_script(remote_path, backup_path)
        out = self._execute(script, timeout=10)
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("BACKUP:"):
                actual_backup = line[len("BACKUP:"):]
                log.info("Safe Main backup: %s → %s", remote_path, actual_backup)
                return actual_backup
        log.debug("Safe Main backup skipped; no existing file: %s", remote_path)
        return None

    def _upload_ack_every(self) -> int:
        if isinstance(self.transport, SerialTransport) and self.baudrate <= 230400:
            return 1
        return BATCH_ACK_EVERY

    def repl_(
        self,
        command_handler: Optional[Callable[[bytes], bool]] = None,
        idle_hook: Optional[Callable[[], None]] = None,
        output_mapper: Optional[Callable[[str], str]] = None,
    ) -> None:
        """交互式 MicroPython REPL（串口透传模式）。"""
        import codecs

        try:
            import msvcrt
            import queue
            import threading
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

        sys.stderr.write("\n=== MicroPython REPL ===\n\n")
        sys.stderr.flush()
        log.info("进入交互式 REPL (端口=%s)", self.port)

        old_tty = None
        if not win:
            fd = sys.stdin.fileno()
            old_tty = termios.tcgetattr(fd)
            mode = old_tty[:]
            mode[tty.CC] = mode[tty.CC][:]
            mode[tty.LFLAG] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
            mode[tty.CC][termios.VMIN] = 1
            mode[tty.CC][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSAFLUSH, mode)

        in_error = False
        utf8_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        echo_filter = _WindowsReplEchoFilter() if win else None
        line_editor = _WindowsReplLineEditor(sys.stdout) if win else None
        keyboard_queue = None
        keyboard_stop = None
        queue_empty = None
        if win:
            keyboard_queue = queue.Queue()
            keyboard_stop = threading.Event()
            queue_empty = queue.Empty
            keyboard_thread = threading.Thread(
                target=_windows_repl_input_reader,
                args=(msvcrt, keyboard_queue, keyboard_stop),
                daemon=True,
            )
            keyboard_thread.start()

        try:
            while self.is_connected:
                # 串口 → 终端
                while self.transport.in_waiting:
                    chunk = self.transport.read(self.transport.in_waiting)
                    if not chunk:
                        continue
                    if echo_filter is not None:
                        chunk = echo_filter.feed(chunk)
                        if not chunk:
                            continue
                    text = utf8_decoder.decode(chunk, final=False)
                    if not text:
                        continue
                    if output_mapper is not None:
                        text = output_mapper(text)
                    output, in_error = _colorize_repl_output(text, in_error)
                    sys.stdout.write(output)
                    sys.stdout.flush()

                # 键盘 → 串口
                if win:
                    while keyboard_queue is not None:
                        try:
                            item = keyboard_queue.get_nowait()
                        except queue_empty:
                            break
                        data, should_exit = line_editor.handle(item)  # type: ignore[union-attr]
                        if should_exit:
                            raise KeyboardInterrupt
                        if data:
                            if command_handler is not None and data.endswith((b"\r", b"\n")):
                                if command_handler(data):
                                    continue
                            if data != b"\r" and data.endswith(b"\r") and echo_filter is not None:
                                echo_filter.add(data[:-1], b"")
                            self._write(data)
                else:
                    if select.select([sys.stdin], [], [], 0)[0]:
                        buf = os.read(sys.stdin.fileno(), 1)
                        if not buf:
                            break
                        if buf == b"\x03":
                            break
                        if buf == b"\x1b":
                            for _ in range(16):
                                if select.select([sys.stdin], [], [], 0.02)[0]:
                                    b = os.read(sys.stdin.fileno(), 1)
                                    if not b:
                                        break
                                    buf += b
                                    if b in (b"~",) or (
                                        len(buf) >= 2 and b in b"ABCDHPQRS"
                                    ):
                                        break
                                else:
                                    break
                        self._write(buf)

                if idle_hook is not None:
                    idle_hook()
                time.sleep(0.01)
        except KeyboardInterrupt:
            pass
        finally:
            if keyboard_stop is not None:
                keyboard_stop.set()
            pending_text = utf8_decoder.decode(b"", final=True)
            if pending_text:
                if output_mapper is not None:
                    pending_text = output_mapper(pending_text)
                output, _ = _colorize_repl_output(pending_text, in_error)
                sys.stdout.write(output)
                sys.stdout.flush()
            sys.stdout.buffer.write(b"\r\n")
            sys.stdout.buffer.flush()
            if old_tty is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_tty)
                except Exception:
                    pass
            log.info("已退出交互式 REPL")

    def _send_data_with_sparse_ack(
        self,
        data_iter: Iterable[bytes],
        total: int,
        ack_every: int = BATCH_ACK_EVERY,
        desc: str = "batch transfer",
    ) -> None:
        ack_every = max(1, ack_every)
        self._suppress_traffic = True
        try:
            if self._traffic_monitor:
                self._traffic_monitor.log.traffic(
                    "TX", f"[data block {total} bytes]".encode()
                )
            if self._trace_recorder:
                self._trace_recorder.traffic_summary(
                    "TX",
                    total,
                    phase=self._trace_phase,
                    text=f"[data block {total} bytes]",
                )

            sent = 0
            chunks_since_ack = 0

            def send_one(chunk: bytes) -> None:
                nonlocal sent, chunks_since_ack
                self._write(chunk)
                sent += len(chunk)
                chunks_since_ack += 1
                if chunks_since_ack >= ack_every and sent < total:
                    found, err_data = self._read_until_marker(b"+", timeout=10)
                    if not found:
                        raise RuntimeError(
                            f"device write timeout, received: {err_data!r}"
                        )
                    chunks_since_ack = 0

            if tqdm:
                with tqdm(
                    total=total, desc=desc, unit="B",
                    unit_scale=True, leave=False,
                ) as pbar:
                    for chunk in data_iter:
                        send_one(chunk)
                        pbar.update(len(chunk))
            else:
                for chunk in data_iter:
                    send_one(chunk)
        finally:
            self._suppress_traffic = False

    # ═══════════════════════════════════════════════════════════════
    # 校验
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _compute_crc32(data: bytes) -> int:
        """计算数据的 CRC32 校验值。"""
        return binascii.crc32(data) & 0xFFFFFFFF

    def _verify_file_on_device(
        self,
        remote_path: str,
        expected_size: int,
        verify_mode: str = "size",
        expected_crc: Optional[int] = None,
    ) -> bool:
        """验证设备上的文件与预期一致。"""
        try:
            out = self._execute(
                f"import os; print(os.stat({remote_path!r})[6])", timeout=5,
            )
            lines = out.strip().splitlines()
            if not lines:
                log.error("文件大小校验失败: 设备无响应 (%s)", remote_path)
                return False
            try:
                actual_size = int(lines[-1])
            except ValueError:
                log.error(
                    "文件大小校验失败: 设备返回异常数据 %r (%s)",
                    lines[-1], remote_path,
                )
                return False
        except Exception as e:
            log.error("文件大小校验失败: %s (%s)", e, remote_path)
            return False

        if actual_size != expected_size:
            log.error(
                "大小不匹配: 期望 %d 字节, 实际 %d 字节 (%s)",
                expected_size, actual_size, remote_path,
            )
            return False

        if verify_mode == "crc32" and expected_crc is not None:
            try:
                crc_out = self._execute(
                    "import gc,ubinascii\n"
                    f"crc=0\n"
                    f"with open({remote_path!r},'rb') as f:\n"
                    " while True:\n"
                    "  gc.collect()\n"
                    "  chunk=f.read(int(gc.mem_free()*0.7))\n"
                    "  if not chunk:break\n"
                    "  crc=ubinascii.crc32(chunk,crc)\n"
                    "print(crc&0xffffffff)",
                    timeout=15,
                )
                crc_lines = crc_out.strip().splitlines()
                if not crc_lines:
                    log.warning("CRC32 校验无响应，仅验证文件大小 (%s)", remote_path)
                    return True
                actual_crc = int(crc_lines[-1]) & 0xFFFFFFFF
                if actual_crc != expected_crc:
                    log.error(
                        "CRC32 不匹配: 期望 %08X, 实际 %08X (%s)",
                        expected_crc, actual_crc, remote_path,
                    )
                    return False
            except Exception as e:
                log.warning("CRC32 校验不可用 (%s)，仅验证文件大小", e)

        return True

    # ═══════════════════════════════════════════════════════════════
    # 单文件刷入
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _remote_dirs_for_paths(remote_paths: Sequence[str]) -> List[str]:
        seen: Set[str] = set()
        dirs: List[str] = []
        for remote_path in remote_paths:
            normalized = remote_path.replace("\\", "/").rstrip("/")
            parent = normalized.rsplit("/", 1)[0] if "/" in normalized else ""
            if not parent or parent in (".", "/"):
                continue

            absolute = parent.startswith("/")
            parts = [p for p in parent.split("/") if p]
            current = "/" if absolute else ""
            for part in parts:
                if absolute:
                    current = "/" + part if current == "/" else current + "/" + part
                else:
                    current = part if not current else current + "/" + part
                if current not in seen:
                    seen.add(current)
                    dirs.append(current)
        return dirs

    def _mkdirs_on_device(self, remote_paths: Sequence[str]) -> None:
        dirs = self._remote_dirs_for_paths(remote_paths)
        if not dirs:
            return
        self._execute(
            "import os\n"
            f"for d in {dirs!r}:\n"
            "    try:\n"
            "        os.mkdir(d)\n"
            "    except OSError:\n"
            "        pass\n",
            timeout=max(3, len(dirs) // 4 + 3),
        )

    def _verify_files_on_device_batch(
        self,
        file_meta: Sequence[Tuple[str, int]],
        verify_mode: str = "size",
        expected_crcs: Optional[Dict[str, int]] = None,
    ) -> Dict[str, bool]:
        if verify_mode == "off":
            return {remote_path: True for remote_path, _size in file_meta}

        expected_crcs = expected_crcs or {}
        indexed_entries = [
            (idx, remote_path, size, expected_crcs.get(remote_path))
            for idx, (remote_path, size) in enumerate(file_meta)
        ]
        script = (
            "import os,gc\n"
            "try:\n"
            " import ubinascii\n"
            "except Exception:\n"
            " ubinascii=None\n"
            f"entries = {indexed_entries!r}\n"
            f"mode = {verify_mode!r}\n"
            "for idx,path,exp_size,exp_crc in entries:\n"
            " try:\n"
            "  actual=os.stat(path)[6]\n"
            "  ok=(actual==exp_size)\n"
            "  crc=-1\n"
            "  if ok and mode=='crc32' and exp_crc is not None:\n"
            "   if ubinascii is None:\n"
            "    print('OK',idx)\n"
            "    continue\n"
            "   crc=0\n"
            "   with open(path,'rb') as f:\n"
            "    while True:\n"
            "     gc.collect()\n"
            "     chunk=f.read(4096)\n"
            "     if not chunk: break\n"
            "     crc=ubinascii.crc32(chunk,crc)\n"
            "   crc=crc&0xffffffff\n"
            "   ok=(crc==exp_crc)\n"
            "  print('OK '+str(idx) if ok else 'BAD '+str(idx)+' '+str(actual)+' '+str(crc))\n"
            " except Exception as e:\n"
            "  print('ERR '+str(idx)+' '+str(e))\n"
        )
        timeout = max(5, len(indexed_entries) * (8 if verify_mode == "crc32" else 1))
        out = self._execute(script, timeout=timeout)
        result = {remote_path: False for remote_path, _size in file_meta}
        idx_to_path = {idx: remote_path for idx, remote_path, _size, _crc in indexed_entries}
        for line in out.splitlines():
            parts = line.strip().split()
            if len(parts) < 2 or parts[0] not in {"OK", "BAD", "ERR"}:
                continue
            try:
                idx = int(parts[1])
            except ValueError:
                continue
            remote_path = idx_to_path.get(idx)
            if remote_path is not None:
                result[remote_path] = parts[0] == "OK"
        return result

    @staticmethod
    def _parse_delta_header(data: bytes) -> Tuple[str, int, bool, int]:
        marker = b"DELTA:"
        start = data.find(marker)
        if start < 0:
            raise RuntimeError(f"delta header missing: {data!r}")
        end = data.find(b"\n", start)
        if end < 0:
            raise RuntimeError(f"delta header incomplete: {data!r}")
        line = data[start:end].decode("ascii", errors="replace").strip()
        parts = line.split(":")
        if len(parts) != 5 or parts[0] != "DELTA":
            raise RuntimeError(f"invalid delta header: {line!r}")
        action = parts[1]
        if action not in {"full", "suffix", "append", "truncate", "skip"}:
            raise RuntimeError(f"invalid delta action: {action!r}")
        try:
            offset = int(parts[2])
            truncate = bool(int(parts[3]))
            transfer_size = int(parts[4])
        except ValueError as exc:
            raise RuntimeError(f"invalid delta header values: {line!r}") from exc
        if offset < 0 or transfer_size < 0:
            raise RuntimeError(f"invalid delta range: {line!r}")
        return action, offset, truncate, transfer_size

    def _read_delta_header(self, timeout: int = 30) -> Tuple[str, int, bool, int, bytes]:
        deadline = time.time() + timeout
        buf = bytearray()
        while time.time() < deadline:
            if self.transport.in_waiting:
                chunk = self.transport.read(self.transport.in_waiting)
                buf.extend(chunk)
                self._record_rx(chunk)
                start = buf.find(b"DELTA:")
                if start >= 0 and buf.find(b"\n", start) >= 0:
                    data = bytes(buf)
                    action, offset, truncate, transfer_size = self._parse_delta_header(data)
                    return action, offset, truncate, transfer_size, data
            else:
                time.sleep(0.002)
        raise RuntimeError(f"delta decision timeout, received: {bytes(buf)!r}")

    def _send_flash_payload(
        self,
        script: str,
        local_path: str,
        total_size: int,
        chunk_size: int,
        offset: int = 0,
        confirm_timeout: int = 10,
        ack_every: int = BATCH_ACK_EVERY,
    ) -> None:
        self._drain_rx()
        self._write(script.encode())
        self._write(SET_EXECUTE)
        found, err_data = self._read_until_marker(b"READY", timeout=30)
        if not found:
            raise RuntimeError(f"设备未就绪: {err_data!r}")

        def _file_chunks() -> Any:
            with open(local_path, "rb") as f:
                if offset:
                    f.seek(offset)
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk

        self._send_data_with_sparse_ack(
            _file_chunks(),
            total_size,
            ack_every=ack_every,
            desc="传输中",
        )

        # 等待设备端确认脚本退出。
        self._write(b"ok")
        found, err_data = self._read_until_marker(b"ok", timeout=confirm_timeout)
        if not found:
            raise RuntimeError(f"刷入完成但设备未确认: {err_data!r}")

    def _send_delta_flash_payload(
        self,
        script: str,
        local_path: str,
        chunk_size: int,
        confirm_timeout: int = 10,
        ack_every: int = BATCH_ACK_EVERY,
    ) -> str:
        self._drain_rx()
        self._write(script.encode())
        self._write(SET_EXECUTE)

        action, offset, _truncate, transfer_size, header_buf = self._read_delta_header(timeout=30)
        if action == "skip":
            if b"ok" not in header_buf:
                found, err_data = self._read_until_marker(b"ok", timeout=confirm_timeout)
                if not found:
                    raise RuntimeError(f"delta skip did not confirm: {err_data!r}")
            return action

        if b"READY" not in header_buf:
            found, err_data = self._read_until_marker(b"READY", timeout=30)
            if not found:
                raise RuntimeError(f"device did not become ready for delta flash: {err_data!r}")

        def _file_chunks() -> Any:
            remaining = transfer_size
            with open(local_path, "rb") as f:
                if offset:
                    f.seek(offset)
                while remaining:
                    chunk = f.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        self._send_data_with_sparse_ack(
            _file_chunks(),
            transfer_size,
            ack_every=ack_every,
            desc="delta transfer",
        )

        self._write(b"ok")
        found, err_data = self._read_until_marker(b"ok", timeout=confirm_timeout)
        if not found:
            raise RuntimeError(f"delta flash completed but device did not confirm: {err_data!r}")
        return action

    def flash_file(
        self,
        local_path: str,
        remote_path: Optional[str] = None,
        compile: Optional[bool] = None,
        bytecode_ver: Optional[int] = None,
        arch: Optional[str] = None,
        active_tags: Optional[Set[str]] = None,
        dry_run: bool = False,
        safe_main: bool = False,
    ) -> None:
        """连接设备并通过原始 REPL 刷入单个文件。"""
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"本地文件不存在: {local_path}")

        should_compile = self.config.auto_compile if compile is None else compile
        tmp_dirs: List[str] = []
        actual_local = local_path
        actual_remote = (remote_path or os.path.basename(local_path)).replace("\\", "/")

        # 条件编译预处理
        if active_tags and local_path.endswith(".py"):
            from ..build import preprocess

            pp_dir = tempfile.mkdtemp()
            os.chmod(pp_dir, 0o700)
            tmp_dirs.append(pp_dir)
            pp_path = os.path.join(pp_dir, Path(local_path).name)
            Path(pp_path).write_text(
                preprocess(
                    Path(local_path).read_text(encoding="utf-8"),
                    active_tags, local_path,
                ),
                encoding="utf-8",
            )
            actual_local = pp_path
            log.debug("条件编译预处理: %s → %s", local_path, pp_path)

        # manifest.py 不上传；main.py/boot.py 不编译
        remote_basename = Path(actual_remote).name
        if remote_basename == "manifest.py":
            log.warning("'%s' 是编译所需文件，已跳过刷入", remote_basename)
            return
        if remote_basename in ("main.py", "boot.py"):
            should_compile = False

        # 编译 .py → .mpy
        if should_compile and actual_local.endswith(".py"):
            mpy_path, tmp_dir = _compile_to_mpy(actual_local, bytecode_ver, arch)
            if mpy_path:
                if tmp_dir:
                    tmp_dirs.append(tmp_dir)
                actual_local = mpy_path
                if not actual_remote.endswith(".mpy"):
                    actual_remote = actual_remote[:-3] + ".mpy"

        file_size = os.path.getsize(actual_local)
        chunk_size = self.config.chunk_size or DEFAULT_CHUNK_SIZE
        verify_mode = self.config.verify
        max_retries = self.config.max_retries
        delta_mode = self.config.delta_flash
        delta_enabled = (
            delta_mode == "on"
            or (delta_mode == "auto" and file_size > chunk_size)
        )

        local_content: Optional[bytes] = None
        expected_crc: Optional[int] = None
        if verify_mode == "crc32" or delta_enabled:
            with open(actual_local, "rb") as f:
                local_content = f.read()
            if verify_mode == "crc32":
                expected_crc = self._compute_crc32(local_content)

        log.info(
            "刷入: %s → %s (%d 字节, 块=%d)",
            local_path, actual_remote, file_size, chunk_size,
        )
        self._trace_event(
            "operation_prepared",
            phase="prepare",
            local_path=local_path,
            remote_path=actual_remote,
            file_size=file_size,
            chunk_size=chunk_size,
            verify_mode=verify_mode,
            delta_enabled=delta_enabled,
        )

        safe_main_plan = self.plan_safe_main_overwrites(
            [actual_remote],
            enabled=safe_main,
        )
        if dry_run:
            self._trace_event("operation_skipped", phase="prepare", reason="dry_run")
            log.info("[DRY-RUN] 将刷入 %s → %s (%d 字节)", local_path, actual_remote, file_size)
            for plan in safe_main_plan:
                log.info(
                    "[DRY-RUN] 将备份 %s → %s",
                    plan.remote_path,
                    plan.backup_path,
                )
            return

        upload_ack_every = self._upload_ack_every()
        inline_verify_code = _build_inline_verify_code(
            actual_remote,
            file_size,
            verify_mode,
            expected_crc,
            chunk_size,
        )
        inline_verify_timeout = 0
        if verify_mode == "crc32":
            inline_verify_timeout = max(15, file_size // 65536 + 15)
        def _flash_once(allow_delta: bool) -> str:
            confirm_timeout = max(10, inline_verify_timeout)

            if allow_delta and local_content is not None:
                with self._trace_phase_ctx("transfer_delta", remote_path=actual_remote):
                    local_blocks = _compute_block_crc32(local_content, chunk_size)
                    delta_script = (
                        FLASH_DELTA.replace("FILE", repr(actual_remote))
                        .replace("LOCAL_BLOCKS", repr(local_blocks))
                        .replace("BLOCK_SIZE", str(chunk_size))
                        .replace("BFSIZE", str(chunk_size))
                        .replace("FSIZE", str(file_size))
                        .replace("ACK_EVERY", str(upload_ack_every))
                        .replace("VERIFY_CODE", inline_verify_code)
                    )
                    action = self._send_delta_flash_payload(
                        delta_script,
                        actual_local,
                        chunk_size,
                        confirm_timeout=max(10, file_size // max(chunk_size, 1) + confirm_timeout),
                        ack_every=upload_ack_every,
                    )
                    if action != "skip":
                        log.info("delta flash action=%s (%s)", action, actual_remote)
                    return action

            with self._trace_phase_ctx("transfer_full", remote_path=actual_remote):
                full_script = (
                    FLASH.replace("FILE", repr(actual_remote))
                    .replace("BFSIZE", str(chunk_size))
                    .replace("FSIZE", str(file_size))
                    .replace("ACK_EVERY", str(upload_ack_every))
                    .replace("VERIFY_CODE", inline_verify_code)
                )
                self._send_flash_payload(
                    full_script,
                    actual_local,
                    file_size,
                    chunk_size,
                    confirm_timeout=confirm_timeout,
                    ack_every=upload_ack_every,
                )

                return "full"

        _t0 = time.time()
        try:
            with self._traffic_log_ctx():
                if safe_main_plan:
                    with self._trace_phase_ctx("safe_main"):
                        self.safe_break()
                with self._trace_phase_ctx("raw_repl"):
                    self._enter_raw_repl()

                for plan in safe_main_plan:
                    with self._trace_phase_ctx("safe_main", remote_path=plan.remote_path):
                        self._backup_remote_file_if_exists(
                            plan.remote_path,
                            plan.backup_path,
                        )

                # 创建远程目录
                with self._trace_phase_ctx("filesystem", remote_path=actual_remote):
                    self._mkdirs_on_device([actual_remote])

                if delta_enabled:
                    for attempt in range(max_retries + 1):
                        if attempt > 0:
                            with self._trace_phase_ctx("raw_repl", retry=attempt):
                                self._enter_raw_repl()
                            log.warning("增量刷入重试 %d/%d", attempt, max_retries)
                        try:
                            action = _flash_once(allow_delta=True)
                            elapsed = time.time() - _t0
                            if action == "skip":
                                log.info("文件已一致，跳过刷入 (%s)", actual_remote)
                            elif verify_mode == "off":
                                log.info("刷入成功 (校验已关闭) (%s)", actual_remote)
                            else:
                                rate = file_size / elapsed / 1024 if elapsed > 0 else 0
                                log.info(
                                    "刷入成功 (%s): %.1f KB, %.1fs, %.0f KB/s",
                                    actual_remote, file_size / 1024, elapsed, rate,
                                )
                            return
                        except (serial.SerialException, ConnectionError, RuntimeError) as e:
                            if attempt >= max_retries:
                                log.warning("增量刷入失败，回退全量刷入: %s", e)
                                break
                            log.warning(
                                "%s，准备重试增量刷入 (%d/%d)...",
                                e, attempt + 1, max_retries,
                            )

                for attempt in range(max_retries + 1):
                    if attempt > 0:
                        with self._trace_phase_ctx("raw_repl", retry=attempt):
                            self._enter_raw_repl()
                        log.warning("重试 %d/%d", attempt, max_retries)

                    try:
                        _flash_once(allow_delta=False)
                        if verify_mode != "off":
                            elapsed = time.time() - _t0
                            rate = file_size / elapsed / 1024 if elapsed > 0 else 0
                            log.info(
                                "刷入成功 (%s): %.1f KB, %.1fs, %.0f KB/s",
                                actual_remote, file_size / 1024, elapsed, rate,
                            )
                        else:
                            log.info("刷入成功 (校验已关闭) (%s)", actual_remote)
                        return

                    except (serial.SerialException, ConnectionError, RuntimeError) as e:
                        if attempt >= max_retries:
                            raise
                        log.warning(
                            "%s，准备重试 (%d/%d)...", e, attempt + 1, max_retries,
                        )
        finally:
            for d in tmp_dirs:
                shutil.rmtree(d, ignore_errors=True)

    # ═══════════════════════════════════════════════════════════════
    # 批量刷入
    # ═══════════════════════════════════════════════════════════════

    def flash_entries(
        self,
        entries: Sequence[Tuple[str, str]],
        bytecode_ver: Optional[int] = None,
        arch: Optional[str] = None,
        active_tags: Optional[Set[str]] = None,
        dry_run: bool = False,
        safe_main: bool = False,
    ) -> List[Tuple[str, str, bool]]:
        tmp_dirs: List[str] = []
        verify_mode = self.config.verify
        chunk_size = self.config.chunk_size or DEFAULT_CHUNK_SIZE
        expected_crcs: Dict[str, int] = {}
        t0_prepare = time.time()

        try:
            prep: List[Tuple[str, str, str, bool]] = []
            for lp, rp in entries:
                if Path(rp).name == "manifest.py" or lp.endswith(".pyi"):
                    continue
                actual_local = lp
                actual_remote = rp.replace("\\", "/")
                if active_tags and actual_local.endswith(".py"):
                    from ..build import preprocess

                    pp_dir = tempfile.mkdtemp()
                    os.chmod(pp_dir, 0o700)
                    tmp_dirs.append(pp_dir)
                    pp_path = os.path.join(pp_dir, Path(actual_local).name)
                    Path(pp_path).write_text(
                        preprocess(
                            Path(actual_local).read_text(encoding="utf-8"),
                            active_tags,
                            actual_local,
                        ),
                        encoding="utf-8",
                    )
                    actual_local = pp_path

                basename = Path(actual_remote).name
                needs_compile = (
                    self.config.auto_compile
                    and basename not in ("main.py", "boot.py")
                    and actual_local.endswith(".py")
                )
                prep.append((lp, actual_local, actual_remote, needs_compile))

            compile_jobs = [actual for _src, actual, _remote, needs in prep if needs]
            t0_compile = time.time()
            compiled = _compile_files_parallel(compile_jobs, bytecode_ver, arch)
            compile_elapsed = time.time() - t0_compile

            final_jobs: List[Tuple[str, str, str]] = []
            for source_local, actual_local, actual_remote, needs_compile in prep:
                if needs_compile and actual_local in compiled:
                    mpy_path, mpy_tmp_dir = compiled[actual_local]
                    if mpy_path:
                        if mpy_tmp_dir:
                            tmp_dirs.append(mpy_tmp_dir)
                        actual_local = mpy_path
                        if not actual_remote.endswith(".mpy"):
                            actual_remote = actual_remote[:-3] + ".mpy"
                final_jobs.append((source_local, actual_local, actual_remote))

            def read_one(job: Tuple[str, str, str]) -> _PreparedFlashFile:
                source_local, actual_local, actual_remote = job
                content = Path(actual_local).read_bytes()
                return _PreparedFlashFile(
                    source_path=source_local,
                    remote_path=actual_remote,
                    local_path=actual_local,
                    content=content,
                    size=len(content),
                )

            prepared_by_index: Dict[int, _PreparedFlashFile] = {}
            workers = min(8, max(1, len(final_jobs)))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {
                    executor.submit(read_one, job): idx
                    for idx, job in enumerate(final_jobs)
                }
                for future in as_completed(future_map):
                    prepared_by_index[future_map[future]] = future.result()
            prepared = [
                prepared_by_index[idx]
                for idx in range(len(final_jobs))
                if idx in prepared_by_index
            ]

            if verify_mode == "crc32":
                with ThreadPoolExecutor(max_workers=min(8, max(1, len(prepared)))) as executor:
                    future_map = {
                        executor.submit(self._compute_crc32, item.content): item.remote_path
                        for item in prepared
                    }
                    for future in as_completed(future_map):
                        expected_crcs[future_map[future]] = future.result()

            if not prepared:
                log.info("No files to flash")
                return []

            all_data = b"".join(item.content for item in prepared)
            file_meta = [(item.size, item.remote_path) for item in prepared]
            verify_meta = [(item.remote_path, item.size) for item in prepared]
            prepare_elapsed = time.time() - t0_prepare
            log.debug(
                "flash prepare: files=%d bytes=%d prepare=%.3fs compile=%.3fs",
                len(prepared),
                len(all_data),
                prepare_elapsed,
                compile_elapsed,
            )

            safe_main_plan = self.plan_safe_main_overwrites(
                [item.remote_path for item in prepared],
                enabled=safe_main,
            )
            if dry_run:
                log.info("[DRY-RUN] would flash %d files", len(prepared))
                for item in prepared:
                    log.info(
                        "  %s -> %s (%d bytes)",
                        item.source_path,
                        item.remote_path,
                        item.size,
                    )
                for plan in safe_main_plan:
                    log.info(
                        "[DRY-RUN] would back up %s -> %s",
                        plan.remote_path,
                        plan.backup_path,
                    )
                return []

            upload_ack_every = self._upload_ack_every()
            inline_batch_verify_code = _build_inline_batch_verify_code(
                verify_meta,
                verify_mode,
                expected_crcs,
                chunk_size,
            )
            script = (
                FLASH_PROGRAM.replace("FILES", repr(file_meta))
                .replace("BFSIZE", str(chunk_size))
                .replace("ACK_EVERY", str(upload_ack_every))
                .replace("VERIFY_CODE", inline_batch_verify_code)
            )
            max_retries = self.config.max_retries
            transfer_t0 = time.time()
            safe_break_done = False
            backup_done = False

            for attempt in range(max_retries + 1):
                if safe_main_plan and not safe_break_done:
                    self.safe_break()
                    safe_break_done = True
                self._enter_raw_repl()
                if attempt > 0:
                    log.warning("retry %d/%d", attempt, max_retries)

                if safe_main_plan and not backup_done:
                    for plan in safe_main_plan:
                        self._backup_remote_file_if_exists(
                            plan.remote_path,
                            plan.backup_path,
                        )
                    backup_done = True

                self._mkdirs_on_device([item.remote_path for item in prepared])

                with self._traffic_log_ctx():
                    log.info("Batch flashing %d files", len(prepared))
                    for item in prepared:
                        log.debug(
                            "  %s -> %s (%d bytes)",
                            item.source_path,
                            item.remote_path,
                            item.size,
                        )

                    try:
                        self._write(script.encode() + SET_EXECUTE)
                        found, err_data = self._read_until_marker(b"READY", timeout=30)
                        if not found:
                            raise RuntimeError(f"device not ready: {err_data!r}")

                        total_data_size = len(all_data)
                        self._send_data_with_sparse_ack(
                            (
                                all_data[i : i + chunk_size]
                                for i in range(0, total_data_size, chunk_size)
                            ),
                            total_data_size,
                            ack_every=upload_ack_every,
                        )

                        found, prompt_data = self._read_until_marker(b">", timeout=10)
                        if not found:
                            elapsed = time.time() - transfer_t0
                            rate = total_data_size / elapsed / 1024 if elapsed > 0 else 0
                            log.warning(
                                "flash completed but prompt was not received; skipping verification"
                            )
                            log.info(
                                "Flash complete without verification: %.1f KB, %d files, %.1fs, %.0f KB/s",
                                total_data_size / 1024,
                                len(prepared),
                                elapsed,
                                rate,
                            )
                            return [
                                (item.source_path, item.remote_path, True)
                                for item in prepared
                            ]

                        if b"FLASH_ERR:" in prompt_data or b"Traceback" in prompt_data:
                            raise RuntimeError(
                                "device batch flash failed: "
                                + prompt_data.decode("utf-8", errors="replace")
                            )

                        elapsed = time.time() - transfer_t0
                        rate = total_data_size / elapsed / 1024 if elapsed > 0 else 0
                        log.info(
                            "Batch flash successful: %.1f KB, %d files, %.1fs, %.0f KB/s",
                            total_data_size / 1024,
                            len(prepared),
                            elapsed,
                            rate,
                        )
                        return [
                            (item.source_path, item.remote_path, True)
                            for item in prepared
                        ]

                    except (serial.SerialException, ConnectionError, RuntimeError) as e:
                        if attempt >= max_retries:
                            log.error("Batch flash failed: %s", e)
                            return [
                                (item.source_path, item.remote_path, False)
                                for item in prepared
                            ]
                        log.warning(
                            "transfer error; retrying (%d/%d)",
                            attempt + 1,
                            max_retries,
                        )
            return []
        finally:
            for d in tmp_dirs:
                shutil.rmtree(d, ignore_errors=True)

    def flash_program(
        self,
        local_dir: str,
        remote_prefix: str = "",
        bytecode_ver: Optional[int] = None,
        arch: Optional[str] = None,
        active_tags: Optional[Set[str]] = None,
        manifest_path: Optional[str] = None,
        dry_run: bool = False,
        safe_main: bool = False,
    ) -> List[Tuple[str, str, bool]]:
        """连接设备并递归刷入整个本地目录。"""
        if not os.path.isdir(local_dir):
            raise NotADirectoryError(f"不是有效目录: {local_dir}")

        # 收集文件清单
        if manifest_path:
            from ..build import load_manifest

            entries = load_manifest(manifest_path, active_tags or set(), base_dir=local_dir)
        else:
            entries = []
            for root, _dirs, files in os.walk(local_dir):
                for fn in files:
                    if not fn.endswith(".py"):
                        continue
                    lp = os.path.join(root, fn)
                    rp = os.path.join(
                        remote_prefix, os.path.relpath(lp, local_dir),
                    ).replace("\\", "/")
                    entries.append((lp, rp))

        return self.flash_entries(
            entries,
            bytecode_ver=bytecode_ver,
            arch=arch,
            active_tags=active_tags,
            dry_run=dry_run,
            safe_main=safe_main,
        )

    # 设备信息查询
    # ═══════════════════════════════════════════════════════════════

    def get_mpy_version(self) -> Tuple[Optional[int], Optional[str]]:
        """从设备读取 mpy 字节码版本号和架构。"""
        try:
            out = self.run(
                "import sys\n"
                "m=sys.implementation.mpy\n"
                "a=[None,'x86','x64','armv6','armv6m','armv7m','armv7em','armv7emsp','armv7emdp','xtensa','xtensawin'][m>>10]\n"
                "print(m&0xff,a or '')",
            )
            parts = out.strip().split()
            ver = int(parts[0])
            arch = parts[1] if len(parts) > 1 and parts[1] else None
            log.debug("mpy 版本: ver=%d, arch=%s", ver, arch)
            return ver, arch
        except Exception as e:
            log.debug("获取 mpy 版本失败: %s", e)
            return None, None

    def detect_tags(self) -> Set[str]:
        """从设备读取 board 信息，返回 active_tags 集合。"""
        try:
            out = self.run(
                "import os,sys\nprint(os.uname().machine)\nprint(sys.platform)",
            )
        except Exception as e:
            log.debug("设备 tag 检测失败: %s", e)
            return set()

        lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
        combined = " ".join(lines).upper()
        board_tags = self.config.board_tags
        tags: Set[str] = set()
        for kw, tag_list in board_tags.items():
            if kw in combined:
                tags.update(tag_list)
                break
        if len(lines) > 1:
            tags.add(lines[1].upper())
        log.debug("检测到设备 tags: %s", tags)
        return tags

    def run(self, code: str, timeout: int = 10) -> str:
        """在设备上执行任意 Python 代码并返回输出。"""
        self._enter_raw_repl()
        return self._execute(code, timeout=timeout)

    def reset(self) -> None:
        """复位设备。优先使用 DTR/RTS 硬件复位，否则使用软重启。"""
        log.debug("复位设备 %s", self.port)
        if isinstance(self.transport, SerialTransport):
            try:
                self.transport.dtr_rts_reset()
                return
            except Exception as e:
                log.trace("DTR/RTS 复位失败，尝试软重启: %s", e)
        self._enter_raw_repl()
        try:
            self._execute("import machine; machine.reset()", timeout=2)
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════
    # 设备文件读取（bytes 协议）
    # ═══════════════════════════════════════════════════════════════

    def _read_device_file(self, remote_path: str) -> bytes:
        """从设备读取文件内容（原始字节传输，兼容二进制）。

        两阶段协议：
        1. 通过标准 execute 获取文件大小
        2. 在 Raw REPL 中直接执行脚本输出原始字节
        """
        # 阶段 1：获取文件大小
        out = self.run(
            f"import os; print(os.stat({remote_path!r})[6])", timeout=5,
        )
        expected_size = int(out.strip().splitlines()[-1])
        log.trace("设备文件 %s 大小: %d 字节", remote_path, expected_size)

        # 阶段 2：读取原始字节
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
        need = 2 + expected_size + 3
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

        raw = buf[2:] if buf.startswith(b"OK") else buf
        raw = _strip_repl_trailer(raw)
        return raw[:expected_size]

    # ═══════════════════════════════════════════════════════════════
    # 设备文件管理 (fs)
    # ═══════════════════════════════════════════════════════════════

    def fs_ls(self, remote_path: str = "/") -> List[Dict[str, str]]:
        """列出设备目录下的文件和子目录。"""
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
        items: List[Dict[str, str]] = []
        for line in out.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 2)
            if len(parts) == 3:
                items.append({"size": parts[0], "type": parts[1], "name": parts[2]})
        log.trace("fs_ls(%s): %d 个条目", remote_path, len(items))
        return items

    def fs_ls_recursive(self, remote_path: str = "/") -> List[Dict[str, str]]:
        """递归列出设备目录下的所有文件和子目录。"""
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
        items: List[Dict[str, str]] = []
        for line in out.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 2)
            if len(parts) == 3:
                items.append({"size": parts[0], "type": parts[1], "name": parts[2]})
        log.trace("fs_ls_recursive(%s): %d 个条目", remote_path, len(items))
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
            parts = line.strip().split("|")
            if len(parts) == 3:
                result = {
                    "total": int(parts[0]),
                    "used": int(parts[1]),
                    "free": int(parts[2]),
                }
                log.trace("fs_df: total=%d used=%d free=%d", result["total"], result["used"], result["free"])
                return result
        log.trace("fs_df: 无法获取")
        return {"total": 0, "used": 0, "free": 0}

    def fs_rm(
        self,
        remote_path: str,
        recursive: bool = False,
        force: bool = False,
        max_depth: int = 32,
    ) -> bool:
        """删除设备上的文件或递归删除目录。"""
        lit = repr(remote_path)
        if recursive:
            guard = " except:\n  pass\n" if force else ""
            script = (
                "import os\n"
                "def _rmrf(p,d=0):\n"
                " try:\n"
                "  s=os.stat(p)\n"
                "  if s[0]&0x4000:\n"
                f"   if d>{max_depth}: return\n"
                "   for n in os.listdir(p):\n"
                "    fp=n if p=='/' else p+'/'+n\n"
                "    _rmrf(fp,d+1)\n"
                "   os.rmdir(p)\n"
                "  else: os.remove(p)\n"
                f"{guard}"
                f"_rmrf({lit})\n"
                "print('OK')\n"
            )
        else:
            if force:
                script = (
                    "import os\n"
                    f"try:\n os.remove({lit})\n"
                    "except:\n pass\n"
                    "print('OK')\n"
                )
            else:
                script = (
                    "import os\n"
                    f"os.remove({lit})\n"
                    "print('OK')\n"
                )
        out = self.run(script)
        ok = "OK" in out
        if ok:
            log.debug("已删除: %s", remote_path)
        else:
            log.warning("删除失败: %s", remote_path)
        return ok

    def fs_cat(self, remote_path: str) -> str:
        """读取设备上文本文件的内容。"""
        log.trace("fs_cat: %s", remote_path)
        script = f"print(open({remote_path!r}).read())\n"
        return self.run(script)

    def fs_get(self, remote_path: str, local_path: str) -> int:
        """从设备下载文件到本地路径。"""
        log.debug("fs_get: %s → %s", remote_path, local_path)
        data = self._read_device_file(remote_path)
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(data)
        return len(data)

    def fs_mv(self, src: str, dst: str) -> bool:
        """重命名/移动设备上的文件或目录。"""
        log.debug("fs_mv: %s → %s", src, dst)
        script = (
            "import os\n"
            f"os.rename({src!r}, {dst!r})\n"
            "print('OK')\n"
        )
        out = self.run(script)
        return "OK" in out

    def fs_cp(self, src: str, dst: str) -> bool:
        """复制设备上的文件或目录。"""
        log.debug("fs_cp: %s → %s", src, dst)
        script = (
            "import os\n"
            "try:\n"
            " s=os.stat(%r)\n"
            " if s[0]&0x4000:\n"
            "  os.mkdir(%r)\n"
            "  def _cpdir(sd,dd):\n"
            "   for n in os.listdir(sd):\n"
            "    fp=sd+'/'+n; td=dd+'/'+n\n"
            "    try:\n"
            "     st=os.stat(fp)\n"
            "     if st[0]&0x4000:\n"
            "      os.mkdir(td)\n"
            "      _cpdir(fp,td)\n"
            "     else:\n"
            "      with open(fp,'rb') as f:\n"
            "       with open(td,'wb') as t:\n"
            "        t.write(f.read())\n"
            "    except:\n"
            "     pass\n"
            "  _cpdir(%r,%r)\n"
            " else:\n"
            "  with open(%r,'rb') as f:\n"
            "   with open(%r,'wb') as t:\n"
            "    t.write(f.read())\n"
            " print('OK')\n"
            "except Exception as e:\n"
            " print('ERR:'+str(e))\n"
        ) % (src, dst, src, dst, src, dst)
        out = self.run(script)
        return "OK" in out

    def fs_tree(self, remote_path: str = "/") -> str:
        """以树形结构列出设备目录内容。"""
        log.trace("fs_tree: %s", remote_path)
        items = self.fs_ls_recursive(remote_path)
        if not items:
            return "  (空目录)"

        # 从扁平列表构建树
        tree: Dict[str, Any] = {}
        for item in items:
            path = item["name"]
            parts = path.strip("/").split("/")
            node = tree
            for p in parts:
                if p not in node:
                    node[p] = {}
                node = node[p]

        def _render(
            node: Dict[str, Any],
            prefix: str = "",
            is_last: bool = True,
            is_root: bool = True,
        ) -> List[str]:
            lines: List[str] = []
            keys = sorted(
                node.keys(),
                key=lambda k: (0 if isinstance(node[k], dict) else 1, k),
            )
            for i, k in enumerate(keys):
                connector = "└── " if i == len(keys) - 1 else "├── "
                sub_prefix = "    " if i == len(keys) - 1 else "│   "
                is_dir = isinstance(node[k], dict)
                suffix = "/" if is_dir else ""
                if is_root:
                    lines.append(f"  {connector}{k}{suffix}")
                else:
                    lines.append(f"{prefix}{connector}{k}{suffix}")
                if is_dir:
                    lines.extend(
                        _render(
                            node[k],
                            prefix + sub_prefix,
                            i == len(keys) - 1,
                            False,
                        )
                    )
            return lines

        result = [f"  {remote_path}/"]
        result.extend(_render(tree))
        return "\n".join(result)

try:
    del _name
except NameError:
    pass

__all__ = sorted(
    _name
    for _name in globals()
    if not (_name.startswith("__") and _name.endswith("__"))
    and _name not in {"_core", "_core_export_names"}
)

del _core_export_names

"""
pyrite-cli 统一日志系统。

提供命名 Logger、6 级日志、控制台彩色输出、JSONL 与可读文本文件记录、
操作计时上下文管理器、串口/WebSocket 流量监控。

用法::

    from cli.utils.log import get_logger
    log = get_logger(__name__)

    log.trace("原始字节: %r", data)
    log.debug("连接端口 %s", port)
    log.info("刷入 %d 个文件", 3)
    log.warning("超时，重试中")
    log.error("连接失败: %s", err)
    log.fatal("无法恢复")

    with log.operation("flash_file", path="/main.py", size=4096):
        ...  # 自动记录开始/结束/耗时/成败

流量数据写入日志文件时，JSONL 中 ``type`` 字段为 ``"traffic"``。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, TextIO

DEFAULT_JSON_LOG_NAME = "pyrite.jsonl"
DEFAULT_TEXT_LOG_NAME = "pyrite.log"
DEFAULT_LOG_FILE_LIMIT = 25

# ═══════════════════════════════════════════════════════════════════
# 日志级别
# ═══════════════════════════════════════════════════════════════════

TRACE = 5
DEBUG = 10
INFO = 20
WARN = 30
WARNING = 30  # 别名，兼容旧代码
ERROR = 40
FATAL = 50
SILENT = 100

_LEVEL_NAMES: Dict[int, str] = {
    TRACE: "TRACE",
    DEBUG: "DEBUG",
    INFO: "INFO",
    WARN: "WARN",
    ERROR: "ERROR",
    FATAL: "FATAL",
}

# ANSI 颜色（避免循环导入 ansi.py）
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_COLORS: Dict[int, str] = {
    TRACE: "\033[90m",   # 亮黑（灰色）
    DEBUG: "\033[36m",   # 青色
    INFO: "\033[32m",    # 绿色
    WARN: "\033[33m",    # 黄色
    ERROR: "\033[31m",   # 红色
    FATAL: "\033[35m",   # 紫色
}


def safe_text(value: object, *, preserve_newlines: bool = True) -> str:
    """Return text safe for terminal/log display.

    Device-controlled strings may contain ANSI CSI/OSC or C0/C1 controls.
    Render those bytes as visible escapes so they cannot clear the terminal,
    spoof output, or write to the clipboard. Newlines are optionally preserved
    for multiline human-readable output such as trees and diffs.
    """
    text = value if isinstance(value, str) else str(value)
    out: List[str] = []
    for ch in text:
        code = ord(ch)
        if preserve_newlines and ch == "\n":
            out.append(ch)
        elif ch == "\x1b":
            out.append("\\x1b")
        elif code <= 0x1F or code == 0x7F or 0x80 <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    return "".join(out)


def _safe_field(value: object) -> str:
    return safe_text(value, preserve_newlines=False)


def _safe_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return safe_text(value, preserve_newlines=True)
    if isinstance(value, dict):
        return {
            _safe_field(key): _safe_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_json_value(item) for item in value]
    return value


# ═══════════════════════════════════════════════════════════════════
# 日志记录
# ═══════════════════════════════════════════════════════════════════

class LogRecord:
    """单条日志记录，包含所有结构化字段。"""

    __slots__ = (
        "ts", "level", "level_name", "module", "msg", "op",
        "op_status", "duration_ms", "type", "dir", "raw_hex",
        "text", "exc_text", "extra",
    )

    def __init__(
        self,
        level: int,
        module: str,
        msg: str,
        op: Optional[str] = None,
        op_status: Optional[str] = None,
        duration_ms: Optional[float] = None,
        record_type: Optional[str] = None,
        direction: Optional[str] = None,
        raw_hex: Optional[str] = None,
        text: Optional[str] = None,
        exc_text: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.ts = time.time()
        self.level = level
        self.level_name = _LEVEL_NAMES.get(level, "?")
        self.module = module
        self.msg = msg
        self.op = op
        self.op_status = op_status
        self.duration_ms = duration_ms
        self.type = record_type
        self.dir = direction
        self.raw_hex = raw_hex
        self.text = text
        self.exc_text = exc_text
        self.extra = extra or {}


# ═══════════════════════════════════════════════════════════════════
# Handler 基类和实现
# ═══════════════════════════════════════════════════════════════════

class Handler:
    """日志处理器基类。"""

    def __init__(self, level: int = TRACE) -> None:
        self.level = level

    def emit(self, record: LogRecord) -> None:
        """子类实现：输出一条日志记录。"""
        raise NotImplementedError

    def close(self) -> None:
        """子类实现：关闭处理器资源。"""
        pass


class ConsoleHandler(Handler):
    """输出 ANSI 彩色日志到 stderr。"""

    def emit(self, record: LogRecord) -> None:
        if record.level < self.level:
            return

        color = _COLORS.get(record.level, "")
        level_str = f"{color}{record.level_name:<5}{_RESET}"
        module_str = f"{_DIM}[{record.module}]{_RESET}"

        if record.type == "traffic":
            # 流量记录：紧凑格式
            if record.raw_hex:
                detail = f" [hex] {record.raw_hex}"
            elif record.text:
                detail = f" {_safe_field(record.text.strip())}"
            else:
                detail = ""
            line = f"  {level_str} {module_str} {record.dir}{detail}"
        elif record.op:
            icon = {"start": "▶", "end": "✓", "error": "✗"}.get(record.op_status or "", " ")
            dur = f" ({record.duration_ms:.0f}ms)" if record.duration_ms is not None else ""
            line = f"  {level_str} {module_str} {icon} {safe_text(record.msg)}{dur}"
        else:
            line = f"  {level_str} {module_str} {safe_text(record.msg)}"

        # 附加字段
        if record.extra:
            extras = " ".join(
                f"{_safe_field(k)}={_safe_field(v)}"
                for k, v in record.extra.items()
            )
            line += f"  {_DIM}{extras}{_RESET}"

        sys.stderr.write(line + "\n")
        sys.stderr.flush()

        # 异常堆栈
        if record.exc_text:
            exc_text = safe_text(record.exc_text, preserve_newlines=True)
            sys.stderr.write(f"{_COLORS[ERROR]}{exc_text}{_RESET}\n")


def _rotated_log_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.stem}.{index}{path.suffix}")


def _rotation_candidates(path: Path) -> Iterator[tuple[int, Path]]:
    prefix = f"{path.stem}."
    suffix = path.suffix
    for candidate in path.parent.glob(f"{path.stem}.*{suffix}"):
        name = candidate.name
        if not name.startswith(prefix) or not name.endswith(suffix):
            continue
        middle = name[len(prefix):len(name) - len(suffix)]
        if middle.isdigit():
            yield int(middle), candidate


def _rotate_fixed_log(path: Path, max_files: int) -> None:
    max_files = max(1, max_files)
    path.parent.mkdir(parents=True, exist_ok=True)

    for index, candidate in list(_rotation_candidates(path)):
        if index >= max_files:
            candidate.unlink(missing_ok=True)

    if not path.exists() or path.stat().st_size == 0:
        return

    if max_files == 1:
        path.unlink(missing_ok=True)
        return

    for index in range(max_files - 2, 0, -1):
        src = _rotated_log_path(path, index)
        if src.exists():
            src.replace(_rotated_log_path(path, index + 1))

    path.replace(_rotated_log_path(path, 1))


class _RotatingFileHandler(Handler):
    """固定文件名日志处理器基类，首次写入前轮转旧日志。"""

    def __init__(self, log_path: str, level: int = TRACE, max_files: int = DEFAULT_LOG_FILE_LIMIT) -> None:
        super().__init__(level)
        self._path = log_path
        self._max_files = max(1, max_files)
        self._file: Optional[TextIO] = None
        self._lock = threading.Lock()
        self._prepared = False

    def _ensure_open(self) -> None:
        if self._file is None:
            path = Path(self._path)
            if not self._prepared:
                _rotate_fixed_log(path, self._max_files)
                self._prepared = True
            self._file = open(path, "w", encoding="utf-8")

    @property
    def path(self) -> str:
        return self._path

    def close(self) -> None:
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None


class JSONLFileHandler(_RotatingFileHandler):
    """将日志以 JSONL 格式写入单个文件。

    包含结构化日志和流量数据，全部合入同一文件。
    """

    def emit(self, record: LogRecord) -> None:
        if record.level < self.level:
            return

        with self._lock:
            self._ensure_open()
            obj: Dict[str, Any] = {
                "ts": datetime.fromtimestamp(record.ts).strftime("%H:%M:%S.%f")[:-3],
                "level": record.level_name,
                "module": record.module,
                "msg": safe_text(record.msg),
            }

            if record.type == "traffic":
                obj["type"] = "traffic"
                obj["dir"] = record.dir
                if record.raw_hex:
                    obj["hex"] = record.raw_hex
                if record.text:
                    obj["text"] = _safe_field(record.text.strip())
            else:
                if record.op:
                    obj["op"] = record.op
                    obj["op_status"] = record.op_status
                    if record.duration_ms is not None:
                        obj["duration_ms"] = round(record.duration_ms, 1)
                if record.exc_text:
                    obj["exc"] = safe_text(record.exc_text, preserve_newlines=True)
                if record.extra:
                    obj["extra"] = _safe_json_value(record.extra)

            self._file.write(json.dumps(obj, ensure_ascii=False) + "\n")  # type: ignore[union-attr]
            self._file.flush()


class TextFileHandler(_RotatingFileHandler):
    """将日志写入阅读友好的纯文本文件。"""

    def emit(self, record: LogRecord) -> None:
        if record.level < self.level:
            return

        with self._lock:
            self._ensure_open()
            self._file.write(_format_text_record(record) + "\n")  # type: ignore[union-attr]
            if record.exc_text:
                exc_text = safe_text(record.exc_text.rstrip(), preserve_newlines=True)
                self._file.write(exc_text + "\n")  # type: ignore[union-attr]
            self._file.flush()


def _format_text_record(record: LogRecord) -> str:
    ts = datetime.fromtimestamp(record.ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"{ts} {record.level_name:<5} [{record.module}]"

    fields: List[str] = []
    if record.type == "traffic":
        detail = _safe_field(record.dir or "")
        if record.raw_hex:
            fields.append(f"hex={record.raw_hex}")
        if record.text:
            fields.append(f"text={_safe_field(record.text.strip())}")
        return f"{line} {detail}{_format_fields(fields)}"

    msg = safe_text(record.msg)
    if record.op:
        fields.append(f"op={record.op}")
        if record.op_status:
            fields.append(f"op_status={record.op_status}")
        if record.duration_ms is not None:
            fields.append(f"duration_ms={round(record.duration_ms, 1)}")
    for key, value in record.extra.items():
        fields.append(f"{_safe_field(key)}={_safe_field(value)}")
    return f"{line} {msg}{_format_fields(fields)}"


def _format_fields(fields: List[str]) -> str:
    if not fields:
        return ""
    return "  " + " ".join(fields)


# ═══════════════════════════════════════════════════════════════════
# LogManager — 全局单例
# ═══════════════════════════════════════════════════════════════════

class LogManager:
    """中央日志管理器，维护所有 handler 并路由日志记录。"""

    def __init__(self) -> None:
        self._handlers: List[Handler] = []
        self._lock = threading.Lock()
        self._configured = False
        self.traffic_enabled = True

    def add_handler(self, handler: Handler) -> None:
        with self._lock:
            self._handlers.append(handler)

    def remove_handler(self, handler: Handler) -> None:
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def emit(self, record: LogRecord) -> None:
        if record.type == "traffic" and not self.traffic_enabled:
            return
        with self._lock:
            for h in self._handlers:
                try:
                    h.emit(record)
                except Exception:
                    pass  # 日志处理器崩溃不应影响主流程

    def close(self) -> None:
        with self._lock:
            for h in self._handlers:
                try:
                    h.close()
                except Exception:
                    pass
            self._handlers.clear()
            self._configured = False
            self.traffic_enabled = True

    @property
    def jsonl_path(self) -> Optional[str]:
        """返回 JSONL 文件路径（如果已配置），供外部引用。"""
        with self._lock:
            for h in self._handlers:
                if isinstance(h, JSONLFileHandler):
                    return h.path
        return None

    @property
    def text_path(self) -> Optional[str]:
        """返回可读文本日志路径（如果已配置）。"""
        with self._lock:
            for h in self._handlers:
                if isinstance(h, TextFileHandler):
                    return h.path
        return None


# 全局单例
_mgr = LogManager()


# ═══════════════════════════════════════════════════════════════════
# Logger — 命名日志器
# ═══════════════════════════════════════════════════════════════════

class Logger:
    """命名日志器，绑定到特定模块。

    通常通过 ``get_logger(__name__)`` 创建，不要直接实例化。
    """

    def __init__(self, name: str) -> None:
        self._name = name

    # ── 基础日志方法 ──

    def trace(self, msg: str, *args: object, **extra: Any) -> None:
        if args:
            msg = msg % args
        _mgr.emit(LogRecord(TRACE, self._name, msg, extra=extra or None))

    def debug(self, msg: str, *args: object, **extra: Any) -> None:
        if args:
            msg = msg % args
        _mgr.emit(LogRecord(DEBUG, self._name, msg, extra=extra or None))

    def info(self, msg: str, *args: object, **extra: Any) -> None:
        if args:
            msg = msg % args
        _mgr.emit(LogRecord(INFO, self._name, msg, extra=extra or None))

    def warning(self, msg: str, *args: object, **extra: Any) -> None:
        if args:
            msg = msg % args
        _mgr.emit(LogRecord(WARN, self._name, msg, extra=extra or None))

    def error(self, msg: str, *args: object, **extra: Any) -> None:
        exc_text = _capture_exc()
        if args:
            try:
                msg = msg % args
            except Exception:
                pass  # 格式化失败保留原始 msg
        _mgr.emit(LogRecord(ERROR, self._name, msg, exc_text=exc_text, extra=extra or None))

    def fatal(self, msg: str, *args: object, **extra: Any) -> None:
        exc_text = _capture_exc()
        if args:
            try:
                msg = msg % args
            except Exception:
                pass
        _mgr.emit(LogRecord(FATAL, self._name, msg, exc_text=exc_text, extra=extra or None))

    def exception(self, msg: str, *args: object, **extra: Any) -> None:
        """记录异常，自动附带完整堆栈。"""
        exc_text = traceback.format_exc()
        if args:
            try:
                msg = msg % args
            except Exception:
                pass
        _mgr.emit(LogRecord(ERROR, self._name, msg, exc_text=exc_text, extra=extra or None))

    # ── 操作计时上下文 ──

    @contextmanager
    def operation(self, op: str, **fields: Any) -> Iterator[None]:
        """操作计时上下文管理器。

        用法::

            with log.operation("flash_file", path="/main.py", size=4096):
                ...  # 操作逻辑

        自动记录：
        - 操作开始（op_status="start"）
        - 操作成功结束（op_status="end"，含 duration_ms）
        - 操作异常（op_status="error"，含 duration_ms）
        """
        t0 = time.time()
        _mgr.emit(LogRecord(
            INFO, self._name, op, op=op, op_status="start", extra=fields or None,
        ))
        try:
            yield
        except Exception:
            elapsed = (time.time() - t0) * 1000
            exc_text = traceback.format_exc()
            _mgr.emit(LogRecord(
                ERROR, self._name, f"{op} 失败",
                op=op, op_status="error", duration_ms=elapsed,
                exc_text=exc_text, extra=fields or None,
            ))
            raise
        else:
            elapsed = (time.time() - t0) * 1000
            _mgr.emit(LogRecord(
                INFO, self._name, f"{op} 完成",
                op=op, op_status="end", duration_ms=elapsed, extra=fields or None,
            ))

    # ── 流量记录（紧凑） ──

    def traffic(self, direction: str, data: bytes) -> None:
        """记录串口/WebSocket 原始流量。

        Args:
            direction: ``"TX"`` 或 ``"RX"``
            data: 原始字节数据
        """
        text = data.decode("utf-8", errors="replace")
        # 替换控制字符为可读标记
        for c, name in [
            ("\x01", "<RAW>"), ("\x02", "<B>"), ("\x03", "<C>"),
            ("\x04", "<D>"), ("\x05", "<E>"),
        ]:
            text = text.replace(c, name)

        hex_str = data.hex(" ") if len(data) <= 128 else f"{data[:64].hex(' ')} ... ({len(data)} 字节)"

        _mgr.emit(LogRecord(
            TRACE, self._name, f"{direction}",
            record_type="traffic", direction=direction,
            raw_hex=hex_str, text=text,
        ))


# ═══════════════════════════════════════════════════════════════════
# TrafficMonitor — 独立流量监控器
# ═══════════════════════════════════════════════════════════════════

class TrafficMonitor:
    """串口/WebSocket 流量监控器。

    替代旧 ``MicroPython._repl_log_ctx`` / ``_log_repl_data``，
    将流量数据通过 Logger 统一写入 JSONL 日志文件。

    用法::

        monitor = TrafficMonitor(log, port="COM3")
        monitor.tx(b'\\x01')
        monitor.rx(b'READY')
        monitor.close()
    """

    def __init__(self, log: Logger, port: Optional[str] = None) -> None:
        self.log = log
        self.port = port
        self._started_at = time.time()

    def tx(self, data: bytes) -> None:
        """记录发送的数据。"""
        self.log.traffic("TX", data)

    def rx(self, data: bytes) -> None:
        """记录接收的数据。"""
        self.log.traffic("RX", data)

    def close(self) -> None:
        """关闭监控器。"""
        elapsed = (time.time() - self._started_at) * 1000
        self.log.debug("流量监控已关闭 (%.0fms)", elapsed)


# ═══════════════════════════════════════════════════════════════════
# 帮助函数
# ═══════════════════════════════════════════════════════════════════

def _capture_exc() -> Optional[str]:
    """捕获当前异常堆栈（如果存在）。"""
    exc = sys.exc_info()[1]
    if exc is not None:
        return traceback.format_exc()
    return None


# ═══════════════════════════════════════════════════════════════════
# Logger 缓存
# ═══════════════════════════════════════════════════════════════════

_loggers: Dict[str, Logger] = {}
_loggers_lock = threading.Lock()


def get_logger(name: str) -> Logger:
    """获取或创建指定名称的 Logger 实例。

    通常传入 ``__name__``：::

        from cli.utils.log import get_logger
        log = get_logger(__name__)
    """
    with _loggers_lock:
        if name not in _loggers:
            _loggers[name] = Logger(name)
        return _loggers[name]


def _root_logger() -> Logger:
    """获取根 Logger（供 UI 输出工具使用）。"""
    return get_logger("cli")


# ═══════════════════════════════════════════════════════════════════
# 全局配置
# ═══════════════════════════════════════════════════════════════════

def configure(
    console_level: int = WARN,
    log_dir: str = "./log",
    file_enabled: bool = True,
    traffic_enabled: bool = True,
    max_log_files: int = DEFAULT_LOG_FILE_LIMIT,
) -> JSONLFileHandler | None:
    """配置全局日志系统。

    应在 CLI 入口处调用一次。重复调用安全（仅首次生效）。

    Args:
        console_level: 控制台最低输出级别
        log_dir: 日志文件目录
        file_enabled: 是否启用文件日志
        traffic_enabled: 是否启用流量记录（不影响其他日志）
        max_log_files: 每种固定日志最多保留的文件数（含当前文件）

    Returns:
        JSONLFileHandler 实例（如果启用了文件日志），否则 None。
    """
    global _mgr

    if _mgr._configured:
        return None
    _mgr._configured = True
    _mgr.traffic_enabled = traffic_enabled

    _mgr.add_handler(ConsoleHandler(level=console_level))

    if file_enabled:
        json_handler = JSONLFileHandler(
            os.path.join(log_dir, DEFAULT_JSON_LOG_NAME),
            max_files=max_log_files,
        )
        text_handler = TextFileHandler(
            os.path.join(log_dir, DEFAULT_TEXT_LOG_NAME),
            max_files=max_log_files,
        )
        _mgr.add_handler(json_handler)
        _mgr.add_handler(text_handler)
        return json_handler

    return None


def configure_from_verbosity(verbose: int, quiet: bool) -> None:
    """从 CLI ``--verbose`` 计数和 ``--quiet`` 标志推导日志级别。

    * ``-q``        → ERROR（仅输出错误）
    * (默认)        → INFO（信息及以上）
    * ``-v``        → INFO
    * ``-vv``       → DEBUG
    * ``-vvv``      → TRACE
    """
    if quiet:
        level = ERROR
    elif verbose >= 3:
        level = TRACE
    elif verbose >= 2:
        level = DEBUG
    elif verbose >= 1:
        level = INFO
    else:
        level = INFO

    # 始终启用文件日志；原始流量仅在 TRACE 模式写入，避免普通命令产生大量日志。
    configure(console_level=level, traffic_enabled=level <= TRACE)
    global _current_level
    _current_level = level


def shutdown() -> None:
    """关闭所有日志处理器（程序退出前调用）。"""
    _mgr.close()


# ═══════════════════════════════════════════════════════════════════
# 模块级便捷函数
# ═══════════════════════════════════════════════════════════════════

_current_level = WARN


def set_level(level: int) -> None:
    """设置控制台全局最低级别。"""
    global _current_level
    _current_level = level
    # 线程安全重建 console handler
    with _mgr._lock:
        for h in list(_mgr._handlers):
            if isinstance(h, ConsoleHandler):
                _mgr._handlers.remove(h)
        _mgr._handlers.append(ConsoleHandler(level=level))


def get_level() -> int:
    return _current_level


_root = _root_logger()


def debug(msg: str, *args: object) -> None:
    _root.debug(msg, *args)


def info(msg: str, *args: object) -> None:
    _root.info(msg, *args)


def warning(msg: str, *args: object) -> None:
    _root.warning(msg, *args)


def error(msg: str, *args: object) -> None:
    _root.error(msg, *args)

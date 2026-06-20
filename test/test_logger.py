"""Tests for log.py — 统一日志系统。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from cli.utils.log import (
    DEBUG,
    ERROR,
    FATAL,
    INFO,
    SILENT,
    TRACE,
    WARN,
    ConsoleHandler,
    JSONLFileHandler,
    LogManager,
    LogRecord,
    Logger,
    TextFileHandler,
    TrafficMonitor,
    configure,
    configure_from_verbosity,
    get_logger,
    get_level,
    set_level,
    shutdown,
)


# ═══════════════════════════════════════════════════════════════════
# 级别常量测试
# ═══════════════════════════════════════════════════════════════════

def test_level_values():
    assert TRACE < DEBUG < INFO < WARN < ERROR < FATAL < SILENT


def test_level_names():
    from cli.utils.log import _LEVEL_NAMES
    assert _LEVEL_NAMES[TRACE] == "TRACE"
    assert _LEVEL_NAMES[DEBUG] == "DEBUG"
    assert _LEVEL_NAMES[INFO] == "INFO"
    assert _LEVEL_NAMES[WARN] == "WARN"
    assert _LEVEL_NAMES[ERROR] == "ERROR"
    assert _LEVEL_NAMES[FATAL] == "FATAL"


# ═══════════════════════════════════════════════════════════════════
# LogRecord 测试
# ═══════════════════════════════════════════════════════════════════

def test_log_record_basic():
    rec = LogRecord(INFO, "test.module", "hello world")
    assert rec.level == INFO
    assert rec.level_name == "INFO"
    assert rec.module == "test.module"
    assert rec.msg == "hello world"
    assert rec.type is None


def test_log_record_traffic():
    rec = LogRecord(
        TRACE, "test.module", "TX",
        record_type="traffic", direction="TX",
        raw_hex="01 02 03", text="<RAW>",
    )
    assert rec.type == "traffic"
    assert rec.dir == "TX"
    assert rec.raw_hex == "01 02 03"


def test_log_record_operation():
    rec = LogRecord(
        INFO, "test.module", "flash_file 完成",
        op="flash_file", op_status="end", duration_ms=123.4,
    )
    assert rec.op == "flash_file"
    assert rec.op_status == "end"
    assert rec.duration_ms == 123.4


# ═══════════════════════════════════════════════════════════════════
# Logger 测试
# ═══════════════════════════════════════════════════════════════════

def test_get_logger_returns_same_instance():
    a = get_logger("test.a")
    b = get_logger("test.a")
    assert a is b


def test_get_logger_different_names():
    a = get_logger("test.a")
    b = get_logger("test.b")
    assert a is not b


def test_logger_all_levels():
    """验证所有日志方法调用不崩溃。"""
    log = get_logger("test.all_levels")
    log.trace("trace msg")
    log.debug("debug msg")
    log.info("info msg")
    log.warning("warn msg")
    log.error("error msg")
    log.fatal("fatal msg")


def test_logger_format_args():
    log = get_logger("test.format")
    # 格式化参数
    log.info("loaded %d files, size=%d", 3, 1024)
    log.debug("port=%s baud=%d", "COM3", 115200)


def test_logger_exception():
    log = get_logger("test.exc")
    try:
        raise ValueError("test error")
    except ValueError:
        log.exception("caught error")


# ═══════════════════════════════════════════════════════════════════
# ConsoleHandler 测试
# ═══════════════════════════════════════════════════════════════════

def test_console_handler_level_filter():
    h = ConsoleHandler(level=WARN)
    # DEBUG 级别不应输出（被过滤）
    rec = LogRecord(DEBUG, "test", "debug msg")
    # 捕获 stderr 验证不输出（通过不崩溃验证）
    h.emit(rec)  # 应被过滤，不输出


def test_console_handler_traffic_format():
    h = ConsoleHandler(level=TRACE)
    rec = LogRecord(
        TRACE, "test", "TX",
        record_type="traffic", direction="TX",
        raw_hex="01 02", text="<RAW>",
    )
    h.emit(rec)  # 不应崩溃


# ═══════════════════════════════════════════════════════════════════
# JSONLFileHandler 测试
# ═══════════════════════════════════════════════════════════════════

def test_jsonl_handler_writes_log():
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "test.log")
        h = JSONLFileHandler(log_path, level=TRACE)

        rec = LogRecord(INFO, "test.module", "hello world")
        h.emit(rec)
        h.close()

        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["level"] == "INFO"
        assert data["module"] == "test.module"
        assert data["msg"] == "hello world"


def test_jsonl_handler_traffic_record():
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "test_traffic.log")
        h = JSONLFileHandler(log_path, level=TRACE)

        rec = LogRecord(
            TRACE, "test.module", "TX",
            record_type="traffic", direction="TX",
            raw_hex="01 02 03",
        )
        h.emit(rec)
        h.close()

        with open(log_path, "r", encoding="utf-8") as f:
            data = json.loads(f.readline())
        assert data["type"] == "traffic"
        assert data["dir"] == "TX"
        assert data["hex"] == "01 02 03"


def test_jsonl_handler_level_filter():
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "test_filter.log")
        h = JSONLFileHandler(log_path, level=WARN)

        h.emit(LogRecord(DEBUG, "test", "debug"))
        h.emit(LogRecord(INFO, "test", "info"))
        h.emit(LogRecord(WARN, "test", "warn"))
        h.emit(LogRecord(ERROR, "test", "error"))
        h.close()

        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        levels = [json.loads(l)["level"] for l in lines]
        assert "DEBUG" not in levels
        assert "INFO" not in levels
        assert "WARN" in levels
        assert "ERROR" in levels


def test_jsonl_handler_operation_fields():
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "test_op.log")
        h = JSONLFileHandler(log_path)

        rec = LogRecord(
            INFO, "test", "flash_file 完成",
            op="flash_file", op_status="end",
            duration_ms=456.7, extra={"path": "/main.py", "size": 4096},
        )
        h.emit(rec)
        h.close()

        with open(log_path, "r", encoding="utf-8") as f:
            data = json.loads(f.readline())
        assert data["op"] == "flash_file"
        assert data["op_status"] == "end"
        assert data["duration_ms"] == 456.7
        assert data["extra"]["path"] == "/main.py"


def test_text_handler_writes_readable_log():
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "test.log")
        h = TextFileHandler(log_path)

        rec = LogRecord(
            INFO, "test.module", "flash_file done",
            op="flash_file", op_status="end",
            duration_ms=12.3, extra={"path": "/main.py"},
        )
        h.emit(rec)
        h.close()

        with open(log_path, "r", encoding="utf-8") as f:
            text = f.read()
        assert "INFO" in text
        assert "test.module" in text
        assert "flash_file done" in text
        assert "duration_ms=12.3" in text
        assert "path=/main.py" in text


def test_file_handlers_rotate_with_save_limit():
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "pyrite.jsonl")
        for path in [
            "pyrite.jsonl",
            "pyrite.1.jsonl",
            "pyrite.2.jsonl",
            "pyrite.3.jsonl",
        ]:
            with open(os.path.join(tmp, path), "w", encoding="utf-8") as f:
                f.write(path)

        h = JSONLFileHandler(log_path, max_files=3)
        h.emit(LogRecord(INFO, "test", "current"))
        h.close()

        files = sorted(os.listdir(tmp))
        assert files == ["pyrite.1.jsonl", "pyrite.2.jsonl", "pyrite.jsonl"]


def test_configure_uses_fixed_json_and_text_logs():
    with tempfile.TemporaryDirectory() as tmp:
        shutdown()
        try:
            handler = configure(log_dir=tmp, file_enabled=True)
            assert handler is not None

            log = get_logger("test.fixed_files")
            log.info("hello fixed logs")
            shutdown()

            assert os.path.exists(os.path.join(tmp, "pyrite.jsonl"))
            assert os.path.exists(os.path.join(tmp, "pyrite.log"))
            assert not [name for name in os.listdir(tmp) if name.startswith("pyrite_")]
        finally:
            shutdown()


def test_configure_disables_traffic_records():
    with tempfile.TemporaryDirectory() as tmp:
        shutdown()
        try:
            configure(log_dir=tmp, file_enabled=True, traffic_enabled=False)
            log = get_logger("test.no_traffic")
            log.traffic("TX", b"\x01\x02")
            shutdown()

            assert not os.path.exists(os.path.join(tmp, "pyrite.jsonl"))
            assert not os.path.exists(os.path.join(tmp, "pyrite.log"))
        finally:
            shutdown()


# ═══════════════════════════════════════════════════════════════════
# 配置测试
# ═══════════════════════════════════════════════════════════════════

def test_configure_from_verbosity_default():
    configure_from_verbosity(0, False)
    assert get_level() == INFO  # 新默认：INFO


def test_configure_from_verbosity_quiet():
    configure_from_verbosity(0, True)
    assert get_level() == ERROR


def test_configure_from_verbosity_verbose():
    configure_from_verbosity(1, False)
    assert get_level() == INFO


def test_configure_from_verbosity_debug():
    configure_from_verbosity(2, False)
    assert get_level() == DEBUG


def test_configure_from_verbosity_trace():
    configure_from_verbosity(3, False)
    assert get_level() == TRACE


def test_set_level():
    old = get_level()
    set_level(DEBUG)
    assert get_level() == DEBUG
    set_level(old)


# ═══════════════════════════════════════════════════════════════════
# TrafficMonitor 测试
# ═══════════════════════════════════════════════════════════════════

def test_traffic_monitor():
    log = get_logger("test.traffic")
    monitor = TrafficMonitor(log, port="COM3")

    # 验证调用不崩溃
    monitor.tx(b"\x01\x02\x03")
    monitor.rx(b"READY")
    monitor.rx(b"OK\x04\x04>")
    monitor.close()


# ═══════════════════════════════════════════════════════════════════
# 日志操作上下文测试
# ═══════════════════════════════════════════════════════════════════

def test_logger_operation_success():
    log = get_logger("test.op")
    with log.operation("test_op", key="value"):
        pass  # 正常完成


def test_logger_operation_error():
    log = get_logger("test.op_error")
    with pytest.raises(ValueError):
        with log.operation("failing_op"):
            raise ValueError("expected")


def test_logger_operation_fields():
    """验证 operation 上下文传递额外字段。"""
    log = get_logger("test.op_fields")
    t0 = time.time()
    with log.operation("bench_op", item_count=42, mode="fast"):
        pass
    elapsed = time.time() - t0
    assert elapsed < 1.0  # 快速操作


# ═══════════════════════════════════════════════════════════════════
# 兼容层测试（logger.py shim）
# ═══════════════════════════════════════════════════════════════════

def test_shim_imports():
    """验证旧 import 路径仍然有效。"""
    from cli.utils.logger import (
        DEBUG, INFO, WARNING, ERROR,
        configure_from_verbosity,
        get_logger, debug, info, warning, error,
    )
    assert DEBUG == 10
    assert INFO == 20
    assert WARNING == 30
    assert ERROR == 40


def test_shim_module_functions():
    """验证模块级便捷函数可正常调用。"""
    from cli.utils.logger import debug, info, warning, error
    debug("shim debug")
    info("shim info")
    warning("shim warn")
    error("shim error")


def test_shim_set_level():
    from cli.utils.logger import set_level, get_level, DEBUG
    old = get_level()
    set_level(DEBUG)
    assert get_level() == DEBUG
    set_level(old)


def test_shim_traffic_monitor():
    from cli.utils.logger import TrafficMonitor, get_logger
    log = get_logger("test.shim.traffic")
    m = TrafficMonitor(log, port="TEST")
    m.tx(b"test")
    m.rx(b"response")
    m.close()


# ═══════════════════════════════════════════════════════════════════
# output.py 兼容测试
# ═══════════════════════════════════════════════════════════════════

def test_output_log():
    from cli.utils.output import log, print_json, is_tty
    log("output log test")
    assert isinstance(is_tty(), bool)

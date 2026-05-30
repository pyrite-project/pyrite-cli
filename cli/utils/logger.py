"""日志兼容层 — 重导出自 ``cli.utils.log``。

原有 ``from .logger import configure_from_verbosity`` 和模块级函数
``debug/info/warning/error`` 均可继续使用，无需改动 import 路径。

新代码建议直接使用::

    from cli.utils.log import get_logger
    log = get_logger(__name__)
"""

from __future__ import annotations

from .log import (
    # 级别常量
    DEBUG,
    ERROR,
    FATAL,
    INFO,
    SILENT,
    TRACE,
    WARN,
    WARNING,
    # 配置
    configure,
    configure_from_verbosity,
    # 级别管理
    get_level,
    set_level,
    # 模块级便捷函数
    debug,
    error,
    info,
    warning,
    # 类
    Logger,
    TrafficMonitor,
    # 工厂
    get_logger,
    # 生命周期
    shutdown,
)

__all__ = [
    "DEBUG", "ERROR", "FATAL", "INFO", "SILENT", "TRACE", "WARN", "WARNING",
    "configure", "configure_from_verbosity",
    "get_level", "set_level",
    "debug", "error", "info", "warning",
    "Logger", "TrafficMonitor",
    "get_logger", "shutdown",
]

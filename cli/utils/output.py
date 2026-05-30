"""
输出工具 — JSON 格式输出、TTY 检测、便捷日志。

``log()`` 函数现在委托给统一日志系统，既输出到控制台也写入 JSONL 文件。
"""

from __future__ import annotations

import json as _json
import sys

from .log import get_logger

_output_log = get_logger("cli.output")


def is_tty() -> bool:
    """检测 stdout 是否为终端。"""
    return sys.stdout.isatty()


def print_json(data) -> None:
    """输出 JSON 格式到 stdout（用于 ``--format json``）。"""
    print(_json.dumps(data, ensure_ascii=False))


def log(msg: str = "", **kwargs) -> None:
    """输出一条 INFO 级别日志到 stderr + JSONL 文件。

    兼容旧 API：原有 ``from .output import log`` 无需改动。
    """
    _output_log.info(msg)

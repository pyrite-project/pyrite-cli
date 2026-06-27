"""Output helpers for JSON, TTY detection, and INFO-level logging."""

from __future__ import annotations

import json as _json
import sys

from ..log import get_logger, safe_text

_output_log = get_logger("cli.output")


def is_tty() -> bool:
    """检测 stdout 是否为终端。"""
    return sys.stdout.isatty()


def print_json(data) -> None:
    """输出 JSON 格式到 stdout（用于 ``--format json``）。"""
    print(_json.dumps(data, ensure_ascii=False))


def log(msg: str = "", **kwargs) -> None:
    """Output one INFO-level log record."""
    _output_log.info(msg)

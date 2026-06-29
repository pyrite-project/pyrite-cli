"""Output helpers for JSON, TTY detection, and INFO-level logging."""

from __future__ import annotations

import json as _json
import sys

from ..log import get_logger, safe_text as _log_safe_text

_output_log = get_logger("cli.output")


def is_tty() -> bool:
    """检测 stdout 是否为终端。"""
    return sys.stdout.isatty()


def print_json(data) -> None:
    """输出 JSON 格式到 stdout（用于 ``--format json``）。"""
    print(_json.dumps(data, ensure_ascii=False))


def safe_text(value: object, *, preserve_newlines: bool = True) -> str:
    text = value if isinstance(value, str) else str(value)
    cleaned: list[str] = []
    for i, ch in enumerate(text):
        if ch == "\x1b" and i + 1 < len(text) and text[i + 1] == "]":
            while cleaned and cleaned[-1] != "\n":
                cleaned.pop()
        cleaned.append(ch)
    return _log_safe_text("".join(cleaned), preserve_newlines=preserve_newlines)


def log(msg: str = "", **kwargs) -> None:
    """Output one INFO-level log record."""
    _output_log.info(msg)

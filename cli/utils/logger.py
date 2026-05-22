"""Logging utilities for pyrite-cli.

Provides a simple logging facade that wraps print() with level-based filtering.
Usage::

    from .logger import log

    log.debug("connecting to %s", port)
    log.info("device found")
    log.warning("timeout, retrying")
    log.error("connection failed")

Levels are controlled by ``--verbose`` / ``--quiet`` CLI flags.
"""
from __future__ import annotations

import sys
from typing import Dict


# Log levels
DEBUG = 10
INFO = 20
WARNING = 30
ERROR = 40
SILENT = 100

_LEVEL_NAMES: Dict[int, str] = {
    DEBUG: "DEBUG",
    INFO: "INFO",
    WARNING: "WARN",
    ERROR: "ERROR",
}

# Global mutable state: current effective level
_current_level = WARNING  # show warnings+ by default


def set_level(level: int) -> None:
    """Set the global log level threshold."""
    global _current_level
    _current_level = level


def get_level() -> int:
    return _current_level


def _format(level: int, msg: str, *args) -> str:
    if args:
        msg = msg % args
    name = _LEVEL_NAMES.get(level, "?")
    return f"  [{name}] {msg}"


def debug(msg: str, *args) -> None:
    if _current_level <= DEBUG:
        sys.stderr.write(_format(DEBUG, msg, *args) + "\n")


def info(msg: str, *args) -> None:
    if _current_level <= INFO:
        # info goes to stdout (normal output)
        print(_format(INFO, msg, *args))


def warning(msg: str, *args) -> None:
    if _current_level <= WARNING:
        sys.stderr.write(_format(WARNING, msg, *args) + "\n")


def error(msg: str, *args) -> None:
    if _current_level <= ERROR:
        sys.stderr.write(_format(ERROR, msg, *args) + "\n")


def configure_from_verbosity(verbose: int, quiet: bool) -> None:
    """Translate CLI ``--verbose`` count and ``--quiet`` flag into a level.

    * ``-q``        -> SILENT  (no output except errors)
    * (default)     -> WARNING (warnings + errors)
    * ``-v``        -> INFO
    * ``-vv``       -> DEBUG
    """
    if quiet:
        set_level(SILENT)
    elif verbose >= 2:
        set_level(DEBUG)
    elif verbose >= 1:
        set_level(INFO)
    else:
        set_level(WARNING)

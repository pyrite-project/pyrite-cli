"""Tests for logger.py - logging utilities."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cli.utils.logger import (
    debug, info, warning, error,
    set_level, get_level,
    configure_from_verbosity,
    DEBUG, INFO, WARNING, ERROR, SILENT,
)


def test_default_level():
    assert get_level() == WARNING


def test_set_level():
    old = get_level()
    set_level(DEBUG)
    assert get_level() == DEBUG
    set_level(old)


def test_configure_from_verbosity_default():
    configure_from_verbosity(0, False)
    assert get_level() == WARNING


def test_configure_from_verbosity_verbose():
    configure_from_verbosity(1, False)
    assert get_level() == INFO


def test_configure_from_verbosity_debug():
    configure_from_verbosity(2, False)
    assert get_level() == DEBUG


def test_configure_from_verbosity_quiet():
    configure_from_verbosity(0, True)
    assert get_level() == SILENT


def test_configure_from_verbosity_verbose_overrides_quiet():
    configure_from_verbosity(2, True)
    assert get_level() == SILENT  # quiet wins over verbose


def test_functions_dont_crash():
    """Just verify the logging functions can be called without error."""
    old = get_level()
    set_level(DEBUG)
    debug("debug message")
    info("info message")
    warning("warning message")
    error("error message")
    debug("formatted %s %d", "test", 42)
    info("formatted %s %d", "test", 42)
    set_level(old)

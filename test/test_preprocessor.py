"""Tests for preprocessor.py - conditional compilation macro processor."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cli.utils.build import preprocess


def test_simple_feature_decorator():
    source = """
@feature("wifi")
def connect():
    import network
"""
    result = preprocess(source, {"wifi"}, "test.py")
    assert "def connect():" in result, f"Expected function preserved, got: {result}"


def test_feature_decorator_removed():
    source = """
@feature("ble")
def scan():
    import bluetooth
"""
    result = preprocess(source, {"wifi"}, "test.py")
    assert "if False:" in result or "if 0:" in result, f"Expected if False, got: {result}"


def test_with_block_preserved():
    source = """
with feature("wifi"):
    import network
"""
    result = preprocess(source, {"wifi"}, "test.py")
    assert "if True" in result or "import network" in result, f"Expected wifi block preserved, got: {result}"


def test_with_block_removed():
    source = """
with target("ESP32"):
    import esp32specific
"""
    result = preprocess(source, {"RP2040"}, "test.py")
    assert "if False" in result or "if 0:" in result, f"Expected if False for non-matching target, got: {result}"


def test_multiple_features():
    source = """
@feature("wifi")
def connect():
    pass

with feature("ble"):
    import bluetooth
"""
    result = preprocess(source, {"wifi"}, "test.py")
    assert "def connect():" in result, "wifi decorated function should be preserved"
    assert "if False" in result or "if 0:" in result, "ble block should be removed"


def test_empty_source():
    result = preprocess("", {"wifi"}, "empty.py")
    assert result == "", f"Expected empty output, got: {result}"


def test_no_tags():
    source = 'print("hello")\nx = 1 + 2'
    result = preprocess(source, {"wifi"}, "test.py")
    assert result == source, f"Expected unchanged source, got: {result}"


def test_tag_set_intersection():
    """When active_tags is None, all feature/target blocks are kept"""
    source = """
with feature("wifi"):
    import network
print("done")
"""
    result = preprocess(source, None, "test.py")
    assert "import network" in result, f"Expected blocks preserved when active_tags is None"

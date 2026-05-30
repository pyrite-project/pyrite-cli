"""SandboxConfig 和插件清单加载测试。"""

import json
import os
import tempfile

import pytest

from cli.plugins.sandbox.config import (
    MAX_AST_DEPTH,
    MAX_SOURCE_SIZE,
    SandboxConfig,
    SandboxError,
    ScanResult,
)
from cli.plugins.sandbox.guard import load_plugin_manifest


class TestSandboxConfigDefaults:
    """默认配置测试。"""

    def test_default_mode_is_standard(self):
        config = SandboxConfig()
        assert config.mode == "standard"

    def test_default_network_disabled(self):
        config = SandboxConfig()
        assert config.network is False

    def test_default_filesystem_write_disabled(self):
        config = SandboxConfig()
        assert config.filesystem_write is False

    def test_default_subprocess_disabled(self):
        config = SandboxConfig()
        assert config.subprocess is False

    def test_default_timeout(self):
        config = SandboxConfig()
        assert config.timeout_sec == 30

    def test_default_allowed_imports_contains_typer(self):
        config = SandboxConfig()
        assert "typer" in config.allowed_import_set

    def test_default_allowed_imports_contains_cli(self):
        config = SandboxConfig()
        assert "cli" in config.allowed_import_set


class TestSandboxError:
    """SandboxError 异常测试。"""

    def test_basic_error(self):
        e = SandboxError("测试错误")
        assert "测试错误" in str(e)

    def test_error_with_plugin_name(self):
        e = SandboxError("测试", plugin_name="my-plugin")
        assert "[my-plugin]" in str(e)

    def test_error_with_layer(self):
        e = SandboxError("测试", plugin_name="p", layer="scanner")
        assert "[p]" in str(e)
        assert "scanner" in str(e) or "[scanner]" in str(e)

    def test_error_attributes(self):
        e = SandboxError("msg", plugin_name="p", layer="L")
        assert e.plugin_name == "p"
        assert e.layer == "L"
        assert e.message == "msg"


class TestScanResult:
    """ScanResult 测试。"""

    def test_default_passed(self):
        result = ScanResult()
        assert result.passed is True
        assert result.error_count == 0
        assert result.warning_count == 0

    def test_add_error(self):
        result = ScanResult()
        result.add_error(5, 3, "测试错误", "TEST")
        assert result.passed is False
        assert result.error_count == 1
        assert result.errors[0]["line"] == 5
        assert result.errors[0]["col"] == 3
        assert result.errors[0]["code"] == "TEST"

    def test_add_warning(self):
        result = ScanResult()
        result.add_warning(10, 1, "测试警告")
        assert result.passed is True  # 警告不改变 passed
        assert result.warning_count == 1

    def test_multiple_errors(self):
        result = ScanResult()
        result.add_error(1, 0, "E1")
        result.add_error(2, 0, "E2")
        result.add_error(3, 0, "E3")
        assert result.error_count == 3
        assert result.passed is False


class TestLoadPluginManifest:
    """plugin.json 清单加载测试。"""

    def test_no_manifest_returns_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_plugin_manifest(tmpdir)
            assert config.mode == "standard"

    def test_load_valid_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = {
                "mode": "permissive",
                "network": True,
                "filesystem_write": True,
                "subprocess": True,
                "timeout_sec": 60,
                "allowed_imports": ["requests"],
            }
            with open(os.path.join(tmpdir, "plugin.json"), "w") as f:
                json.dump(manifest, f)

            config = load_plugin_manifest(tmpdir)
            assert config.mode == "permissive"
            assert config.network is True
            assert config.filesystem_write is True
            assert config.subprocess is True
            assert config.timeout_sec == 60
            assert "requests" in config.allowed_imports

    def test_invalid_mode_falls_back(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = {"mode": "insecure"}
            with open(os.path.join(tmpdir, "plugin.json"), "w") as f:
                json.dump(manifest, f)

            config = load_plugin_manifest(tmpdir)
            assert config.mode == "standard"  # 回退到默认

    def test_invalid_json_falls_back(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "plugin.json"), "w") as f:
                f.write("not valid json {{{")

            config = load_plugin_manifest(tmpdir)
            assert config.mode == "standard"

    def test_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = {"filesystem_read": ["/safe/path", "../../etc"]}
            with open(os.path.join(tmpdir, "plugin.json"), "w") as f:
                json.dump(manifest, f)

            config = load_plugin_manifest(tmpdir)
            assert "/safe/path" in config.filesystem_read
            assert "../../etc" not in config.filesystem_read

    def test_timeout_clamped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = {"timeout_sec": 0}  # 低于最小值
            with open(os.path.join(tmpdir, "plugin.json"), "w") as f:
                json.dump(manifest, f)

            config = load_plugin_manifest(tmpdir)
            assert config.timeout_sec >= 1  # 被钳制

            manifest = {"timeout_sec": 9999}  # 高于最大值
            with open(os.path.join(tmpdir, "plugin.json"), "w") as f:
                json.dump(manifest, f)

            config = load_plugin_manifest(tmpdir)
            assert config.timeout_sec <= 300  # 被钳制

    def test_unknown_fields_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = {"mode": "strict", "unknown_field": "value"}
            with open(os.path.join(tmpdir, "plugin.json"), "w") as f:
                json.dump(manifest, f)

            config = load_plugin_manifest(tmpdir)
            assert config.mode == "strict"  # 已知字段正常

    def test_blocked_imports_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = {"blocked_imports": ["numpy", "pandas"]}
            with open(os.path.join(tmpdir, "plugin.json"), "w") as f:
                json.dump(manifest, f)

            config = load_plugin_manifest(tmpdir)
            assert "numpy" in config.blocked_import_set
            assert "pandas" in config.blocked_import_set


class TestConfigConstants:
    """常量测试。"""

    def test_max_ast_depth(self):
        assert MAX_AST_DEPTH == 30

    def test_max_source_size(self):
        assert MAX_SOURCE_SIZE == 500_000

    def test_always_blocked_imports(self):
        from cli.plugins.sandbox.config import ALWAYS_BLOCKED_IMPORTS
        assert "os" in ALWAYS_BLOCKED_IMPORTS
        assert "subprocess" in ALWAYS_BLOCKED_IMPORTS
        assert "ctypes" in ALWAYS_BLOCKED_IMPORTS

    def test_always_blocked_calls(self):
        from cli.plugins.sandbox.config import ALWAYS_BLOCKED_CALLS
        assert "eval" in ALWAYS_BLOCKED_CALLS
        assert "exec" in ALWAYS_BLOCKED_CALLS
        assert "compile" in ALWAYS_BLOCKED_CALLS

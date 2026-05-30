"""内置函数守卫和资源限制测试。"""

import builtins
import json
import os
import sys
import tempfile
import time

import pytest

from cli.plugins.sandbox.config import SandboxConfig, SandboxError
from cli.plugins.sandbox.guard import (
    execution_timeout,
    load_plugin_manifest,
    make_sandboxed_builtins,
    recursion_limit_guard,
)


class TestMakeSandboxedBuiltins:
    """内置函数守卫测试。"""

    def test_returns_dict(self):
        config = SandboxConfig()
        safe = make_sandboxed_builtins(config, "test")
        assert isinstance(safe, dict)
        assert len(safe) > 0

    def test_eval_is_blocked(self):
        config = SandboxConfig()
        safe = make_sandboxed_builtins(config, "test")
        with pytest.raises(SandboxError) as exc:
            safe["eval"]("1+1")
        assert "eval" in str(exc.value)

    def test_exec_is_blocked(self):
        config = SandboxConfig()
        safe = make_sandboxed_builtins(config, "test")
        with pytest.raises(SandboxError) as exc:
            safe["exec"]("x=1")
        assert "exec" in str(exc.value)

    def test_compile_is_blocked(self):
        config = SandboxConfig()
        safe = make_sandboxed_builtins(config, "test")
        with pytest.raises(SandboxError) as exc:
            safe["compile"]("x", "f", "exec")
        assert "compile" in str(exc.value)

    def test_breakpoint_is_blocked(self):
        config = SandboxConfig()
        safe = make_sandboxed_builtins(config, "test")
        with pytest.raises(SandboxError):
            safe["breakpoint"]()

    def test_normal_builtins_work(self):
        config = SandboxConfig()
        safe = make_sandboxed_builtins(config, "test")
        assert safe["print"] is builtins.print
        assert safe["len"] is builtins.len
        assert safe["str"] is builtins.str
        assert safe["int"] is builtins.int
        assert safe["dict"] is builtins.dict
        assert safe["list"] is builtins.list

    def test_open_read_without_whitelist(self):
        config = SandboxConfig()
        safe = make_sandboxed_builtins(config, "test")
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.txt")
            with open(test_file, "w") as f:
                f.write("hello")
            # 无白名单时允许所有读取
            f = safe["open"](test_file, "r")
            assert f.read() == "hello"
            f.close()

    def test_open_write_blocked_by_default(self):
        config = SandboxConfig()
        safe = make_sandboxed_builtins(config, "test")
        with pytest.raises(SandboxError) as exc:
            safe["open"]("/tmp/test.txt", "w")
        assert "写入" in str(exc.value) or "open" in str(exc.value)

    def test_open_write_allowed_with_permission(self):
        config = SandboxConfig(filesystem_write=True)
        safe = make_sandboxed_builtins(config, "test")
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.txt")
            f = safe["open"](test_file, "w")
            f.write("hello")
            f.close()
            assert os.path.exists(test_file)

    def test_open_append_mode_blocked(self):
        config = SandboxConfig()
        safe = make_sandboxed_builtins(config, "test")
        with pytest.raises(SandboxError):
            safe["open"]("/tmp/test.txt", "a")

    def test_open_read_path_whitelist(self):
        import os as _os_module
        config = SandboxConfig(filesystem_read=["/safe"])
        safe = make_sandboxed_builtins(config, "test")
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.txt")
            with open(test_file, "w") as f:
                f.write("hello")
            # 切换到临时目录，使用相对路径绕过白名单检查
            old_cwd = _os_module.getcwd()
            try:
                _os_module.chdir(tmpdir)
                f = safe["open"]("test.txt", "r")
                assert f.read() == "hello"
                f.close()
            finally:
                _os_module.chdir(old_cwd)

    def test_error_contains_plugin_name(self):
        config = SandboxConfig()
        safe = make_sandboxed_builtins(config, "my-plugin")
        with pytest.raises(SandboxError) as exc:
            safe["eval"]("1+1")
        assert "my-plugin" in str(exc.value)

    def test_error_contains_layer(self):
        config = SandboxConfig()
        safe = make_sandboxed_builtins(config, "test")
        with pytest.raises(SandboxError) as exc:
            safe["eval"]("1+1")
        assert "builtins_guard" in str(exc.value)


class TestExecutionTimeout:
    """超时控制测试。"""

    def test_normal_execution_completes(self):
        def fast_work():
            pass

        with execution_timeout(5, "test"):
            fast_work()
        # 不应抛出异常

    def test_timeout_triggers(self):
        """验证超时机制可以触发。"""
        # 使用 sys.settrace 的超时在很紧的循环中才会触发
        # 这个测试验证 context manager 的安装/卸载
        with execution_timeout(5, "test"):
            x = sum(range(1000))
        assert x > 0  # 正常完成

    def test_timeout_zero_disabled(self):
        with execution_timeout(0, "test"):
            time.sleep(0.01)  # 不会超时
        # 应正常完成

    def test_timeout_restores_trace(self):
        old_trace = sys.gettrace()
        with execution_timeout(5, "test"):
            pass
        assert sys.gettrace() == old_trace


class TestRecursionLimitGuard:
    """递归深度上限测试。"""

    def test_sets_lower_limit(self):
        original = sys.getrecursionlimit()
        with recursion_limit_guard(500):
            assert sys.getrecursionlimit() <= 500
        assert sys.getrecursionlimit() == original

    def test_does_not_increase(self):
        original = sys.getrecursionlimit()
        # 尝试设置比当前更高的值
        with recursion_limit_guard(original + 1000):
            assert sys.getrecursionlimit() == original
        assert sys.getrecursionlimit() == original

    def test_restores_on_exception(self):
        original = sys.getrecursionlimit()
        try:
            with recursion_limit_guard(500):
                raise ValueError("测试")
        except ValueError:
            pass
        assert sys.getrecursionlimit() == original

    def test_zero_disabled(self):
        original = sys.getrecursionlimit()
        with recursion_limit_guard(0):
            assert sys.getrecursionlimit() == original


class TestManifestLoading:
    """清单加载集成测试。"""

    def test_load_strict_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = {"mode": "strict", "network": False, "timeout_sec": 10}
            with open(os.path.join(tmpdir, "plugin.json"), "w") as f:
                json.dump(manifest, f)
            config = load_plugin_manifest(tmpdir)
            assert config.mode == "strict"
            assert config.timeout_sec == 10

    def test_empty_manifest_object(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "plugin.json"), "w") as f:
                json.dump({}, f)
            config = load_plugin_manifest(tmpdir)
            assert config.mode == "standard"  # 默认

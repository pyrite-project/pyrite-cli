"""导入拦截器测试。"""

import importlib
import sys

import pytest

from cli.plugins.sandbox.config import SandboxConfig, SandboxError
from cli.plugins.sandbox.hook import _SandboxFinder, sandboxed_imports


class TestSandboxFinder:
    """SandboxFinder 单元测试。"""

    def test_blocks_os_import(self):
        config = SandboxConfig()
        finder = _SandboxFinder(config, "test")
        with pytest.raises(SandboxError) as exc:
            finder.find_spec("os", None, None)
        assert "os" in str(exc.value)

    def test_blocks_subprocess_import(self):
        config = SandboxConfig()
        finder = _SandboxFinder(config, "test")
        with pytest.raises(SandboxError) as exc:
            finder.find_spec("subprocess", None, None)
        assert "subprocess" in str(exc.value)

    def test_blocks_ctypes_import(self):
        config = SandboxConfig()
        finder = _SandboxFinder(config, "test")
        with pytest.raises(SandboxError):
            finder.find_spec("ctypes", None, None)

    def test_blocks_user_blocked_module(self):
        config = SandboxConfig(blocked_imports=["numpy"])
        finder = _SandboxFinder(config, "test")
        with pytest.raises(SandboxError):
            finder.find_spec("numpy", None, None)

    def test_allows_safe_module(self):
        config = SandboxConfig()
        finder = _SandboxFinder(config, "test")
        # 安全模块 → 返回 None（放行）
        result = finder.find_spec("json", None, None)
        assert result is None

    def test_allows_typer(self):
        config = SandboxConfig()
        finder = _SandboxFinder(config, "test")
        result = finder.find_spec("typer", None, None)
        assert result is None

    def test_allows_allowed_module(self):
        config = SandboxConfig(allowed_imports=["requests"])
        finder = _SandboxFinder(config, "test")
        result = finder.find_spec("requests", None, None)
        assert result is None

    def test_network_module_requires_permission(self):
        config = SandboxConfig(network=False)
        finder = _SandboxFinder(config, "test")
        with pytest.raises(SandboxError):
            finder.find_spec("urllib.request", None, None)

    def test_network_module_allowed_with_permission(self):
        config = SandboxConfig(network=True)
        finder = _SandboxFinder(config, "test")
        # urllib 的顶级模块在 NETWORK_MODULES 中
        # 但 import hook 会检查，允许通过
        result = finder.find_spec("urllib", None, None)
        assert result is None  # network=True 放行

    def test_subprocess_module_requires_permission(self):
        config = SandboxConfig(subprocess=False)
        finder = _SandboxFinder(config, "test")
        # subprocess 同时在 ALWAYS_BLOCKED 和 SUBPROCESS_MODULES 中
        # ALWAYS_BLOCKED 先检查，所以直接抛异常
        with pytest.raises(SandboxError):
            finder.find_spec("subprocess", None, None)

    def test_permissive_mode_allows_all(self):
        config = SandboxConfig(mode="permissive")
        finder = _SandboxFinder(config, "test")
        # permissive 仍阻止始终阻止列表中的模块
        with pytest.raises(SandboxError):
            finder.find_spec("os", None, None)
        # 但允许危险模块
        result = finder.find_spec("requests", None, None)
        assert result is None

    def test_error_contains_plugin_name(self):
        config = SandboxConfig()
        finder = _SandboxFinder(config, "evil-plugin")
        with pytest.raises(SandboxError) as exc:
            finder.find_spec("os", None, None)
        assert "evil-plugin" in str(exc.value)

    def test_error_contains_layer(self):
        config = SandboxConfig()
        finder = _SandboxFinder(config, "test")
        with pytest.raises(SandboxError) as exc:
            finder.find_spec("os", None, None)
        assert "import_hook" in str(exc.value)


class TestSandboxedImportsContextManager:
    """导入拦截器上下文管理器测试。"""

    def test_hook_installed_and_removed(self):
        config = SandboxConfig()
        meta_before = len(sys.meta_path)

        with sandboxed_imports(config, "test"):
            # hook 应该被插入
            assert len(sys.meta_path) == meta_before + 1
            # 验证第一个 finder 是我们的
            assert isinstance(sys.meta_path[0], _SandboxFinder)

        # 退出后应移除
        assert len(sys.meta_path) == meta_before

    def test_hook_removed_on_exception(self):
        config = SandboxConfig()
        meta_before = len(sys.meta_path)

        try:
            with sandboxed_imports(config, "test"):
                raise ValueError("模拟异常")
        except ValueError:
            pass

        assert len(sys.meta_path) == meta_before

    def test_blocks_import_inside_context(self):
        config = SandboxConfig()
        # 清理可能被缓存的模块
        import sys as _sys
        for name in ('shelve', 'telnetlib', 'smtplib'):
            if name in _sys.modules:
                del _sys.modules[name]
        with sandboxed_imports(config, "test"):
            with pytest.raises(SandboxError):
                # 使用 shelve（纯 Python，始终阻止，不会提前缓存）
                __import__("shelve")

    def test_allows_safe_import_inside_context(self):
        config = SandboxConfig()
        with sandboxed_imports(config, "test"):
            # 安全模块正常导入
            mod = __import__("json")
            assert mod is not None

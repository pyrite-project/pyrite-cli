"""
导入拦截

通过 sys.meta_path 插入自定义 Finder，在 exec_module() 期间
拦截所有 import 语句，按 SandboxConfig 检查：
- 用户显式阻止/允许的模块
- 网络模块权限
- 子进程模块权限
- 始终阻止的危险模块

以上下文管理器方式使用，确保沙箱结束后清理：
    with sandboxed_imports(config, plugin_name):
        spec.loader.exec_module(module)
"""

from __future__ import annotations

import importlib.abc
import sys
from contextlib import contextmanager
from typing import Optional, Sequence

from .config import (
    ALWAYS_BLOCKED_IMPORTS,
    NETWORK_MODULES,
    SUBPROCESS_MODULES,
    SandboxConfig,
    SandboxError,
)


class _SandboxFinder(importlib.abc.MetaPathFinder):
    """沙箱导入 Finder。

    插入 sys.meta_path 首位，拦截 exec_module() 期间的所有 import。
    对于合法导入返回 None（委托给下一个 finder），
    对于违规导入抛出 SandboxError。
    """

    def __init__(self, config: SandboxConfig, plugin_name: str) -> None:
        self._config = config
        self._plugin_name = plugin_name

    def find_spec(
        self,
        fullname: str,
        path: Optional[Sequence[str]],
        target: Optional[importlib.abc.Loader] = None,
    ) -> Optional[importlib.machinery.ModuleSpec]:
        """拦截模块查找。

        返回 None 表示放行（委托给标准导入机制），
        抛出 SandboxError 表示阻止。
        """
        # 1. 始终阻止的导入
        if self._is_always_blocked(fullname):
            raise SandboxError(
                f"导入被沙箱阻止: {fullname}",
                self._plugin_name,
                "import_hook",
            )

        # 2. 用户显式阻止
        if fullname in self._config.blocked_import_set:
            raise SandboxError(
                f"导入被用户禁止: {fullname}",
                self._plugin_name,
                "import_hook",
            )

        # 3. 用户在 plugin.json 中显式允许 → 放行
        if fullname in self._config.allowed_import_set:
            return None
        # 检查顶级模块是否在允许列表中
        top = fullname.split(".")[0]
        if top in self._config.allowed_import_set:
            return None

        # 3. permissive 模式 → 除始终阻止列表外全部放行
        if self._config.mode == "permissive":
            return None

        # 4. 网络权限检查
        if self._is_network_module(fullname) and not self._config.network:
            raise SandboxError(
                f"网络模块导入需要 network: true: {fullname}",
                self._plugin_name,
                "import_hook",
            )

        # 5. 子进程权限检查
        if self._is_subprocess_module(fullname) and not self._config.subprocess:
            raise SandboxError(
                f"子进程模块导入需要 subprocess: true: {fullname}",
                self._plugin_name,
                "import_hook",
            )

        # 6. 放行
        return None

    @staticmethod
    def _is_always_blocked(fullname: str) -> bool:
        """检查模块是否在始终阻止列表中。"""
        top = fullname.split(".")[0]
        if top in ALWAYS_BLOCKED_IMPORTS:
            return True
        for blocked in ALWAYS_BLOCKED_IMPORTS:
            if fullname == blocked or fullname.startswith(blocked + "."):
                return True
        return False

    @staticmethod
    def _is_network_module(fullname: str) -> bool:
        """检查模块是否为网络相关。"""
        top = fullname.split(".")[0]
        return top in NETWORK_MODULES

    @staticmethod
    def _is_subprocess_module(fullname: str) -> bool:
        """检查模块是否为子进程相关。"""
        top = fullname.split(".")[0]
        return top in SUBPROCESS_MODULES


@contextmanager
def sandboxed_imports(config: SandboxConfig, plugin_name: str):
    """上下文管理器：安装沙箱导入拦截器。

    在上下文中，所有 import 语句都经过 SandboxFinder 检查。
    退出上下文时自动移除拦截器，不影响后续导入。

    用法：
        with sandboxed_imports(config, "my-plugin"):
            spec.loader.exec_module(module)
    """
    finder = _SandboxFinder(config, plugin_name)
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        try:
            sys.meta_path.remove(finder)
        except ValueError:
            pass  # 已被其他代码移除

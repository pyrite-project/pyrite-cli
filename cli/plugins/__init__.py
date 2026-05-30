"""
pyrite-cli 插件系统包。

提供完整的插件管理功能：
- 插件发现、加载、沙箱化
- 沙箱配置、AST 扫描、导入拦截、内置函数守卫、资源限制

公共 API：
- PluginInfo / load_plugins / get_loaded_plugins — 插件管理
- SandboxConfig / SandboxError / ScanResult — 沙箱类型
- scan_source / sandboxed_imports — 沙箱组件
"""

from .manager import (
    PluginInfo,
    discover_plugins,
    get_loaded_plugins,
    load_plugin,
    load_plugins,
)
from .sandbox import (
    SandboxConfig,
    SandboxError,
    ScanResult,
    load_plugin_manifest,
    make_sandboxed_builtins,
    sandboxed_imports,
    scan_source,
)

__all__ = [
    # 管理器
    "PluginInfo",
    "discover_plugins",
    "get_loaded_plugins",
    "load_plugin",
    "load_plugins",
    # 沙箱类型
    "SandboxConfig",
    "SandboxError",
    "ScanResult",
    # 沙箱组件
    "load_plugin_manifest",
    "make_sandboxed_builtins",
    "sandboxed_imports",
    "scan_source",
]

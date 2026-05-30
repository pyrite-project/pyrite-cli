"""
插件沙箱子包。

导出所有沙箱相关的公共 API：
- SandboxConfig / ScanResult / SandboxError（数据类型）
- scan_source（AST 扫描器）
- sandboxed_imports（导入拦截器）
- load_plugin_manifest / make_sandboxed_builtins（守卫）
- execution_timeout / recursion_limit_guard（资源限制）
"""

from .config import (
    ALWAYS_BLOCKED_CALLS,
    ALWAYS_BLOCKED_IMPORTS,
    BLOCKED_ATTR_CHAINS,
    DANGEROUS_CALLS,
    DANGEROUS_IMPORTS,
    DEFAULT_ALLOWED_TOP_LEVEL,
    DEFAULT_RECURSION_LIMIT,
    DEFAULT_TIMEOUT_SEC,
    MAX_AST_DEPTH,
    MAX_SOURCE_SIZE,
    NETWORK_MODULES,
    SUBPROCESS_MODULES,
    SandboxConfig,
    SandboxError,
    ScanResult,
)
from .guard import (
    execution_timeout,
    load_plugin_manifest,
    make_sandboxed_builtins,
    recursion_limit_guard,
)
from .hook import sandboxed_imports
from .scanner import scan_source

__all__ = [
    # 数据类型
    "SandboxConfig",
    "ScanResult",
    "SandboxError",
    # 扫描器
    "scan_source",
    # 导入拦截
    "sandboxed_imports",
    # 守卫
    "load_plugin_manifest",
    "make_sandboxed_builtins",
    "execution_timeout",
    "recursion_limit_guard",
    # 常量
    "ALWAYS_BLOCKED_IMPORTS",
    "ALWAYS_BLOCKED_CALLS",
    "BLOCKED_ATTR_CHAINS",
    "DANGEROUS_IMPORTS",
    "DANGEROUS_CALLS",
    "DEFAULT_ALLOWED_TOP_LEVEL",
    "NETWORK_MODULES",
    "SUBPROCESS_MODULES",
    "DEFAULT_TIMEOUT_SEC",
    "DEFAULT_RECURSION_LIMIT",
    "MAX_AST_DEPTH",
    "MAX_SOURCE_SIZE",
]

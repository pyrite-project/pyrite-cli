"""
向后兼容 shim — 重导出自 cli.plugins。

原有代码 ``from .plugin_manager import ...`` 无需任何改动，
所有功能已迁移至 ``cli.plugins`` 包。
"""

from .plugins.manager import (  # noqa: F401
    PluginInfo,
    _get_pyrite_root,
    _load_local_plugin,
    _scan_plugin_dir,
    discover_plugins,
    get_loaded_plugins,
    load_plugin,
    load_plugins,
)

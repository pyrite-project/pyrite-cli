"""
Plugin system for pyrite-cli.

Uses setuptools entry points (group: ``pyrite.commands``) to discover and load
third-party plugins that register Typer subcommands.

本地插件
--------
除了通过 pip 安装的插件外，pyrite-cli 还会从以下位置自动扫描本地插件：

- **全局**：pyrite-cli 安装根目录下的 ``plugin/`` 目录
- **局部**：当前工作目录下的 ``plugin/`` 目录

每个子目录是一个独立的插件包，须包含 ``__init__.py`` 并导出 ``app: typer.Typer``。
"""

from __future__ import annotations

import dataclasses
import importlib
import importlib.metadata
import importlib.util
import os
from typing import List

import typer


@dataclasses.dataclass
class PluginInfo:
    """已加载插件的元信息。"""

    name: str
    version: str
    description: str
    module_path: str
    app: typer.Typer | None = None  # 内部使用，不对外暴露


# 全局缓存 — load_plugins() 写入，get_loaded_plugins() 读取
_loaded_plugins: List[PluginInfo] = []


# ── 辅助函数 ──────────────────────────────────────────────────────


def _get_pyrite_root() -> str:
    """返回 pyrite-cli 安装根目录（``cli/`` 的父目录）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _attach_plugin(main_app: typer.Typer, info: PluginInfo) -> None:
    """将插件挂载到主应用，并加入已加载列表。"""
    assert info.app is not None  # 由上层调用保证
    main_app.add_typer(info.app, name=info.name)
    _loaded_plugins.append(info)


# ── Entry point 发现（pip 安装的插件） ────────────────────────────


def discover_plugins() -> list:
    """发现所有注册了 ``pyrite.commands`` entry point 的第三方包。"""
    try:
        # Python 3.9+
        eps = importlib.metadata.entry_points(group="pyrite.commands")
        return list(eps)
    except TypeError:
        # Python 3.8 fallback
        try:
            eps = importlib.metadata.entry_points()
            return list(eps.get("pyrite.commands", []))
        except Exception:
            return []


def _get_module_attr(module_name: str, attr: str, default: str = "") -> str:
    """安全获取模块属性的字符串值，失败返回 default。"""
    try:
        mod = importlib.import_module(module_name)
        return getattr(mod, attr, default)
    except Exception:
        return default


def load_plugin(ep) -> PluginInfo | None:
    """加载单个 entry point 插件，失败返回 ``None``（仅打印警告）。"""
    try:
        app = ep.load()
        if not isinstance(app, typer.Typer):
            return None

        name = _get_module_attr(ep.module, "__plugin_name__", ep.name)
        version = _get_module_attr(ep.module, "__plugin_version__", "0.0.0")
        desc = app.info.help if app.info and app.info.help else ""
        return PluginInfo(
            name=name, version=version, description=desc, module_path=ep.value, app=app
        )
    except Exception as e:
        typer.secho(
            f"  [WARN] 加载插件 '{ep.name}' 失败: {e}", fg=typer.colors.YELLOW
        )
        return None


# ── 本地插件发现（plugin/ 目录） ──────────────────────────────────


def _load_local_plugin(name: str, init_file: str, source: str) -> PluginInfo | None:
    """从 ``__init__.py`` 加载单个本地插件。"""
    try:
        module_name = f"_pyrite_local_plugin_{source}_{name}"
        spec = importlib.util.spec_from_file_location(module_name, init_file)
        if spec is None or spec.loader is None:
            return None

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        app = getattr(module, "app", None)
        if not isinstance(app, typer.Typer):
            return None

        plugin_name = getattr(module, "__plugin_name__", name)
        version = getattr(module, "__plugin_version__", "0.0.0")
        desc = app.info.help if app.info and app.info.help else ""
        return PluginInfo(
            name=plugin_name,
            version=version,
            description=desc,
            module_path=init_file,
            app=app,
        )
    except Exception as e:
        typer.secho(
            f"  [WARN] 加载本地插件 '{name}' ({source}) 失败: {e}",
            fg=typer.colors.YELLOW,
        )
        return None


def _scan_plugin_dir(plugin_dir: str, source: str) -> List[PluginInfo]:
    """扫描一个 ``plugin/`` 目录，加载其中所有有效插件包。"""
    if not os.path.isdir(plugin_dir):
        return []

    results: List[PluginInfo] = []
    for entry in sorted(os.listdir(plugin_dir)):
        sub_path = os.path.join(plugin_dir, entry)
        init_file = os.path.join(sub_path, "__init__.py")
        if not os.path.isdir(sub_path) or not os.path.isfile(init_file):
            continue
        info = _load_local_plugin(entry, init_file, source)
        if info is not None:
            results.append(info)
    return results


# ── 公共入口 ──────────────────────────────────────────────────────


def load_plugins(main_app: typer.Typer) -> List[PluginInfo]:
    """发现、加载所有插件并挂载到 ``main_app``。

    加载顺序（同名时后者覆盖前者）：
    1. pip 安装的插件（通过 entry points）
    2. 全局本地插件（``<pyrite-root>/plugin/``）
    3. 项目本地插件（``<CWD>/plugin/``）

    单个插件失败不影响其他插件。
    """
    global _loaded_plugins
    _loaded_plugins = []

    # 1. pip 安装的插件
    for ep in discover_plugins():
        info = load_plugin(ep)
        if info is not None:
            _attach_plugin(main_app, info)

    # 2. 全局本地插件（与 pyrite-cli 安装目录同级的 plugin/）
    global_dir = os.path.join(_get_pyrite_root(), "plugin")
    for info in _scan_plugin_dir(global_dir, "global"):
        _attach_plugin(main_app, info)

    # 3. 项目本地插件（当前工作目录下的 plugin/）
    local_dir = os.path.join(os.getcwd(), "plugin")
    if os.path.abspath(local_dir) != os.path.abspath(global_dir):
        for info in _scan_plugin_dir(local_dir, "local"):
            _attach_plugin(main_app, info)

    return _loaded_plugins


def get_loaded_plugins() -> List[PluginInfo]:
    """返回本次会话中已成功加载的插件列表（副本）。"""
    return list(_loaded_plugins)

"""
插件管理系统 — 发现、加载、沙箱化插件。

插件来源：
1. pip 安装的插件（通过 ``pyrite.commands`` entry point）— 完全信任
2. 全局本地插件（``<pyrite-root>/plugin/``）— 沙箱化
3. 项目本地插件（``<CWD>/plugin/``）— 沙箱化

沙箱集成：本地插件在 exec_module() 前经过五层防御：
1. plugin.json 清单验证
2. AST 预扫描
3. 导入拦截器
4. 内置函数守卫
5. 资源限制
"""

from __future__ import annotations

import dataclasses
import importlib
import importlib.metadata
import importlib.util
import os
import sys
from pathlib import Path
from typing import List, Optional

import typer

from ..utils.log import get_logger
from .sandbox import (
    SandboxConfig,
    SandboxError,
    ScanResult,
    execution_timeout,
    load_plugin_manifest,
    make_sandboxed_builtins,
    recursion_limit_guard,
    sandboxed_imports,
    scan_source,
)

log = get_logger(__name__)


@dataclasses.dataclass
class PluginInfo:
    """已加载插件的元信息。"""

    name: str
    version: str
    description: str
    module_path: str
    app: typer.Typer | None = None
    sandbox_config: SandboxConfig | None = None


# 全局缓存 — load_plugins() 写入，get_loaded_plugins() 读取
_loaded_plugins: List[PluginInfo] = []


# ── 辅助函数 ──────────────────────────────────────────────────────


def _get_pyrite_root() -> str:
    """返回 pyrite-cli 安装根目录。"""
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


def _attach_plugin(main_app: typer.Typer, info: PluginInfo) -> None:
    """将插件挂载到主应用，并加入已加载列表。"""
    assert info.app is not None
    main_app.add_typer(info.app, name=info.name)
    _loaded_plugins.append(info)
    log.info("插件已加载: %s v%s (%s)", info.name, info.version, info.description)


def _report_scan_result(
    name: str, source_type: str, result: ScanResult, config: SandboxConfig,
) -> None:
    """输出沙箱诊断信息。"""
    log.warning(
        "插件 '%s' (%s) 被沙箱拒绝 (模式=%s): %d 个错误, %d 个警告",
        name, source_type, config.mode,
        result.error_count, result.warning_count,
    )
    for err in result.errors:
        log.warning("  ├─ [错误] %s (第%d行)", err["message"], err["line"])
    for warn in result.warnings:
        log.warning("  ├─ [警告] %s (第%d行)", warn["message"], warn["line"])
    log.warning("  └─ 设置 mode=permissive 于 plugin/%s/plugin.json 以跳过沙箱", name)
    log.warning("     或通过 pip 安装: pip install -e plugin/%s/", name)


# ── Entry point 发现（pip 安装的插件） ──────────────────────────


def discover_plugins() -> list:
    """发现所有注册了 ``pyrite.commands`` entry point 的第三方包。"""
    try:
        eps = importlib.metadata.entry_points(group="pyrite.commands")
        return list(eps)
    except TypeError:
        try:
            eps = importlib.metadata.entry_points()
            return list(eps.get("pyrite.commands", []))
        except Exception:
            return []


def _get_module_attr(module_name: str, attr: str, default: str = "") -> str:
    """安全获取模块属性的字符串值。"""
    try:
        mod = importlib.import_module(module_name)
        return getattr(mod, attr, default)
    except Exception:
        return default


def load_plugin(ep) -> PluginInfo | None:
    """加载单个 entry point 插件（pip 安装，完全信任，不经过沙箱）。"""
    try:
        app = ep.load()
        if not isinstance(app, typer.Typer):
            return None

        name = _get_module_attr(ep.module, "__plugin_name__", ep.name)
        version = _get_module_attr(ep.module, "__plugin_version__", "0.0.0")
        desc = app.info.help if app.info and app.info.help else ""
        return PluginInfo(
            name=name,
            version=version,
            description=desc,
            module_path=ep.value,
            app=app,
            sandbox_config=None,
        )
    except Exception as e:
        log.warning("加载插件 '%s' 失败: %s", ep.name, e)
        return None


# ── 本地插件发现（plugin/ 目录） ──────────────────────────────────


def _load_local_plugin(
    name: str,
    init_file: str,
    source: str,
    sandbox_config: SandboxConfig | None = None,
) -> PluginInfo | None:
    """从 ``__init__.py`` 加载单个本地插件，经过完整沙箱检查。"""
    plugin_dir = os.path.dirname(init_file)

    # 第1层：加载插件清单
    if sandbox_config is None:
        sandbox_config = load_plugin_manifest(plugin_dir)

    # 第2层：AST 预扫描
    try:
        source_code = Path(init_file).read_text(encoding="utf-8")
    except OSError as e:
        log.warning("无法读取插件源码 '%s': %s", name, e)
        return None

    scan_result = scan_source(source_code, init_file, sandbox_config)

    if not scan_result.passed and sandbox_config.mode != "permissive":
        _report_scan_result(name, source, scan_result, sandbox_config)
        return None

    if scan_result.warnings:
        log.info(
            "插件 '%s' 沙箱扫描有 %d 个警告:", name, scan_result.warning_count,
        )
        for warn in scan_result.warnings:
            log.warning("  ├─ [警告] %s (第%d行)", warn["message"], warn["line"])

    # 创建模块
    try:
        module_name = f"_pyrite_local_plugin_{source}_{name}"
        spec = importlib.util.spec_from_file_location(module_name, init_file)
        if spec is None or spec.loader is None:
            return None

        module = importlib.util.module_from_spec(spec)

        # 第4层：内置函数守卫
        module.__builtins__ = make_sandboxed_builtins(sandbox_config, name)

        # 第3+5层：导入拦截 + 资源限制下执行模块
        with execution_timeout(sandbox_config.timeout_sec, name):
            with recursion_limit_guard(sandbox_config.recursion_limit):
                with sandboxed_imports(sandbox_config, name):
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
            sandbox_config=sandbox_config,
        )
    except SandboxError as e:
        log.warning("插件 '%s' 违反沙箱规则: %s", name, e)
        return None
    except Exception as e:
        log.warning("加载本地插件 '%s' (%s) 失败: %s", name, source, e)
        return None


def _scan_plugin_dir(
    plugin_dir: str,
    source: str,
    sandbox_config: SandboxConfig | None = None,
) -> List[PluginInfo]:
    """扫描一个 ``plugin/`` 目录，加载其中所有有效插件包。"""
    if not os.path.isdir(plugin_dir):
        return []

    results: List[PluginInfo] = []
    for entry in sorted(os.listdir(plugin_dir)):
        sub_path = os.path.join(plugin_dir, entry)
        init_file = os.path.join(sub_path, "__init__.py")
        if not os.path.isdir(sub_path) or not os.path.isfile(init_file):
            continue
        info = _load_local_plugin(entry, init_file, source, sandbox_config)
        if info is not None:
            results.append(info)
    return results


# ── 公共入口 ──────────────────────────────────────────────────────


def load_plugins(main_app: typer.Typer) -> List[PluginInfo]:
    """发现、加载所有插件并挂载到 ``main_app``。

    加载顺序（同名时后者覆盖前者）：
    1. pip 安装的插件（通过 entry points）— 不受沙箱限制
    2. 全局本地插件（``<pyrite-root>/plugin/``）— 受沙箱限制
    3. 项目本地插件（``<CWD>/plugin/``）— 受沙箱限制
    """
    global _loaded_plugins
    _loaded_plugins = []

    log.debug("开始加载插件...")

    # 1. pip 安装的插件
    for ep in discover_plugins():
        info = load_plugin(ep)
        if info is not None:
            _attach_plugin(main_app, info)

    # 2. 全局本地插件
    global_dir = os.path.join(_get_pyrite_root(), "plugin")
    for info in _scan_plugin_dir(global_dir, "global"):
        _attach_plugin(main_app, info)

    # 3. 项目本地插件
    local_dir = os.path.join(os.getcwd(), "plugin")
    if os.path.abspath(local_dir) != os.path.abspath(global_dir):
        for info in _scan_plugin_dir(local_dir, "local"):
            _attach_plugin(main_app, info)

    log.debug("插件加载完成，共 %d 个", len(_loaded_plugins))
    return _loaded_plugins


def get_loaded_plugins() -> List[PluginInfo]:
    """返回本次会话中已成功加载的插件列表（副本）。"""
    return list(_loaded_plugins)

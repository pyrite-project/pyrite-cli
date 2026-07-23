"""
项目脚手架 — 新建项目、交互式硬件选择、自动检测、存根初始化。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

from .stubs import (
    create_vscode_config,
    download_stubs,
    ensure_feature_stub,
    find_stub_dir,
    get_hardware_types,
    list_available,
    list_stub_dirs,
    warn_legacy_project_stubs,
    write_project_stub_config,
    version_to_dir,
)
from ..utils.board_alias import resolve_port_alias
from ..utils.config import _load_config, resolve_connection_settings
from ..utils.log import get_logger
from ..utils.ui import interactive_select

log = get_logger(__name__)


def detect_device_info(
    port: str,
    baudrate: Optional[int] = None,
    timeout: Optional[int] = None,
) -> tuple[str, str]:
    """连接到 MicroPython 设备并自动检测硬件类型和固件版本。"""
    from ..utils.flash import MicroPython

    port = resolve_port_alias(port)
    baudrate, timeout = resolve_connection_settings(
        baudrate,
        timeout,
        _load_config(),
    )
    mp = MicroPython(port=port, baudrate=baudrate, timeout=timeout)
    try:
        mp.connect()
        context = mp.ensure_device_context()
    except Exception as e:
        raise RuntimeError(f"设备通信失败: {e}") from e
    finally:
        mp.disconnect()

    version = context.version
    hardware = context.platform
    if not version or not hardware:
        raise RuntimeError("设备返回数据异常：缺少固件版本或平台信息")

    log.info("检测到硬件: %s, 固件版本: %s", hardware, version)
    return hardware, version


_MANIFEST_TEMPLATE = """\
# manifest.py - 控制哪些文件刷入设备
# module("main.py")
# module("lib/utils.py", features=["wifi"])
# package("lib")
"""


def init_project(proj_name: str) -> None:
    os.mkdir(proj_name)
    (Path(proj_name) / "manifest.py").write_text(_MANIFEST_TEMPLATE, encoding="utf-8")


def _configure_project_stubs(
    *,
    stub_dir: str,
    hardware: str,
    version: str,
    variant: Optional[str],
) -> None:
    count, out_path = download_stubs(stub_dir, "")
    log.info("已准备 %d 个 .pyi 文件到 %s", count, out_path)
    pyrite_stub_path = ensure_feature_stub()
    settings_file = create_vscode_config(
        out_path,
        hardware,
        version,
        extra_paths=[pyrite_stub_path],
    )
    config_file = write_project_stub_config(
        hardware=hardware,
        version=version,
        variant=variant,
        stub_dir=stub_dir,
        stub_path=out_path,
    )
    warn_legacy_project_stubs()
    log.info("VS Code 配置: %s", settings_file)
    log.info("Pyrite stubs 配置: %s", config_file)


def new_project_interactive(
    proj_name: str,
    platform: Optional[str] = None,
    port: Optional[str] = None,
    baudrate: Optional[int] = None,
    timeout: Optional[int] = None,
) -> None:
    """创建一个支持交互式硬件/版本选择的新 MicroPython 项目。"""
    init_project(proj_name)

    if port:
        log.info("通过 %s 连接设备进行自动检测...", port)
        try:
            connection_options = {}
            if baudrate is not None:
                connection_options["baudrate"] = baudrate
            if timeout is not None:
                connection_options["timeout"] = timeout
            hardware, version = detect_device_info(port, **connection_options)
        except Exception as e:
            log.error("设备检测失败: %s", e)
            log.info(
                "项目目录 '%s' 已创建，可稍后运行 "
                "'cd %s && pyrcli project init' 手动配置",
                proj_name,
                proj_name,
            )
            return

        log.info("正在查询可用存根...")
        try:
            dirs = list_stub_dirs()
        except Exception as e:
            log.error("获取存根列表失败: %s", e)
            log.info(
                "项目目录 '%s' 已创建，可稍后运行 "
                "'pyrcli project init %s %s' 手动配置",
                proj_name,
                hardware,
                version,
            )
            return
        log.info("可用存根查询完成，共 %d 个目录", len(dirs))

        orig_cwd = os.getcwd()
        try:
            os.chdir(proj_name)
            stub_dir = find_stub_dir(dirs, hardware, version, None)
            if not stub_dir:
                versions = _get_versions_for_hardware(dirs, hardware)
                nearest = (
                    _find_nearest_version(version, versions)
                    if versions else None
                )
                if nearest:
                    log.warning("未找到 v%s 的精确匹配，尝试最接近版本 v%s", version, nearest)
                    version = nearest
                    stub_dir = find_stub_dir(dirs, hardware, version, None)
            if stub_dir:
                _configure_project_stubs(
                    stub_dir=stub_dir,
                    hardware=hardware,
                    version=version,
                    variant=None,
                )
            else:
                log.warning(
                    "未找到 %s v%s 的匹配存根，可稍后运行 "
                    "'pyrcli project init %s %s' 配置",
                    hardware,
                    version,
                    hardware,
                    version,
                )
        finally:
            os.chdir(orig_cwd)
        return

    log.info("正在查询可用平台...")
    try:
        dirs = list_stub_dirs()
    except Exception as e:
        log.error("获取平台列表失败: %s", e)
        return

    hw_types = sorted(get_hardware_types(dirs))
    if not hw_types:
        log.warning("未找到可用平台")
        return
    log.info("可用平台查询完成，共 %d 个", len(hw_types))

    if platform:
        selected_hw = next(
            (item for item in hw_types if item.casefold() == platform.casefold()),
            None,
        )
        if selected_hw is None:
            log.error(
                "不支持的平台 '%s'，可用平台: %s",
                platform,
                ", ".join(hw_types),
            )
            return
        log.info("已指定平台: %s", selected_hw)
    else:
        selected_hw = interactive_select(hw_types, "选择平台")

    versions = _get_versions_for_hardware(dirs, selected_hw)
    if not versions:
        log.warning("未找到 %s 的可用版本", selected_hw)
        return
    selected_ver = interactive_select(versions, "选择固件版本")

    variants = _get_variants_for_hw_version(dirs, selected_hw, selected_ver)
    variant = None
    if variants:
        variant_opts = ["(不指定，自动匹配)"] + variants
        picked = interactive_select(variant_opts, "选择开发板变体")
        if not picked.startswith("("):
            variant = picked

    orig_cwd = os.getcwd()
    try:
        os.chdir(proj_name)
        stub_dir = find_stub_dir(dirs, selected_hw, selected_ver, variant)
        if stub_dir:
            _configure_project_stubs(
                stub_dir=stub_dir,
                hardware=selected_hw,
                version=selected_ver,
                variant=variant,
            )
        else:
            log.warning("未找到 %s v%s 的匹配存根，可稍后运行 'pyrcli init' 配置", selected_hw, selected_ver)
    finally:
        os.chdir(orig_cwd)


def _get_versions_for_hardware(
    dirs: list[str], hardware: str,
) -> list[str]:
    """提取指定硬件类型的可用固件版本。"""
    versions: set[str] = set()
    prefix = "micropython-"
    for d in dirs:
        if d.startswith(prefix) and f"-{hardware}" in d:
            parts = d.split("-", 2)
            if len(parts) >= 2:
                v = parts[1]
                if v.startswith("v"):
                    version = v[1:].replace("_", ".")
                    versions.add(version)

    def _sort_key(v: str) -> tuple:
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (0,)

    return sorted(versions, key=_sort_key, reverse=True)


def _get_variants_for_hw_version(
    dirs: list[str], hardware: str, version: str,
) -> list[str]:
    """提取特定硬件 + 版本组合对应的可用固件型号。"""
    vdir = version_to_dir(version)
    prefix = f"micropython-{vdir}-{hardware}"
    return sorted(
        d[len(prefix) + 1:] for d in dirs
        if d.startswith(prefix) and d != prefix and d[len(prefix)] == "-"
    )


def _find_nearest_version(
    target: str, available: list[str],
) -> Optional[str]:
    """在可用版本列表中查找与目标版本最接近的版本（仅限同主版本号）。"""
    def _parse(v: str) -> tuple[int, ...]:
        parts = []
        for x in v.split("."):
            m = re.match(r"(\d+)", x)
            parts.append(int(m.group(1)) if m else 0)
        return tuple(parts)

    target_parts = _parse(target)
    best = None
    best_dist = None

    for v in available:
        v_parts = _parse(v)
        if v_parts[0] != target_parts[0]:
            continue

        max_len = max(len(target_parts), len(v_parts))
        t = target_parts + (0,) * (max_len - len(target_parts))
        vp = v_parts + (0,) * (max_len - len(v_parts))
        dist = sum(abs(a - b) for a, b in zip(t, vp))

        if best is None or dist < best_dist:
            best = v
            best_dist = dist

    return best


def init_stubs(
    hardware: Optional[str] = None,
    version: Optional[str] = None,
    variant: Optional[str] = None,
    port: Optional[str] = None,
    baudrate: Optional[int] = None,
    timeout: Optional[int] = None,
) -> None:
    """在已有项目中下载 MicroPython 类型存根。"""
    if port:
        try:
            connection_options = {}
            if baudrate is not None:
                connection_options["baudrate"] = baudrate
            if timeout is not None:
                connection_options["timeout"] = timeout
            hardware, version = detect_device_info(port, **connection_options)
        except Exception as e:
            log.error("设备检测失败: %s", e)
            sys.exit(1)
        variant = None
    elif hardware and version:
        pass
    else:
        # 交互式选择模式
        log.info("正在查询可用存根...")
        try:
            dirs = list_stub_dirs()
        except Exception as e:
            log.error("获取存根列表失败: %s", e)
            sys.exit(1)

        hw_types = sorted(get_hardware_types(dirs))
        if not hw_types:
            log.error("未找到可用的硬件类型")
            sys.exit(1)
        hardware = interactive_select(hw_types, "选择硬件类型")

        versions = _get_versions_for_hardware(dirs, hardware)
        if not versions:
            log.error("未找到 %s 的可用版本", hardware)
            sys.exit(1)
        version = interactive_select(versions, "选择固件版本")

        variants = _get_variants_for_hw_version(dirs, hardware, version)
        if variants:
            variant_opts = ["(不指定，自动匹配)"] + variants
            picked = interactive_select(variant_opts, "选择开发板变体")
            if not picked.startswith("("):
                variant = picked

    log.info("正在查询存根：硬件=%s，版本=%s%s", hardware, version, f"，变体={variant}" if variant else "")
    dirs = list_stub_dirs()

    stub_dir = find_stub_dir(dirs, hardware, version, variant)
    if not stub_dir and not variant:
        versions = _get_versions_for_hardware(dirs, hardware)
        nearest = _find_nearest_version(version, versions) if versions else None
        if nearest:
            log.warning("未找到 v%s 的精确匹配，尝试最接近版本 v%s", version, nearest)
            version = nearest
            stub_dir = find_stub_dir(dirs, hardware, version, None)
    if not stub_dir:
        msg = f"错误：未找到 {hardware} v{version}"
        if variant:
            msg += f" 变体 {variant}"
        log.error(msg)
        vdir = version_to_dir(version)
        if variant:
            log.info("预期模式：micropython-%s-%s-%s[...]", vdir, hardware, variant)
        else:
            log.info("预期模式：micropython-%s-%s[...]", vdir, hardware)
        list_available(dirs, hardware)
        sys.exit(1)

    log.info("找到存根目录：%s", stub_dir)
    _configure_project_stubs(
        stub_dir=stub_dir,
        hardware=hardware,
        version=version,
        variant=variant,
    )

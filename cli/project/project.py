import os
import re
import sys
from .stubs import *
from ..utils.selector import interactive_select

def detect_device_info(port: str, baudrate: int = 115200,
                       timeout: int = 10) -> tuple[str, str]:
    """连接到 MicroPython 设备并自动检测硬件类型和固件版本。

    通过串口连接设备，执行 import sys; print(sys.version); print(sys.platform)
    并解析输出以获取硬件平台和 MicroPython 固件版本。

    Args:
        port: 串口号，如 COM3 或 /dev/ttyUSB0
        baudrate: 波特率，默认 115200
        timeout: 超时秒数，默认 10

    Returns:
        (hardware, version) 元组，如 ('esp32', '1.22.2')

    Raises:
        RuntimeError: 设备连接失败或输出解析失败
    """
    from ..utils.Flash import MicroPython

    mp = MicroPython(port=port, baudrate=baudrate, timeout=timeout)
    try:
        mp.connect()
        output = mp.run("import sys;print(sys.version);print(sys.platform)")
    except Exception as e:
        raise RuntimeError(f"设备通信失败: {e}") from e
    finally:
        mp.disconnect()

    lines = [l.strip() for l in output.strip().split('\n') if l.strip()]
    if len(lines) < 2:
        raise RuntimeError(f"设备返回数据异常:\n{output}")

    version_line = lines[0]
    platform_line = lines[-1]

    m = re.search(r'MicroPython v([\d.]+)', version_line)
    if not m:
        raise RuntimeError(f"无法从输出中解析固件版本: {version_line}")

    version = m.group(1)
    hardware = platform_line

    print(f"\n  \033[32m检测到硬件: {hardware}, 固件版本: {version}\033[0m\n")
    return hardware, version


def init_project(proj_name: str):
    os.mkdir(proj_name)


def new_project_interactive(proj_name: str, platform: str | None = None):
    """创建一个支持交互式硬件/版本选择的新 MicroPython 项目。

    引导用户完成以下步骤：
      1. 创建项目目录
      2. 选择硬件类型（从 GitHub 获取）
      3. 选择固件版本（根据所选硬件进行筛选）
      4. 下载代码模板 + VS Code 配置
    """
    init_project(proj_name)

    # ── 自动检测模式（--platform） ──
    if platform:
        print(f"\n · 正在通过 {platform} 连接设备...\n")
        try:
            hardware, version = detect_device_info(platform)
        except Exception as e:
            print(f"  \033[31m设备检测失败: {e}\033[0m")
            print(f"  \033[33m项目目录 '{proj_name}' 已创建，可稍后运行"
                  f" 'cd {proj_name} && pyrcli init' 手动配置\033[0m\n")
            return

        print(" · 正在查询可用存根...\n")
        try:
            dirs = list_stub_dirs()
        except Exception as e:
            print(f"  \033[31m获取存根列表失败: {e}\033[0m")
            print(f"  \033[33m项目目录 '{proj_name}' 已创建，可稍后运行"
                  f" 'pyrcli init {hardware} {version}' 手动配置\033[0m\n")
            return

        orig_cwd = os.getcwd()
        try:
            os.chdir(proj_name)
            stub_dir = find_stub_dir(dirs, hardware, version, None)
            if not stub_dir:
                versions = _get_versions_for_hardware(dirs, hardware)
                nearest = _find_nearest_version(version, versions) if versions else None
                if nearest:
                    print(f"  \033[33m未找到 v{version} 的精确匹配，"
                          f"尝试最接近版本 v{nearest}\033[0m")
                    version = nearest
                    stub_dir = find_stub_dir(dirs, hardware, version, None)
            if stub_dir:
                count, out_path = download_stubs(stub_dir, '')
                print(f"\n  \033[32m已下载 {count} 个 .pyi 文件\033[0m")
                settings_file = create_vscode_config(out_path, hardware, version)
                print(f"  \033[32mVS Code 配置: {settings_file}\033[0m\n")
            else:
                print(f"\n  \033[31m未找到 {hardware} v{version} 的匹配存根"
                      f"，可稍后运行 'pyrcli init {hardware} {version}' 配置\033[0m\n")
        finally:
            os.chdir(orig_cwd)
        return

    # ── 交互式选择模式 ──
    print("\n · 正在查询可用硬件...\n")
    try:
        dirs = list_stub_dirs()
    except Exception as e:
        print(f"  \033[31m获取硬件列表失败: {e}\033[0m")
        print(f"  \033[33m项目目录 '{proj_name}' 已创建，可稍后运行"
              f" 'cd {proj_name} && pyrcli init' 手动配置\033[0m\n")
        return

    hw_types = sorted(get_hardware_types(dirs))
    if not hw_types:
        print("  未找到可用的硬件类型\n")
        return
    selected_hw = interactive_select(hw_types, "选择硬件类型")

    versions = _get_versions_for_hardware(dirs, selected_hw)
    if not versions:
        print(f"  未找到 {selected_hw} 的可用版本\n")
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
            count, out_path = download_stubs(stub_dir, '')
            print(f"\n  \033[32m已下载 {count} 个 .pyi 文件\033[0m")
            settings_file = create_vscode_config(out_path, selected_hw, selected_ver)
            print(f"  \033[32mVS Code 配置: {settings_file}\033[0m\n")
        else:
            print(f"\n  \033[33m未找到 {selected_hw} v{selected_ver} 的匹配存根"
                  f"，可稍后运行 'pyrcli init' 配置\033[0m\n")
    finally:
        os.chdir(orig_cwd)


def _get_versions_for_hardware(dirs: list[str], hardware: str) -> list[str]:
    """提取指定硬件类型的可用固件版本。"""
    versions: set[str] = set()
    prefix = f"micropython-"
    for d in dirs:
        if d.startswith(prefix) and f"-{hardware}" in d:
            parts = d.split("-", 2)
            if len(parts) >= 2:
                v = parts[1]
                if v.startswith("v"):
                    version = v[1:].replace("_", ".")
                    versions.add(version)

    def _sort_key(v: str):
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (0,)

    return sorted(versions, key=_sort_key, reverse=True)


def _get_variants_for_hw_version(dirs: list[str], hardware: str,
                                  version: str) -> list[str]:
    """提取特定硬件 + 版本组合对应的可用固件型号。"""
    vdir = version_to_dir(version)
    prefix = f"micropython-{vdir}-{hardware}"
    return sorted(
        d[len(prefix) + 1:] for d in dirs
        if d.startswith(prefix) and d != prefix and d[len(prefix)] == "-"
    )


def _find_nearest_version(target: str, available: list[str]) -> str | None:
    """在可用版本列表中查找与目标版本最接近的版本（仅限同主版本号）。"""
    def _parse(v: str) -> tuple[int, ...]:
        parts = []
        for x in v.split('.'):
            m = re.match(r'(\d+)', x)
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


def init_stubs(hardware=None, version=None, variant=None, platform=None):
    if platform:
        try:
            hardware, version = detect_device_info(platform)
        except Exception as e:
            print(f"\033[31m设备检测失败: {e}\033[0m")
            sys.exit(1)
        variant = None
    elif not hardware or not version:
        print("错误：请指定 hardware 和 version 参数，或使用 --platform 自动检测")
        sys.exit(1)

    print(f"正在查询存根：硬件={hardware}，版本={version}"
          + (f"，变体={variant}" if variant else ""))
    dirs = list_stub_dirs()

    stub_dir = find_stub_dir(dirs, hardware, version, variant)
    if not stub_dir and not variant:
        versions = _get_versions_for_hardware(dirs, hardware)
        nearest = _find_nearest_version(version, versions) if versions else None
        if nearest:
            print(f"\033[33m未找到 v{version} 的精确匹配，"
                  f"尝试最接近版本 v{nearest}\033[0m")
            version = nearest
            stub_dir = find_stub_dir(dirs, hardware, version, None)
    if not stub_dir:
        msg = f"错误：未找到 {hardware} v{version}"
        if variant:
            msg += f" 变体 {variant}"
        print(msg)
        vdir = version_to_dir(version)
        if variant:
            print(f"预期模式：micropython-{vdir}-{hardware}-{variant}[...]")
        else:
            print(f"预期模式：micropython-{vdir}-{hardware}[...]")
        list_available(dirs, hardware)
        sys.exit(1)
        
    print(f"找到存根目录：{stub_dir}")
    count, out_path = download_stubs(stub_dir, '')
    print(f"已下载 {count} 个 .pyi 文件到 {out_path}")

    # 创建/更新 VS Code 配置
    settings_file = create_vscode_config(out_path, hardware, version)
    print(f"已更新 VS Code 配置：{settings_file}")
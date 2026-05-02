import os
import sys
from .stubs import *
from ..utils.selector import interactive_select

def init_project(proj_name: str):
    os.mkdir(proj_name)


def new_project_interactive(proj_name: str):
    """创建一个支持交互式硬件/版本选择的新 MicroPython 项目。

    引导用户完成以下步骤：
      1. 创建项目目录
      2. 选择硬件类型（从 GitHub 获取）
      3. 选择固件版本（根据所选硬件进行筛选）
      4. 下载代码模板 + VS Code 配置
    """
    init_project(proj_name)

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


def init_stubs(hardware, version, variant=None):
    print(f"正在查询存根：硬件={hardware}，版本={version}"
          + (f"，变体={variant}" if variant else ""))
    dirs = list_stub_dirs()

    stub_dir = find_stub_dir(dirs, hardware, version, variant)
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
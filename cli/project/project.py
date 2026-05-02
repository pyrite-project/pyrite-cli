import os
import sys
from .stubs import *

def init_project(proj_name:str):
    os.mkdir(proj_name)
   
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
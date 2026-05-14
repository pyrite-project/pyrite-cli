import os
import subprocess
import tempfile
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .ansi import _YELLOW, _RESET


def _compile_to_mpy(local_path: str, bytecode_ver: int = None, arch: str = None):
    """编译 .py -> .mpy，返回 (tmp_mpy_path, tmp_dir)；失败返回 (None, None)。"""
    tmp_dir = tempfile.mkdtemp()
    os.chmod(tmp_dir, 0o700)
    out_path = os.path.join(tmp_dir, Path(local_path).stem + ".mpy")
    args = [local_path, "-o", out_path]
    if arch is not None:
        args += [f"-march={arch}"]
    try:
        import mpy_cross
        if bytecode_ver is not None:
            mpy_cross.set_version(micropython=None, bytecode=str(bytecode_ver))
        r = mpy_cross.run(*args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        r.wait(timeout=30)
        if r.returncode == 0:
            return out_path, tmp_dir
        print(f"  {_YELLOW}[WARN]{_RESET} mpy-cross 编译失败，回退到 .py\n"
              f"         {r.stderr.read().decode(errors='replace').strip()}")
    except ImportError:
        print(f"  {_YELLOW}[INFO]{_RESET} 未找到 mpy-cross，跳过编译")
    except Exception as e:
        print(f"  {_YELLOW}[WARN]{_RESET} 编译异常: {e}，回退到 .py")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None, None


def _compile_files_parallel(local_paths: list, bytecode_ver: int = None,
                             arch: str = None, max_workers: int = 4):
    """并行编译多个 .py → .mpy。

    Args:
        local_paths: 本地 .py 路径列表
        bytecode_ver: mpy 字节码版本
        arch: 目标架构
        max_workers: 最大并行数

    Returns:
        dict: {local_path: (mpy_path, tmp_dir)}，编译失败则值为 (None, None)
    """
    if not local_paths:
        return {}
    results = {}
    workers = min(max_workers, len(local_paths))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_compile_to_mpy, lp, bytecode_ver, arch): lp
                   for lp in local_paths}
        for future in as_completed(futures):
            lp = futures[future]
            try:
                results[lp] = future.result()
            except Exception:
                results[lp] = (None, None)
    return results

"""
mpy-cross 编译器封装 — 单文件编译与并行编译。

通过 mpy-cross Python API 将 .py 编译为 .mpy，
失败时回退到原始 .py 文件。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Tuple

from .log import get_logger

log = get_logger(__name__)


def _compile_to_mpy(
    local_path: str,
    bytecode_ver: Optional[int] = None,
    arch: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """编译 .py → .mpy，返回 ``(mpy_path, tmp_dir)``；失败返回 ``(None, None)``。"""
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
            log.trace("编译成功: %s → %s", local_path, out_path)
            return out_path, tmp_dir
        err_msg = r.stderr.read().decode(errors="replace").strip()
        log.warning("mpy-cross 编译失败，回退到 .py: %s", err_msg)
    except ImportError:
        log.info("未找到 mpy-cross，跳过编译: %s", local_path)
    except Exception as e:
        log.warning("编译异常: %s，回退到 .py", e)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None, None


def _compile_files_parallel(
    local_paths: list,
    bytecode_ver: Optional[int] = None,
    arch: Optional[str] = None,
    max_workers: int = 4,
) -> dict:
    """并行编译多个 .py → .mpy。

    Args:
        local_paths: 本地 .py 路径列表
        bytecode_ver: mpy 字节码版本
        arch: 目标架构
        max_workers: 最大并行数

    Returns:
        ``{local_path: (mpy_path, tmp_dir)}``，编译失败则值为 ``(None, None)``
    """
    if not local_paths:
        return {}
    results = {}
    workers = min(max_workers, len(local_paths))
    log.debug("并行编译 %d 个文件 (workers=%d)", len(local_paths), workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_compile_to_mpy, lp, bytecode_ver, arch): lp
            for lp in local_paths
        }
        for future in as_completed(futures):
            lp = futures[future]
            try:
                results[lp] = future.result()
            except Exception as e:
                log.warning("并行编译任务异常: %s (%s)", lp, e)
                results[lp] = (None, None)
    return results

"""
GitHub 存根下载 — MicroPython 类型存根管理。

从 micropython-stubs 仓库获取可用硬件/版本列表，
多线程下载 .pyi 文件，并生成 VS Code Pylance 配置。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

from ..utils.log import get_logger

log = get_logger(__name__)

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

SOURCE = "https://api.github.com/repos/josverl/micropython-stubs"
VSCODE_DIR = ".vscode"
VSCODE_SETTINGS = "settings.json"

_DEFAULT_THREADS = 4
_MAX_THREADS = 12


def _get_download_threads() -> int:
    """从 .pyrite_config.json 读取下载线程数。"""
    cfg_file = Path(".pyrite_config.json")
    if not cfg_file.exists():
        return _DEFAULT_THREADS
    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        t = data.get("download_threads", _DEFAULT_THREADS)
        if not isinstance(t, int) or t <= 0:
            return _DEFAULT_THREADS
        return min(t, _MAX_THREADS)
    except (json.JSONDecodeError, OSError):
        return _DEFAULT_THREADS


def version_to_dir(v: str) -> str:
    """将 '1.20.0' 转换为 'v1_20_0'。"""
    return "v" + v.replace(".", "_")


def _request_with_retry(
    url: str, max_retries: int = 3, **kwargs,
) -> requests.Response:
    """带重试的 HTTP GET 请求。"""
    kwargs.setdefault("timeout", 30)
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, **kwargs)
            if resp.status_code == 403:
                log.error("GitHub API 速率限制已超，请稍后重试")
                sys.exit(1)
            if resp.status_code >= 500:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    log.warning(
                        "服务器错误 (%d)，%d 秒后重试 (%d/%d)",
                        resp.status_code, wait, attempt + 1, max_retries,
                    )
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                log.warning(
                    "网络错误: %s，%d 秒后重试 (%d/%d)",
                    e, wait, attempt + 1, max_retries,
                )
                time.sleep(wait)
                continue
            raise
    raise last_exc  # type: ignore[misc]


def list_stub_dirs() -> list[str]:
    """从仓库列出所有存根目录。"""
    url = f"{SOURCE}/contents/stubs"
    resp = _request_with_retry(url)
    return [item["name"] for item in resp.json() if item["type"] == "dir"]


def find_stub_dir(
    dirs: list[str],
    hardware: str,
    version: str,
    variant: Optional[str] = None,
) -> Optional[str]:
    """查找最匹配的存根目录名。"""
    vdir = version_to_dir(version)
    if variant:
        base = f"micropython-{vdir}-{hardware}-{variant}"
    else:
        base = f"micropython-{vdir}-{hardware}"

    if base in dirs:
        return base

    merged = f"{base}-merged"
    if merged in dirs:
        return merged

    matches = sorted(d for d in dirs if d.startswith(base))
    if matches:
        return matches[0]

    return None


def list_available(dirs: list[str], hardware: str) -> None:
    """显示指定硬件的可用存根。"""
    matches = [d for d in dirs if f"-{hardware}" in d or d.endswith(hardware)]
    if matches:
        log.info("匹配 '%s' 的可用存根 (%d 个):", hardware, len(matches))
        for m in sorted(matches):
            log.info("  %s", m)
    else:
        hw_types = get_hardware_types(dirs)
        log.info("未找到 '%s' 的存根，可用的硬件类型: %s", hardware, ", ".join(sorted(hw_types)))


def get_hardware_types(dirs: list[str]) -> set[str]:
    """从存根目录名中提取所有可用的硬件类型。"""
    hw_types = set()
    for d in dirs:
        if d.startswith("micropython-v"):
            parts = d.split("-")
            if len(parts) >= 3:
                hw_types.add(parts[2])
    return hw_types


def list_all_hardware(dirs: list[str]) -> None:
    """列出所有可用的 MicroPython 硬件类型。"""
    hw_types = sorted(get_hardware_types(dirs))
    log.info("可用的 MicroPython 硬件类型（共 %d 个）:", len(hw_types))
    for hw in hw_types:
        log.info("  %s", hw)


def download_stubs(
    stub_dir: str,
    output_dir: str,
    max_workers: Optional[int] = None,
) -> tuple[int, Path]:
    """多线程下载指定存根目录中的所有 .pyi 文件。"""
    if max_workers is None:
        max_workers = _get_download_threads()

    url = f"{SOURCE}/contents/stubs/{stub_dir}"
    resp = _request_with_retry(url)
    items = resp.json()

    out_path = Path(output_dir) / stub_dir
    out_path.mkdir(parents=True, exist_ok=True)

    pyi_files = [
        item for item in items
        if item["type"] == "file" and item["name"].endswith(".pyi")
    ]

    if not pyi_files:
        return 0, out_path

    log.info("下载存根: %s (%d 个文件, %d 线程)", stub_dir, len(pyi_files), max_workers)

    def _download_one(item: dict) -> str:
        file_resp = _request_with_retry(item["download_url"])
        (out_path / item["name"]).write_text(file_resp.text, encoding="utf-8")
        return item["name"]

    downloaded = 0
    failed = 0
    total = len(pyi_files)

    bar: tqdm | None = None
    if tqdm:
        bar = tqdm(total=total, desc="下载中", unit="file")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_download_one, item): item for item in pyi_files
        }
        for future in as_completed(futures):
            try:
                future.result()
                downloaded += 1
            except Exception as e:
                failed += 1
                log.warning("下载失败: %s (%s)", futures[future]["name"], e)
            if bar:
                bar.update(1)

    if bar:
        bar.close()

    if not tqdm:
        total_str = f"{downloaded}/{total}"
        if failed:
            total_str += f" ({failed} 失败)"
        log.info("下载完成: %s", total_str)

    out_path = Path(f"./.stubs/{out_path}")
    log.debug("存根保存到: %s", out_path)
    return downloaded, out_path


def create_vscode_config(
    stub_path: Path, hardware: str, version: str,
) -> Path:
    """创建 .vscode/settings.json，配置 Pylance 指向下载的存根。"""
    vscode_dir = Path(VSCODE_DIR)
    vscode_dir.mkdir(parents=True, exist_ok=True)

    settings_file = vscode_dir / VSCODE_SETTINGS

    config = {}
    if settings_file.exists():
        try:
            config = json.loads(settings_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("现有 .vscode/settings.json 格式错误，将覆盖")
            config = {}

    rel_stub_path = stub_path.as_posix()

    existing_paths = config.get("python.analysis.extraPaths", [])
    if rel_stub_path not in existing_paths:
        existing_paths.append(rel_stub_path)
    config["python.analysis.extraPaths"] = existing_paths

    config.setdefault("python.languageServer", "Pylance")
    config.setdefault("python.analysis.typeCheckingMode", "basic")
    config.setdefault("python.analysis.stubPath", stub_path.parent.as_posix())

    settings_file.write_text(
        json.dumps(config, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    log.debug("VS Code 配置已更新: %s (硬件=%s, 版本=%s)", settings_file, hardware, version)
    return settings_file

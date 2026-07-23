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

from ..utils.config import _load_config
from ..utils.log import get_logger

log = get_logger(__name__)

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

SOURCE = "https://api.github.com/repos/josverl/micropython-stubs"
VSCODE_DIR = ".vscode"
VSCODE_SETTINGS = "settings.json"
PROJECT_CONFIG = ".pyrite_config.json"
STUB_CACHE_ROOT = Path("~/.pyrcli/stubs")
PYRITE_STUB_DIR = "pyrite"

def _get_download_threads() -> int:
    """从 .pyrite_config.json 读取下载线程数。"""
    return _load_config().download_threads


def version_to_dir(v: str) -> str:
    """将 '1.20.0' 转换为 'v1_20_0'。"""
    return "v" + v.replace(".", "_")


def get_stub_cache_root() -> Path:
    """返回 CLI 统一管理的 MicroPython stubs 缓存根目录。"""
    return STUB_CACHE_ROOT.expanduser().resolve()


def ensure_feature_stub(cache_root: Optional[Path] = None) -> Path:
    """将 Pyrite 条件编译辅助 .pyi 放入 CLI 管理的 stubs 目录。"""
    root = get_stub_cache_root() if cache_root is None else cache_root.expanduser().resolve()
    out_dir = root / PYRITE_STUB_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    src = Path(__file__).with_name("feature_stub.pyi")
    dst = out_dir / "feature_stub.pyi"
    content = src.read_text(encoding="utf-8")
    if not dst.exists() or dst.read_text(encoding="utf-8") != content:
        dst.write_text(content, encoding="utf-8")
    return out_dir


def warn_legacy_project_stubs(project_root: Path = Path(".")) -> None:
    """提示旧项目内 .stubs 已不再由新流程使用。"""
    legacy = project_root / ".stubs"
    if legacy.exists():
        log.warning(
            "检测到项目内旧 stubs 目录 %s；新版本使用 %s，可确认无自定义内容后手动删除",
            legacy,
            get_stub_cache_root(),
        )


def write_project_stub_config(
    *,
    hardware: str,
    version: str,
    variant: Optional[str],
    stub_dir: str,
    stub_path: Path,
    project_root: Path = Path("."),
) -> Path:
    """在项目配置中记录当前 stubs 选择结果，保留已有配置项。"""
    config_file = project_root / PROJECT_CONFIG
    data: dict = {}
    if config_file.exists():
        try:
            loaded = json.loads(config_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
            else:
                log.warning("%s 不是 JSON 对象，将覆盖", config_file)
        except json.JSONDecodeError:
            log.warning("%s 格式错误，将覆盖", config_file)
        except OSError as e:
            log.warning("读取 %s 失败: %s，将覆盖", config_file, e)

    data["stubs"] = {
        "hardware": hardware,
        "version": version,
        "variant": variant,
        "stub_dir": stub_dir,
        "path": stub_path.expanduser().resolve().as_posix(),
    }
    config_file.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return config_file


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


def download_stubs(
    stub_dir: str,
    output_dir: str,
    max_workers: Optional[int] = None,
) -> tuple[int, Path]:
    """多线程下载指定存根目录中的所有 .pyi 文件。"""
    if max_workers is None:
        max_workers = _get_download_threads()

    output_root = Path(output_dir).expanduser() if output_dir else get_stub_cache_root()
    out_path = output_root / stub_dir
    cached_files = list(out_path.glob("*.pyi")) if out_path.exists() else []
    if cached_files:
        log.info("使用已缓存存根: %s (%d 个 .pyi 文件)", out_path, len(cached_files))
        return len(cached_files), out_path

    url = f"{SOURCE}/contents/stubs/{stub_dir}"
    resp = _request_with_retry(url)
    items = resp.json()

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

    log.debug("存根保存到: %s", out_path)
    return downloaded, out_path


def create_vscode_config(
    stub_path: Path,
    hardware: str,
    version: str,
    extra_paths: Optional[list[Path]] = None,
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

    abs_stub_path = stub_path.expanduser().resolve()
    configured_paths = [abs_stub_path.as_posix()]
    if extra_paths:
        configured_paths.extend(p.expanduser().resolve().as_posix() for p in extra_paths)

    existing_paths = config.get("python.analysis.extraPaths", [])
    if not isinstance(existing_paths, list):
        existing_paths = []
    for path in configured_paths:
        if path not in existing_paths:
            existing_paths.append(path)
    config["python.analysis.extraPaths"] = existing_paths

    config.setdefault("python.languageServer", "Pylance")
    config.setdefault("python.analysis.typeCheckingMode", "basic")
    config.setdefault("python.analysis.stubPath", abs_stub_path.parent.as_posix())

    settings_file.write_text(
        json.dumps(config, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    log.debug("VS Code 配置已更新: %s (硬件=%s, 版本=%s)", settings_file, hardware, version)
    return settings_file

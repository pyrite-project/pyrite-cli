import json
import sys
import time
from pathlib import Path
import requests
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None  # tqdm 不可用时使用简单回退方案

API_BASE = "https://api.github.com/repos/josverl/micropython-stubs"
VSCODE_DIR = ".vscode"
VSCODE_SETTINGS = "settings.json"


def version_to_dir(v: str) -> str:
    """将 '1.20.0' 转换为 'v1_20_0'。"""
    return "v" + v.replace(".", "_")


def _request_with_retry(url: str, max_retries: int = 3, **kwargs) -> requests.Response:
    """带重试的 HTTP GET 请求，仅在可恢复的错误时重试。

    重试策略：
    - 连接错误 / 超时：重试（指数退避 1s, 2s, 4s）
    - HTTP 5xx：重试
    - HTTP 403（API 速率限制）：立即退出
    - 其他 HTTP 错误：直接抛出，不重试

    Returns:
        requests.Response

    Raises:
        SystemExit: 遇到 GitHub API 速率限制
        requests.RequestException: 重试耗尽后仍然失败
    """
    kwargs.setdefault("timeout", 30)
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, **kwargs)
            if resp.status_code == 403:
                print("错误：GitHub API 速率限制已超，请稍后重试。")
                sys.exit(1)
            if resp.status_code >= 500:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    print(f"服务器错误 ({resp.status_code})，{wait} 秒后重试"
                          f"（第 {attempt+1}/{max_retries} 次）...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"网络错误: {e}，{wait} 秒后重试"
                      f"（第 {attempt+1}/{max_retries} 次）...")
                time.sleep(wait)
                continue
            raise
    raise last_exc  # type: ignore[union-attr]


def list_stub_dirs() -> list[str]:
    """从仓库列出所有存根目录。"""
    url = f"{API_BASE}/contents/stubs"
    resp = _request_with_retry(url)
    return [item["name"] for item in resp.json() if item["type"] == "dir"]


def find_stub_dir(dirs: list[str], hardware: str, version: str,
                  variant: str | None = None) -> str | None:
    """查找最匹配的存根目录名。

    Args:
        dirs: 可用存根目录名列表
        hardware: 硬件类型（如 esp32、rp2）
        version: 固件版本（如 1.20.0）
        variant: 具体硬件变体（如 ESP32_GENERIC、PICO_W），可选
    """
    vdir = version_to_dir(version)
    if variant:
        base = f"micropython-{vdir}-{hardware}-{variant}"
    else:
        base = f"micropython-{vdir}-{hardware}"

    if base in dirs:
        return base

    # 尝试 merged 变体
    merged = f"{base}-merged"
    if merged in dirs:
        return merged

    # 模糊匹配：以基础模式开头的任意目录
    matches = sorted(d for d in dirs if d.startswith(base))
    if matches:
        return matches[0]

    return None


def list_available(dirs: list[str], hardware: str) -> None:
    """显示指定硬件的可用存根。"""
    matches = [d for d in dirs if f"-{hardware}" in d or d.endswith(hardware)]
    if matches:
        print(f"\n匹配 '{hardware}' 的可用存根：")
        for m in sorted(matches):
            print(f"  {m}")
    else:
        hw_types = get_hardware_types(dirs)
        print(f"\n未找到 '{hardware}' 的存根。")
        print(f"可用的硬件类型：{', '.join(sorted(hw_types))}")


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
    print(f"\n可用的 MicroPython 硬件类型（共 {len(hw_types)} 个）：")
    for hw in hw_types:
        print(f"  {hw}")


def download_stubs(stub_dir: str, output_dir: str) -> tuple[int, Path]:
    """下载指定存根目录中的所有 .pyi 文件。"""
    url = f"{API_BASE}/contents/stubs/{stub_dir}"
    resp = _request_with_retry(url)
    items = resp.json()

    out_path = Path(output_dir) / stub_dir
    out_path.mkdir(parents=True, exist_ok=True)

    # 筛选出 .pyi 文件
    pyi_files = [
        item for item in items
        if item["type"] == "file" and item["name"].endswith(".pyi")
    ]

    downloaded = 0
    file_iter = tqdm(pyi_files, desc="下载中", unit="file") if tqdm else pyi_files

    for item in file_iter:
        file_resp = _request_with_retry(item["download_url"])
        (out_path / item["name"]).write_text(file_resp.text, encoding="utf-8")
        downloaded += 1
        if not tqdm:
            print(f"  [{downloaded}/{len(pyi_files)}] {item['name']}")

    return downloaded, out_path

def create_vscode_config(stub_path: Path, hardware: str, version: str) -> Path:
    """创建 .vscode/settings.json，配置 Pylance 指向下载的存根。"""
    vscode_dir = Path(VSCODE_DIR)
    vscode_dir.mkdir(parents=True, exist_ok=True)

    settings_file = vscode_dir / VSCODE_SETTINGS

    config = {}
    if settings_file.exists():
        try:
            config = json.loads(settings_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("警告：现有 .vscode/settings.json 格式错误，将覆盖。")
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

    return settings_file

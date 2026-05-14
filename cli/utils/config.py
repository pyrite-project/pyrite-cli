import json
import os
from pathlib import Path
from typing import Dict, List

try:
    import tomllib  # type: ignore
except ImportError:
    import tomli as tomllib  # type: ignore

from .types import PyriteConfig

CONFIG_FILE = ".pyrite_config.json"
DEFAULT_CHUNK_SIZE = 4096
HASH_CONFIG_FILE = "pyrite_file_config.json"
_HASH_VERSION = 1

_DEFAULT_BOARD_TAGS: Dict[str, List[str]] = {
    "ESP32":  ["ESP32", "wifi"],
    "ESP8266": ["ESP8266"],
    "RP2040": ["RP2040"],
    "PICO":   ["RP2040"],
    "STM32":  ["STM32"],
}


def _load_config() -> PyriteConfig:
    """从当前或上级目录加载配置文件，未找到则使用默认值。"""
    cfg = PyriteConfig(board_tags=dict(_DEFAULT_BOARD_TAGS))
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        p = parent / CONFIG_FILE
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data.get("chunk_size"), int) and data["chunk_size"] > 0:
                    cfg.chunk_size = data["chunk_size"]
                t = data.get("download_threads", 4)
                if isinstance(t, int) and t > 0:
                    cfg.download_threads = min(t, 12)
                if isinstance(data.get("auto_compile"), bool):
                    cfg.auto_compile = data["auto_compile"]
                v = data.get("verify", "size")
                if v in ("off", "size", "crc32"):
                    cfg.verify = v
                r = data.get("max_retries", 2)
                if isinstance(r, int) and r >= 0:
                    cfg.max_retries = r
            except (json.JSONDecodeError, OSError):
                pass
            break
    for parent in [cwd] + list(cwd.parents):
        p = parent / "pyproject.toml"
        if p.exists():
            try:
                data = tomllib.loads(p.read_text(encoding="utf-8"))
                bt = data.get("tool", {}).get("pyrite", {}).get("board_tags", {})
                cfg.board_tags.update({k.upper(): v for k, v in bt.items()})
            except Exception:
                pass
            break
    return cfg


def create_default_config() -> str:
    """在工作目录创建默认配置文件。"""
    cfg_path = Path.cwd() / CONFIG_FILE
    cfg_path.write_text(
        json.dumps({
            "chunk_size": DEFAULT_CHUNK_SIZE,
            "download_threads": 4,
            "auto_compile": True,
            "verify": "size",
            "max_retries": 2,
        }, indent=2),
        encoding="utf-8",
    )
    print(f"默认配置文件已创建: {cfg_path}")
    print(f"  chunk_size = {DEFAULT_CHUNK_SIZE} 字节（修改后需重启本工具）")
    print("  download_threads = 4（存根下载线程数，范围 1~12）")
    print("  auto_compile = true（自动编译 .py -> .mpy，设为 false 可关闭）")
    print('  verify = "size"（校验模式：off=不校验, size=文件大小, crc32=文件大小+CRC32）')
    print("  max_retries = 2（校验失败时最大重试次数，设为 0 关闭重试）")
    return str(cfg_path)

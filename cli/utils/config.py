"""
配置加载模块 — 从 .pyrite_config.json 和 pyproject.toml 读取配置。

从当前目录向上逐级搜索 .pyrite_config.json，并与 pyproject.toml 的
``[tool.pyrite.board_tags]`` 合并。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

from .log import get_logger
from .types import PyriteConfig

log = get_logger(__name__)

CONFIG_FILE = ".pyrite_config.json"
DEFAULT_CHUNK_SIZE = 4096
DEFAULT_BAUDRATE = 921600
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
            log.trace("加载配置文件: %s", p)
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
                delta = data.get("delta_flash", "auto")
                if delta in ("off", "auto", "on"):
                    cfg.delta_flash = delta
                delta_min = data.get("delta_min_size", 10240)
                if isinstance(delta_min, int) and delta_min >= 0:
                    cfg.delta_min_size = delta_min
                r = data.get("max_retries", 2)
                if isinstance(r, int) and r >= 0:
                    cfg.max_retries = r
                b = data.get("baudrate", 0)
                if isinstance(b, int) and b > 0:
                    cfg.baudrate = b
                # Apply profile overrides
                selected = data.get("profile", "")
                if selected and isinstance(data.get("profiles"), dict):
                    prof = data["profiles"].get(selected, {})
                    if isinstance(prof.get("baudrate"), int) and prof["baudrate"] > 0:
                        cfg.baudrate = prof["baudrate"]
                    if isinstance(prof.get("chunk_size"), int) and prof["chunk_size"] > 0:
                        cfg.chunk_size = prof["chunk_size"]
                    if isinstance(prof.get("verify"), str) and prof["verify"] in ("off", "size", "crc32"):
                        cfg.verify = prof["verify"]
                    if isinstance(prof.get("delta_flash"), str) and prof["delta_flash"] in ("off", "auto", "on"):
                        cfg.delta_flash = prof["delta_flash"]
                    if isinstance(prof.get("delta_min_size"), int) and prof["delta_min_size"] >= 0:
                        cfg.delta_min_size = prof["delta_min_size"]
                    if isinstance(prof.get("download_threads"), int) and prof["download_threads"] > 0:
                        cfg.download_threads = min(prof["download_threads"], 12)
                    if isinstance(prof.get("timeout"), int) and prof["timeout"] > 0:
                        cfg.timeout = prof["timeout"]
            except (json.JSONDecodeError, OSError):
                pass
            break

    for parent in [cwd] + list(cwd.parents):
        p = parent / "pyproject.toml"
        if p.exists():
            log.trace("加载 pyproject.toml board_tags: %s", p)
            try:
                data = tomllib.loads(p.read_text(encoding="utf-8"))
                bt = data.get("tool", {}).get("pyrite", {}).get("board_tags", {})
                cfg.board_tags.update({k.upper(): v for k, v in bt.items()})
            except Exception:
                pass
            break

    log.debug("配置: chunk_size=%d, verify=%s, auto_compile=%s",
              cfg.chunk_size, cfg.verify, cfg.auto_compile)
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
            "delta_flash": "auto",
            "delta_min_size": 10240,
            "max_retries": 2,
            "baudrate": DEFAULT_BAUDRATE,
        }, indent=2),
        encoding="utf-8",
    )
    log.info("默认配置文件已创建: %s", cfg_path)
    print(f"  chunk_size = {DEFAULT_CHUNK_SIZE} 字节（修改后需重启本工具）")
    print("  download_threads = 4（存根下载线程数，范围 1~12）")
    print("  auto_compile = true（自动编译 .py -> .mpy，设为 false 可关闭）")
    print('  verify = "size"（校验模式：off=不校验, size=文件大小, crc32=文件大小+CRC32）')
    print("  max_retries = 2（校验失败时最大重试次数，设为 0 关闭重试）")
    print(f"  baudrate = {DEFAULT_BAUDRATE}（默认串口波特率，可按板子稳定性调整）")
    return str(cfg_path)

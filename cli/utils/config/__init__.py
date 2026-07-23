"""
配置加载模块 — 从 .pyrite_config.json 和 pyproject.toml 读取配置。

从当前目录向上逐级搜索 .pyrite_config.json，并与 pyproject.toml 的
``[tool.pyrite.board_tags]`` 合并。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

from ..log import get_logger
from .types import PyriteConfig

log = get_logger(__name__)

CONFIG_FILE = ".pyrite_config.json"
DEFAULT_CHUNK_SIZE = 4096
DEFAULT_BAUDRATE = 921600
DEFAULT_TIMEOUT = 10
HASH_CONFIG_FILE = "pyrite_file_config.json"
_HASH_VERSION = 1
_WARNED_LEGACY_PROFILE_FILES: set[Path] = set()

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
                if not isinstance(data, dict):
                    log.warning("配置文件顶层必须是 JSON 对象, 已忽略: %s", p)
                    break
                if type(data.get("chunk_size")) is int and data["chunk_size"] > 0:
                    cfg.chunk_size = data["chunk_size"]
                t = data.get("download_threads", 4)
                if type(t) is int and t > 0:
                    cfg.download_threads = min(t, 12)
                if isinstance(data.get("auto_compile"), bool):
                    cfg.auto_compile = data["auto_compile"]
                v = data.get("verify", "size")
                if v in ("off", "size", "crc32"):
                    cfg.verify = v
                delta = data.get("delta_flash", "auto")
                if delta in ("off", "auto", "on"):
                    cfg.delta_flash = delta
                precheck = data.get("precheck", "basic")
                if precheck in ("off", "basic", "strict"):
                    cfg.precheck = precheck
                precheck_compat = data.get("precheck_compat", "warn")
                if precheck_compat in ("warn", "error", "off"):
                    cfg.precheck_compat = precheck_compat
                precheck_mp_version = data.get("precheck_mp_version", "")
                if isinstance(precheck_mp_version, str):
                    cfg.precheck_mp_version = precheck_mp_version.strip()
                r = data.get("max_retries", 2)
                if type(r) is int and r >= 0:
                    cfg.max_retries = r
                b = data.get("baudrate", 0)
                if type(b) is int and b > 0:
                    cfg.baudrate = b
                t = data.get("timeout")
                if type(t) is int and t > 0:
                    cfg.timeout = t
                legacy_profile_keys = {"profile", "profiles"}.intersection(data)
                warning_path = p.resolve()
                if (
                    legacy_profile_keys
                    and warning_path not in _WARNED_LEGACY_PROFILE_FILES
                ):
                    log.warning(
                        "配置文件包含已废弃的 profile/profiles 配置, 已忽略: %s",
                        ", ".join(sorted(legacy_profile_keys)),
                    )
                    _WARNED_LEGACY_PROFILE_FILES.add(warning_path)
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


def resolve_connection_settings(
    baudrate: int | None,
    timeout: int | None,
    config: PyriteConfig | None = None,
) -> tuple[int, int]:
    """Resolve serial settings from explicit values, config, and defaults."""
    cfg = config if config is not None else PyriteConfig()

    resolved_baudrate = (
        baudrate
        if type(baudrate) is int and baudrate > 0
        else cfg.baudrate
        if type(cfg.baudrate) is int and cfg.baudrate > 0
        else DEFAULT_BAUDRATE
    )
    resolved_timeout = (
        timeout
        if type(timeout) is int and timeout > 0
        else cfg.timeout
        if type(cfg.timeout) is int and cfg.timeout > 0
        else DEFAULT_TIMEOUT
    )
    return resolved_baudrate, resolved_timeout


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
            "precheck": "basic",
            "precheck_compat": "warn",
            "precheck_mp_version": "",
            "max_retries": 2,
            "baudrate": DEFAULT_BAUDRATE,
            "timeout": DEFAULT_TIMEOUT,
        }, indent=2),
        encoding="utf-8",
    )
    log.info("默认配置文件已创建: %s", cfg_path)
    print(f"  chunk_size = {DEFAULT_CHUNK_SIZE} 字节（修改后需重启本工具）")
    print("  download_threads = 4（存根下载线程数，范围 1~12）")
    print("  auto_compile = true（自动编译 .py -> .mpy，设为 false 可关闭）")
    print('  verify = "size"（校验模式：off=不校验, size=文件大小, crc32=文件大小+CRC32）')
    print('  precheck = "basic"（刷入前代码预检查：off/basic/strict）')
    print('  precheck_compat = "warn"（strict 兼容性问题：warn/error/off）')
    print('  precheck_mp_version = ""（可选目标 MicroPython 固件版本，如 1.20.0）')
    print("  max_retries = 2（校验失败时最大重试次数，设为 0 关闭重试）")
    print(f"  baudrate = {DEFAULT_BAUDRATE}（默认串口波特率，可按板子稳定性调整）")
    print(f"  timeout = {DEFAULT_TIMEOUT}（串口连接与读写超时秒数）")
    return str(cfg_path)

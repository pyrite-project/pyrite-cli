"""
plugin.json 清单加载与验证
限制 eval/exec/open/__import__
资源限制（超时控制、递归深度上限）
"""

from __future__ import annotations

import builtins
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .config import (
    DEFAULT_RECURSION_LIMIT,
    DEFAULT_TIMEOUT_SEC,
    SandboxConfig,
    SandboxError,
)

# ═══════════════════════════════════════════════════════════════════════
# 第1层：插件清单加载
# ═══════════════════════════════════════════════════════════════════════

_VALID_MODES = frozenset({"strict", "standard", "permissive"})
_VALID_CONFIG_KEYS = frozenset({
    "mode", "network", "filesystem_read", "filesystem_write",
    "subprocess", "timeout_sec", "recursion_limit",
    "allowed_imports", "blocked_imports", "allow_builtins",
})
_MIN_TIMEOUT = 1
_MAX_TIMEOUT = 300
_MIN_RECURSION = 100
_MAX_RECURSION = 10000


def load_plugin_manifest(plugin_dir: str) -> SandboxConfig:
    """从插件目录加载 plugin.json 并返回 SandboxConfig。

    若 plugin.json 不存在，返回默认 standard 配置。
    若存在但内容无效，打印警告并使用默认值。
    """
    manifest_path = os.path.join(plugin_dir, "plugin.json")

    if not os.path.isfile(manifest_path):
        return SandboxConfig()

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        import typer
        typer.secho(
            f"  [WARN] plugin.json 解析失败: {e}，使用默认配置",
            fg=typer.colors.YELLOW,
        )
        return SandboxConfig()

    if not isinstance(raw, dict):
        import typer
        typer.secho(
            "  [WARN] plugin.json 格式错误（应为 JSON 对象），使用默认配置",
            fg=typer.colors.YELLOW,
        )
        return SandboxConfig()

    # 过滤未知键
    unknown_keys = set(raw.keys()) - _VALID_CONFIG_KEYS
    if unknown_keys:
        import typer
        typer.secho(
            f"  [WARN] plugin.json 包含未知字段: {', '.join(sorted(unknown_keys))}",
            fg=typer.colors.YELLOW,
        )

    config = SandboxConfig()

    # mode
    if "mode" in raw and isinstance(raw["mode"], str):
        if raw["mode"] in _VALID_MODES:
            config.mode = raw["mode"]
        else:
            import typer
            typer.secho(
                f"  [WARN] plugin.json mode='{raw['mode']}' 无效，使用 'standard'",
                fg=typer.colors.YELLOW,
            )

    # 布尔字段
    for key in ("network", "filesystem_write", "subprocess"):
        if key in raw and isinstance(raw[key], bool):
            setattr(config, key, raw[key])

    # 列表字段
    for key in ("allowed_imports", "blocked_imports", "allow_builtins"):
        if key in raw and isinstance(raw[key], list):
            validated = []
            for item in raw[key]:
                if isinstance(item, str):
                    validated.append(item)
            setattr(config, key, validated)

    # filesystem_read — 需校验路径遍历
    if "filesystem_read" in raw and isinstance(raw["filesystem_read"], list):
        validated_paths = []
        for p in raw["filesystem_read"]:
            if isinstance(p, str):
                if ".." in Path(p).parts:
                    import typer
                    typer.secho(
                        f"  [WARN] plugin.json filesystem_read 路径包含 '..'，已拒绝: {p!r}",
                        fg=typer.colors.YELLOW,
                    )
                    continue
                validated_paths.append(p)
        config.filesystem_read = validated_paths

    # timeout_sec
    if "timeout_sec" in raw and isinstance(raw["timeout_sec"], (int, float)):
        config.timeout_sec = max(_MIN_TIMEOUT, min(_MAX_TIMEOUT, int(raw["timeout_sec"])))

    # recursion_limit
    if "recursion_limit" in raw and isinstance(raw["recursion_limit"], int):
        config.recursion_limit = max(_MIN_RECURSION, min(_MAX_RECURSION, raw["recursion_limit"]))

    return config


# ═══════════════════════════════════════════════════════════════════════
# 第4层：内置函数守卫
# ═══════════════════════════════════════════════════════════════════════

# 始终禁用的内置函数（注意：__import__ 不在此列表，
# 因为导入控制由第3层 sys.meta_path 导入拦截器负责，
# 禁用 __import__ 会导致所有 import 语句（包括合法的）全部失败）
_ALWAYS_BLOCK_BUILTINS = frozenset({
    "eval",
    "exec",
    "compile",
    "breakpoint",
})


def make_sandboxed_builtins(
    config: SandboxConfig, plugin_name: str
) -> dict[str, Any]:
    """创建受限的 __builtins__ 字典。

    复制真实 builtins 并将危险函数替换为受限版本。
    注入 exec_module() 前设置到 module.__builtins__。
    """
    real_builtins_dict = builtins.__dict__
    safe: dict[str, Any] = {}

    for name, value in real_builtins_dict.items():
        if name in _ALWAYS_BLOCK_BUILTINS:
            safe[name] = _make_blocked_builtin(name, plugin_name)
        elif name == "open":
            safe[name] = _make_restricted_open(config, plugin_name)
        else:
            safe[name] = value

    return safe


def _make_blocked_builtin(name: str, plugin_name: str) -> Callable:
    """创建一个调用时抛出 SandboxError 的函数。"""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise SandboxError(
            f"内置函数 '{name}()' 被沙箱禁用",
            plugin_name,
            "builtins_guard",
        )

    # 保留原函数名便于调试
    blocked.__name__ = name
    blocked.__doc__ = f"[沙箱禁用] {name}() is blocked by sandbox"
    return blocked


def _make_restricted_open(
    config: SandboxConfig, plugin_name: str
) -> Callable:
    """创建受限的 open() 函数。

    - 写/追加/读写模式 → 需 filesystem_write: true
    - 读模式 → 若配置了 filesystem_read 白名单，需在路径内
    """
    real_open = builtins.open

    def restricted_open(file, mode="r", *args, **kwargs):  # type: ignore
        mode_str = str(mode)
        is_write = any(c in mode_str for c in ("w", "a", "+", "x"))

        if is_write:
            if not config.filesystem_write:
                raise SandboxError(
                    f"文件写入被禁止: open('{file}', '{mode_str}')",
                    plugin_name,
                    "builtins_guard",
                )

        # 读路径白名单检查
        if config.filesystem_read:
            path_str = str(file)
            # 相对路径跳过检查（无法可靠解析）
            if not os.path.isabs(path_str):
                pass  # 允许相对路径读取
            else:
                allowed = any(
                    path_str.startswith(p) for p in config.filesystem_read
                )
                if not allowed:
                    raise SandboxError(
                        f"文件路径不在允许列表中: '{path_str}'",
                        plugin_name,
                        "builtins_guard",
                    )

        return real_open(file, mode, *args, **kwargs)

    restricted_open.__name__ = "open"
    restricted_open.__doc__ = "[沙箱受限] open() with sandbox restrictions"
    return restricted_open


# ═══════════════════════════════════════════════════════════════════════
# 第5层：资源限制
# ═══════════════════════════════════════════════════════════════════════


@contextmanager
def execution_timeout(seconds: int, plugin_name: str):
    """执行超时控制（协作式，兼容 Windows）。

    通过 sys.settrace() 在每个字节码指令处检查耗时。
    这不是精确的 wall-clock 超时，但对防止无限循环很有效。
    """
    if seconds <= 0:
        yield
        return

    start_time = time.monotonic()
    timed_out = False

    def _trace(frame, event, arg):  # type: ignore
        nonlocal timed_out
        if event == "line":
            elapsed = time.monotonic() - start_time
            if elapsed > seconds:
                timed_out = True
                raise SandboxError(
                    f"插件执行超时（{seconds} 秒）",
                    plugin_name,
                    "resource_limit",
                )
        return _trace

    old_trace = sys.gettrace()
    sys.settrace(_trace)
    try:
        yield
    except SandboxError:
        raise
    except Exception:
        raise
    finally:
        sys.settrace(old_trace)


@contextmanager
def recursion_limit_guard(limit: int):
    """递归深度上限控制。

    临时降低 sys.getrecursionlimit()，退出时恢复原值。
    """
    if limit <= 0:
        yield
        return

    old_limit = sys.getrecursionlimit()
    new_limit = min(old_limit, limit)
    sys.setrecursionlimit(new_limit)
    try:
        yield
    finally:
        sys.setrecursionlimit(old_limit)

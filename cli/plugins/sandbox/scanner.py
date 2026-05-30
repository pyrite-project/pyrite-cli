"""
AST 预扫描器

在 exec_module() 之前用 ast.parse() 扫描插件源码，检测：
- 禁止的 import 语句
- 危险的函数调用（eval, exec 等）
- 危险的属性访问链（os.system 等）
- AST 深度过深

沿用 manifest_loader.py 的 ast.parse() + ast.walk() 模式，但更宽松：
不禁用 FunctionDef/ClassDef/Assign 等节点类型，只检查导入和调用
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional, Set

from .config import (
    ALWAYS_BLOCKED_CALLS,
    ALWAYS_BLOCKED_IMPORTS,
    BLOCKED_ATTR_CHAINS,
    DANGEROUS_CALLS,
    DANGEROUS_IMPORTS,
    MAX_AST_DEPTH,
    MAX_SOURCE_SIZE,
    NETWORK_MODULES,
    SUBPROCESS_MODULES,
    SandboxConfig,
    ScanResult,
)


def _check_depth(node: ast.AST, max_depth: int = MAX_AST_DEPTH) -> None:
    """递归检查 AST 深度，防止恶意深层嵌套耗尽 CPython 递归栈。

    沿用 manifest_loader.py:_check_depth() 的递归模式。
    """

    def _recurse(n: ast.AST, depth: int = 0) -> None:
        if depth > max_depth:
            raise ValueError(
                f"AST 嵌套过深（超过 {max_depth} 层），已拒绝"
            )
        for child in ast.iter_child_nodes(n):
            _recurse(child, depth + 1)

    _recurse(node)


def _resolve_import_name(
    node: ast.Import | ast.ImportFrom, alias: ast.alias
) -> str:
    """解析 import 语句的完整模块名。"""
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if module:
            return f"{module}.{alias.name}" if alias.name != "*" else module
        return alias.name
    else:
        return alias.name


def _get_attr_chain(node: ast.AST) -> Optional[str]:
    """将 ast.Attribute 链还原为字符串，如 'os.system'、'shutil.rmtree'。"""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _get_call_name(node: ast.Call) -> Optional[str]:
    """获取函数调用的名称字符串。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_open_write_mode(node: ast.Call) -> bool:
    """检测 open() 调用是否使用了写模式。"""
    if _get_call_name(node) != "open":
        return False

    # open() 的第二个参数是 mode
    if len(node.args) >= 2:
        mode_arg = node.args[1]
        if isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str):
            mode = mode_arg.value
            if "w" in mode or "a" in mode or "+" in mode:
                return True
        return False

    # 检查关键字参数 mode=
    for kw in node.keywords:
        if kw.arg == "mode":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                mode = kw.value.value
                if "w" in mode or "a" in mode or "+" in mode:
                    return True
    return False


def scan_source(
    source: str, filename: str, config: SandboxConfig
) -> ScanResult:
    """对插件源码执行 AST 预扫描。

    参数：
        source: 插件源码字符串
        filename: 源文件路径（用于错误报告）
        config: 沙箱配置

    返回：
        ScanResult 包含所有发现的问题和通过/失败判定
    """
    result = ScanResult()

    # 大小检查
    if len(source) > MAX_SOURCE_SIZE:
        result.add_error(
            0, 0,
            f"源码过大 ({len(source)} 字符，上限 {MAX_SOURCE_SIZE})",
            "SIZE_EXCEEDED",
        )
        return result

    # 解析 AST
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        result.add_error(
            e.lineno or 0, e.offset or 0,
            f"语法错误: {e.msg}",
            "SYNTAX_ERROR",
        )
        return result

    # 深度检查
    try:
        _check_depth(tree)
    except ValueError as e:
        result.add_error(0, 0, str(e), "DEPTH_EXCEEDED")
        return result

    # 遍历所有 AST 节点
    for node in ast.walk(tree):
        # ── 检查 import 语句 ──
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                module_name = _resolve_import_name(node, alias)

                # 1. 检查用户是否显式允许
                if module_name in config.allowed_import_set:
                    continue
                top = module_name.split(".")[0]
                if top in config.allowed_import_set:
                    continue

                # 2. 始终阻止的导入
                if _is_always_blocked_import(module_name):
                    result.add_error(
                        node.lineno, node.col_offset,
                        f"禁止的导入: {module_name}",
                        "BLOCKED_IMPORT",
                    )
                    continue

                # 2. 用户显式阻止的导入
                if module_name in config.blocked_import_set:
                    result.add_error(
                        node.lineno, node.col_offset,
                        f"用户禁止的导入: {module_name}",
                        "USER_BLOCKED_IMPORT",
                    )
                    continue

                # 3. 网络模块检查
                if _is_network_import(module_name) and not config.network:
                    if config.mode == "strict":
                        result.add_error(
                            node.lineno, node.col_offset,
                            f"网络模块导入被禁止: {module_name}（需 network: true）",
                            "NETWORK_BLOCKED",
                        )
                    elif config.mode == "standard":
                        result.add_warning(
                            node.lineno, node.col_offset,
                            f"网络模块导入需要 network: true: {module_name}",
                            "NETWORK_WARN",
                        )
                        result.dangerous_imports.append(module_name)

                # 4. 子进程模块检查
                if _is_subprocess_import(module_name) and not config.subprocess:
                    if config.mode == "strict":
                        result.add_error(
                            node.lineno, node.col_offset,
                            f"子进程模块导入被禁止: {module_name}（需 subprocess: true）",
                            "SUBPROCESS_BLOCKED",
                        )
                    elif config.mode == "standard":
                        result.add_warning(
                            node.lineno, node.col_offset,
                            f"子进程模块导入需要 subprocess: true: {module_name}",
                            "SUBPROCESS_WARN",
                        )
                        result.dangerous_imports.append(module_name)

                # 5. 危险导入
                if module_name in DANGEROUS_IMPORTS:
                    result.dangerous_imports.append(module_name)
                    if config.mode == "strict":
                        result.add_error(
                            node.lineno, node.col_offset,
                            f"危险模块导入（strict 模式禁止）: {module_name}",
                            "DANGEROUS_IMPORT_STRICT",
                        )
                    elif config.mode == "standard":
                        result.add_warning(
                            node.lineno, node.col_offset,
                            f"可疑导入: {module_name}",
                            "DANGEROUS_IMPORT_WARN",
                        )

        # ── 检查函数调用 ──
        elif isinstance(node, ast.Call):
            call_name = _get_call_name(node)

            if call_name and call_name in ALWAYS_BLOCKED_CALLS:
                result.add_error(
                    node.lineno, node.col_offset,
                    f"禁止的函数调用: {call_name}()",
                    "BLOCKED_CALL",
                )
                result.suspicious_calls.append(call_name)

            elif call_name and call_name in DANGEROUS_CALLS:
                # open() 特殊处理：只阻止写模式
                if call_name == "open" and _is_open_write_mode(node):
                    if not config.filesystem_write:
                        result.add_error(
                            node.lineno, node.col_offset,
                            "文件写入被禁止: open() 写模式（需 filesystem_write: true）",
                            "FILE_WRITE_BLOCKED",
                        )
                elif call_name != "open":
                    if config.mode == "strict":
                        result.add_error(
                            node.lineno, node.col_offset,
                            f"危险函数调用（strict 模式禁止）: {call_name}()",
                            "DANGEROUS_CALL_STRICT",
                        )
                    elif config.mode == "standard":
                        result.add_warning(
                            node.lineno, node.col_offset,
                            f"可疑调用: {call_name}()",
                            "DANGEROUS_CALL_WARN",
                        )

        # ── 检查属性链 ──
        elif isinstance(node, ast.Attribute):
            attr_chain = _get_attr_chain(node)
            if attr_chain and attr_chain in BLOCKED_ATTR_CHAINS:
                result.add_error(
                    node.lineno, node.col_offset,
                    f"禁止的属性访问: {attr_chain}",
                    "BLOCKED_ATTR_CHAIN",
                )
                result.suspicious_calls.append(attr_chain)

    return result


def _is_always_blocked_import(module_name: str) -> bool:
    """检查模块名是否属于始终阻止列表（含子模块）。"""
    top = module_name.split(".")[0]
    if top in ALWAYS_BLOCKED_IMPORTS:
        return True
    # 检查完整模块名前缀匹配
    for blocked in ALWAYS_BLOCKED_IMPORTS:
        if module_name == blocked or module_name.startswith(blocked + "."):
            return True
    return False


def _is_network_import(module_name: str) -> bool:
    """检查模块名是否属于网络相关。"""
    top = module_name.split(".")[0]
    return top in NETWORK_MODULES


def _is_subprocess_import(module_name: str) -> bool:
    """检查模块名是否属于子进程相关。"""
    top = module_name.split(".")[0]
    return top in SUBPROCESS_MODULES

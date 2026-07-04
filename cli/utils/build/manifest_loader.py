"""
Manifest 安全解析器 — 使用 AST（非 exec）解析 manifest.py。

仅允许 ``module()`` 和 ``package()`` 调用，参数必须为字面量。
支持 ``remote``（路径重映射）和 ``features``（标签过滤）关键字参数。
防御机制：最大嵌套深度 15 层、最多 500 条目。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import List, Optional, Set, Tuple

from ..log import get_logger

log = get_logger(__name__)

_MAX_MANIFEST_DEPTH = 15
_MAX_MANIFEST_ENTRIES = 500


@dataclass(frozen=True)
class ManifestEntry:
    """Expanded, included manifest entry."""

    directive: str
    local_path: str
    remote_path: str
    source: str
    features: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ManifestPlan:
    """Structured manifest parse result used by lockfile generation."""

    entries: Tuple[ManifestEntry, ...]
    active_tags: Tuple[str, ...]
    included_features: Tuple[str, ...]
    excluded_features: Tuple[str, ...]


def _has_parent_reference(path: str) -> bool:
    return ".." in path.replace("\\", "/").split("/")


def _has_windows_drive_or_unc(path: str) -> bool:
    windows_path = PureWindowsPath(path)
    return bool(windows_path.drive) or path.startswith(("\\\\", "//"))


def _validate_local_path_literal(path: str, lineno: int) -> None:
    windows_path = PureWindowsPath(path)
    if (
        PurePosixPath(path).is_absolute()
        or bool(windows_path.root)
        or _has_windows_drive_or_unc(path)
    ):
        raise ValueError(
            f"manifest.py line {lineno}: absolute host paths are not allowed "
            f"in filename: {path!r}"
        )
    if _has_parent_reference(path):
        raise ValueError(
            f"manifest.py line {lineno}: path traversal ('..') is not allowed "
            f"in filename: {path!r}"
        )


def _validate_remote_path_literal(path: str, lineno: int) -> None:
    if _has_parent_reference(path):
        raise ValueError(
            f"manifest.py line {lineno}: path traversal ('..') is not allowed "
            f"in remote: {path!r}"
        )
    if _has_windows_drive_or_unc(path):
        raise ValueError(
            f"manifest.py line {lineno}: host-style paths are not allowed "
            f"in remote: {path!r}"
        )


def _ensure_inside_base(path: Path, base: Path, lineno: int, original: str) -> None:
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError(
            f"manifest.py line {lineno}: path must resolve inside base_dir: "
            f"{original!r}"
        ) from exc


def _resolve_local_path(base: Path, filename: str, lineno: int) -> Path:
    _validate_local_path_literal(filename, lineno)
    resolved = (base / filename).resolve()
    _ensure_inside_base(resolved, base, lineno, filename)
    return resolved


def _parse_str(node: ast.AST, what: str) -> str:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        raise ValueError(f"{what} must be a string literal")
    return node.value


def _parse_list_of_str(node: ast.AST, what: str) -> List[str]:
    if not isinstance(node, ast.List):
        raise ValueError(f"{what} must be a list literal")
    result: List[str] = []
    for elt in node.elts:
        if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
            raise ValueError(f"{what} elements must be string literals")
        result.append(elt.value)
    return result


def _parse_call(call: ast.Call, lineno: int) -> Tuple[str, str, dict]:
    """解析 module()/package() 调用，返回 ``(func_name, filename, kwargs)``。"""
    if not isinstance(call.func, ast.Name):
        raise ValueError(
            f"manifest.py line {lineno}: only module() and package() calls are allowed"
        )

    func_name = call.func.id
    if func_name not in ("module", "package"):
        raise ValueError(
            f"manifest.py line {lineno}: unknown directive '{func_name}', "
            "only module() and package() are allowed"
        )

    if len(call.args) < 1:
        raise ValueError(
            f"manifest.py line {lineno}: {func_name}() requires a filename argument"
        )
    if len(call.args) > 1:
        raise ValueError(
            f"manifest.py line {lineno}: {func_name}() accepts only one positional argument"
        )

    filename = _parse_str(call.args[0], f"{func_name}() first argument")
    _validate_local_path_literal(filename, lineno)

    kwargs: dict = {}
    for kw in call.keywords:
        if kw.arg is None:
            raise ValueError(
                f"manifest.py line {kw.lineno}: **kwargs expansion is not supported"
            )
        if kw.arg not in ("remote", "features"):
            raise ValueError(
                f"manifest.py line {kw.lineno}: unsupported parameter '{kw.arg}', "
                "only remote/features are allowed"
            )
        if kw.arg in kwargs:
            raise ValueError(
                f"manifest.py line {kw.lineno}: duplicate parameter '{kw.arg}'"
            )
        kwargs[kw.arg] = kw.value

    parsed_kwargs: dict = {}
    if "remote" in kwargs:
        remote_str = _parse_str(kwargs["remote"], "remote")
        _validate_remote_path_literal(remote_str, kwargs["remote"].lineno)
        parsed_kwargs["remote"] = remote_str
    if "features" in kwargs:
        parsed_kwargs["features"] = _parse_list_of_str(
            kwargs["features"], "features",
        )

    return func_name, filename, parsed_kwargs


def _check_depth(node: ast.AST, depth: int = 0) -> None:
    """递归检查 AST 深度，防止恶意深层嵌套耗尽 CPython 递归栈。"""
    if depth > _MAX_MANIFEST_DEPTH:
        raise ValueError(
            f"manifest.py 嵌套过深（超过 {_MAX_MANIFEST_DEPTH} 层），已拒绝"
        )
    for child in ast.iter_child_nodes(node):
        _check_depth(child, depth + 1)


def _check_unsafe_nodes(tree: ast.Module) -> None:
    """拒绝顶层表达式调用以外的语句。"""
    _check_depth(tree)
    for node in tree.body:
        if isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.ClassDef,
                ast.FunctionDef,
                ast.Assign,
                ast.AugAssign,
                ast.AnnAssign,
                ast.Delete,
                ast.For,
                ast.While,
                ast.If,
                ast.With,
                ast.Try,
                ast.Raise,
            ),
        ):
            raise ValueError(
                f"manifest.py line {node.lineno}: {type(node).__name__} is not allowed, "
                "only module()/package() calls are allowed"
            )


def load_manifest(
    manifest_path: str,
    active_tags: Optional[Set[str]],
    base_dir: str | None = None,
) -> List[Tuple[str, str]]:
    """安全解析 manifest.py，返回 ``[(local_path, remote_path), ...]``。

    使用 AST 而非 exec() 解析，仅接受 module()/package() 字面量调用。
    """
    plan = load_manifest_plan(manifest_path, active_tags, base_dir=base_dir)
    return [(entry.local_path, entry.remote_path) for entry in plan.entries]


def load_manifest_plan(
    manifest_path: str,
    active_tags: Optional[Set[str]],
    base_dir: str | None = None,
) -> ManifestPlan:
    """安全解析 manifest.py，返回结构化解析计划。"""
    base = Path(base_dir or Path(manifest_path).parent).resolve()
    entries: List[ManifestEntry] = []
    included_features: Set[str] = set()
    excluded_features: Set[str] = set()

    source = Path(manifest_path).read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=manifest_path)
    except SyntaxError as e:
        raise ValueError(f"manifest.py syntax error: {e}")

    _check_unsafe_nodes(tree)

    for node in tree.body:
        if not isinstance(node, ast.Expr):
            continue
        if not isinstance(node.value, ast.Call):
            continue

        func_name, filename, kwargs = _parse_call(
            node.value, node.value.lineno,
        )

        features = kwargs.get("features")
        if (
            features is not None
            and active_tags is not None
            and not (set(features) & active_tags)
        ):
            excluded_features.update(features)
            continue
        if features is not None:
            included_features.update(features)

        remote = kwargs.get("remote")
        local_path = _resolve_local_path(base, filename, node.value.lineno)

        if func_name == "module":
            entries.append(ManifestEntry(
                directive=func_name,
                local_path=str(local_path),
                remote_path=str(remote or filename),
                source=filename,
                features=tuple(features or ()),
            ))
        elif func_name == "package":
            for f in sorted(
                path for path in local_path.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix not in {".pyi", ".pyc"}
            ):
                _ensure_inside_base(f.resolve(), base, node.value.lineno, str(f))
                rel = str(f.relative_to(base)).replace("\\", "/")
                if remote:
                    rp = f"{str(remote).rstrip('/')}/{rel}"
                else:
                    rp = rel
                entries.append(ManifestEntry(
                    directive=func_name,
                    local_path=str(f),
                    remote_path=rp,
                    source=filename,
                    features=tuple(features or ()),
                ))
                if len(entries) > _MAX_MANIFEST_ENTRIES:
                    raise ValueError(
                        f"manifest.py: 条目数超过上限 ({_MAX_MANIFEST_ENTRIES})，已拒绝"
                    )

        if len(entries) > _MAX_MANIFEST_ENTRIES:
            raise ValueError(
                f"manifest.py: 条目数超过上限 ({_MAX_MANIFEST_ENTRIES})，已拒绝"
            )

    log.debug("manifest 解析完成: %s (%d 个条目)", manifest_path, len(entries))
    return ManifestPlan(
        entries=tuple(entries),
        active_tags=tuple(sorted(active_tags or set())),
        included_features=tuple(sorted(included_features)),
        excluded_features=tuple(sorted(excluded_features - included_features)),
    )

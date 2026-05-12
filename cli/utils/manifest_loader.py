from pathlib import Path
from typing import List, Set, Tuple
import ast


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


def _parse_call(call: ast.Call, lineno: int):
    """Parse module()/package() call and return (func_name, filename, kwargs)."""
    if not isinstance(call.func, ast.Name):
        raise ValueError(f"manifest.py line {lineno}: only module() and package() calls are allowed")

    func_name = call.func.id
    if func_name not in ("module", "package"):
        raise ValueError(
            f"manifest.py line {lineno}: unknown directive '{func_name}', only module() and package() are allowed"
        )

    if len(call.args) < 1:
        raise ValueError(f"manifest.py line {lineno}: {func_name}() requires a filename argument")
    if len(call.args) > 1:
        raise ValueError(f"manifest.py line {lineno}: {func_name}() accepts only one positional argument")

    filename = _parse_str(call.args[0], f"{func_name}() first argument")
    if ".." in Path(filename).parts:
        raise ValueError(
            f"manifest.py line {lineno}: path traversal ('..') is not allowed in filename: {filename!r}"
        )

    kwargs: dict[str, ast.AST] = {}
    for kw in call.keywords:
        if kw.arg is None:
            raise ValueError(f"manifest.py line {kw.lineno}: **kwargs expansion is not supported")
        if kw.arg not in ("remote", "features"):
            raise ValueError(
                f"manifest.py line {kw.lineno}: unsupported parameter '{kw.arg}', only remote/features are allowed"
            )
        if kw.arg in kwargs:
            raise ValueError(f"manifest.py line {kw.lineno}: duplicate parameter '{kw.arg}'")
        kwargs[kw.arg] = kw.value

    parsed_kwargs: dict[str, object] = {}
    if "remote" in kwargs:
        remote_str = _parse_str(kwargs["remote"], "remote")
        if ".." in Path(remote_str).parts:
            raise ValueError(
                f"manifest.py line {kw.lineno}: path traversal ('..') is not allowed in remote: {remote_str!r}"
            )
        parsed_kwargs["remote"] = remote_str
    if "features" in kwargs:
        parsed_kwargs["features"] = _parse_list_of_str(kwargs["features"], "features")

    return func_name, filename, parsed_kwargs


_MAX_MANIFEST_DEPTH = 15
_MAX_MANIFEST_ENTRIES = 500


def _check_depth(node: ast.AST, depth: int = 0):
    """递归检查 AST 深度，防止恶意深层嵌套耗尽 CPython 递归栈。"""
    if depth > _MAX_MANIFEST_DEPTH:
        raise ValueError(f"manifest.py 嵌套过深（超过 {_MAX_MANIFEST_DEPTH} 层），已拒绝")
    for child in ast.iter_child_nodes(node):
        _check_depth(child, depth + 1)


def _check_unsafe_nodes(tree: ast.Module):
    """Reject statements other than top-level expression calls."""
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
                f"manifest.py line {node.lineno}: {type(node).__name__} is not allowed, only module()/package() calls are allowed"
            )


def load_manifest(manifest_path: str, active_tags: Set[str], base_dir: str = None) -> List[Tuple[str, str]]:
    """Parse manifest.py and return a list of (local_path, remote_path) entries.

    Uses AST parsing instead of exec() for security. Only module() and
    package() calls with literal arguments are accepted.
    """
    base = Path(base_dir or Path(manifest_path).parent)
    entries: List[Tuple[str, str]] = []

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

        func_name, filename, kwargs = _parse_call(node.value, node.value.lineno)

        features = kwargs.get("features")
        if features is not None and not (set(features) & active_tags):
            continue

        remote = kwargs.get("remote")

        if func_name == "module":
            entries.append((str(base / filename), str(remote or filename)))
        elif func_name == "package":
            for f in (base / filename).rglob("*.py"):
                rel = str(f.relative_to(base)).replace("\\", "/")
                if remote:
                    rp = f"{str(remote).rstrip('/')}/{rel}"
                else:
                    rp = rel
                entries.append((str(f), rp))
                if len(entries) > _MAX_MANIFEST_ENTRIES:
                    raise ValueError(
                        f"manifest.py: 条目数超过上限 ({_MAX_MANIFEST_ENTRIES})，已拒绝"
                    )

        if len(entries) > _MAX_MANIFEST_ENTRIES:
            raise ValueError(
                f"manifest.py: 条目数超过上限 ({_MAX_MANIFEST_ENTRIES})，已拒绝"
            )

    return entries

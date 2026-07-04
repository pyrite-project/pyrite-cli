from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import token
import tokenize
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Iterable, Optional, Set, Tuple

from .build import preprocess
from .config import HASH_CONFIG_FILE

PrecheckEntry = Tuple[str, str]
PrecheckMode = str
PrecheckCompat = str


@dataclass(frozen=True)
class PrecheckItem:
    severity: str
    path: str
    line: int
    column: int
    message: str
    remote_path: Optional[str] = None

    def format(self) -> str:
        loc = f"{self.path}:{self.line}:{self.column}"
        remote = f" -> {self.remote_path}" if self.remote_path else ""
        return f"{self.severity}: {loc}{remote}: {self.message}"


@dataclass(frozen=True)
class PrecheckReport:
    items: Tuple[PrecheckItem, ...]

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.items)

    @property
    def warnings(self) -> Tuple[PrecheckItem, ...]:
        return tuple(item for item in self.items if item.severity == "warning")

    @property
    def errors(self) -> Tuple[PrecheckItem, ...]:
        return tuple(item for item in self.items if item.severity == "error")


class PrecheckError(Exception):
    def __init__(self, report: PrecheckReport) -> None:
        self.report = report
        super().__init__("\n".join(item.format() for item in report.errors))


def validate_precheck_mode(mode: Optional[str]) -> str:
    selected = "basic" if mode in (None, "") else str(mode).lower()
    if selected not in {"off", "basic", "strict"}:
        raise ValueError("--check must be one of: off, basic, strict")
    return selected


def validate_precheck_compat(compat: Optional[str]) -> str:
    selected = "warn" if compat in (None, "") else str(compat).lower()
    if selected not in {"warn", "error", "off"}:
        raise ValueError("precheck_compat must be one of: warn, error, off")
    return selected


def _has_parent_reference(path: str) -> bool:
    return ".." in path.replace("\\", "/").split("/")


def _has_windows_drive_or_unc(path: str) -> bool:
    windows_path = PureWindowsPath(path)
    return bool(windows_path.drive) or path.startswith(("\\\\", "//"))


def _remote_path_error(remote_path: str) -> Optional[str]:
    if not remote_path:
        return "remote path is empty"
    if "\\" in remote_path:
        return "remote path must use '/' separators, not backslashes"
    if _has_parent_reference(remote_path):
        return "remote path must not contain '..'"
    if _has_windows_drive_or_unc(remote_path):
        return "remote path must not be a Windows drive or UNC path"
    return None


def _item(
    severity: str,
    path: str,
    message: str,
    line: int = 1,
    column: int = 1,
    remote_path: Optional[str] = None,
) -> PrecheckItem:
    return PrecheckItem(
        severity=severity,
        path=str(path),
        line=max(int(line or 1), 1),
        column=max(int(column or 1), 1),
        message=message,
        remote_path=remote_path,
    )


def _read_python_source(path: str) -> str:
    with tokenize.open(path) as f:
        return f.read()


def _compute_file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1048576)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _syntax_error_item(
    path: str,
    remote_path: str,
    exc: SyntaxError,
    prefix: str = "syntax error",
) -> PrecheckItem:
    return _item(
        "error",
        path,
        f"{prefix}: {exc.msg}",
        exc.lineno or 1,
        exc.offset or 1,
        remote_path,
    )


@dataclass(frozen=True)
class _FeatureRule:
    label: str
    parse_tag: Optional[str]
    runtime_tag: Optional[str]
    semantic_tag: Optional[str]
    required_axis: str = "runtime"
    hard_axis: Optional[str] = None
    runtime_status: str = "supported"
    semantic_status: str = "runtime_substantial"
    config_gated: bool = False
    generic_message: Optional[str] = None


@dataclass(frozen=True)
class _FeatureUse:
    feature: str
    line: int
    column: int
    confidence: str = "high"
    required_axis: Optional[str] = None
    detail: str = ""


@dataclass(frozen=True)
class _FeatureStatus:
    parse: str
    runtime: str
    semantic: str
    config_gated: bool


# Ordered exactly as generated in /tmp/micropython_syntax_version_diff.json.
# Do not sort these tags as semver: preview/rc and historical release order matter.
_MICROPYTHON_TAG_ORDER: Tuple[str, ...] = (
    "v1.0-rc1", "v1.0", "v1.0.1", "v1.1", "v1.1.1", "v1.2",
    "v1.3", "v1.3.1", "v1.3.2", "v1.3.3", "v1.3.4", "v1.3.5",
    "v1.3.6", "v1.3.7", "v1.3.8", "v1.3.9", "v1.3.10", "v1.4",
    "v1.4.1", "v1.4.2", "v1.4.3", "v1.4.4", "v1.4.5", "v1.4.6",
    "v1.5", "v1.5.1", "v1.5.2", "v1.6", "v1.7", "v1.8",
    "v1.8.1", "v1.8.2", "v1.8.3", "v1.8.4", "v1.8.5", "v1.8.6",
    "v1.8.7", "v1.9", "v1.9.1", "v1.9.2", "v1.9.3", "v1.9.4",
    "v1.10", "v1.11", "v1.12", "v1.13", "v1.14", "v1.15",
    "v1.16", "v1.17", "v1.18", "v1.19", "v1.19.1", "v1.20.0",
    "v1.21.0", "v1.22.0-preview", "v1.22.0", "v1.23.0-preview",
    "v1.22.1", "v1.22.2", "v1.23.0", "v1.24.0-preview",
    "v1.24.0", "v1.25.0-preview", "v1.24.1", "v1.25.0",
    "v1.26.0-preview", "v1.26.0", "v1.27.0-preview", "v1.26.1",
    "v1.27.0", "v1.28.0-preview", "v1.28.0", "v1.29.0-preview",
)
_TAG_INDEX = {tag: i for i, tag in enumerate(_MICROPYTHON_TAG_ORDER)}


_FEATURE_RULES: dict[str, _FeatureRule] = {
    "yield_from": _FeatureRule("yield from", "v1.0-rc1", "v1.0-rc1", "v1.0-rc1"),
    "keyword_only_args": _FeatureRule(
        "keyword-only arguments", "v1.0-rc1", "v1.0-rc1", "v1.0-rc1", required_axis="parse",
    ),
    "extended_unpack_assignment": _FeatureRule(
        "extended unpack assignment", "v1.0-rc1", "v1.0-rc1", "v1.0-rc1",
    ),
    "call_star_star_unpack": _FeatureRule(
        "*args/**kwargs call unpacking", "v1.0-rc1", "v1.0-rc1", "v1.0-rc1",
    ),
    "nonlocal_statement": _FeatureRule(
        "nonlocal statement", "v1.0-rc1", "v1.0-rc1", "v1.0-rc1", required_axis="parse",
    ),
    "raise_from": _FeatureRule("raise ... from", "v1.0-rc1", "v1.0-rc1", "v1.0-rc1"),
    "set_literals_comprehensions": _FeatureRule(
        "set literal/comprehension", "v1.0-rc1", "v1.0-rc1", "v1.0-rc1",
    ),
    "comprehensions": _FeatureRule("comprehension", "v1.0-rc1", "v1.0-rc1", "v1.0-rc1"),
    "slice_syntax": _FeatureRule(
        "slice syntax", "v1.0-rc1", "v1.0-rc1", "v1.0-rc1", required_axis="parse",
    ),
    "function_annotations_syntax": _FeatureRule(
        "function annotations", "v1.0-rc1", "v1.0-rc1", "v1.0-rc1",
        required_axis="parse", runtime_status="parse_only", semantic_status="limited",
        generic_message="function annotations may be unsupported or semantically limited on MicroPython firmware",
    ),
    "function_annotations_semantics": _FeatureRule(
        "runtime function annotation access", "v1.0-rc1", "v1.0-rc1", "v1.0-rc1",
        required_axis="semantic", hard_axis=None, runtime_status="parse_only",
        semantic_status="limited",
        generic_message="runtime __annotations__ access is semantically limited on MicroPython",
    ),
    "variable_annotations": _FeatureRule(
        "variable annotations", "v1.13", "v1.13", "v1.13",
        runtime_status="partial", semantic_status="limited",
        generic_message="variable type annotations may be unsupported on older MicroPython firmware",
    ),
    "viper_annotations": _FeatureRule(
        "viper/native annotations", "v1.3.1", "v1.3.1", "v1.3.1",
        required_axis="semantic", hard_axis=None, semantic_status="limited",
    ),
    "assignment_expressions": _FeatureRule(
        "walrus operator :=", "v1.13", "v1.13", "v1.13",
        config_gated=True,
        generic_message="walrus operator may be unsupported on older MicroPython firmware",
    ),
    "assignment_expr_comp_scope_fixed": _FeatureRule(
        "walrus in comprehension scope", "v1.20.0", "v1.20.0", "v1.20.0",
        required_axis="semantic", hard_axis=None, config_gated=True,
    ),
    "async_await": _FeatureRule(
        "async/await", "v1.8", "v1.8", "v1.8", config_gated=True,
        generic_message="async/await may be unsupported on older MicroPython firmware",
    ),
    "async_for_with_guard": _FeatureRule(
        "async for/with outside async function guard", "v1.8", "v1.8", "v1.8",
        required_axis="semantic", hard_axis=None, config_gated=True,
    ),
    "top_level_await_option": _FeatureRule(
        "top-level await compile option", "v1.23.0", "v1.23.0", "v1.23.0",
        required_axis="runtime", hard_axis="runtime", semantic_status="limited",
    ),
    "fstrings": _FeatureRule(
        "f-string", "v1.17", "v1.17", "v1.17",
        runtime_status="partial", semantic_status="runtime_partial", config_gated=True,
        generic_message="f-string may be unsupported on older MicroPython firmware",
    ),
    "fstring_debug_expr": _FeatureRule(
        "f-string debug expression", "v1.17", "v1.17", "v1.17",
        required_axis="semantic", hard_axis=None, semantic_status="runtime_partial",
        config_gated=True,
    ),
    "fstring_nested_brackets": _FeatureRule(
        "f-string expression with nested brackets", "v1.18", "v1.18", "v1.18",
        hard_axis="runtime", semantic_status="runtime_partial", config_gated=True,
    ),
    "fstring_conversion_rs": _FeatureRule(
        "f-string !r/!s conversion", "v1.21.0", "v1.21.0", "v1.21.0",
        hard_axis="runtime", semantic_status="runtime_partial", config_gated=True,
    ),
    "fstring_raw_prefix": _FeatureRule(
        "raw f-string prefix", "v1.24.0", "v1.24.0", "v1.24.0",
        hard_axis="runtime", semantic_status="runtime_partial", config_gated=True,
    ),
    "fstring_adjacent_concat": _FeatureRule(
        "adjacent f-string concatenation", "v1.24.0", "v1.24.0", "v1.24.0",
        hard_axis="runtime", semantic_status="runtime_partial", config_gated=True,
    ),
    "fstring_nested_replacement_fields": _FeatureRule(
        "f-string nested replacement fields", "v1.28.0", "v1.28.0", "v1.28.0",
        hard_axis="runtime", semantic_status="runtime_partial", config_gated=True,
    ),
    "tstrings": _FeatureRule(
        "t-string", "v1.28.0", "v1.28.0", "v1.28.0", config_gated=True,
        generic_message="t-string requires MicroPython v1.28.0 or newer",
    ),
    "matmul_operator": _FeatureRule("matrix multiplication operator @", "v1.12", "v1.12", "v1.12"),
    "dict_union": _FeatureRule("dict union operator |", "v1.0-rc1", "v1.20.0", "v1.20.0"),
    "numeric_literal_underscores": _FeatureRule(
        "numeric literal underscores", "v1.10", "v1.10", "v1.10", required_axis="parse",
    ),
    "positional_only_params": _FeatureRule(
        "positional-only parameters", None, None, None, required_axis="parse", hard_axis="parse",
        generic_message="positional-only parameters are not supported by current MicroPython releases",
    ),
    "match_case": _FeatureRule(
        "match/case", None, None, None, required_axis="parse", hard_axis="parse",
        generic_message="match/case is not supported by current MicroPython releases",
    ),
    "except_star_exception_groups": _FeatureRule(
        "except* / ExceptionGroup", None, None, None, required_axis="parse", hard_axis="parse",
        generic_message="except* is not supported by current MicroPython releases",
    ),
    "type_params_pep695": _FeatureRule(
        "PEP 695 type parameters", None, None, None, required_axis="parse", hard_axis="parse",
        generic_message="PEP 695 type parameters are not supported by current MicroPython releases",
    ),
}


_VERSION_TOKEN_RE = re.compile(r"v?\d+(?:\.\d+)*(?:-(?:preview|rc\d+))?", re.IGNORECASE)


def _release_parts(tag: str) -> Optional[tuple[int, ...]]:
    match = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", tag)
    if not match:
        return None
    parts = [int(part) for part in match.groups() if part is not None]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _minor_floor_tag(tag: str) -> Optional[str]:
    target = _release_parts(tag)
    if target is None or len(target) < 2:
        return None
    best: Optional[str] = None
    best_parts: Optional[tuple[int, ...]] = None
    for known in _MICROPYTHON_TAG_ORDER:
        parts = _release_parts(known)
        if parts is None or parts[:2] != target[:2] or parts > target:
            continue
        if best_parts is None or parts > best_parts:
            best = known
            best_parts = parts
    return best


def normalize_micropython_version(version: Optional[str]) -> Optional[str]:
    """Return the generated MicroPython tag for a user/device version string."""
    if version in (None, ""):
        return None
    text = str(version).strip()
    if not text:
        return None
    candidates = [text]
    candidates.extend(match.group(0) for match in _VERSION_TOKEN_RE.finditer(text))
    for candidate in candidates:
        cleaned = candidate.strip().lower()
        if not cleaned:
            continue
        if not cleaned.startswith("v"):
            cleaned = "v" + cleaned
        exact = cleaned
        if exact in _TAG_INDEX:
            return exact
        if re.fullmatch(r"v\d+\.\d+", exact):
            patch_zero = exact + ".0"
            if patch_zero in _TAG_INDEX:
                return patch_zero
        if re.fullmatch(r"v\d+\.\d+\.0", exact):
            minor_tag = exact[:-2]
            if minor_tag in _TAG_INDEX:
                return minor_tag
        floor_tag = _minor_floor_tag(exact)
        if floor_tag is not None:
            return floor_tag
    raise ValueError(
        "unknown MicroPython version for precheck: "
        f"{version!r}; known range is {_MICROPYTHON_TAG_ORDER[0]}..{_MICROPYTHON_TAG_ORDER[-1]}"
    )


def _tag_at_least(target_tag: str, minimum_tag: Optional[str]) -> bool:
    return minimum_tag is not None and _TAG_INDEX[target_tag] >= _TAG_INDEX[minimum_tag]


def _feature_status(feature: str, target_tag: str) -> _FeatureStatus:
    rule = _FEATURE_RULES[feature]
    parse_status = "supported" if _tag_at_least(target_tag, rule.parse_tag) else "absent"
    runtime_status = rule.runtime_status if _tag_at_least(target_tag, rule.runtime_tag) else "absent"
    semantic_status = rule.semantic_status if _tag_at_least(target_tag, rule.semantic_tag) else "unsupported"
    if feature == "assignment_expressions" and _tag_at_least(target_tag, "v1.13") and not _tag_at_least(target_tag, "v1.20.0"):
        semantic_status = "runtime_partial"
    return _FeatureStatus(
        parse=parse_status,
        runtime=runtime_status,
        semantic=semantic_status,
        config_gated=rule.config_gated and _tag_at_least(target_tag, rule.parse_tag),
    )


def _node_location(node: ast.AST) -> tuple[int, int]:
    return getattr(node, "lineno", 1), getattr(node, "col_offset", 0) + 1


def _is_dict_constructor(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict"


def _is_dict_annotation(node: Optional[ast.AST]) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id in {"dict", "Dict", "Mapping", "MutableMapping"}
    if isinstance(node, ast.Subscript):
        return _is_dict_annotation(node.value)
    if isinstance(node, ast.Attribute):
        return node.attr in {"Dict", "Mapping", "MutableMapping"}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value in {"dict", "Dict", "Mapping", "MutableMapping"}
    return False


def _is_dict_like_expr(node: ast.AST, dict_names: Set[str]) -> bool:
    if isinstance(node, ast.Dict) or _is_dict_constructor(node):
        return True
    if isinstance(node, ast.Name):
        return node.id in dict_names
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return isinstance(node.func.value, ast.Name) and node.func.attr == "copy" and node.func.value.id in dict_names
    return False


def _target_has_starred(node: ast.AST) -> bool:
    if isinstance(node, ast.Starred):
        return True
    return any(isinstance(child, ast.Starred) for child in ast.walk(node))


def _fstring_value_has_nested_brackets(node: ast.AST) -> bool:
    return any(isinstance(child, (ast.Subscript, ast.List, ast.Dict, ast.Set)) for child in ast.walk(node))


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _decorator_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ""


def _tree_has_function_annotations(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = (
                list(getattr(node.args, "posonlyargs", []))
                + list(node.args.args)
                + list(node.args.kwonlyargs)
            )
            if node.args.vararg is not None:
                args.append(node.args.vararg)
            if node.args.kwarg is not None:
                args.append(node.args.kwarg)
            if node.returns is not None or any(arg.annotation is not None for arg in args):
                return True
    return False


class _FeatureVisitor(ast.NodeVisitor):
    def __init__(self, has_function_annotations: bool = False) -> None:
        self.uses: list[_FeatureUse] = []
        self.dict_names: Set[str] = set()
        self._function_depth = 0
        self._class_depth = 0
        self._comprehension_depth = 0
        self._has_function_annotations = has_function_annotations

    def _add(
        self,
        feature: str,
        node: ast.AST,
        *,
        confidence: str = "high",
        required_axis: Optional[str] = None,
        detail: str = "",
    ) -> None:
        line, column = _node_location(node)
        self.uses.append(_FeatureUse(feature, line, column, confidence, required_axis, detail))

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if self._function_has_annotations(node):
            self._has_function_annotations = True
            self._add("function_annotations_syntax", node)
        if self._has_viper_annotations(node):
            self._add("viper_annotations", node, confidence="medium")
        if isinstance(node, ast.AsyncFunctionDef):
            self._add("async_await", node)
        if getattr(node.args, "posonlyargs", None):
            self._add("positional_only_params", node, required_axis="parse")
        if node.args.kwonlyargs:
            self._add("keyword_only_args", node, required_axis="parse")
        self._function_depth += 1
        self.generic_visit(node)
        self._function_depth -= 1

    def _function_has_annotations(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        args = (
            list(getattr(node.args, "posonlyargs", []))
            + list(node.args.args)
            + list(node.args.kwonlyargs)
        )
        if node.args.vararg is not None:
            args.append(node.args.vararg)
        if node.args.kwarg is not None:
            args.append(node.args.kwarg)
        return node.returns is not None or any(arg.annotation is not None for arg in args)

    def _has_viper_annotations(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        if not self._function_has_annotations(node):
            return False
        decorators = {_decorator_name(item) for item in node.decorator_list}
        return bool(decorators & {
            "viper", "native", "asm_thumb", "asm_xtensa", "asm_rv32",
            "micropython.viper", "micropython.native", "micropython.asm_thumb",
            "micropython.asm_xtensa", "micropython.asm_rv32",
        })

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        if getattr(node.args, "posonlyargs", None):
            self._add("positional_only_params", node, required_axis="parse")
        if node.args.kwonlyargs:
            self._add("keyword_only_args", node, required_axis="parse")
        args = (
            list(getattr(node.args, "posonlyargs", []))
            + list(node.args.args)
            + list(node.args.kwonlyargs)
        )
        if node.args.vararg is not None:
            args.append(node.args.vararg)
        if node.args.kwarg is not None:
            args.append(node.args.kwarg)
        if any(arg.annotation is not None for arg in args):
            self._add("function_annotations_syntax", node)
        self._function_depth += 1
        self.generic_visit(node)
        self._function_depth -= 1

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if getattr(node, "type_params", None):
            self._add("type_params_pep695", node, required_axis="parse")
        self._class_depth += 1
        self.generic_visit(node)
        self._class_depth -= 1

    def visit_TypeAlias(self, node: ast.AST) -> None:  # pragma: no cover - Python version gated
        self._add("type_params_pep695", node, required_axis="parse")
        self.generic_visit(node)

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
        self._add("yield_from", node)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._add("assignment_expressions", node)
        if self._comprehension_depth:
            self._add("assignment_expr_comp_scope_fixed", node, required_axis="semantic")
        self.generic_visit(node)

    def visit_Await(self, node: ast.Await) -> None:
        self._add("async_await", node)
        if self._function_depth == 0 and self._class_depth == 0:
            self._add("top_level_await_option", node)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._add("async_await", node)
        if self._function_depth == 0:
            self._add("async_for_with_guard", node, required_axis="semantic")
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._add("async_await", node)
        if self._function_depth == 0:
            self._add("async_for_with_guard", node, required_axis="semantic")
        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self._add("fstrings", node)
        self.generic_visit(node)

    def visit_FormattedValue(self, node: ast.FormattedValue) -> None:
        if node.conversion in {ord("r"), ord("s")}:
            self._add("fstring_conversion_rs", node)
        elif node.conversion == ord("a"):
            self._add(
                "fstrings",
                node,
                confidence="medium",
                detail="MicroPython does not provide CPython-compatible f-string !a conversion",
            )
        if _fstring_value_has_nested_brackets(node.value):
            self._add("fstring_nested_brackets", node, confidence="medium")
        if isinstance(node.format_spec, ast.JoinedStr) and any(
            isinstance(value, ast.FormattedValue) for value in node.format_spec.values
        ):
            self._add("fstring_nested_replacement_fields", node)
        self.generic_visit(node)

    def visit_TemplateStr(self, node: ast.AST) -> None:  # pragma: no cover - Python version gated
        self._add("tstrings", node)
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.MatMult):
            self._add("matmul_operator", node)
        elif isinstance(node.op, ast.BitOr):
            left_dict = _is_dict_like_expr(node.left, self.dict_names)
            right_dict = _is_dict_like_expr(node.right, self.dict_names)
            if left_dict or right_dict:
                self._add("dict_union", node)
            elif not isinstance(node.left, (ast.Set, ast.Constant)) and not isinstance(node.right, (ast.Set, ast.Constant)):
                self._add("dict_union", node, confidence="low")
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.op, ast.MatMult):
            self._add("matmul_operator", node)
        elif isinstance(node.op, ast.BitOr):
            if _is_dict_like_expr(node.target, self.dict_names) or _is_dict_like_expr(node.value, self.dict_names):
                self._add("dict_union", node)
            else:
                self._add("dict_union", node, confidence="low")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if _is_dict_like_expr(node.value, self.dict_names):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.dict_names.add(target.id)
        for target in node.targets:
            if _target_has_starred(target):
                self._add("extended_unpack_assignment", target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._add("variable_annotations", node)
        if isinstance(node.target, ast.Name) and _is_dict_annotation(node.annotation):
            self.dict_names.add(node.target.id)
        if _target_has_starred(node.target):
            self._add("extended_unpack_assignment", node.target)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        if _target_has_starred(node.target):
            self._add("extended_unpack_assignment", node.target)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if item.optional_vars is not None and _target_has_starred(item.optional_vars):
                self._add("extended_unpack_assignment", item.optional_vars)
        self.generic_visit(node)

    def _visit_comprehension_node(self, node: ast.AST) -> None:
        self._add("comprehensions", node)
        if isinstance(node, ast.SetComp):
            self._add("set_literals_comprehensions", node)
        self._comprehension_depth += 1
        self.generic_visit(node)
        self._comprehension_depth -= 1

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension_node(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension_node(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension_node(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension_node(node)

    def visit_Call(self, node: ast.Call) -> None:
        if any(isinstance(arg, ast.Starred) for arg in node.args) or any(keyword.arg is None for keyword in node.keywords):
            self._add("call_star_star_unpack", node)
        if isinstance(node.func, ast.Name) and node.func.id == "get_type_hints" and self._has_function_annotations:
            self._add(
                "function_annotations_semantics",
                node,
                confidence="medium",
                required_axis="semantic",
                detail="typing.get_type_hints depends on CPython-style runtime annotation storage",
            )
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "get_type_hints" and self._has_function_annotations:
            self._add(
                "function_annotations_semantics",
                node,
                confidence="medium",
                required_axis="semantic",
                detail="typing.get_type_hints depends on CPython-style runtime annotation storage",
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "__annotations__" and self._has_function_annotations:
            self._add(
                "function_annotations_semantics",
                node,
                confidence="medium",
                required_axis="semantic",
                detail="runtime __annotations__ access is semantically limited on MicroPython",
            )
        self.generic_visit(node)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self._add("nonlocal_statement", node, required_axis="parse")
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        if node.cause is not None:
            self._add("raise_from", node)
        self.generic_visit(node)

    def visit_Set(self, node: ast.Set) -> None:
        self._add("set_literals_comprehensions", node)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if any(isinstance(child, ast.Slice) for child in ast.walk(node.slice)):
            self._add("slice_syntax", node, required_axis="parse")
        self.generic_visit(node)

    def visit_Match(self, node: ast.AST) -> None:  # pragma: no cover - Python version gated
        self._add("match_case", node, required_axis="parse")
        self.generic_visit(node)

    def visit_TryStar(self, node: ast.AST) -> None:  # pragma: no cover - Python version gated
        self._add("except_star_exception_groups", node, required_axis="parse")
        self.generic_visit(node)


def _string_prefix(text: str) -> str:
    prefix = []
    for ch in text:
        if ch in "'\"":
            break
        prefix.append(ch.lower())
    return "".join(prefix)


def _token_name(tok_type: int) -> str:
    return token.tok_name.get(tok_type, str(tok_type))


def _is_string_start(tok: tokenize.TokenInfo) -> bool:
    name = _token_name(tok.type)
    if name in {"FSTRING_START", "TSTRING_START"}:
        return True
    return tok.type == token.STRING


def _is_string_end(tok: tokenize.TokenInfo) -> bool:
    name = _token_name(tok.type)
    return tok.type == token.STRING or name in {"FSTRING_END", "TSTRING_END"}


def _start_prefix(tok: tokenize.TokenInfo) -> str:
    name = _token_name(tok.type)
    if name in {"FSTRING_START", "TSTRING_START"}:
        return _string_prefix(tok.string)
    if tok.type == token.STRING:
        return _string_prefix(tok.string)
    return ""


def _scan_tstring_fallback(tokens: list[tokenize.TokenInfo]) -> list[_FeatureUse]:
    uses: list[_FeatureUse] = []
    for index, tok in enumerate(tokens[:-1]):
        if tok.type != token.NAME or tok.string.lower() not in {"t", "rt", "tr"}:
            continue
        nxt = tokens[index + 1]
        if nxt.type == token.STRING and tok.end == nxt.start:
            uses.append(_FeatureUse("tstrings", tok.start[0], tok.start[1] + 1))
    return uses


def _scan_except_star_fallback(tokens: list[tokenize.TokenInfo]) -> list[_FeatureUse]:
    uses: list[_FeatureUse] = []
    significant = [
        tok for tok in tokens
        if tok.type not in {tokenize.NL, token.NEWLINE, token.INDENT, token.DEDENT, tokenize.COMMENT}
    ]
    for index, tok in enumerate(significant[:-1]):
        if tok.type == token.NAME and tok.string == "except" and significant[index + 1].string == "*":
            uses.append(_FeatureUse("except_star_exception_groups", tok.start[0], tok.start[1] + 1, required_axis="parse"))
    return uses


def _scan_pep695_fallback(tokens: list[tokenize.TokenInfo]) -> list[_FeatureUse]:
    uses: list[_FeatureUse] = []
    significant = [
        tok for tok in tokens
        if tok.type not in {tokenize.NL, token.NEWLINE, token.INDENT, token.DEDENT, tokenize.COMMENT}
    ]
    for index, tok in enumerate(significant[:-3]):
        if tok.type != token.NAME:
            continue
        if tok.string == "type" and significant[index + 2].string == "[":
            uses.append(_FeatureUse("type_params_pep695", tok.start[0], tok.start[1] + 1, required_axis="parse"))
        elif tok.string in {"def", "class"} and significant[index + 2].string == "[":
            uses.append(_FeatureUse("type_params_pep695", tok.start[0], tok.start[1] + 1, required_axis="parse"))
    return uses


def _scan_source_features(source: str) -> list[_FeatureUse]:
    uses: list[_FeatureUse] = []
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    uses.extend(_scan_tstring_fallback(tokens))
    uses.extend(_scan_except_star_fallback(tokens))
    uses.extend(_scan_pep695_fallback(tokens))

    last_literal: Optional[tuple[tuple[int, int], bool, bool]] = None
    in_fstring = False
    replacement_depth = 0
    seen_field_separator = False
    debug_reported = False

    for tok in tokens:
        name = _token_name(tok.type)
        prefix = _start_prefix(tok)
        has_f = "f" in prefix
        has_t = "t" in prefix

        if tok.type == token.NUMBER and "_" in tok.string:
            uses.append(_FeatureUse("numeric_literal_underscores", tok.start[0], tok.start[1] + 1, required_axis="parse"))

        if _is_string_start(tok):
            if has_t or name == "TSTRING_START":
                uses.append(_FeatureUse("tstrings", tok.start[0], tok.start[1] + 1))
            if has_f and "r" in prefix:
                uses.append(_FeatureUse("fstring_raw_prefix", tok.start[0], tok.start[1] + 1))
            if last_literal is not None and (last_literal[1] or has_f):
                uses.append(_FeatureUse("fstring_adjacent_concat", tok.start[0], tok.start[1] + 1, confidence="medium"))
            if name == "FSTRING_START":
                in_fstring = True
                replacement_depth = 0
                seen_field_separator = False
                debug_reported = False
            if tok.type == token.STRING:
                last_literal = (tok.end, has_f, has_t)
            continue

        if name == "FSTRING_END":
            in_fstring = False
            last_literal = (tok.end, True, False)
            continue
        if name == "TSTRING_END":
            last_literal = (tok.end, False, True)
            continue
        if name in {"FSTRING_MIDDLE", "TSTRING_MIDDLE"}:
            continue
        if tok.type in {tokenize.NL, token.NEWLINE, token.INDENT, token.DEDENT, tokenize.COMMENT}:
            if tok.type in {tokenize.NL, token.NEWLINE}:
                last_literal = None
            continue
        if tok.type == token.ENDMARKER:
            continue

        if in_fstring and tok.string == "{":
            replacement_depth += 1
            if replacement_depth == 1:
                seen_field_separator = False
                debug_reported = False
            continue
        if in_fstring and tok.string == "}":
            replacement_depth = max(replacement_depth - 1, 0)
            if replacement_depth == 0:
                seen_field_separator = False
                debug_reported = False
            continue
        if in_fstring and replacement_depth == 1 and tok.string in {":", "!"}:
            seen_field_separator = True
            continue
        if in_fstring and replacement_depth == 1 and tok.string == "=" and not seen_field_separator and not debug_reported:
            uses.append(_FeatureUse("fstring_debug_expr", tok.start[0], tok.start[1] + 1, confidence="medium", required_axis="semantic"))
            debug_reported = True

        last_literal = None

    return uses


def _detect_feature_uses(tree: ast.AST, source: str) -> list[_FeatureUse]:
    visitor = _FeatureVisitor(_tree_has_function_annotations(tree))
    visitor.visit(tree)
    ast_features = {use.feature for use in visitor.uses}
    source_uses = [
        use for use in _scan_source_features(source)
        if not (
            use.feature in {"except_star_exception_groups", "type_params_pep695", "tstrings"}
            and use.feature in ast_features
        )
    ]
    uses = visitor.uses + source_uses
    deduped: dict[tuple[str, int, int, str], _FeatureUse] = {}
    for use in uses:
        deduped.setdefault((use.feature, use.line, use.column, use.detail), use)
    return list(deduped.values())


def _compat_warning_severity(compat: str) -> Optional[str]:
    if compat == "off":
        return None
    return "error" if compat == "error" else "warning"


def _legacy_strict_items(
    uses: Iterable[_FeatureUse],
    path: str,
    remote_path: str,
    compat: str,
) -> list[PrecheckItem]:
    severity = _compat_warning_severity(compat)
    if severity is None:
        return []
    items: list[PrecheckItem] = []
    for use in uses:
        rule = _FEATURE_RULES.get(use.feature)
        if rule is None:
            continue
        if rule.parse_tag == "v1.0-rc1" and rule.runtime_tag == "v1.0-rc1" and rule.semantic_status == "runtime_substantial":
            continue
        message = rule.generic_message or f"{rule.label} may be unsupported or behave differently on MicroPython firmware"
        if use.detail:
            message = f"{message}: {use.detail}"
        items.append(_item(severity, path, message, use.line, use.column, remote_path))
    return items


def _min_tag_for_axis(rule: _FeatureRule, axis: str) -> Optional[str]:
    if axis == "parse":
        return rule.parse_tag
    if axis == "runtime":
        return rule.runtime_tag
    return rule.semantic_tag


def _feature_item_message(
    use: _FeatureUse,
    target_tag: str,
    status: _FeatureStatus,
    severity: str,
) -> str:
    rule = _FEATURE_RULES[use.feature]
    required_axis = use.required_axis or rule.required_axis
    min_tag = _min_tag_for_axis(rule, required_axis)
    base = f"{rule.label} requires MicroPython"
    if min_tag is None:
        base = f"{rule.label} is not supported by MicroPython through {_MICROPYTHON_TAG_ORDER[-1]}"
    else:
        base = f"{rule.label} requires MicroPython {min_tag}+ for {required_axis} support"
    status_text = (
        f"target {target_tag} has parse={status.parse}, runtime={status.runtime}, "
        f"semantic={status.semantic}"
    )
    notes: list[str] = []
    if status.config_gated:
        notes.append("feature is gated by firmware build options")
    if status.runtime in {"partial", "parse_only"}:
        notes.append(f"runtime support is {status.runtime}")
    if status.semantic in {"limited", "runtime_partial", "parse_only"}:
        notes.append(f"semantic support is {status.semantic}")
    if use.confidence == "low":
        notes.append("static detector confidence is low")
    if use.detail:
        notes.append(use.detail)
    suffix = f"; {status_text}"
    if notes:
        suffix += "; " + "; ".join(notes)
    return f"{base}{suffix}"


def _versioned_strict_items(
    uses: Iterable[_FeatureUse],
    path: str,
    remote_path: str,
    compat: str,
    target_tag: str,
) -> list[PrecheckItem]:
    if compat == "off":
        return []
    warning_severity = "error" if compat == "error" else "warning"
    items: list[PrecheckItem] = []
    for use in uses:
        rule = _FEATURE_RULES.get(use.feature)
        if rule is None:
            continue
        status = _feature_status(use.feature, target_tag)
        hard_axis = rule.hard_axis if rule.hard_axis is not None else rule.required_axis
        severity: Optional[str] = None
        if use.confidence == "low":
            if (
                status.parse == "absent"
                or status.runtime == "absent"
                or status.runtime in {"partial", "parse_only"}
                or status.semantic in {"limited", "runtime_partial", "parse_only"}
                or status.config_gated
                or use.detail
            ):
                severity = warning_severity
        elif hard_axis == "parse" and status.parse == "absent":
            severity = "error"
        elif hard_axis == "runtime" and status.parse == "absent":
            severity = "error"
        elif hard_axis == "runtime" and status.runtime == "absent":
            severity = "error"
        elif status.runtime in {"partial", "parse_only"}:
            severity = warning_severity
        elif status.semantic in {"limited", "runtime_partial", "parse_only"}:
            severity = warning_severity
        elif status.config_gated:
            severity = warning_severity
        elif use.detail:
            severity = warning_severity
        if severity is None:
            continue
        items.append(_item(
            severity,
            path,
            _feature_item_message(use, target_tag, status, severity),
            use.line,
            use.column,
            remote_path,
        ))
    return items


def _versioned_hard_error_items(
    uses: Iterable[_FeatureUse],
    path: str,
    remote_path: str,
    target_tag: str,
) -> list[PrecheckItem]:
    return [
        item
        for item in _versioned_strict_items(uses, path, remote_path, "warn", target_tag)
        if item.severity == "error"
    ]


def _strict_items_from_uses(
    uses: Iterable[_FeatureUse],
    path: str,
    remote_path: str,
    compat: str,
    target_tag: Optional[str],
) -> list[PrecheckItem]:
    if target_tag is None:
        return _legacy_strict_items(uses, path, remote_path, compat)
    return _versioned_strict_items(uses, path, remote_path, compat, target_tag)


def _strict_items(
    tree: ast.AST,
    source: str,
    path: str,
    remote_path: str,
    compat: str,
    target_tag: Optional[str],
) -> list[PrecheckItem]:
    uses = _detect_feature_uses(tree, source)
    return _strict_items_from_uses(uses, path, remote_path, compat, target_tag)


def run_precheck(
    entries: Iterable[PrecheckEntry],
    mode: PrecheckMode = "basic",
    compat: PrecheckCompat = "warn",
    active_tags: Optional[Set[str]] = None,
    mp_version: Optional[str] = None,
) -> PrecheckReport:
    mode = validate_precheck_mode(mode)
    compat = validate_precheck_compat(compat)
    target_tag = normalize_micropython_version(mp_version)
    if mode == "off":
        return PrecheckReport(())

    items: list[PrecheckItem] = []
    seen_remote: dict[str, str] = {}

    for local_path, remote_path in entries:
        path = str(local_path)
        remote = str(remote_path)
        remote_error = _remote_path_error(remote)
        if remote_error:
            items.append(_item("error", path, remote_error, remote_path=remote))
        normalized_remote = remote.replace("\\", "/")
        previous = seen_remote.get(normalized_remote)
        if previous is not None:
            items.append(_item(
                "error",
                path,
                f"remote path conflicts with {previous}",
                remote_path=remote,
            ))
        else:
            seen_remote[normalized_remote] = path

        if not str(path).endswith(".py"):
            continue
        try:
            source = _read_python_source(path)
        except (OSError, SyntaxError, UnicodeError) as exc:
            items.append(_item("error", path, f"cannot read source: {exc}", remote_path=remote))
            continue
        if not source.strip():
            items.append(_item("error", path, "empty Python file", remote_path=remote))
            continue

        token_uses: list[_FeatureUse] = []
        if mode == "strict" or target_tag is not None:
            try:
                token_uses = _scan_source_features(source)
            except (tokenize.TokenError, IndentationError) as exc:
                items.append(_item("error", path, f"tokenize failed: {exc}", remote_path=remote))

        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            items.append(_syntax_error_item(path, remote, exc))
            if mode == "strict" and token_uses:
                items.extend(_strict_items_from_uses(token_uses, path, remote, compat, target_tag))
            elif target_tag is not None and token_uses:
                items.extend(_versioned_hard_error_items(token_uses, path, remote, target_tag))
            continue

        try:
            processed = preprocess(source, active_tags or set(), path)
            ast.parse(processed, filename=path)
        except SyntaxError as exc:
            items.append(_syntax_error_item(path, remote, exc, "preprocessed syntax error"))
        except Exception as exc:
            items.append(_item(
                "error",
                path,
                f"preprocess failed: {exc}",
                remote_path=remote,
            ))

        if mode == "strict":
            items.extend(_strict_items(tree, source, path, remote, compat, target_tag))
        elif target_tag is not None:
            items.extend(_versioned_hard_error_items(
                _detect_feature_uses(tree, source),
                path,
                remote,
                target_tag,
            ))

    report = PrecheckReport(tuple(items))
    if not report.ok:
        raise PrecheckError(report)
    return report


def collect_directory_entries(
    directory: str,
    remote_prefix: str,
    active_tags: Optional[Set[str]] = None,
    manifest_path: Optional[str] = None,
    exclude_paths: Optional[Iterable[str]] = None,
) -> list[PrecheckEntry]:
    from .project_files import collect_project_files

    raw_entries = collect_project_files(
        directory,
        active_tags=active_tags,
        manifest_path=manifest_path,
        exclude_paths=exclude_paths,
    )

    entries: list[PrecheckEntry] = []
    for local_path, remote_part in raw_entries:
        if Path(str(remote_part)).name == "manifest.py" or str(local_path).endswith(".pyi"):
            continue
        if str(remote_part).startswith("/"):
            remote_path = str(remote_part).replace("\\", "/")
        else:
            remote_path = os.path.join(remote_prefix, str(remote_part)).replace("\\", "/")
        entries.append((str(local_path), remote_path))
    return entries


def filter_entries_to_changed(
    entries: Iterable[PrecheckEntry],
    local_dir: str,
    hash_config_path: Optional[str] = None,
) -> list[PrecheckEntry]:
    """Return entries whose content would be considered changed by project flash."""
    if hash_config_path is None:
        hash_config_path = os.path.join(local_dir, HASH_CONFIG_FILE)
    if not os.path.exists(hash_config_path):
        return list(entries)

    with open(hash_config_path, "r", encoding="utf-8") as f:
        stored_hashes = json.load(f).get("files", {})

    changed: list[PrecheckEntry] = []
    for local_path, remote_path in entries:
        rel_path = os.path.relpath(local_path, local_dir).replace("\\", "/")
        try:
            current_hash = _compute_file_hash(local_path)
        except OSError:
            changed.append((local_path, remote_path))
            continue
        if stored_hashes.get(rel_path) != current_hash:
            changed.append((local_path, remote_path))
    return changed


def collect_project_precheck_entries(
    directory: str,
    remote_prefix: str,
    *,
    hash_config_path: Optional[str] = None,
    active_tags: Optional[Set[str]] = None,
    manifest_path: Optional[str] = None,
) -> list[PrecheckEntry]:
    entries = collect_directory_entries(
        directory,
        remote_prefix,
        active_tags=active_tags,
        manifest_path=manifest_path,
        exclude_paths={hash_config_path} if hash_config_path else None,
    )
    return filter_entries_to_changed(entries, directory, hash_config_path)

from __future__ import annotations

import ast
import hashlib
import json
import os
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


class _CompatVisitor(ast.NodeVisitor):
    def __init__(self, path: str, remote_path: str, severity: str) -> None:
        self.path = path
        self.remote_path = remote_path
        self.severity = severity
        self.items: list[PrecheckItem] = []

    def _add(self, node: ast.AST, message: str) -> None:
        self.items.append(_item(
            self.severity,
            self.path,
            message,
            getattr(node, "lineno", 1),
            getattr(node, "col_offset", 0) + 1,
            self.remote_path,
        ))

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self._add(node, "f-string may be unsupported on older MicroPython firmware")
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._add(node, "walrus operator may be unsupported on older MicroPython firmware")
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        self._add(node, "match/case may be unsupported on older MicroPython firmware")
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._add(node, "variable type annotations may be unsupported on older MicroPython firmware")
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        if node.annotation is not None:
            self._add(node, "argument type annotations may be unsupported on older MicroPython firmware")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.returns is not None:
            self._add(node, "return type annotations may be unsupported on older MicroPython firmware")
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node.returns is not None:
            self._add(node, "return type annotations may be unsupported on older MicroPython firmware")
        self.generic_visit(node)


def _strict_items(
    tree: ast.AST,
    path: str,
    remote_path: str,
    compat: str,
) -> list[PrecheckItem]:
    if compat == "off":
        return []
    visitor = _CompatVisitor(path, remote_path, "error" if compat == "error" else "warning")
    visitor.visit(tree)
    return visitor.items


def run_precheck(
    entries: Iterable[PrecheckEntry],
    mode: PrecheckMode = "basic",
    compat: PrecheckCompat = "warn",
    active_tags: Optional[Set[str]] = None,
) -> PrecheckReport:
    mode = validate_precheck_mode(mode)
    compat = validate_precheck_compat(compat)
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

        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            items.append(_syntax_error_item(path, remote, exc))
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
            items.extend(_strict_items(tree, path, remote, compat))

    report = PrecheckReport(tuple(items))
    if not report.ok:
        raise PrecheckError(report)
    return report


def collect_directory_entries(
    directory: str,
    remote_prefix: str,
    active_tags: Optional[Set[str]] = None,
    manifest_path: Optional[str] = None,
) -> list[PrecheckEntry]:
    from .build import load_manifest

    if manifest_path:
        raw_entries = load_manifest(manifest_path, active_tags, base_dir=directory)
    else:
        raw_entries = []
        for root, _dirs, files in os.walk(directory):
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                local_path = os.path.join(root, filename)
                rel = os.path.relpath(local_path, directory).replace("\\", "/")
                raw_entries.append((local_path, rel))

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
    )
    return filter_entries_to_changed(entries, directory, hash_config_path)

from __future__ import annotations

import ast
import hashlib
from pathlib import Path


_ALLOWED_CALLS = {"module", "package", "include", "require"}


def _is_literal(node: ast.AST) -> bool:
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, MemoryError, RecursionError):
        return False
    return True


def validate_manifest(path: Path) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        return [str(exc)]

    errors: list[str] = []
    for statement in tree.body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            errors.append(
                f"line {getattr(statement, 'lineno', '?')}: "
                "manifest contains unsupported statement"
            )
            continue
        call = statement.value
        if not isinstance(call.func, ast.Name) or call.func.id not in _ALLOWED_CALLS:
            errors.append(f"line {call.lineno}: manifest contains unsupported call")
            continue
        if any(isinstance(argument, ast.Starred) for argument in call.args):
            errors.append(f"line {call.lineno}: starred arguments are not supported")
        if any(keyword.arg is None for keyword in call.keywords):
            errors.append(f"line {call.lineno}: expanded keyword arguments are not supported")
        for argument in call.args:
            value = argument.value if isinstance(argument, ast.Starred) else argument
            if not _is_literal(value):
                errors.append(f"line {call.lineno}: manifest arguments must be literals")
                break
        for keyword in call.keywords:
            if keyword.arg is not None and not _is_literal(keyword.value):
                errors.append(f"line {call.lineno}: manifest arguments must be literals")
                break
    return errors


def frozen_payload(path: Path) -> dict[str, object]:
    errors = validate_manifest(path)
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = ""
    return {"path": str(path), "sha256": digest, "errors": errors}

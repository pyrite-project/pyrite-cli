from __future__ import annotations

import hashlib
import re
from pathlib import Path


_FORBIDDEN_COMMAND_RE = re.compile(
    r"\b(execute_process|add_custom_command|add_custom_target)\s*\(", re.IGNORECASE
)
# CMake modules legitimately use variables for flags and generated source lists.
# Rejecting every variable other than CMAKE_CURRENT_LIST_DIR made valid modules
# unusable, so confinement checks focus on execution primitives and host paths.
_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_}])(?:[A-Za-z]:[/\\]|/(?:home|Users|tmp|etc|var|opt)/)")


def validate_module_cmake(path: Path) -> list[str]:
    if not path.is_file():
        return ["module cmake does not exist"]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [str(exc)]

    errors: list[str] = []
    if _FORBIDDEN_COMMAND_RE.search(text):
        errors.append("shell or custom command execution is not allowed")
    if _ABSOLUTE_PATH_RE.search(text):
        errors.append("absolute module paths are not allowed")
    return errors


def module_payload(path: Path) -> dict[str, object]:
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = ""
    return {"path": str(path), "sha256": digest, "errors": validate_module_cmake(path)}

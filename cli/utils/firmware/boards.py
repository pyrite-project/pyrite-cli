from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from .models import BoardContract

_BOARD_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")


def _checkout_path(checkout: Path) -> Path:
    """Return an absolute checkout path without changing anything in it."""
    return checkout.expanduser().resolve(strict=True)


def _safe_board_name(name: str) -> str:
    if not name or not _BOARD_NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise ValueError(f"invalid board name: {name!r}")
    return name


def _git(checkout: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def checkout_revision(checkout: Path) -> str | None:
    """Return the exact source revision, or ``None`` for a non-git tree."""
    return _git(_checkout_path(checkout), "rev-parse", "HEAD")


def checkout_status(checkout: Path) -> tuple[str, ...]:
    """Return tracked and untracked changes in porcelain format.

    This is intentionally a read-only check.  No checkout files are created,
    cleaned, or reset by the builder.
    """
    root = _checkout_path(checkout)
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"MicroPython checkout is not a git repository: {root}") from exc
    return tuple(line for line in result.stdout.splitlines() if line)


def assert_clean_checkout(checkout: Path) -> str:
    """Require a clean checkout and return its immutable HEAD revision."""
    root = _checkout_path(checkout)
    changes = checkout_status(root)
    if changes:
        raise ValueError(
            "MicroPython checkout must be clean; refusing to build in a modified checkout: "
            + ", ".join(changes)
        )
    revision = checkout_revision(root)
    if not revision:
        raise ValueError(f"cannot determine MicroPython checkout revision: {root}")
    return revision


def discover_boards(checkout: Path, port: str = "esp32") -> list[str]:
    root = _checkout_path(checkout) / "ports" / _safe_port(port) / "boards"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and _BOARD_NAME_RE.fullmatch(p.name))


def _safe_port(port: str) -> str:
    # MicroPython exposes all ESP chips from the esp32 port.  The IDF chip target
    # belongs to the board cmake contract and must not be mistaken for a port.
    value = port.strip().lower()
    if value != "esp32":
        raise ValueError(f"unsupported MicroPython port: {port!r}; expected 'esp32'")
    return value


def load_contract(checkout: Path, base: str, name: str | None = None, *, port: str = "esp32") -> BoardContract:
    """Read a board contract without modifying the MicroPython checkout."""
    root = _checkout_path(checkout)
    port = _safe_port(port)
    base = _safe_board_name(base)
    board = _safe_board_name(name or base)
    board_root = root / "ports" / port / "boards" / base
    sources: list[dict[str, str]] = []
    complete = board_root.is_dir()
    for filename in ("mpconfigboard.h", "mpconfigboard.cmake"):
        path = board_root / filename
        if path.exists() and path.is_file():
            sources.append(
                {
                    "path": f"ports/{port}/boards/{base}/{filename}",
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        else:
            complete = False
    revision = checkout_revision(root)
    if revision is None:
        complete = False
    identity = {
        "base_board": base,
        "micropython_commit": revision or "unknown",
        "sources": sources,
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return BoardContract(board, base, "esp32", revision or "unknown", complete, tuple(sources), digest, port)


def verify_version_hook(
    checkout: Path,
    expected_commit: str,
    *,
    require_clean: bool = True,
) -> str:
    """Verify that a build uses the contract's exact MicroPython revision.

    The check is a hook rather than a checkout operation: it only reads git
    metadata, and optionally verifies that the source tree is clean.  This
    prevents a board contract generated from one revision being used against
    another revision while preserving the checkout byte-for-byte.
    """
    revision = assert_clean_checkout(checkout) if require_clean else checkout_revision(checkout)
    if not revision:
        raise ValueError("cannot determine MicroPython checkout revision")
    expected = expected_commit.strip()
    if not expected or expected == "unknown":
        raise ValueError("board contract does not pin a MicroPython commit")
    if revision != expected:
        raise ValueError(
            f"MicroPython checkout revision {revision} does not match contract {expected}"
        )
    return revision


def normalize_board_name(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").upper()
    if not value or not re.match(r"^[A-Z_]", value):
        raise ValueError("invalid board name")
    return value

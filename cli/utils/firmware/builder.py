from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from .boards import assert_clean_checkout, checkout_revision


def tool_available(name: str) -> bool:
    return shutil.which(name) is not None


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def validate_external_dir(path: Path, checkout: Path) -> Path:
    """Validate a generated/build directory is outside the source checkout."""
    destination = _resolved(path)
    source = _resolved(checkout)
    if _inside(destination, source):
        raise ValueError(
            f"generated/build directory must be outside the MicroPython checkout: {destination}"
        )
    return destination


def plan_argv(
    micropython: Path,
    board_dir: Path,
    build_dir: Path,
    target: str = "esp32",
    variant: str | None = None,
) -> list[str]:
    """Create a read-only-source MicroPython make command.

    ``BOARD_DIR`` and ``BUILD`` are both required to live outside the checkout;
    this keeps custom board files and build products out of upstream sources.
    The function only creates argv and never invokes make.
    """
    source = _resolved(micropython)
    if not source.is_dir():
        raise ValueError(f"MicroPython checkout does not exist: {source}")
    target = target.strip()
    if not target or "/" in target or "\\" in target or target in {".", ".."}:
        raise ValueError(f"invalid MicroPython port target: {target!r}")
    board = validate_external_dir(board_dir, source)
    build = validate_external_dir(build_dir, source)
    if not board.is_dir():
        raise ValueError(f"BOARD_DIR does not exist: {board}")
    build.parent.mkdir(parents=True, exist_ok=True)
    return [
        "make",
        "-C",
        str(source / "ports" / target),
        f"BOARD_DIR={board}",
        f"BUILD={build}",
        *([f"BOARD_VARIANT={variant.strip()}"] if variant and variant.strip() else []),
    ]

def run_build(
    micropython: Path,
    board_dir: Path,
    build_dir: Path,
    *,
    target: str = "esp32",
    expected_commit: str | None = None,
    variant: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the build while enforcing source immutability and revision pinning."""
    source = _resolved(micropython)
    before = assert_clean_checkout(source)
    if expected_commit is not None and before != expected_commit.strip():
        raise ValueError(
            f"MicroPython checkout revision {before} does not match contract {expected_commit}"
        )
    build_error: BaseException | None = None
    result: subprocess.CompletedProcess[str] | None = None
    try:
        result = subprocess.run(
            plan_argv(source, board_dir, build_dir, target, variant),
            check=True,
            text=True,
            capture_output=True,
        )
    except BaseException as exc:
        build_error = exc
    finally:
        try:
            after = checkout_revision(source)
            from .boards import checkout_status
            changes = list(checkout_status(source))
            integrity_error = after != before or bool(changes)
            diagnostic = ", ".join(changes or ([f"revision changed to {after}"] if after != before else []))
        except (OSError, ValueError) as exc:
            integrity_error = True
            diagnostic = f"checkout integrity check failed: {exc}"
    if integrity_error:
        message = "MicroPython checkout was modified during build: " + diagnostic
        if build_error is not None:
            message += f"; build also failed: {build_error}"
        raise RuntimeError(message) from build_error
    if build_error is not None:
        raise build_error
    assert result is not None
    return result


def artifact_report(build_dir: Path) -> dict[str, object]:
    files = []
    if build_dir.exists():
        for path in sorted(build_dir.rglob("*")):
            if path.is_file() and path.suffix in {".bin", ".elf", ".map", ".uf2"}:
                files.append(
                    {
                        "path": str(path),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "size": path.stat().st_size,
                    }
                )
    return {"status": "unknown" if not files else "discovered", "artifacts": files}

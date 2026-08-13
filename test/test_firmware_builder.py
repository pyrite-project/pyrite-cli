from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cli.utils.firmware.boards import (
    assert_clean_checkout,
    checkout_status,
    load_contract,
    verify_version_hook,
)
from cli.utils.firmware.builder import plan_argv, run_build
from cli.utils.firmware.generator import generate_board
from cli.utils.firmware.models import BoardContract, FirmwarePlan, Resolution


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "micropython"
    board = checkout / "ports" / "esp32" / "boards" / "GENERIC"
    board.mkdir(parents=True)
    (board / "mpconfigboard.h").write_text("#define BASE 1\n", encoding="utf8")
    (board / "mpconfigboard.cmake").write_text("set(IDF_TARGET esp32)\n", encoding="utf8")
    _git(checkout, "init", "-q")
    _git(checkout, "config", "user.email", "test@example.invalid")
    _git(checkout, "config", "user.name", "Firmware Test")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", "fixture")
    return checkout


def _plan(contract: BoardContract) -> FirmwarePlan:
    return FirmwarePlan(contract, Resolution({}, {}), status="ready")


def test_contract_pins_base_board_sources_and_checkout_commit(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path)

    contract = load_contract(checkout, "GENERIC", "CUSTOM")

    assert contract.complete
    assert contract.micropython_commit == _git(checkout, "rev-parse", "HEAD")
    assert contract.base_board == "GENERIC"
    assert contract.name == "CUSTOM"
    assert {source["path"] for source in contract.sources} == {
        "ports/esp32/boards/GENERIC/mpconfigboard.h",
        "ports/esp32/boards/GENERIC/mpconfigboard.cmake",
    }
    assert contract.contract_hash
    assert checkout_status(checkout) == ()


def test_incomplete_contract_cannot_claim_completeness(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path)
    (checkout / "ports/esp32/boards/GENERIC/mpconfigboard.h").unlink()

    contract = load_contract(checkout, "GENERIC")

    assert not contract.complete
    with pytest.raises(ValueError, match="both base board files"):
        BoardContract(
            name="CUSTOM",
            base_board="GENERIC",
            micropython_commit=_git(checkout, "rev-parse", "HEAD"),
            complete=True,
            sources=(),
            contract_hash="hash",
        )


def test_version_hook_rejects_dirty_or_mismatched_checkout(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path)
    revision = assert_clean_checkout(checkout)
    assert verify_version_hook(checkout, revision) == revision

    with pytest.raises(ValueError, match="does not match contract"):
        verify_version_hook(checkout, "0" * 40)

    (checkout / "untracked.txt").write_text("dirty", encoding="utf8")
    with pytest.raises(ValueError, match="must be clean"):
        verify_version_hook(checkout, revision)


def test_generate_board_uses_external_board_dir_and_leaves_checkout_clean(
    tmp_path: Path,
) -> None:
    checkout = _checkout(tmp_path)
    contract = load_contract(checkout, "GENERIC", "CUSTOM")
    destination = tmp_path / "generated" / "CUSTOM"

    generate_board(_plan(contract), destination, checkout=checkout)

    assert (destination / "mpconfigboard.h").read_text(encoding="utf8") == "#define BASE 1\n"
    cmake = (destination / "mpconfigboard.cmake").read_text(encoding="utf8")
    assert cmake.startswith("set(IDF_TARGET esp32)\n")
    assert 'list(APPEND SDKCONFIG_DEFAULTS "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.pyrite")' in cmake
    assert checkout_status(checkout) == ()
    with pytest.raises(ValueError, match="outside the MicroPython checkout"):
        generate_board(
            _plan(contract),
            checkout / "ports/esp32/boards/CUSTOM",
            checkout=checkout,
        )


def test_plan_argv_requires_board_and_build_dirs_outside_checkout(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path)
    board_dir = tmp_path / "board"
    board_dir.mkdir()

    argv = plan_argv(checkout, board_dir, tmp_path / "build")

    assert argv == [
        "make",
        "-C",
        str(checkout / "ports/esp32"),
        f"BOARD_DIR={board_dir}",
        f"BUILD={tmp_path / 'build'}",
    ]
    with pytest.raises(ValueError, match="outside the MicroPython checkout"):
        plan_argv(checkout, checkout / "boards/CUSTOM", tmp_path / "build")
    with pytest.raises(ValueError, match="outside the MicroPython checkout"):
        plan_argv(checkout, board_dir, checkout / "build-CUSTOM")


def test_run_build_checks_checkout_before_and_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    board_dir = tmp_path / "board"
    board_dir.mkdir()
    revision = _git(checkout, "rev-parse", "HEAD")

    real_run = subprocess.run

    def clean_run(*args, **kwargs):
        command = args[0]
        if command[0] == "git":
            return real_run(*args, **kwargs)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr("cli.utils.firmware.builder.subprocess.run", clean_run)
    result = run_build(
        checkout,
        board_dir,
        tmp_path / "build",
        expected_commit=revision,
    )
    assert result.stdout == "ok"

    def dirty_run(*args, **kwargs):
        command = args[0]
        if command[0] == "git":
            return real_run(*args, **kwargs)
        (checkout / "generated.txt").write_text("modified", encoding="utf8")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr("cli.utils.firmware.builder.subprocess.run", dirty_run)
    with pytest.raises(RuntimeError, match="modified during build"):
        run_build(
            checkout,
            board_dir,
            tmp_path / "other-build",
            expected_commit=revision,
        )

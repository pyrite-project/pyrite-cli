from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import typer

from ..utils.firmware.boards import discover_boards, load_contract
from ..utils.firmware.builder import artifact_report, plan_argv, run_build
from ..utils.firmware.catalog import get_recipe, list_recipes, resolve
from ..utils.firmware.cmodules import module_payload
from ..utils.firmware.config import load_lock, load_project_data, update_firmware, update_lock
from ..utils.firmware.frozen import frozen_payload
from ..utils.firmware.generator import generate_board
from ..utils.firmware.models import FirmwarePlan
from ..utils.firmware.partitions import parse_partitions, validate_partitions
from ..utils.firmware.pins import parse_pins, validate_pins
from ..utils.firmware.variants import validate_variants

firmware_app = typer.Typer(help="ESP32 firmware board builder", add_completion=False)
board_app = typer.Typer(help="Manage custom firmware boards", add_completion=False)
firmware_app.add_typer(board_app, name="board")


def register(app: typer.Typer) -> None:
    app.add_typer(firmware_app, name="firmware")


def _config_path() -> Path:
    current = Path.cwd().resolve()
    for directory in (current, *current.parents):
        candidate = directory / ".pyrite_config.json"
        if candidate.is_file():
            return candidate
    return current / ".pyrite_config.json"


def _root() -> Path:
    return _config_path().parent


def _interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _firmware_config() -> dict[str, Any]:
    data = load_project_data(_config_path())
    firmware = data.get("firmware") or {}
    return dict(firmware) if isinstance(firmware, dict) else {}


def _path(value: object, default: str) -> Path:
    result = Path(str(value or default)).expanduser()
    return result if result.is_absolute() else _root() / result


def _extension_path(value: str) -> Path:
    """Resolve configured extension files relative to the project root."""
    return _path(value, value)


def _plan_from_config() -> tuple[FirmwarePlan, Path, Path, Path]:
    firmware = _firmware_config()
    checkout = _path(firmware.get("micropython"), "micropython")
    board_name = str(firmware.get("board", "PYRITE_CUSTOM"))
    base_board = str(firmware.get("base_board", "GENERIC"))
    port = str(firmware.get("port", "esp32")).strip().lower()
    selections = firmware.get("selections") or {}
    if not isinstance(selections, dict):
        selections = {}
    errors: list[str] = []
    try:
        contract = load_contract(checkout, base_board, board_name, port=port)
    except (OSError, ValueError) as exc:
        contract = None
        errors.append(f"MicroPython checkout/contract unavailable: {exc}")
    resolution = resolve(selections, port)
    if contract is None:
        # A blocked plan still has a useful, serializable contract-shaped payload.
        from ..utils.firmware.models import BoardContract
        contract = BoardContract(board_name, base_board, "esp32", "unknown", False, (), "", port)
    elif not contract.complete:
        errors.append("board contract is incomplete")
    extensions: dict[str, Any] = {}
    for key in ("pins", "partition", "frozen", "c_modules", "variants"):
        value = firmware.get(key, [])
        # Extension files follow the same project-root-relative convention as
        # checkout, board_dir, and build_dir.  Keep variant selections intact.
        extensions[key] = [str(_path(item, "")) for item in value] if isinstance(value, list) else (str(_path(value, "")) if isinstance(value, str) else value)
    plan = FirmwarePlan(contract, resolution, extensions=extensions, errors=errors)
    board_dir = _path(firmware.get("board_dir"), ".pyrite/firmware/board")
    build_dir = _path(firmware.get("build_dir"), ".pyrite/firmware/build")
    return plan, checkout, board_dir, build_dir


def _emit(payload: object, fmt: str = "text") -> None:
    if fmt not in {"text", "json"}:
        raise typer.BadParameter("--format must be text or json")
    if fmt == "json":
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    elif isinstance(payload, str):
        typer.echo(payload)
    else:
        typer.echo(str(payload))


def _extension_checks(plan: FirmwarePlan) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    ext = plan.extensions
    for key in ("pins", "partition", "frozen", "c_modules", "variants"):
        value = ext.get(key)
        values = value if isinstance(value, list) else ([value] if isinstance(value, (str, Path)) else [])
        for entry in values:
            path = Path(str(entry))
            try:
                if key == "pins":
                    errors = validate_pins(parse_pins(path.read_text(encoding="utf8")))
                elif key == "partition":
                    errors = validate_partitions(parse_partitions(path.read_text(encoding="utf8")))
                elif key == "frozen":
                    errors_obj = frozen_payload(path)["errors"]
                    errors = list(errors_obj) if isinstance(errors_obj, list) else []
                elif key == "c_modules":
                    errors_obj = module_payload(path)["errors"]
                    errors = list(errors_obj) if isinstance(errors_obj, list) else []
                else:
                    errors = validate_variants([str(v) for v in path.read_text(encoding="utf8").splitlines() if v.strip()])
            except (OSError, ValueError) as exc:
                errors = [str(exc)]
            checks.append({"type": key, "path": str(path), "errors": errors})
    for check in checks:
        check_errors = check.get("errors", [])
        if isinstance(check_errors, list):
            for error in check_errors:
                if str(error) not in plan.errors:
                    plan.errors.append(str(error))
    if plan.errors:
        plan.status = "blocked"
    return checks


@firmware_app.command("options")
def options(
    action: str = typer.Argument("list", help="操作：list 或 show"),
    option_id: str | None = typer.Argument(None, help="show 操作要查看的固件选项 ID"),
    fmt: str = typer.Option("text", "--format", help="输出格式：text 或 json"),
) -> None:
    """列出或查看可用固件选项。"""
    payload: object
    if action == "list":
        payload = [r.__dict__ for r in list_recipes()]
    elif action == "show":
        if not option_id:
            raise typer.BadParameter("options show requires an option id")
        payload = get_recipe(option_id).__dict__
    elif option_id is None and action.startswith("show:"):
        payload = get_recipe(action[5:]).__dict__
    else:
        raise typer.BadParameter("supported actions: list or show OPTION_ID")
    if fmt == "json":
        _emit(payload, fmt)
    elif isinstance(payload, dict):
        _emit(f"{payload['id']}\t{payload['title']}")
    elif isinstance(payload, list):
        for recipe in payload:
            typer.echo(f"{recipe['id']}\t{recipe['title']}")


@board_app.command("list")
def board_list(
    fmt: str = typer.Option("text", "--format", help="输出格式：text 或 json"),
    checkout: Path | None = typer.Option(
        None,
        "--checkout",
        help="MicroPython checkout 路径；默认读取项目固件配置",
    ),
) -> None:
    """列出 MicroPython checkout 中的 ESP32 boards。"""
    root = checkout or _path(_firmware_config().get("micropython"), "micropython")
    try:
        names = discover_boards(root)
    except (OSError, ValueError) as exc:
        raise click.UsageError(str(exc)) from exc
    _emit(names if fmt == "json" else "\n".join(names), fmt)


@board_app.command("show")
def board_show(
    name: str = typer.Argument(..., help="要查看的自定义 board 名称"),
    checkout: Path | None = typer.Option(
        None,
        "--checkout",
        help="MicroPython checkout 路径；默认读取项目固件配置",
    ),
    base_board: str = typer.Option(
        "GENERIC",
        "--base-board",
        help="自定义 board 继承的基础 board",
    ),
    fmt: str = typer.Option("text", "--format", help="输出格式：text 或 json"),
) -> None:
    """查看自定义 board 的构建契约。"""
    root = checkout or _path(_firmware_config().get("micropython"), "micropython")
    try:
        payload = load_contract(root, base_board, name).__dict__
    except (OSError, ValueError) as exc:
        raise click.UsageError(str(exc)) from exc
    _emit(payload if fmt == "json" else json.dumps(payload, sort_keys=True, default=str), fmt)


@board_app.command("new")
def board_new(
    name: str = typer.Argument(..., help="要配置的自定义 board 名称"),
    base_board: str = typer.Option(
        "GENERIC",
        "--base-board",
        help="自定义 board 继承的基础 board",
    ),
    output: Path = typer.Option(
        Path(".pyrite/firmware/board"),
        "--output",
        help="生成 board 文件的目录",
    ),
) -> None:
    """配置自定义 board，不修改 MicroPython checkout。"""
    firmware = _firmware_config()
    firmware.update({"board": name, "base_board": base_board, "board_dir": str(output)})
    update_firmware(_config_path(), firmware)
    typer.echo(f"configured board {name}")


@firmware_app.command("plan")
def plan(
    fmt: str = typer.Option("text", "--format", help="输出格式：text 或 json"),
) -> None:
    """检查当前固件配置并输出构建计划状态。"""
    current, checkout, _, _ = _plan_from_config()
    _extension_checks(current)
    payload = current.to_dict()
    payload["checkout"] = str(checkout)
    _emit(payload if fmt == "json" else ("blocked: " + "; ".join(current.errors) if current.errors else "ready"), fmt)


@firmware_app.command("config")
def config(
    set_values: list[str] = typer.Option(
        [],
        "--set",
        help="设置固件选项（OPTION_ID=true|false），可重复指定",
    ),
    tui: bool = typer.Option(False, "--tui", help="打开交互式固件配置界面"),
    keys: str = typer.Option(
        "arrows",
        "--keys",
        help="TUI 按键方案：arrows 或 vim",
    ),
) -> None:
    """查看或修改当前项目的固件配置。"""
    if tui:
        if keys not in {"arrows", "vim"}:
            raise typer.BadParameter("--keys must be arrows or vim")
        if not _interactive_terminal():
            raise click.UsageError("TUI requires an interactive terminal; use firmware config --set for automation")
        try:
            from ..utils.firmware.tui.app import run_tui
            run_tui(keys=keys, mode="config", project_path=_config_path())
        except ImportError as exc:
            raise click.UsageError("TUI requires textual; install pyrite-cli[tui]") from exc
        return
    firmware = _firmware_config()
    selections = firmware.get("selections")
    if not isinstance(selections, dict):
        selections = {}
    firmware["selections"] = selections
    for item in set_values:
        if "=" not in item:
            raise typer.BadParameter("--set requires OPTION_ID=VALUE")
        key, value = item.split("=", 1)
        if not key.strip():
            raise typer.BadParameter("--set requires a non-empty option id")
        key = key.strip()
        try:
            get_recipe(key)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        normalized = value.strip().lower()
        if normalized not in {"true", "false"}:
            raise typer.BadParameter("firmware recipe values must be exactly true or false")
        selections[key] = normalized == "true"
    if set_values:
        update_firmware(_config_path(), firmware)
    _emit(firmware if set_values else json.dumps(firmware, ensure_ascii=False, sort_keys=True, indent=2))


@firmware_app.command("setup")
def setup() -> None:
    """打开交互式向导，完成项目固件的初始配置。"""
    if not _interactive_terminal():
        raise click.UsageError("Firmware setup requires an interactive terminal")
    try:
        from ..utils.firmware.tui.app import run_tui
        run_tui(keys="arrows", mode="setup", project_path=_config_path())
    except ImportError as exc:
        raise click.UsageError("TUI requires textual; install pyrite-cli[tui]") from exc


@firmware_app.command("generate")
def generate() -> None:
    """根据固件配置生成 board 文件并更新固件锁定信息。"""
    current, checkout, board_dir, _ = _plan_from_config()
    _extension_checks(current)
    if current.errors:
        typer.echo("Generation blocked: " + "; ".join(current.errors))
        raise typer.Exit(2)
    try:
        files = generate_board(current, board_dir, checkout=checkout)
    except (OSError, ValueError) as exc:
        raise click.UsageError(str(exc)) from exc
    update_lock(_config_path(), {"contract_hash": current.board.contract_hash, "commit": current.board.micropython_commit, "files": files, "board_dir": str(board_dir), "resolution": current.resolution.to_dict(), "extensions": current.extensions})
    _emit({"status": "generated", "board_dir": str(board_dir), "files": files}, "json")


@firmware_app.command("build")
def build(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="只显示构建命令，不执行构建",
    ),
) -> None:
    """构建当前项目配置对应的 MicroPython 固件。"""
    current, checkout, board_dir, build_dir = _plan_from_config()
    _extension_checks(current)
    if current.errors:
        typer.echo("Build blocked: " + "; ".join(current.errors))
        raise typer.Exit(2)
    if not board_dir.is_dir():
        try:
            files = generate_board(current, board_dir, checkout=checkout)
            update_lock(_config_path(), {"contract_hash": current.board.contract_hash, "commit": current.board.micropython_commit, "files": files, "board_dir": str(board_dir), "resolution": current.resolution.to_dict(), "extensions": current.extensions})
        except (OSError, ValueError) as exc:
            raise click.UsageError(f"BOARD_DIR is not generated; run firmware generate: {exc}") from exc
    try:
        lock = load_lock(_config_path())
        lock_firmware = (lock.get("build") or {}).get("firmware") if isinstance(lock.get("build"), dict) else None
        if not isinstance(lock_firmware, dict):
            raise ValueError("firmware lock is missing; run firmware generate")
        if lock_firmware.get("contract_hash") != current.board.contract_hash or lock_firmware.get("commit") != current.board.micropython_commit:
            raise ValueError("pyrite.lock firmware section is stale; run firmware generate")
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    try:
        argv = plan_argv(checkout, board_dir, build_dir, current.board.target)
    except (OSError, ValueError) as exc:
        raise click.UsageError(str(exc)) from exc
    if dry_run:
        typer.echo(json.dumps({"status": "dry-run", "argv": argv}, sort_keys=True)); return
    if shutil.which(argv[0]) is None:
        typer.echo("Build failed: required tool 'make' is not available", err=True); raise typer.Exit(1)
    try:
        result = run_build(checkout, board_dir, build_dir, target=current.board.target, expected_commit=current.board.micropython_commit)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        typer.echo(f"Build failed: {exc}", err=True); raise typer.Exit(1) from exc
    report = artifact_report(build_dir)
    if not report.get("artifacts"):
        typer.echo("Build failed: make succeeded but no firmware artifact was produced", err=True)
        raise typer.Exit(1)
    _emit({"status": "built", "stdout": result.stdout, "artifacts": report}, "json")


@firmware_app.command("flash")
def flash(
    port: str = typer.Argument(..., help="目标设备串口，如 /dev/ttyUSB0 或 COM3"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="只显示 esptool 命令，不执行刷写",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "--confirm",
        help="确认执行刷写（--confirm 为别名）",
    ),
    image: Path | None = typer.Option(
        None,
        "--image",
        help="显式固件镜像路径；刷写布局仍以 flash_layout.json 为准",
    ),
) -> None:
    """使用 esptool 将构建产物刷写到 ESP32 设备。"""
    firmware = _firmware_config(); build_dir = _path(firmware.get("build_dir"), ".pyrite/firmware/build")
    try:
        lock = load_lock(_config_path())
        lock_firmware = (lock.get("build") or {}).get("firmware") if isinstance(lock.get("build"), dict) else None
        if not isinstance(lock_firmware, dict):
            raise ValueError("firmware lock is missing; run firmware generate")
        current, checkout, board_dir, _ = _plan_from_config()
        if lock_firmware.get("contract_hash") != current.board.contract_hash or lock_firmware.get("commit") != current.board.micropython_commit:
            raise ValueError("pyrite.lock firmware section is stale; run firmware generate")
    except (OSError, ValueError) as exc:
        typer.echo(f"Flash blocked: {exc}", err=True); raise typer.Exit(2) from exc
    artifacts = artifact_report(build_dir).get("artifacts", [])
    images: list[tuple[str, str]] = []
    metadata = build_dir / "flash_layout.json"
    if metadata.is_file():
        try:
            layout = json.loads(metadata.read_text(encoding="utf8"))
            images = [(str(item["offset"]), str(item["path"])) for item in layout.get("images", [])]
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            typer.echo(f"Flash blocked: invalid flash layout metadata: {exc}", err=True); raise typer.Exit(2)
    if not images:
        if image is not None:
            typer.echo("Flash blocked: explicit image has no reliable ESP32 flash address", err=True); raise typer.Exit(2)
        typer.echo("Flash blocked: no reliable multi-image flash layout metadata", err=True); raise typer.Exit(2)
    argv = ["esptool", "--port", port, "write_flash"]
    for offset, path in images:
        argv.extend([offset, path])
    if dry_run:
        typer.echo(json.dumps({"status": "dry-run", "argv": argv}, sort_keys=True)); return
    if not yes:
        typer.echo("Flash not executed: pass --yes/--confirm", err=True); raise typer.Exit(2)
    if shutil.which(argv[0]) is None:
        typer.echo("Flash failed: required tool 'esptool' is not available", err=True); raise typer.Exit(1)
    try:
        subprocess.run(argv, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        typer.echo(f"Flash failed: {exc}", err=True); raise typer.Exit(1) from exc
    typer.echo(f"flashed {len(images)} image(s)")

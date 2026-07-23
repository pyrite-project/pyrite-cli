from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ..utils.board_alias import (
    BoardAlias,
    BoardAliasError,
    BoardAliasStore,
    default_alias_path,
)
from ..utils.ui import print_json
from .common import _FORMAT_OPTION, _JSON_OPTION, _resolve_format, log


board_app = typer.Typer(help="开发板串口别名", add_completion=False)


_ALIAS_FILE_OPTION = typer.Option(
    None,
    "--alias-file",
    help="别名 JSON 文件路径",
)
_PROFILE_FILE_OPTION = typer.Option(
    None,
    "--profile-file",
    help="旧 profile JSON 文件路径, 仅用于兼容迁移",
)


def register(app: typer.Typer) -> None:
    app.add_typer(board_app, name="board")


@board_app.command("register")
def board_register(
    port: str = typer.Argument(..., help="串口号, 如 COM3 或 /dev/ttyUSB0"),
    name: str = typer.Option(..., "--name", "-n", help="开发板别名, 不包含 @"),
    force: bool = typer.Option(False, "--force", "-F", help="覆盖同名别名"),
    alias_file: Optional[Path] = _ALIAS_FILE_OPTION,
    profile_file: Optional[Path] = _PROFILE_FILE_OPTION,
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """注册或更新开发板串口别名。"""

    fmt = _resolve_format(fmt, json_output)
    try:
        alias = _alias_store(alias_file, profile_file).register(port, name=name, overwrite=force)
    except BoardAliasError as exc:
        _exit_alias_error(exc)
    _emit_alias(alias, fmt)


@board_app.command("list")
def board_list(
    alias_file: Optional[Path] = _ALIAS_FILE_OPTION,
    profile_file: Optional[Path] = _PROFILE_FILE_OPTION,
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """列出已注册的开发板别名。"""

    fmt = _resolve_format(fmt, json_output)
    try:
        aliases = _alias_store(alias_file, profile_file).list()
    except BoardAliasError as exc:
        _exit_alias_error(exc)
    if fmt == "json":
        print_json({"aliases": [_alias_payload(alias) for alias in aliases], "count": len(aliases)})
        return
    if not aliases:
        log.info("未注册开发板别名")
        return
    for alias in aliases:
        typer.echo(f"{alias.name}\t{alias.port}")


@board_app.command("show")
def board_show(
    name: str = typer.Argument(..., help="别名名称或 @alias"),
    alias_file: Optional[Path] = _ALIAS_FILE_OPTION,
    profile_file: Optional[Path] = _PROFILE_FILE_OPTION,
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """查看单个开发板别名。"""

    fmt = _resolve_format(fmt, json_output)
    try:
        alias = _alias_store(alias_file, profile_file).show(name)
    except BoardAliasError as exc:
        _exit_alias_error(exc)
    _emit_alias(alias, fmt)


@board_app.command("remove")
def board_remove(
    name: str = typer.Argument(..., help="别名名称或 @alias"),
    alias_file: Optional[Path] = _ALIAS_FILE_OPTION,
    profile_file: Optional[Path] = _PROFILE_FILE_OPTION,
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """删除开发板别名。"""

    fmt = _resolve_format(fmt, json_output)
    try:
        alias = _alias_store(alias_file, profile_file).remove(name)
    except BoardAliasError as exc:
        _exit_alias_error(exc)
    if fmt == "json":
        print_json({"removed": _alias_payload(alias)})
        return
    log.info("已删除开发板别名: %s", alias.name)


@board_app.command("resolve")
def board_resolve(
    value: str = typer.Argument(..., help="@alias 或普通串口号"),
    alias_file: Optional[Path] = _ALIAS_FILE_OPTION,
    profile_file: Optional[Path] = _PROFILE_FILE_OPTION,
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """解析 @alias 为真实串口号。"""

    fmt = _resolve_format(fmt, json_output)
    try:
        port = _alias_store(alias_file, profile_file).resolve(value)
    except BoardAliasError as exc:
        _exit_alias_error(exc)
    if fmt == "json":
        print_json({"input": value, "port": port})
        return
    typer.echo(port)


def _alias_payload(alias: BoardAlias) -> dict[str, str]:
    return alias.to_dict()


def _alias_store(
    alias_file: Optional[Path],
    profile_file: Optional[Path],
) -> BoardAliasStore:
    if alias_file is not None and profile_file is not None:
        raise typer.BadParameter("--alias-file 和 --profile-file 不能同时使用")
    if profile_file is not None:
        return BoardAliasStore(default_alias_path(), legacy_path=profile_file)
    return BoardAliasStore(alias_file)


def _emit_alias(alias: BoardAlias, fmt: str) -> None:
    if fmt == "json":
        print_json(_alias_payload(alias))
        return
    typer.echo(f"name: {alias.name}")
    typer.echo(f"port: {alias.port}")


def _exit_alias_error(exc: BoardAliasError) -> None:
    log.error("%s", exc)
    raise typer.Exit(1) from exc

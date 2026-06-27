from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import typer

from ..utils.board_profile import (
    BoardProfile,
    BoardProfileError,
    BoardProfileStore,
    parse_recommended_items,
)
from ..utils.ui import print_json
from .common import _FORMAT_OPTION, _JSON_OPTION, _resolve_format, log


board_app = typer.Typer(help="Board profiles and serial aliases", add_completion=False)


def register(app: typer.Typer) -> None:
    app.add_typer(board_app, name="board")


@board_app.command("register")
def board_register(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0"),
    name: str = typer.Option(..., "--name", "-n", help="设备别名，不包含 @"),
    tag: Optional[List[str]] = typer.Option(None, "--tag", help="保存一个 board tag，可重复"),
    tags: Optional[str] = typer.Option(None, "--tags", help="逗号分隔的 board tags"),
    firmware: Optional[str] = typer.Option(None, "--firmware", help="固件版本描述"),
    recommended: Optional[List[str]] = typer.Option(
        None,
        "--recommended",
        "-r",
        help="推荐配置，格式 KEY=VALUE，可重复，如 verify=size",
    ),
    force: bool = typer.Option(False, "--force", "-F", help="覆盖同名 profile"),
    profile_file: Optional[Path] = typer.Option(None, "--profile-file", help="profile JSON 文件路径"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """注册或更新常用开发板 profile。"""
    fmt = _resolve_format(fmt, json_output)
    try:
        profile = BoardProfileStore(profile_file).register(
            port,
            name=name,
            tags=_merge_tags(tag or [], tags),
            firmware=firmware,
            recommended=parse_recommended_items(recommended),
            overwrite=force,
        )
    except BoardProfileError as exc:
        _exit_profile_error(exc)
    _emit_profile(profile, fmt)


@board_app.command("list")
def board_list(
    profile_file: Optional[Path] = typer.Option(None, "--profile-file", help="profile JSON 文件路径"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """列出已注册的 board profiles。"""
    fmt = _resolve_format(fmt, json_output)
    try:
        profiles = BoardProfileStore(profile_file).list()
    except BoardProfileError as exc:
        _exit_profile_error(exc)
    if fmt == "json":
        print_json({"profiles": [_profile_payload(p) for p in profiles], "count": len(profiles)})
        return
    if not profiles:
        log.info("未注册 board profile")
        return
    for profile in profiles:
        tags = f" [{', '.join(profile.tags)}]" if profile.tags else ""
        typer.echo(f"{profile.name}\t{profile.port}{tags}")


@board_app.command("show")
def board_show(
    name: str = typer.Argument(..., help="profile 名称或 @alias"),
    profile_file: Optional[Path] = typer.Option(None, "--profile-file", help="profile JSON 文件路径"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """查看单个 board profile。"""
    fmt = _resolve_format(fmt, json_output)
    try:
        profile = BoardProfileStore(profile_file).show(name)
    except BoardProfileError as exc:
        _exit_profile_error(exc)
    _emit_profile(profile, fmt)


@board_app.command("remove")
def board_remove(
    name: str = typer.Argument(..., help="profile 名称或 @alias"),
    profile_file: Optional[Path] = typer.Option(None, "--profile-file", help="profile JSON 文件路径"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """删除 board profile。"""
    fmt = _resolve_format(fmt, json_output)
    try:
        profile = BoardProfileStore(profile_file).remove(name)
    except BoardProfileError as exc:
        _exit_profile_error(exc)
    if fmt == "json":
        print_json({"removed": _profile_payload(profile)})
        return
    log.info("已删除 board profile: %s", profile.name)


@board_app.command("resolve")
def board_resolve(
    value: str = typer.Argument(..., help="@alias 或普通串口号"),
    profile_file: Optional[Path] = typer.Option(None, "--profile-file", help="profile JSON 文件路径"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """解析 @alias 为真实串口号。"""
    fmt = _resolve_format(fmt, json_output)
    try:
        port = BoardProfileStore(profile_file).resolve(value)
    except BoardProfileError as exc:
        _exit_profile_error(exc)
    if fmt == "json":
        print_json({"input": value, "port": port})
        return
    typer.echo(port)


def _merge_tags(repeated: List[str], comma_separated: Optional[str]) -> list[str]:
    values: list[str] = []
    values.extend(repeated)
    if comma_separated:
        values.extend(comma_separated.split(","))
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _profile_payload(profile: BoardProfile) -> dict:
    return profile.to_dict()


def _emit_profile(profile: BoardProfile, fmt: str) -> None:
    if fmt == "json":
        print_json(_profile_payload(profile))
        return
    typer.echo(f"name: {profile.name}")
    typer.echo(f"port: {profile.port}")
    if profile.tags:
        typer.echo(f"tags: {', '.join(profile.tags)}")
    if profile.last_firmware:
        typer.echo(f"firmware: {profile.last_firmware}")
    if profile.recommended:
        typer.echo("recommended:")
        for key, value in sorted(profile.recommended.items()):
            typer.echo(f"  {key}: {value}")


def _exit_profile_error(exc: BoardProfileError) -> None:
    log.error("%s", exc)
    raise typer.Exit(1) from exc

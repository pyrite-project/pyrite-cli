from __future__ import annotations

from typing import Optional

import typer

from .common import (
    DEFAULT_BAUDRATE,
    MicroPython,
    ProjectSyncManager,
    init_stubs,
    new_project_interactive,
    _complete_port,
    _FORMAT_OPTION,
    _JSON_OPTION,
    _mp_factory,
    _norm_path,
    _resolve_format,
    log,
)

# project 子命令组
# ═══════════════════════════════════════════════════════════════════

project_app = typer.Typer(help="项目脚手架、存根、文件哈希与增量刷入", add_completion=False)


def register(app: typer.Typer) -> None:
    app.add_typer(project_app, name="project")


@project_app.command("new")
def project_new(
    project_name: str = typer.Argument(..., help="新项目名称"),
    platform: Optional[str] = typer.Option(
        None, "--platform",
        help="串口号，用于自动检测硬件并下载匹配的 stubs",
    ),
) -> None:
    """创建新 MicroPython 项目目录及脚手架。"""
    new_project_interactive(project_name, platform=platform)


@project_app.command("init")
def project_init(
    hardware: Optional[str] = typer.Argument(None, help="MicroPython 硬件名称"),
    version: Optional[str] = typer.Argument(None, help="固件版本，如 '1.20.0'"),
    variant: Optional[str] = typer.Option(None, "--variant", "-V", help="硬件变体"),
    platform: Optional[str] = typer.Option(
        None, "--platform",
        help="串口号，用于自动检测硬件并下载匹配的 stubs",
    ),
) -> None:
    """在已有项目中下载 MicroPython 类型存根。"""
    init_stubs(hardware, version, variant, platform)


@project_app.command("hash")
def project_hash(
    directory: str = typer.Argument(".", help="项目目录路径"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="哈希配置文件输出路径"),
) -> None:
    """离线扫描项目目录，计算 SHA256 哈希并保存到哈希配置文件。"""
    mp = MicroPython()
    active_tags: set[str] = set()
    if feature:
        active_tags.update(t.strip() for t in feature.split(","))
    if no_feature:
        active_tags.difference_update(t.strip() for t in no_feature.split(","))
    ProjectSyncManager(mp).scan(
        directory, hash_config_path=output,
        active_tags=active_tags or None, manifest_path=manifest,
    )


@project_app.command("scan")
def project_scan(
    directory: str = typer.Argument(".", help="项目目录路径"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="哈希配置文件输出路径"),
) -> None:
    """扫描项目目录，计算 SHA256 哈希并保存到哈希配置文件。"""
    mp = MicroPython()
    active_tags = set()
    if feature:
        active_tags.update(t.strip() for t in feature.split(","))
    if no_feature:
        active_tags.difference_update(t.strip() for t in no_feature.split(","))
    ProjectSyncManager(mp).scan(
        directory, hash_config_path=output,
        active_tags=active_tags or None, manifest_path=manifest,
    )


@project_app.command("flash")
def project_flash(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument("./", help="本地项目目录路径"),
    remote_path: str = typer.Argument("./", help="设备上的远程路径前缀"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    no_compile: bool = typer.Option(False, "--no-compile", help="跳过 mpy 编译"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    hash_config: Optional[str] = typer.Option(None, "--config", "-c", help="哈希配置文件路径"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    dry_run: bool = typer.Option(False, "--dry-run", help="预览模式"),
) -> None:
    """连接设备并根据哈希配置增量刷入新增或变更的文件。"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if no_compile:
            mp.config.auto_compile = False
        ver, arch = mp.get_mpy_version() if not no_compile else (None, None)
        if target:
            active_tags = set(mp.config.board_tags.get(target.upper(), [target.upper()]))
            active_tags.add(target.upper())
        else:
            active_tags = mp.detect_tags()
            if not active_tags:
                log.error("无法识别设备 target，请使用 --target 手动指定")
                raise typer.Exit(1)
        if feature:
            active_tags.update(t.strip() for t in feature.split(","))
        if no_feature:
            active_tags.difference_update(t.strip() for t in no_feature.split(","))
        ProjectSyncManager(mp).flash(
            directory, remote_path, hash_config_path=hash_config,
            bytecode_ver=ver, arch=arch,
            active_tags=active_tags or None,
            manifest_path=manifest, dry_run=dry_run,
        )
    finally:
        mp.disconnect()


@project_app.command("status")
def project_status(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument(..., help="本地项目目录路径"),
    remote_path: str = typer.Argument(..., help="设备上的远程路径前缀"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    hash_config: Optional[str] = typer.Option(None, "--config", "-c", help="哈希配置文件路径"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    diff: bool = typer.Option(False, "--diff", help="download device files and print unified diff"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """连接设备并比对本地哈希与设备文件，显示差异清单（不刷入）。"""
    fmt = _resolve_format(fmt, json_output)
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if target:
            active_tags = set(mp.config.board_tags.get(target.upper(), [target.upper()]))
            active_tags.add(target.upper())
        elif (feature or no_feature):
            active_tags = mp.detect_tags() if not target else set()
        else:
            active_tags = mp.detect_tags()
        if feature:
            active_tags.update(t.strip() for t in feature.split(","))
        if no_feature:
            active_tags.difference_update(t.strip() for t in no_feature.split(","))
        has_diff = ProjectSyncManager(mp).status(
            directory, remote_path, hash_config_path=hash_config,
            active_tags=active_tags or None,
            manifest_path=manifest, fmt=fmt, diff=diff,
        )
    finally:
        mp.disconnect()
    if has_diff:
        raise typer.Exit(1)


@project_app.command("pull")
def project_pull(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument(help="本地项目目录路径（如 . 或 ./bak）"),
    remote_path: str = typer.Argument("/", help="设备上的远程路径前缀", show_default=False),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="预览模式"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """连接设备并按项目配置拉取文件到本地目录。"""
    fmt = _resolve_format(fmt, json_output)
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if target:
            active_tags: Optional[set[str]] = set(
                mp.config.board_tags.get(target.upper(), [target.upper()])
            )
            active_tags.add(target.upper())
        elif feature or no_feature:
            active_tags = set()
        else:
            active_tags = None
        if feature:
            if active_tags is None:
                active_tags = set()
            active_tags.update(t.strip() for t in feature.split(","))
        if no_feature:
            if active_tags is None:
                active_tags = set()
            active_tags.difference_update(t.strip() for t in no_feature.split(","))
        ok = ProjectSyncManager(mp).pull(
            directory, remote_path,
            active_tags=active_tags, manifest_path=manifest,
            dry_run=dry_run, fmt=fmt,
        )
    finally:
        mp.disconnect()
    if ok is False:
        raise typer.Exit(1)


@project_app.command("run")
def project_run(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument("./", help="本地项目目录路径"),
    remote_path: str = typer.Argument("./", help="设备上的远程路径前缀"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    no_compile: bool = typer.Option(False, "--no-compile", help="跳过 mpy 编译"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    hash_config: Optional[str] = typer.Option(None, "--config", "-c", help="哈希配置文件路径"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    dry_run: bool = typer.Option(False, "--dry-run", help="预览模式（仅显示差异，不刷入不进入 REPL）"),
) -> None:
    """增量刷入项目文件后进入交互式 REPL 监控。"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if no_compile:
            mp.config.auto_compile = False
        ver, arch = mp.get_mpy_version() if not no_compile else (None, None)
        if target:
            active_tags = set(mp.config.board_tags.get(target.upper(), [target.upper()]))
            active_tags.add(target.upper())
        else:
            active_tags = mp.detect_tags()
            if not active_tags:
                log.error("无法识别设备 target，请使用 --target 手动指定")
                raise typer.Exit(1)
        if feature:
            active_tags.update(t.strip() for t in feature.split(","))
        if no_feature:
            active_tags.difference_update(t.strip() for t in no_feature.split(","))

        ProjectSyncManager(mp).flash(
            directory, remote_path, hash_config_path=hash_config,
            bytecode_ver=ver, arch=arch,
            active_tags=active_tags or None,
            manifest_path=manifest, dry_run=dry_run,
        )

        if not dry_run:
            log.info("刷入完成，进入 REPL 监控...")
            mp.repl_()
    finally:
        mp.disconnect()


@project_app.command("dev")
def project_dev(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument("./", help="本地项目目录路径"),
    remote_path: str = typer.Argument("./", help="设备上的远程路径前缀"),
    baudrate: int = typer.Option(DEFAULT_BAUDRATE, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    no_compile: bool = typer.Option(False, "--no-compile", help="跳过 mpy 编译"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    hash_config: Optional[str] = typer.Option(None, "--config", "-c", help="哈希配置文件路径"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    dry_run: bool = typer.Option(False, "--dry-run", help="预览模式"),
    auto_run: bool = typer.Option(False, "--run", help="每次成功刷入后软重启，按 boot.py/main.py 正常启动"),
    no_repl: bool = typer.Option(False, "--no-repl", help="只监听和刷入，不进入交互 REPL"),
    once: bool = typer.Option(False, "--once", help="执行一轮同步后退出（适合测试/CI）"),
    poll_interval: float = typer.Option(0.3, "--poll-interval", help="文件轮询间隔秒数"),
    debounce: float = typer.Option(0.5, "--debounce", help="文件变化稳定等待秒数"),
    on_error: str = typer.Option("continue", "--on-error", help="错误策略: continue | stop | keep-repl"),
) -> None:
    """持续监听项目变化，增量刷入，并打开调试 REPL。"""
    if on_error not in {"continue", "stop", "keep-repl"}:
        log.error("--on-error 必须是 continue、stop 或 keep-repl")
        raise typer.Exit(2)
    from ..project.dev import DevOptions, run_project_dev

    run_project_dev(
        DevOptions(
            port=port,
            local_dir=directory,
            remote_path=_norm_path(remote_path),
            baudrate=baudrate,
            timeout=timeout,
            no_compile=no_compile,
            target=target,
            feature=feature,
            no_feature=no_feature,
            manifest_path=manifest,
            hash_config_path=hash_config,
            ws=ws,
            password=password,
            dry_run=dry_run,
            auto_run=auto_run,
            no_repl=no_repl,
            once=once,
            poll_interval=poll_interval,
            debounce=debounce,
            on_error=on_error,
        ),
        mp_factory=_mp_factory,
    )


# ═══════════════════════════════════════════════════════════════════

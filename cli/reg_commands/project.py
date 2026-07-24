from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .common import (
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
from ..utils.config import _load_config
from ..utils.device_context import (
    CommandNeeds,
    command_needs,
    needs_no_mpy,
    needs_with_flash_verify_feature,
    prepare_device,
    resolve_active_tags,
    split_tags,
)
from ..utils.pipes import read_jsonl, record_text

# project 子命令组
# ═══════════════════════════════════════════════════════════════════

project_app = typer.Typer(help="项目脚手架、存根、文件哈希与增量刷入", add_completion=False)


PROJECT_FLASH_NEEDS = CommandNeeds(
    connection=True,
    raw_repl=True,
    repl_preempt=True,
    device_context=True,
    active_tags=True,
    mpy_version=True,
    precheck_version=True,
)

PROJECT_STATUS_NEEDS = CommandNeeds(
    connection=True,
    raw_repl=True,
    repl_preempt=False,
    device_context=True,
    active_tags=True,
)


def register(app: typer.Typer) -> None:
    app.add_typer(project_app, name="project")


def _apply_feature_options(
    active_tags: set[str],
    feature: Optional[str],
    no_feature: Optional[str],
) -> None:
    active_tags.update(split_tags(feature))
    active_tags.difference_update(split_tags(no_feature))


def _run_precheck_or_exit(
    entries,
    check: Optional[str],
    no_check: bool,
    active_tags: Optional[set[str]] = None,
    mp_version: Optional[str] = None,
) -> None:
    if no_check:
        return
    from ..utils.precheck import PrecheckError, run_precheck, validate_precheck_mode

    cfg = _load_config()
    try:
        mode = validate_precheck_mode(check if check is not None else cfg.precheck)
        report = run_precheck(
            entries,
            mode=mode,
            compat=cfg.precheck_compat,
            active_tags=active_tags,
            mp_version=mp_version if mp_version is not None else cfg.precheck_mp_version,
        )
    except ValueError as exc:
        log.error("%s", exc, exc_info=False)
        raise typer.Exit(2) from None
    except PrecheckError as exc:
        log.error("precheck failed:\n%s", exc, exc_info=False)
        raise typer.Exit(1) from None
    for item in report.warnings:
        log.warning("%s", item.format())


def _consume_check_option(args: list[str]) -> Optional[str]:
    check: Optional[str] = None
    leftovers: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--check":
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                check = args[i + 1]
                i += 2
            else:
                check = "basic"
                i += 1
            continue
        if arg.startswith("--check="):
            check = arg.split("=", 1)[1] or "basic"
            i += 1
            continue
        leftovers.append(arg)
        i += 1
    if leftovers:
        log.error("unknown option(s): %s", " ".join(leftovers))
        raise typer.Exit(2)
    return check


def _check_project_manifest_lock(
    manifest: str,
    directory: str,
    lockfile: str,
    active_tags: set[str],
    target: Optional[str],
    auto_compile: bool,
) -> None:
    from ..utils.build import ManifestLockError, check_manifest_lock_current

    lock_path = Path(lockfile)
    if not lock_path.is_absolute():
        lock_path = Path(directory) / lock_path
    try:
        check_manifest_lock_current(
            manifest,
            active_tags=active_tags,
            base_dir=directory,
            lock_path=lock_path,
            target=target,
            build_settings={"auto_compile": auto_compile},
        )
    except (FileNotFoundError, OSError, ValueError, ManifestLockError) as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc


def _resolve_dev_deep_options(
    *,
    deep: bool,
    auto_run: Optional[bool],
    map_traceback: Optional[bool],
) -> tuple[bool, bool]:
    return (
        deep if auto_run is None else auto_run,
        deep if map_traceback is None else map_traceback,
    )


@project_app.command("new")
def project_new(
    project_name: str = typer.Argument(..., help="新项目名称"),
    platform: Optional[str] = typer.Option(
        None, "--platform",
        help="直接指定 MicroPython 平台，如 esp32 或 rp2",
    ),
    port: Optional[str] = typer.Option(
        None, "--port", "-p",
        help="串口号，用于自动检测平台和固件版本",
        autocompletion=_complete_port,
    ),
    baudrate: Optional[int] = typer.Option(
        None, "--baudrate", "-b", help="设备探测波特率", envvar="PYRITE_BAUDRATE",
    ),
    timeout: Optional[int] = typer.Option(
        None, "--timeout", "-t", help="设备探测超时秒数", envvar="PYRITE_TIMEOUT",
    ),
) -> None:
    """创建新 MicroPython 项目目录及脚手架。"""
    if platform and port:
        raise typer.BadParameter("--platform 和 --port 不能同时使用")
    new_project_interactive(
        project_name,
        platform=platform,
        port=port,
        baudrate=baudrate,
        timeout=timeout,
    )


@project_app.command("init")
def project_init(
    hardware: Optional[str] = typer.Argument(None, help="MicroPython 硬件名称"),
    version: Optional[str] = typer.Argument(None, help="固件版本，如 '1.20.0'"),
    variant: Optional[str] = typer.Option(None, "--variant", "-V", help="硬件变体"),
    port: Optional[str] = typer.Option(
        None, "--port", "-p",
        help="串口号，用于自动检测硬件并下载匹配的 stubs",
        autocompletion=_complete_port,
    ),
    baudrate: Optional[int] = typer.Option(
        None, "--baudrate", "-b", help="设备探测波特率", envvar="PYRITE_BAUDRATE",
    ),
    timeout: Optional[int] = typer.Option(
        None, "--timeout", "-t", help="设备探测超时秒数", envvar="PYRITE_TIMEOUT",
    ),
) -> None:
    """在已有项目中下载 MicroPython 类型存根。"""
    init_stubs(
        hardware,
        version,
        variant,
        port,
        baudrate=baudrate,
        timeout=timeout,
    )


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
    active_tags = split_tags(feature)
    active_tags.difference_update(split_tags(no_feature))
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
    active_tags = split_tags(feature)
    active_tags.difference_update(split_tags(no_feature))
    ProjectSyncManager(mp).scan(
        directory, hash_config_path=output,
        active_tags=active_tags or None, manifest_path=manifest,
    )


@project_app.command("flash", context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@command_needs(PROJECT_FLASH_NEEDS)
def project_flash(
    ctx: typer.Context,
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument("./", help="本地项目目录路径"),
    remote_path: str = typer.Argument("./", help="设备上的远程路径前缀"),
    baudrate: Optional[int] = typer.Option(None, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: Optional[int] = typer.Option(None, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    no_compile: bool = typer.Option(False, "--no-compile", help="跳过 mpy 编译"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    mp_version: Optional[str] = typer.Option(None, "--mp-version", help="目标 MicroPython 固件版本，用于 strict 兼容性预检查"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    hash_config: Optional[str] = typer.Option(None, "--config", "-c", help="哈希配置文件路径"),
    changed_from: Optional[str] = typer.Option(None, "--changed-from", help="JSONL 变更清单路径，或 - 从 stdin 读取"),
    locked: bool = typer.Option(False, "--locked", help="要求 manifest 与 pyrite.lock 一致后才刷入"),
    lockfile: str = typer.Option("pyrite.lock", "--lockfile", help="lockfile 路径；相对路径基于项目目录"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    dry_run: bool = typer.Option(False, "--dry-run", help="预览模式"),
    snapshot_before: Optional[str] = typer.Option(
        None,
        "--snapshot-before",
        help="刷入前保存设备文件系统快照到 .pyrite_snapshots/<name>/",
    ),
    no_check: bool = typer.Option(False, "--no-check", help="跳过刷入前预检查"),
) -> None:
    """连接设备并根据哈希配置增量刷入新增或变更的文件。

    预检查可用 --check、--check=basic|strict 或 --no-check 控制。
    """
    remote_path = _norm_path(remote_path)
    check = _consume_check_option(list(ctx.args))
    changed_paths = _read_changed_paths_jsonl(changed_from) if changed_from else None
    if locked and manifest is None:
        manifest = str(Path(directory) / "manifest.py")
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    lock_checked = False
    if locked and target:
        active_tags = resolve_active_tags(
            mp,
            target=target,
            feature=feature,
            no_feature=no_feature,
        )
        _check_project_manifest_lock(
            manifest,
            directory,
            lockfile,
            active_tags,
            target,
            auto_compile=bool(mp.config.auto_compile and not no_compile),
        )
        lock_checked = True
    try:
        needs = PROJECT_FLASH_NEEDS if not no_compile else needs_no_mpy(PROJECT_FLASH_NEEDS)
        needs = needs_with_flash_verify_feature(needs, mp.config)
        prepared = prepare_device(
            mp,
            needs,
            target=target,
            feature=feature,
            no_feature=no_feature,
            explicit_mp_version=mp_version,
        )
        if snapshot_before:
            from .snapshot import save_device_snapshot

            manifest_snapshot = save_device_snapshot(
                mp,
                name=snapshot_before,
                port=port,
                remote_path="/",
            )
            log.info(
                "刷入前快照已保存: %s (%d files)",
                manifest_snapshot.name,
                len(manifest_snapshot.files),
            )
        active_tags = prepared.active_tags or set()
        if not no_check:
            from ..utils.precheck import collect_project_precheck_entries

            _run_precheck_or_exit(
                collect_project_precheck_entries(
                    directory,
                    remote_path,
                    hash_config_path=hash_config,
                    active_tags=active_tags,
                    manifest_path=manifest,
                ),
                check,
                no_check,
                active_tags=active_tags,
                mp_version=prepared.precheck_mp_version,
            )
        if no_compile:
            mp.config.auto_compile = False
        if locked and not lock_checked:
            _check_project_manifest_lock(
                manifest,
                directory,
                lockfile,
                active_tags or set(),
                target,
                auto_compile=bool(mp.config.auto_compile and not no_compile),
            )
        ProjectSyncManager(mp).flash(
            directory, remote_path, hash_config_path=hash_config,
            bytecode_ver=prepared.bytecode_ver, arch=prepared.arch,
            active_tags=active_tags or None,
            manifest_path=manifest, dry_run=dry_run,
            changed_paths=changed_paths,
        )
    finally:
        mp.disconnect()


@project_app.command("status")
@command_needs(PROJECT_STATUS_NEEDS)
def project_status(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument(..., help="本地项目目录路径"),
    remote_path: str = typer.Argument(..., help="设备上的远程路径前缀"),
    baudrate: Optional[int] = typer.Option(None, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: Optional[int] = typer.Option(None, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
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
        prepared = prepare_device(
            mp,
            PROJECT_STATUS_NEEDS,
            target=target,
            feature=feature,
            no_feature=no_feature,
        )
        active_tags = prepared.active_tags or set()
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
    baudrate: Optional[int] = typer.Option(None, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: Optional[int] = typer.Option(None, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="预览模式"),
    stdout_jsonl: bool = typer.Option(False, "--stdout-jsonl", help="将设备文件内容作为 JSONL 输出到 stdout，不写本地文件"),
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
            active_tags.update(split_tags(feature))
        if no_feature:
            if active_tags is None:
                active_tags = set()
            active_tags.difference_update(split_tags(no_feature))
        manager = ProjectSyncManager(mp)
        if stdout_jsonl:
            ok = manager.pull_stdout_jsonl(
                directory, remote_path,
                active_tags=active_tags,
                manifest_path=manifest,
            )
        else:
            ok = manager.pull(
                directory, remote_path,
                active_tags=active_tags, manifest_path=manifest,
                dry_run=dry_run, fmt=fmt,
            )
    finally:
        mp.disconnect()
    if ok is False:
        raise typer.Exit(1)


def _read_changed_paths_jsonl(path: str) -> set[str]:
    changed: set[str] = set()
    failed = False
    for item in read_jsonl(path):
        record = item.data
        if record.get("_invalid"):
            log.error("changed-from line %d: %s", item.line, record.get("error", "invalid record"))
            failed = True
            continue
        value = record_text(record, "local", "path", "file")
        if not value:
            log.error("changed-from line %d: missing local/path", item.line)
            failed = True
            continue
        changed.add(value)
    if failed:
        raise typer.Exit(1)
    return changed


@project_app.command("run")
@command_needs(PROJECT_FLASH_NEEDS)
def project_run(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument("./", help="本地项目目录路径"),
    remote_path: str = typer.Argument("./", help="设备上的远程路径前缀"),
    baudrate: Optional[int] = typer.Option(None, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: Optional[int] = typer.Option(None, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
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
        if no_compile:
            mp.config.auto_compile = False
        needs = PROJECT_FLASH_NEEDS if not no_compile else needs_no_mpy(PROJECT_FLASH_NEEDS)
        needs = needs_with_flash_verify_feature(needs, mp.config)
        prepared = prepare_device(
            mp,
            needs,
            target=target,
            feature=feature,
            no_feature=no_feature,
        )
        active_tags = prepared.active_tags or set()

        ProjectSyncManager(mp).flash(
            directory, remote_path, hash_config_path=hash_config,
            bytecode_ver=prepared.bytecode_ver, arch=prepared.arch,
            active_tags=active_tags or None,
            manifest_path=manifest, dry_run=dry_run,
        )

        if not dry_run:
            log.info("刷入完成，进入 REPL 监控...")
            mp.repl_()
    finally:
        mp.disconnect()


@project_app.command("dev", context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@command_needs(PROJECT_FLASH_NEEDS)
def project_dev(
    ctx: typer.Context,
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    directory: str = typer.Argument("./", help="本地项目目录路径"),
    remote_path: str = typer.Argument("./", help="设备上的远程路径前缀"),
    baudrate: Optional[int] = typer.Option(None, "--baudrate", "-b", help="波特率", envvar="PYRITE_BAUDRATE"),
    timeout: Optional[int] = typer.Option(None, "--timeout", "-t", help="超时秒数", envvar="PYRITE_TIMEOUT"),
    no_compile: bool = typer.Option(False, "--no-compile", help="跳过 mpy 编译"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    hash_config: Optional[str] = typer.Option(None, "--config", "-c", help="哈希配置文件路径"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
    dry_run: bool = typer.Option(False, "--dry-run", help="预览模式"),
    deep: bool = typer.Option(False, "--deep", help="启用深度开发会话：默认刷入后运行并映射 traceback"),
    auto_run: Optional[bool] = typer.Option(None, "--run/--no-run", help="每次成功刷入后软重启，按 boot.py/main.py 正常启动"),
    no_repl: bool = typer.Option(False, "--no-repl", help="只监听和刷入，不进入交互 REPL"),
    map_traceback: Optional[bool] = typer.Option(None, "--map-traceback/--no-map-traceback", help="将设备 traceback 路径映射到本地源码"),
    lens: bool = typer.Option(False, "--lens", help="展开 traceback 对应的本地源码上下文"),
    open_editor: bool = typer.Option(False, "--open-editor", help="与 --lens 配合，尝试打开首个匹配源码位置"),
    once: bool = typer.Option(False, "--once", help="执行一轮同步后退出（适合测试/CI）"),
    poll_interval: float = typer.Option(0.3, "--poll-interval", help="文件轮询间隔秒数"),
    debounce: float = typer.Option(0.5, "--debounce", help="文件变化稳定等待秒数"),
    on_error: str = typer.Option("continue", "--on-error", help="错误策略: continue | stop | keep-repl"),
    test_on_save_option: str = typer.Option(
        "off",
        "--test-on-save",
        help="保存并成功刷入后运行设备端测试: all | changed | off",
    ),
    test_path: Optional[str] = typer.Option(None, "--test-path", help="设备端测试目录或单个 .py 文件，默认 test_device/"),
    test_timeout: int = typer.Option(10, "--test-timeout", min=1, help="设备端测试执行超时秒数"),
) -> None:
    """持续监听项目变化，增量刷入，并打开调试 REPL。"""
    if on_error not in {"continue", "stop", "keep-repl"}:
        log.error("--on-error 必须是 continue、stop 或 keep-repl")
        raise typer.Exit(2)
    if ctx.args:
        log.error("unknown option(s): %s", " ".join(ctx.args))
        raise typer.Exit(2)
    from ..project.dev import DevOptions, normalize_test_on_save, run_project_dev

    try:
        test_on_save = normalize_test_on_save(test_on_save_option)
    except ValueError as exc:
        log.error("%s", exc)
        raise typer.Exit(2) from None

    resolved_auto_run, resolved_map_traceback = _resolve_dev_deep_options(
        deep=deep,
        auto_run=auto_run,
        map_traceback=map_traceback,
    )
    if lens:
        resolved_map_traceback = True
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
            auto_run=resolved_auto_run,
            no_repl=no_repl,
            map_traceback=resolved_map_traceback,
            lens=lens,
            open_editor=open_editor,
            once=once,
            poll_interval=poll_interval,
            debounce=debounce,
            on_error=on_error,
            test_on_save=test_on_save,
            test_path=test_path,
            test_timeout=test_timeout,
        ),
        mp_factory=_mp_factory,
    )


# ═══════════════════════════════════════════════════════════════════

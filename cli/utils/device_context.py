"""Shared device context discovery and command preparation helpers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from .config import _load_config
from .log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class DeviceContext:
    implementation: Optional[str] = None
    version: Optional[str] = None
    platform: Optional[str] = None
    machine: Optional[str] = None
    release: Optional[str] = None
    sysname: Optional[str] = None
    mpy_version: Optional[int] = None
    arch: Optional[str] = None

    @classmethod
    def from_runtime_info(cls, runtime_info: Any) -> "DeviceContext":
        def text(name: str) -> Optional[str]:
            value = getattr(runtime_info, name, None)
            return value if isinstance(value, str) and value.strip() else None

        mpy_value = getattr(runtime_info, "mpy_version", None)
        mpy_version = mpy_value if isinstance(mpy_value, int) else None
        return cls(
            implementation=text("implementation"),
            version=text("version"),
            platform=text("platform"),
            machine=text("machine"),
            release=text("release"),
            sysname=text("sysname"),
            mpy_version=mpy_version,
            arch=text("arch"),
        )


@dataclass(frozen=True)
class CommandNeeds:
    connection: bool = False
    raw_repl: bool = False
    repl_preempt: bool = False
    repl_soft_reset_fallback: bool = True
    repl_boot_preempt_fallback: bool = True
    device_context: bool = False
    active_tags: bool = False
    mpy_version: bool = False
    precheck_version: bool = False
    board_extra_info: bool = False
    capability_probe: bool = False


@dataclass(frozen=True)
class PreparedDevice:
    mp: Any
    needs: CommandNeeds
    device_context: Optional[DeviceContext] = None
    active_tags: Optional[set[str]] = None
    bytecode_ver: Optional[int] = None
    arch: Optional[str] = None
    precheck_mp_version: Optional[str] = None


def command_needs(needs: CommandNeeds) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Attach pyrite device preparation metadata to a Typer command callback."""

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        setattr(func, "__pyrite_needs__", needs)
        return func

    return decorate


def command_needs_of(func: Callable[..., Any]) -> CommandNeeds:
    return getattr(func, "__pyrite_needs__", CommandNeeds())


def needs_without(needs: CommandNeeds, **updates: bool) -> CommandNeeds:
    return replace(needs, **updates)


def needs_no_mpy(needs: CommandNeeds) -> CommandNeeds:
    return replace(needs, mpy_version=False)


def split_tags(value: Optional[str]) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def resolve_active_tags(
    mp: Any,
    *,
    target: Optional[str] = None,
    feature: Optional[str] = None,
    no_feature: Optional[str] = None,
    require_detected: bool = True,
) -> set[str]:
    """Resolve target/feature options into the active board tag set."""
    if target:
        normalized = target.upper()
        active_tags = set(mp.config.board_tags.get(normalized, [normalized]))
        active_tags.add(normalized)
    else:
        active_tags = set(mp.detect_tags())
        if require_detected and not active_tags:
            raise RuntimeError("无法识别设备 target，请使用 --target 手动指定")
    active_tags.update(split_tags(feature))
    active_tags.difference_update(split_tags(no_feature))
    return active_tags


def resolve_precheck_mp_version(
    *,
    explicit_version: Optional[str],
    context: Optional[DeviceContext],
    config: Any = None,
) -> Optional[str]:
    if explicit_version is not None:
        return explicit_version
    if isinstance(getattr(context, "version", None), str) and context.version.strip():
        return context.version
    cfg = config if config is not None else _load_config()
    configured = getattr(cfg, "precheck_mp_version", None)
    return configured if isinstance(configured, str) and configured.strip() else None


def _is_connected(mp: Any) -> bool:
    try:
        value = getattr(mp, "is_connected")
    except Exception:
        return False
    return isinstance(value, bool) and value


def _real_method(mp: Any, name: str) -> Optional[Callable[..., Any]]:
    method = getattr(mp, name, None)
    if not callable(method):
        return None
    if callable(getattr(type(mp), name, None)):
        return method
    if name in getattr(mp, "_mock_children", {}):
        return_value = getattr(method, "return_value", None)
        if isinstance(return_value, DeviceContext):
            return method
        return None
    if name in vars(mp):
        return method
    return None


def _context_from_mp(mp: Any) -> DeviceContext:
    return DeviceContext.from_runtime_info(getattr(mp, "runtime_info", None))


def prepare_device(
    mp: Any,
    needs: CommandNeeds,
    *,
    target: Optional[str] = None,
    feature: Optional[str] = None,
    no_feature: Optional[str] = None,
    explicit_mp_version: Optional[str] = None,
    require_detected_tags: bool = True,
) -> PreparedDevice:
    """Prepare a device according to command metadata and return shared context."""
    steps: list[str] = []
    log.debug("命令设备需求: %s", needs)

    if needs.connection and not _is_connected(mp):
        mp.connect()
        steps.append("connect")

    if needs.raw_repl:
        enter_raw = getattr(mp, "_enter_raw_repl", None)
        if callable(enter_raw):
            enter_raw(
                preempt=needs.repl_preempt,
                soft_reset_fallback=needs.repl_soft_reset_fallback,
                boot_preempt_fallback=needs.repl_boot_preempt_fallback,
            )
            steps.append("raw_repl")
        else:
            log.debug("设备对象没有 _enter_raw_repl，跳过 Raw REPL 准备")

    device_context: Optional[DeviceContext] = None
    if needs.device_context:
        ensure = _real_method(mp, "ensure_device_context")
        if ensure is not None:
            device_context = ensure()
        else:
            if not needs.raw_repl:
                mp._enter_raw_repl(
                    preempt=needs.repl_preempt,
                    soft_reset_fallback=needs.repl_soft_reset_fallback,
                    boot_preempt_fallback=needs.repl_boot_preempt_fallback,
                )
                steps.append("raw_repl")
            device_context = _context_from_mp(mp)
        steps.append("device_context")

    active_tags: Optional[set[str]] = None
    if needs.active_tags:
        active_tags = resolve_active_tags(
            mp,
            target=target,
            feature=feature,
            no_feature=no_feature,
            require_detected=require_detected_tags,
        )
        steps.append("active_tags")

    bytecode_ver: Optional[int] = None
    arch: Optional[str] = None
    if needs.mpy_version:
        bytecode_ver, arch = mp.get_mpy_version()
        steps.append("mpy_version")

    precheck_mp_version: Optional[str] = None
    if needs.precheck_version:
        precheck_mp_version = resolve_precheck_mp_version(
            explicit_version=explicit_mp_version,
            context=device_context or _context_from_mp(mp),
            config=getattr(mp, "config", None),
        )
        steps.append("precheck_version")

    log.debug("设备准备步骤: %s", ", ".join(steps) if steps else "none")
    return PreparedDevice(
        mp=mp,
        needs=needs,
        device_context=device_context,
        active_tags=active_tags,
        bytecode_ver=bytecode_ver,
        arch=arch,
        precheck_mp_version=precheck_mp_version,
    )

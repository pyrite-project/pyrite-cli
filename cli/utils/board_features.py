"""Runtime-observable board feature probes and CLI feature dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence


@dataclass(frozen=True)
class BoardFeatureStatus:
    id: str
    category: str
    status: str
    confidence: str
    macro_hint: str
    probe: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "category": self.category,
            "status": self.status,
            "confidence": self.confidence,
            "macro_hint": self.macro_hint,
            "probe": self.probe,
        }


@dataclass(frozen=True)
class BoardFeatureProbe:
    id: str
    category: str
    confidence: str
    macro_hint: str
    probe: str
    script: str
    default: bool = True


@dataclass(frozen=True)
class CliFeatureDependency:
    cli_feature: str
    board_feature: str
    required: bool = True
    fallback: Optional[str] = None


@dataclass(frozen=True)
class CapabilityNotice:
    cli_feature: str
    board_feature: str
    status: str
    required: bool
    fallback: Optional[str] = None

    @property
    def message(self) -> str:
        if self.fallback:
            return (
                f"{self.fallback}: {self.cli_feature} 缺少设备能力 "
                f"{self.board_feature} (status={self.status})"
            )
        return (
            f"{self.cli_feature} 缺少设备能力 "
            f"{self.board_feature} (status={self.status})"
        )


class BoardFeatureRegistry:
    """Registry for board feature probes and CLI feature dependency edges."""

    def __init__(self) -> None:
        self._probes: dict[str, BoardFeatureProbe] = {}
        self._dependencies: dict[str, list[CliFeatureDependency]] = {}

    def register_probe(self, probe: BoardFeatureProbe) -> BoardFeatureProbe:
        if probe.id in self._probes:
            raise ValueError(f"board feature probe already registered: {probe.id}")
        self._probes[probe.id] = probe
        return probe

    def register_import_probe(
        self,
        feature_id: str,
        *,
        module: Optional[str] = None,
        category: str,
        macro_hint: str,
        default: bool = True,
    ) -> BoardFeatureProbe:
        module_name = module or feature_id
        probe_text = f"import {module_name}"
        script = (
            "_pyrite_import_probe("
            f"{feature_id!r},{module_name!r},{category!r},{macro_hint!r},{probe_text!r}"
            ")"
        )
        return self.register_probe(BoardFeatureProbe(
            id=feature_id,
            category=category,
            confidence="import-probe",
            macro_hint=macro_hint,
            probe=probe_text,
            script=script,
            default=default,
        ))

    def register_hasattr_probe(
        self,
        feature_id: str,
        *,
        module: str,
        attr: str,
        category: str,
        macro_hint: str,
        default: bool = True,
    ) -> BoardFeatureProbe:
        if "." in attr:
            parent, child = attr.rsplit(".", 1)
            probe_text = f"hasattr({module}.{parent}, {child!r})"
        else:
            probe_text = f"hasattr({module}, {attr!r})"
        script = (
            "_pyrite_hasattr_probe("
            f"{feature_id!r},{module!r},{attr!r},{category!r},{macro_hint!r},{probe_text!r}"
            ")"
        )
        return self.register_probe(BoardFeatureProbe(
            id=feature_id,
            category=category,
            confidence="hasattr-probe",
            macro_hint=macro_hint,
            probe=probe_text,
            script=script,
            default=default,
        ))

    def register_script_probe(
        self,
        feature_id: str,
        *,
        category: str,
        confidence: str,
        macro_hint: str,
        probe: str,
        script: str,
        default: bool = True,
    ) -> BoardFeatureProbe:
        return self.register_probe(BoardFeatureProbe(
            id=feature_id,
            category=category,
            confidence=confidence,
            macro_hint=macro_hint,
            probe=probe,
            script=script,
            default=default,
        ))

    def register_cli_dependency(
        self,
        cli_feature: str,
        board_feature: str,
        *,
        required: bool = True,
        fallback: Optional[str] = None,
    ) -> CliFeatureDependency:
        if board_feature not in self._probes:
            raise ValueError(f"unknown board feature for dependency: {board_feature}")
        dep = CliFeatureDependency(
            cli_feature=cli_feature,
            board_feature=board_feature,
            required=required,
            fallback=fallback,
        )
        self._dependencies.setdefault(cli_feature, []).append(dep)
        return dep

    def probe(self, feature_id: str) -> BoardFeatureProbe:
        try:
            return self._probes[feature_id]
        except KeyError as exc:
            raise KeyError(f"unknown board feature probe: {feature_id}") from exc

    def probes_for(self, feature_ids: Optional[Iterable[str]] = None) -> tuple[BoardFeatureProbe, ...]:
        if feature_ids is None:
            return tuple(probe for probe in self._probes.values() if probe.default)
        return tuple(self.probe(feature_id) for feature_id in _dedupe(feature_ids))

    def default_feature_ids(self) -> tuple[str, ...]:
        return tuple(probe.id for probe in self._probes.values() if probe.default)

    def dependencies_for(self, cli_features: Iterable[str]) -> tuple[CliFeatureDependency, ...]:
        deps: list[CliFeatureDependency] = []
        for cli_feature in _dedupe(cli_features):
            deps.extend(self._dependencies.get(cli_feature, ()))
        return tuple(deps)

    def feature_ids_for_cli_features(self, cli_features: Iterable[str]) -> tuple[str, ...]:
        return tuple(_dedupe(dep.board_feature for dep in self.dependencies_for(cli_features)))


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


_PROBE_HEADER = r"""
def _pyrite_clean(v):
    return str(v).replace("|", "/").replace("\r", " ").replace("\n", " ")

def _pyrite_feature(name, category, status, confidence, macro_hint, probe):
    print("FEATURE|" + _pyrite_clean(name) + "|" + _pyrite_clean(category) + "|" + _pyrite_clean(status) + "|" + _pyrite_clean(confidence) + "|" + _pyrite_clean(macro_hint) + "|" + _pyrite_clean(probe))

def _pyrite_supported(value):
    return "supported" if value else "unsupported"

def _pyrite_import_probe(feature_id, module_name, category, macro_hint, probe):
    try:
        __import__(module_name)
        _pyrite_feature(feature_id, category, "supported", "import-probe", macro_hint, probe)
    except Exception:
        _pyrite_feature(feature_id, category, "unsupported", "import-probe", macro_hint, probe)

def _pyrite_getattr_path(obj, attr_path):
    cur = obj
    for part in attr_path.split("."):
        cur = getattr(cur, part)
    return cur

def _pyrite_hasattr_probe(feature_id, module_name, attr_path, category, macro_hint, probe):
    try:
        mod = __import__(module_name)
        target = mod
        parts = attr_path.split(".")
        for part in parts[:-1]:
            target = getattr(target, part)
        ok = hasattr(target, parts[-1])
        _pyrite_feature(feature_id, category, _pyrite_supported(ok), "hasattr-probe", macro_hint, probe)
    except Exception:
        _pyrite_feature(feature_id, category, "unsupported", "hasattr-probe", macro_hint, probe)
"""


def build_probe_script(
    feature_ids: Optional[Iterable[str]] = None,
    *,
    registry: BoardFeatureRegistry | None = None,
) -> str:
    reg = registry or DEFAULT_REGISTRY
    probes = reg.probes_for(feature_ids)
    if not probes:
        return ""
    lines = [
        "print('PYRITE_FEATURES_BEGIN')",
        _PROBE_HEADER.strip(),
    ]
    lines.extend(probe.script.strip() for probe in probes)
    lines.append("print('PYRITE_FEATURES_END')")
    return "\n".join(lines) + "\n"


def parse_board_feature_output(output: str) -> tuple[BoardFeatureStatus, ...]:
    features: list[BoardFeatureStatus] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("PYRITE_FEATURES_"):
            continue
        parts = line.split("|")
        if parts[0] != "FEATURE" or len(parts) < 7:
            continue
        features.append(BoardFeatureStatus(
            id=parts[1],
            category=parts[2],
            status=parts[3],
            confidence=parts[4],
            macro_hint=parts[5],
            probe="|".join(parts[6:]),
        ))
    return tuple(features)


def probe_board_features(
    mp: Any,
    feature_ids: Optional[Iterable[str]] = None,
    *,
    registry: BoardFeatureRegistry | None = None,
    timeout: int = 30,
) -> tuple[BoardFeatureStatus, ...]:
    script = build_probe_script(feature_ids, registry=registry)
    if not script:
        return ()
    output = mp.run(script, timeout=timeout)
    return parse_board_feature_output(output)


def evaluate_cli_feature_requirements(
    features: Sequence[BoardFeatureStatus],
    cli_features: Iterable[str],
    *,
    registry: BoardFeatureRegistry | None = None,
) -> tuple[CapabilityNotice, ...]:
    reg = registry or DEFAULT_REGISTRY
    by_id = {feature.id: feature for feature in features}
    notices: list[CapabilityNotice] = []
    missing_required: list[CapabilityNotice] = []

    for dep in reg.dependencies_for(cli_features):
        feature = by_id.get(dep.board_feature)
        status = feature.status if feature is not None else "unknown"
        if status == "supported":
            continue
        notice = CapabilityNotice(
            cli_feature=dep.cli_feature,
            board_feature=dep.board_feature,
            status=status,
            required=dep.required,
            fallback=dep.fallback,
        )
        if dep.required:
            missing_required.append(notice)
        else:
            notices.append(notice)

    if missing_required:
        detail = "; ".join(notice.message for notice in missing_required)
        raise RuntimeError(f"设备缺少必需能力: {detail}")
    return tuple(notices)


DEFAULT_REGISTRY = BoardFeatureRegistry()


def _register_builtin_board_features(registry: BoardFeatureRegistry) -> None:
    registry.register_hasattr_probe(
        "os.statvfs",
        module="os",
        attr="statvfs",
        category="filesystem",
        macro_hint="MICROPY_PY_OS_STATVFS",
    )
    registry.register_hasattr_probe(
        "gc.mem_free",
        module="gc",
        attr="mem_free",
        category="memory",
        macro_hint="MICROPY_PY_GC",
    )
    for name, macro in (
        ("kbd_intr", "MICROPY_KBD_EXCEPTION"),
        ("mem_info", "MICROPY_PY_MICROPYTHON_MEM_INFO"),
        ("stack_use", "MICROPY_PY_MICROPYTHON_STACK_USE"),
        ("schedule", "MICROPY_ENABLE_SCHEDULER"),
    ):
        registry.register_hasattr_probe(
            "micropython." + name,
            module="micropython",
            attr=name,
            category="debug" if name == "kbd_intr" else "memory" if name in {"mem_info", "stack_use"} else "runtime",
            macro_hint=macro,
        )

    registry.register_script_probe(
        "filesystem.write",
        category="filesystem",
        confidence="behaviour-probe",
        macro_hint="MICROPY_VFS_WRITABLE",
        probe='open(path, "wb").write(...)',
        script=r"""
try:
    import os
    p = "/.pyrite_doctor.tmp"
    try:
        f = open(p, "wb")
        f.write(b"pyrite")
        f.close()
        f = open(p, "rb")
        data = f.read()
        f.close()
        _pyrite_feature("filesystem.write", "filesystem", _pyrite_supported(data == b"pyrite"), "behaviour-probe", "MICROPY_VFS_WRITABLE", 'open(path, "wb").write(...)')
    finally:
        try:
            os.remove(p)
        except Exception:
            pass
except Exception:
    _pyrite_feature("filesystem.write", "filesystem", "unsupported", "behaviour-probe", "MICROPY_VFS_WRITABLE", 'open(path, "wb").write(...)')
""",
    )
    registry.register_script_probe(
        "external_import",
        category="filesystem",
        confidence="behaviour-probe",
        macro_hint="MICROPY_ENABLE_EXTERNAL_IMPORT",
        probe="write temp .py then import",
        script=r"""
try:
    import os, sys
    mod_path = "/_pyrite_doctor_mod.py"
    mod_name = "_pyrite_doctor_mod"
    try:
        f = open(mod_path, "w")
        f.write("VALUE=7\n")
        f.close()
        if "/" not in sys.path:
            sys.path.insert(0, "/")
        mod = __import__(mod_name)
        _pyrite_feature("external_import", "filesystem", _pyrite_supported(getattr(mod, "VALUE", None) == 7), "behaviour-probe", "MICROPY_ENABLE_EXTERNAL_IMPORT", "write temp .py then import")
    finally:
        try:
            del sys.modules[mod_name]
        except Exception:
            pass
        try:
            os.remove(mod_path)
        except Exception:
            pass
except Exception:
    _pyrite_feature("external_import", "filesystem", "unsupported", "behaviour-probe", "MICROPY_ENABLE_EXTERNAL_IMPORT", "write temp .py then import")
""",
    )
    registry.register_script_probe(
        "eval",
        category="compiler",
        confidence="behaviour-probe",
        macro_hint="MICROPY_PY_BUILTINS_EVAL_EXEC",
        probe='eval("1+1")',
        script=r"""
try:
    _pyrite_feature("eval", "compiler", _pyrite_supported(eval("1+1") == 2), "behaviour-probe", "MICROPY_PY_BUILTINS_EVAL_EXEC", 'eval("1+1")')
except Exception:
    _pyrite_feature("eval", "compiler", "unsupported", "behaviour-probe", "MICROPY_PY_BUILTINS_EVAL_EXEC", 'eval("1+1")')
""",
    )
    registry.register_script_probe(
        "compile",
        category="compiler",
        confidence="behaviour-probe",
        macro_hint="MICROPY_PY_BUILTINS_COMPILE",
        probe='compile("1+1", "<doctor>", "eval")',
        script=r"""
try:
    compile("1+1", "<doctor>", "eval")
    _pyrite_feature("compile", "compiler", "supported", "behaviour-probe", "MICROPY_PY_BUILTINS_COMPILE", 'compile("1+1", "<doctor>", "eval")')
except Exception:
    _pyrite_feature("compile", "compiler", "unsupported", "behaviour-probe", "MICROPY_PY_BUILTINS_COMPILE", 'compile("1+1", "<doctor>", "eval")')
""",
    )
    registry.register_script_probe(
        "async_await",
        category="compiler",
        confidence="behaviour-probe",
        macro_hint="MICROPY_PY_ASYNC_AWAIT",
        probe="compile async def",
        script=r"""
try:
    compile("async def f():\n return 1", "<doctor>", "exec")
    _pyrite_feature("async_await", "compiler", "supported", "behaviour-probe", "MICROPY_PY_ASYNC_AWAIT", "compile async def")
except Exception:
    _pyrite_feature("async_await", "compiler", "unsupported", "behaviour-probe", "MICROPY_PY_ASYNC_AWAIT", "compile async def")
""",
    )

    for feature_id, attr, category, macro in (
        ("sys.settrace", "settrace", "debug", "MICROPY_PY_SYS_SETTRACE"),
        ("sys.tracebacklimit", "tracebacklimit", "debug", "MICROPY_PY_SYS_TRACEBACKLIMIT"),
        ("sys.stdin.buffer", "stdin.buffer", "runtime", "MICROPY_PY_SYS_STDIO_BUFFER"),
    ):
        registry.register_hasattr_probe(
            feature_id,
            module="sys",
            attr=attr,
            category=category,
            macro_hint=macro,
        )

    for name, category, macro in (
        ("asyncio", "runtime", "MICROPY_PY_ASYNCIO"),
        ("machine", "hardware", "MICROPY_PY_MACHINE"),
        ("network", "network", "MICROPY_PY_NETWORK"),
        ("socket", "network", "MICROPY_PY_SOCKET"),
        ("ssl", "network", "MICROPY_PY_SSL"),
        ("bluetooth", "network", "MICROPY_PY_BLUETOOTH"),
        ("webrepl", "network", "MICROPY_PY_WEBREPL"),
        ("websocket", "network", "MICROPY_PY_WEBSOCKET"),
        ("deflate", "compression", "MICROPY_PY_DEFLATE"),
        ("zlib", "compression", "MICROPY_PY_DEFLATE"),
    ):
        registry.register_import_probe(name, category=category, macro_hint=macro)

    registry.register_hasattr_probe(
        "ubinascii.crc32",
        module="ubinascii",
        attr="crc32",
        category="filesystem",
        macro_hint="MICROPY_PY_BINASCII",
    )

    for name, macro in (
        ("reset", "MICROPY_PY_MACHINE_RESET"),
        ("freq", "MICROPY_PY_MACHINE"),
        ("Pin", "MICROPY_PY_MACHINE_PIN"),
        ("I2C", "MICROPY_PY_MACHINE_I2C"),
        ("SPI", "MICROPY_PY_MACHINE_SPI"),
        ("UART", "MICROPY_PY_MACHINE_UART"),
        ("WDT", "MICROPY_PY_MACHINE_WDT"),
    ):
        registry.register_hasattr_probe(
            "machine." + name,
            module="machine",
            attr=name,
            category="hardware",
            macro_hint=macro,
        )

    registry.register_cli_dependency(
        "flash.crc32_verify",
        "ubinascii.crc32",
        required=False,
        fallback="FallbackToSizeVerify",
    )


_register_builtin_board_features(DEFAULT_REGISTRY)

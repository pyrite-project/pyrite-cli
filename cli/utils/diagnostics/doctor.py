"""Runtime diagnostics for MicroPython devices.

The doctor report only describes capabilities observable from the Python
runtime. Macro names are emitted as source-level hints, not as read macro
values from firmware.
"""

from __future__ import annotations

import time
from typing import Any


DOCTOR_SCRIPT = r"""
import sys

print("PYRITE_DOCTOR_BEGIN")

def _clean(v):
    return str(v).replace("|", "/").replace("\r", " ").replace("\n", " ")

def info(key, value):
    print("INFO|" + key + "|" + _clean(value))

def check(name, status, confidence, message):
    print("CHECK|" + name + "|" + status + "|" + confidence + "|" + _clean(message))

def feature(name, category, status, confidence, macro_hint, probe):
    print("FEATURE|" + name + "|" + category + "|" + status + "|" + confidence + "|" + macro_hint + "|" + probe)

def supported(value):
    return "supported" if value else "unsupported"

try:
    # Kept in the capability payload for older parsers; run_doctor overwrites
    # report["board"] from the shared DeviceContext after this script returns.
    info("firmware.name", sys.implementation.name)
    info("firmware.version", ".".join(str(x) for x in sys.implementation.version))
    info("firmware.platform", sys.platform)
    info("sys.version", sys.version)
except Exception as e:
    check("identity", "error", "direct-read", type(e).__name__)

try:
    import os
    if hasattr(os, "uname"):
        u = os.uname()
        info("firmware.machine", getattr(u, "machine", ""))
        info("firmware.release", getattr(u, "release", ""))
        info("firmware.sysname", getattr(u, "sysname", ""))
    if hasattr(os, "statvfs"):
        st = os.statvfs("/")
        info("filesystem.total", st[0] * st[2])
        info("filesystem.free", st[0] * st[3])
        feature("os.statvfs", "filesystem", "supported", "hasattr-probe", "MICROPY_PY_OS_STATVFS", 'hasattr(os, "statvfs")')
    else:
        feature("os.statvfs", "filesystem", "unsupported", "hasattr-probe", "MICROPY_PY_OS_STATVFS", 'hasattr(os, "statvfs")')
except Exception as e:
    check("filesystem_info", "error", "direct-read", type(e).__name__)

try:
    import gc
    if hasattr(gc, "collect"):
        gc.collect()
    if hasattr(gc, "mem_free"):
        info("memory.free", gc.mem_free())
    if hasattr(gc, "mem_alloc"):
        info("memory.allocated", gc.mem_alloc())
    feature("gc.mem_free", "memory", supported(hasattr(gc, "mem_free")), "hasattr-probe", "MICROPY_PY_GC", 'hasattr(gc, "mem_free")')
except Exception:
    feature("gc.mem_free", "memory", "unsupported", "import-probe", "MICROPY_PY_GC", "import gc")

check("raw_repl", "ok", "behaviour-probe", "command execution succeeded")

try:
    import micropython
    has_kbd = hasattr(micropython, "kbd_intr")
    feature("micropython.kbd_intr", "debug", supported(has_kbd), "hasattr-probe", "MICROPY_KBD_EXCEPTION", 'hasattr(micropython, "kbd_intr")')
    feature("micropython.mem_info", "memory", supported(hasattr(micropython, "mem_info")), "hasattr-probe", "MICROPY_PY_MICROPYTHON_MEM_INFO", 'hasattr(micropython, "mem_info")')
    feature("micropython.stack_use", "memory", supported(hasattr(micropython, "stack_use")), "hasattr-probe", "MICROPY_PY_MICROPYTHON_STACK_USE", 'hasattr(micropython, "stack_use")')
    feature("micropython.schedule", "runtime", supported(hasattr(micropython, "schedule")), "hasattr-probe", "MICROPY_ENABLE_SCHEDULER", 'hasattr(micropython, "schedule")')
except Exception:
    feature("micropython.kbd_intr", "debug", "unsupported", "import-probe", "MICROPY_PY_MICROPYTHON", "import micropython")

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
        if data == b"pyrite":
            check("filesystem_rw", "ok", "behaviour-probe", "write/read/delete passed")
            feature("filesystem.write", "filesystem", "supported", "behaviour-probe", "MICROPY_VFS_WRITABLE", 'open(path, "wb").write(...)')
        else:
            check("filesystem_rw", "error", "behaviour-probe", "readback mismatch")
            feature("filesystem.write", "filesystem", "error", "behaviour-probe", "MICROPY_VFS_WRITABLE", 'open(path, "wb").write(...)')
    finally:
        try:
            os.remove(p)
        except Exception:
            pass
except Exception as e:
    check("filesystem_rw", "error", "behaviour-probe", type(e).__name__)
    feature("filesystem.write", "filesystem", "unsupported", "behaviour-probe", "MICROPY_VFS_WRITABLE", 'open(path, "wb").write(...)')

try:
    import os
    mod_path = "/_pyrite_doctor_mod.py"
    mod_name = "_pyrite_doctor_mod"
    try:
        f = open(mod_path, "w")
        f.write("VALUE=7\n")
        f.close()
        if "/" not in sys.path:
            sys.path.insert(0, "/")
        mod = __import__(mod_name)
        ok = getattr(mod, "VALUE", None) == 7
        feature("external_import", "filesystem", supported(ok), "behaviour-probe", "MICROPY_ENABLE_EXTERNAL_IMPORT", "write temp .py then import")
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
    feature("external_import", "filesystem", "unsupported", "behaviour-probe", "MICROPY_ENABLE_EXTERNAL_IMPORT", "write temp .py then import")

try:
    ok = eval("1+1") == 2
    feature("eval", "compiler", supported(ok), "behaviour-probe", "MICROPY_PY_BUILTINS_EVAL_EXEC", 'eval("1+1")')
except Exception:
    feature("eval", "compiler", "unsupported", "behaviour-probe", "MICROPY_PY_BUILTINS_EVAL_EXEC", 'eval("1+1")')

try:
    compile("1+1", "<doctor>", "eval")
    feature("compile", "compiler", "supported", "behaviour-probe", "MICROPY_PY_BUILTINS_COMPILE", 'compile("1+1", "<doctor>", "eval")')
except Exception:
    feature("compile", "compiler", "unsupported", "behaviour-probe", "MICROPY_PY_BUILTINS_COMPILE", 'compile("1+1", "<doctor>", "eval")')

try:
    compile("async def f():\n return 1", "<doctor>", "exec")
    feature("async_await", "compiler", "supported", "behaviour-probe", "MICROPY_PY_ASYNC_AWAIT", "compile async def")
except Exception:
    feature("async_await", "compiler", "unsupported", "behaviour-probe", "MICROPY_PY_ASYNC_AWAIT", "compile async def")

feature("sys.settrace", "debug", supported(hasattr(sys, "settrace")), "hasattr-probe", "MICROPY_PY_SYS_SETTRACE", 'hasattr(sys, "settrace")')
feature("sys.tracebacklimit", "debug", supported(hasattr(sys, "tracebacklimit")), "hasattr-probe", "MICROPY_PY_SYS_TRACEBACKLIMIT", 'hasattr(sys, "tracebacklimit")')
feature("sys.stdin.buffer", "runtime", supported(hasattr(getattr(sys, "stdin", None), "buffer")), "hasattr-probe", "MICROPY_PY_SYS_STDIO_BUFFER", 'hasattr(sys.stdin, "buffer")')

def import_probe(name, category, macro_hint):
    try:
        __import__(name)
        feature(name, category, "supported", "import-probe", macro_hint, "import " + name)
    except Exception:
        feature(name, category, "unsupported", "import-probe", macro_hint, "import " + name)

for item in (
    ("asyncio", "runtime", "MICROPY_PY_ASYNCIO"),
    ("machine", "hardware", "MICROPY_PY_MACHINE"),
    ("network", "network", "MICROPY_PY_NETWORK"),
    ("socket", "network", "MICROPY_PY_SOCKET"),
    ("ssl", "network", "MICROPY_PY_SSL"),
    ("bluetooth", "network", "MICROPY_PY_BLUETOOTH"),
    ("webrepl", "network", "MICROPY_PY_WEBREPL"),
    ("websocket", "network", "MICROPY_PY_WEBSOCKET"),
):
    import_probe(item[0], item[1], item[2])

try:
    import machine
    for name, macro in (
        ("reset", "MICROPY_PY_MACHINE_RESET"),
        ("freq", "MICROPY_PY_MACHINE"),
        ("Pin", "MICROPY_PY_MACHINE_PIN"),
        ("I2C", "MICROPY_PY_MACHINE_I2C"),
        ("SPI", "MICROPY_PY_MACHINE_SPI"),
        ("UART", "MICROPY_PY_MACHINE_UART"),
        ("WDT", "MICROPY_PY_MACHINE_WDT"),
    ):
        feature("machine." + name, "hardware", supported(hasattr(machine, name)), "hasattr-probe", macro, 'hasattr(machine, "' + name + '")')
except Exception:
    pass

print("PYRITE_DOCTOR_END")
"""


def parse_doctor_output(output: str) -> dict[str, Any]:
    info: dict[str, Any] = {}
    checks: list[dict[str, Any]] = []
    features: list[dict[str, str]] = []

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("PYRITE_DOCTOR_"):
            continue
        parts = line.split("|")
        kind = parts[0]
        if kind == "INFO" and len(parts) >= 3:
            info[parts[1]] = _coerce_scalar("|".join(parts[2:]))
        elif kind == "CHECK" and len(parts) >= 5:
            checks.append({
                "id": parts[1],
                "status": parts[2],
                "confidence": parts[3],
                "message": "|".join(parts[4:]),
            })
        elif kind == "FEATURE" and len(parts) >= 7:
            features.append({
                "id": parts[1],
                "category": parts[2],
                "status": parts[3],
                "confidence": parts[4],
                "macro_hint": parts[5],
                "probe": "|".join(parts[6:]),
            })

    board = {
        "implementation": info.get("firmware.name"),
        "version": info.get("firmware.version"),
        "platform": info.get("firmware.platform"),
        "machine": info.get("firmware.machine"),
        "release": info.get("firmware.release"),
        "sysname": info.get("firmware.sysname"),
    }
    memory = {
        "free": info.get("memory.free"),
        "allocated": info.get("memory.allocated"),
        "total": None,
    }
    if isinstance(memory["free"], int) and isinstance(memory["allocated"], int):
        memory["total"] = memory["free"] + memory["allocated"]

    filesystem = {
        "total": info.get("filesystem.total"),
        "free": info.get("filesystem.free"),
        "used": None,
    }
    if isinstance(filesystem["total"], int) and isinstance(filesystem["free"], int):
        filesystem["used"] = filesystem["total"] - filesystem["free"]

    return {
        "board": board,
        "memory": memory,
        "filesystem": filesystem,
        "checks": checks,
        "firmware_features": {"items": features},
        "raw": {"info": info},
    }


def run_doctor(mp: Any, connect_ms: int | None = None) -> dict[str, Any]:
    start = time.perf_counter()
    output = mp.run(DOCTOR_SCRIPT, timeout=30)
    raw_repl_ms = int((time.perf_counter() - start) * 1000)
    report = parse_doctor_output(output)
    _apply_shared_board_context(report, mp)

    checks = report["checks"]
    if connect_ms is not None:
        checks.insert(0, {
            "id": "serial_connect",
            "status": "ok",
            "confidence": "behaviour-probe",
            "message": f"connect={connect_ms}ms",
        })
    for check in checks:
        if check["id"] == "raw_repl":
            check["duration_ms"] = raw_repl_ms
            break
    else:
        checks.append({
            "id": "raw_repl",
            "status": "ok",
            "confidence": "behaviour-probe",
            "message": "command execution succeeded",
            "duration_ms": raw_repl_ms,
        })

    report["connection"] = {
        "connect_ms": connect_ms,
        "raw_repl_ms": raw_repl_ms,
    }
    report["configuration"] = _configuration_report(mp)
    report["recommendations"] = _doctor_recommendations(report)
    report["summary"] = _summary(report)
    return report


def _apply_shared_board_context(report: dict[str, Any], mp: Any) -> None:
    ensure_context = getattr(mp, "ensure_device_context", None)
    if not callable(ensure_context):
        return
    try:
        context = ensure_context()
    except Exception:
        return
    board = report.setdefault("board", {})
    for key, attr in (
        ("implementation", "implementation"),
        ("version", "version"),
        ("platform", "platform"),
        ("machine", "machine"),
        ("release", "release"),
        ("sysname", "sysname"),
    ):
        value = getattr(context, attr, None)
        if isinstance(value, str) and value.strip():
            board[key] = value


def _coerce_scalar(value: str) -> Any:
    text = value.strip()
    if text and (text.isdigit() or (text.startswith("-") and text[1:].isdigit())):
        try:
            return int(text)
        except ValueError:
            pass
    return text


def _configuration_report(mp: Any) -> dict[str, Any]:
    cfg = getattr(mp, "config", None)
    chunk_size = getattr(cfg, "chunk_size", None)
    verify = getattr(cfg, "verify", None)
    max_retries = getattr(cfg, "max_retries", None)
    recommendations: list[str] = []
    if verify == "off":
        recommendations.append("verify=off disables post-write validation; use only for trusted links")
    elif verify == "size":
        recommendations.append("keep verify=size unless a board needs stronger crc32 validation")
    elif verify == "crc32":
        recommendations.append("crc32 gives stronger validation at the cost of extra device work")
    if max_retries == 0:
        recommendations.append("max_retries=0 disables retry after validation or connection failure")
    elif max_retries is not None:
        recommendations.append(f"max_retries={max_retries} is available for transient serial failures")
    if chunk_size is not None:
        recommendations.append(f"chunk_size={chunk_size} is the active host transfer chunk size")

    return {
        "chunk_size": chunk_size,
        "verify": verify,
        "max_retries": max_retries,
        "recommendations": recommendations,
    }


def _feature_status(report: dict[str, Any], feature_id: str) -> str | None:
    for item in report.get("firmware_features", {}).get("items", []):
        if item.get("id") == feature_id:
            return item.get("status")
    return None


def _doctor_recommendations(report: dict[str, Any]) -> list[dict[str, str]]:
    recommendations: list[dict[str, str]] = []
    board = report.get("board", {})
    version = board.get("version") or board.get("release")

    if _feature_status(report, "network") == "unsupported":
        recommendations.append({
            "id": "host_assisted_tunnel_candidate",
            "category": "network",
            "severity": "info",
            "message": (
                "network is unavailable on this firmware; for development-only checks, "
                "try a host-assisted tunnel when that command is available."
            ),
        })

    if version:
        recommendations.append({
            "id": "firmware_version_precheck",
            "category": "compatibility",
            "severity": "info",
            "message": (
                f"Firmware version {version} is available for compatibility precheck rules "
                "before flashing syntax-sensitive code."
            ),
        })

    missing_dev_features: list[str] = []
    for feature_id in ("micropython.kbd_intr", "sys.stdin.buffer", "external_import"):
        if _feature_status(report, feature_id) == "unsupported":
            missing_dev_features.append(feature_id)
    if missing_dev_features:
        recommendations.append({
            "id": "project_dev_degraded",
            "category": "project_dev",
            "severity": "warning",
            "message": (
                "Missing "
                + ", ".join(missing_dev_features)
                + "; project dev workflows should fall back to simpler flash/run behaviour."
            ),
        })

    if _feature_status(report, "sys.settrace") == "unsupported":
        recommendations.append({
            "id": "traceback_observability_limited",
            "category": "debug",
            "severity": "info",
            "message": "sys.settrace is unavailable; prefer Flight Recorder traces and captured traceback output.",
        })

    return recommendations


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    checks = report.get("checks", [])
    features = report.get("firmware_features", {}).get("items", [])
    failed_checks = [
        item for item in checks
        if item.get("status") not in {"ok", "supported", "skipped"}
    ]
    feature_errors = [
        item for item in features
        if item.get("status") == "error"
    ]
    return {
        "ok": not failed_checks and not feature_errors,
        "checks": len(checks),
        "failed_checks": len(failed_checks),
        "features_supported": sum(1 for item in features if item.get("status") == "supported"),
        "features_unsupported": sum(1 for item in features if item.get("status") == "unsupported"),
        "feature_errors": len(feature_errors),
    }

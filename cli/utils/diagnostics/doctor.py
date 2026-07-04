"""Runtime diagnostics for MicroPython devices.

The doctor report only describes capabilities observable from the Python
runtime. Macro names are emitted as source-level hints, not as read macro
values from firmware.
"""

from __future__ import annotations

import time
from typing import Any

from ..board_features import build_probe_script, parse_board_feature_output

DOCTOR_SCRIPT = r"""
import sys

print("PYRITE_DOCTOR_BEGIN")

def _clean(v):
    return str(v).replace("|", "/").replace("\r", " ").replace("\n", " ")

def info(key, value):
    print("INFO|" + key + "|" + _clean(value))

def check(name, status, confidence, message):
    print("CHECK|" + name + "|" + status + "|" + confidence + "|" + _clean(message))

try:
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
except Exception:
    pass

check("raw_repl", "ok", "behaviour-probe", "command execution succeeded")

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
        else:
            check("filesystem_rw", "error", "behaviour-probe", "readback mismatch")
    finally:
        try:
            os.remove(p)
        except Exception:
            pass
except Exception as e:
    check("filesystem_rw", "error", "behaviour-probe", type(e).__name__)

print("PYRITE_DOCTOR_END")
"""


def parse_doctor_output(output: str) -> dict[str, Any]:
    info: dict[str, Any] = {}
    checks: list[dict[str, Any]] = []

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

    features = [
        feature.to_dict()
        for feature in parse_board_feature_output(output)
    ]

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
    output = mp.run(DOCTOR_SCRIPT + "\n" + build_probe_script(), timeout=30)
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

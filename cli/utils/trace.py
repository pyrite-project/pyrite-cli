"""Flight Recorder trace schema and helpers."""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional

TRACE_SCHEMA_VERSION = 1
TRACE_EVENT_TYPE = "trace_event"
REDACTED = "<redacted>"

_CONTROL_NAMES = {
    0x00: "<NUL>",
    0x01: "<RAW>",
    0x02: "<B>",
    0x03: "<C>",
    0x04: "<D>",
    0x05: "<E>",
    0x08: "<BS>",
    0x09: "<TAB>",
    0x0A: "<LF>",
    0x0D: "<CR>",
    0x1B: "<ESC>",
    0x7F: "<DEL>",
}

_SENSITIVE_KEY_MARKERS = (
    "password",
    "passwd",
    "passphrase",
    "token",
    "secret",
    "apikey",
    "authorization",
)

_INLINE_SECRET_PATTERNS = [
    re.compile(
        r"(?i)\b(password|passwd|passphrase|token|secret|api[_-]?key)"
        r"(\s*[=:]\s*)([^\s&,'\"]+)"
    ),
    re.compile(r"(?i)(authorization\s*:\s*)(bearer|basic)\s+([^\s]+)"),
    re.compile(r"(?i)([?&](?:password|token|secret|api[_-]?key)=)([^&\s]+)"),
]


def make_session_id() -> str:
    return uuid.uuid4().hex[:12]


def default_trace_path(
    log_dir: str | Path = "./log",
    operation: str = "trace",
    session_id: Optional[str] = None,
) -> Path:
    session = session_id or make_session_id()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_operation = re.sub(r"[^A-Za-z0-9_.-]+", "-", operation).strip("-") or "trace"
    return Path(log_dir) / f"{stamp}-{safe_operation}-{session}.pyrite-trace"


def render_control_bytes(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    out: List[str] = []
    for ch in text:
        code = ord(ch)
        if code in _CONTROL_NAMES:
            out.append(_CONTROL_NAMES[code])
        elif code <= 0x1F or 0x80 <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    return "".join(out)


def _is_sensitive_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", str(key).lower())
    return any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS)


def redact_text(text: str) -> str:
    redacted = text
    redacted = _INLINE_SECRET_PATTERNS[0].sub(
        lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}",
        redacted,
    )
    redacted = _INLINE_SECRET_PATTERNS[1].sub(
        lambda m: f"{m.group(1)}{m.group(2)} {REDACTED}",
        redacted,
    )
    redacted = _INLINE_SECRET_PATTERNS[2].sub(
        lambda m: f"{m.group(1)}{REDACTED}",
        redacted,
    )
    return redacted


def redact_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return redact_text(render_control_bytes(value))
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            safe_key = str(key)
            result[safe_key] = REDACTED if _is_sensitive_key(key) else redact_value(item)
        return result
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _hex_preview(data: bytes, max_bytes: int) -> str:
    preview = data[:max_bytes]
    suffix = f" ... ({len(data)} bytes)" if len(data) > max_bytes else ""
    return preview.hex(" ") + suffix


def _compact_record(record: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "ts",
        "event",
        "phase",
        "direction",
        "byte_count",
        "text",
        "error_type",
        "message",
        "status",
    )
    return {key: record[key] for key in keys if key in record}


class TraceRecorder:
    """Write redacted Flight Recorder trace events as JSONL."""

    def __init__(
        self,
        path: str | Path,
        *,
        operation: str,
        port: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        max_payload_bytes: int = 256,
        tail_size: int = 20,
    ) -> None:
        self.path = Path(path)
        self.operation = operation
        self.port = port
        self.session_id = session_id or make_session_id()
        self.max_payload_bytes = max(16, max_payload_bytes)
        self._lock = threading.Lock()
        self._closed = False
        self._tail: Deque[Dict[str, Any]] = deque(maxlen=max(1, tail_size))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", encoding="utf-8")
        self.event(
            "session_start",
            phase="session",
            metadata=metadata or {},
        )

    def event(self, event: str, phase: str = "session", **fields: Any) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "type": TRACE_EVENT_TYPE,
            "schema_version": TRACE_SCHEMA_VERSION,
            "session_id": self.session_id,
            "ts": _utc_now(),
            "time": time.time(),
            "operation": self.operation,
            "event": event,
            "phase": phase,
        }
        if self.port is not None:
            record["port"] = redact_text(self.port)
        record.update(redact_value(fields))
        self._write_record(record)
        return record

    def traffic(self, direction: str, data: bytes, phase: str = "traffic") -> Dict[str, Any]:
        preview = data[: self.max_payload_bytes]
        fields: Dict[str, Any] = {
            "direction": direction,
            "byte_count": len(data),
            "text": render_control_bytes(preview),
            "hex": _hex_preview(data, self.max_payload_bytes),
        }
        if len(data) > self.max_payload_bytes:
            fields["truncated"] = True
            fields["omitted_bytes"] = len(data) - self.max_payload_bytes
        return self.event("traffic", phase=phase, **fields)

    def traffic_summary(
        self,
        direction: str,
        byte_count: int,
        *,
        phase: str = "traffic",
        text: str = "",
    ) -> Dict[str, Any]:
        return self.event(
            "traffic_summary",
            phase=phase,
            direction=direction,
            byte_count=byte_count,
            text=text or f"[{byte_count} bytes]",
            summary=True,
        )

    def failure(
        self,
        exc: BaseException | str,
        *,
        phase: str = "error",
        tail: int = 10,
    ) -> Dict[str, Any]:
        if isinstance(exc, BaseException):
            error_type = type(exc).__name__
            message = str(exc)
            if exc.__traceback__ is not None:
                stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            else:
                stack = "".join(traceback.format_exception_only(type(exc), exc))
        else:
            error_type = "Error"
            message = str(exc)
            stack = message
        return self.event(
            "failure",
            phase=phase,
            error_type=error_type,
            message=message,
            stack=stack,
            tail=[_compact_record(item) for item in list(self._tail)[-tail:]],
        )

    def close(self, status: str = "ok") -> None:
        if self._closed:
            return
        self.event("session_end", phase="session", status=status)
        with self._lock:
            self._closed = True
            self._file.close()

    def _write_record(self, record: Dict[str, Any]) -> None:
        with self._lock:
            if self._closed:
                return
            self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._file.flush()
            self._tail.append(record)

    def __enter__(self) -> "TraceRecorder":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if isinstance(exc, BaseException):
            self.failure(exc)
            self.close(status="error")
        else:
            self.close(status="ok")


def load_trace(path: str | Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("type") == TRACE_EVENT_TYPE:
                records.append(redact_value(record))
    return records


def summarize_trace(path: str | Path, tail: int = 10) -> Dict[str, Any]:
    records = load_trace(path)
    summary: Dict[str, Any] = {
        "path": str(path),
        "schema_version": TRACE_SCHEMA_VERSION,
        "session_id": None,
        "operation": None,
        "port": None,
        "status": "unknown",
        "started_at": None,
        "ended_at": None,
        "duration_ms": None,
        "event_count": len(records),
        "traffic": {
            "TX": {"events": 0, "bytes": 0},
            "RX": {"events": 0, "bytes": 0},
        },
        "phases": {},
        "failures": [],
        "recommendations": [],
        "tail": [_compact_record(record) for record in records[-tail:]],
    }
    if not records:
        return summary

    first = records[0]
    last = records[-1]
    summary.update({
        "session_id": first.get("session_id"),
        "operation": first.get("operation"),
        "port": first.get("port"),
        "started_at": first.get("ts"),
        "ended_at": last.get("ts"),
    })
    if isinstance(first.get("time"), (int, float)) and isinstance(last.get("time"), (int, float)):
        summary["duration_ms"] = round((last["time"] - first["time"]) * 1000, 1)

    phases: Dict[str, Dict[str, Any]] = {}
    for record in records:
        phase = str(record.get("phase") or "unknown")
        phase_stats = phases.setdefault(
            phase,
            {"events": 0, "traffic_events": 0, "bytes": 0},
        )
        phase_stats["events"] += 1

        if record.get("event") == "session_end":
            summary["status"] = record.get("status", "unknown")
        if record.get("event") in {"traffic", "traffic_summary"}:
            direction = record.get("direction")
            byte_count = int(record.get("byte_count") or 0)
            if direction in summary["traffic"]:
                summary["traffic"][direction]["events"] += 1
                summary["traffic"][direction]["bytes"] += byte_count
            phase_stats["traffic_events"] += 1
            phase_stats["bytes"] += byte_count
        if record.get("event") == "failure":
            summary["failures"].append({
                "phase": record.get("phase"),
                "error_type": record.get("error_type"),
                "message": record.get("message"),
            })

    summary["phases"] = phases
    if summary["status"] == "unknown" and summary["failures"]:
        summary["status"] = "error"
    summary["recommendations"] = _trace_recommendations(summary)
    return summary


def _trace_recommendations(summary: Dict[str, Any]) -> List[Dict[str, str]]:
    recommendations: List[Dict[str, str]] = []
    if summary.get("failures"):
        recommendations.append({
            "id": "attach_trace_on_failure",
            "severity": "info",
            "message": "Attach this .pyrite-trace when reporting disconnect, verify, or Raw REPL failures.",
        })
    if summary.get("traffic", {}).get("RX", {}).get("bytes", 0) == 0:
        recommendations.append({
            "id": "no_rx_traffic",
            "severity": "warning",
            "message": "No RX traffic was recorded; check port selection, reset timing, or board boot state.",
        })
    if "raw_repl" in summary.get("phases", {}) and summary.get("status") != "ok":
        recommendations.append({
            "id": "raw_repl_failure_context",
            "severity": "info",
            "message": "Use the raw_repl phase tail to compare control characters such as <RAW>, <C>, and <D>.",
        })
    return recommendations


def format_trace_view(records: Iterable[Dict[str, Any]], limit: Optional[int] = None) -> str:
    items = list(records)
    if limit is not None and limit > 0:
        items = items[-limit:]
    if not items:
        return "empty trace"

    first = items[0]
    lines = [
        "Trace "
        f"{first.get('session_id', '?')} "
        f"operation={first.get('operation', '?')} "
        f"port={first.get('port', '-')}"
    ]
    for record in items:
        event = record.get("event", "?")
        phase = record.get("phase", "?")
        ts = record.get("ts", "?")
        if event in {"traffic", "traffic_summary"}:
            direction = record.get("direction", "?")
            byte_count = record.get("byte_count", 0)
            text = record.get("text", "")
            marker = " summary" if record.get("summary") else ""
            lines.append(f"{ts} [{phase}] {direction}{marker} {byte_count}B {text}")
        elif event == "failure":
            lines.append(
                f"{ts} [{phase}] FAIL {record.get('error_type', 'Error')}: "
                f"{record.get('message', '')}"
            )
        elif event == "session_end":
            lines.append(f"{ts} [{phase}] session_end status={record.get('status', '?')}")
        else:
            lines.append(f"{ts} [{phase}] {event}")
    return "\n".join(redact_text(line) for line in lines)


def format_trace_summary(summary: Dict[str, Any]) -> str:
    lines = [
        f"Trace {summary.get('session_id') or '?'}",
        f"operation: {summary.get('operation') or '?'}",
        f"port: {summary.get('port') or '-'}",
        f"status: {summary.get('status')}",
        f"events: {summary.get('event_count')}",
        (
            "traffic: "
            f"TX {summary['traffic']['TX']['events']} events/"
            f"{summary['traffic']['TX']['bytes']} bytes, "
            f"RX {summary['traffic']['RX']['events']} events/"
            f"{summary['traffic']['RX']['bytes']} bytes"
        ),
    ]
    if summary.get("duration_ms") is not None:
        lines.append(f"duration_ms: {summary['duration_ms']}")
    if summary.get("failures"):
        lines.append("failures:")
        for failure in summary["failures"]:
            lines.append(
                f"  [{failure.get('phase')}] {failure.get('error_type')}: "
                f"{failure.get('message')}"
            )
    if summary.get("recommendations"):
        lines.append("recommendations:")
        for item in summary["recommendations"]:
            lines.append(f"  {item.get('severity', 'info')}: {item.get('message')}")
    return "\n".join(redact_text(line) for line in lines)

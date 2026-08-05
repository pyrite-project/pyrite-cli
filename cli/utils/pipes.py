"""JSONL helpers for shell pipeline integration."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional, TextIO


@dataclass(frozen=True)
class JsonlItem:
    line: int
    data: dict[str, object]


def read_jsonl(path: str = "-") -> Iterator[JsonlItem]:
    """Read JSON objects from a JSONL file or stdin."""
    if path == "-":
        yield from _read_jsonl_stream(sys.stdin)
        return
    with open(path, "r", encoding="utf-8") as handle:
        yield from _read_jsonl_stream(handle)


def _read_jsonl_stream(stream: TextIO) -> Iterator[JsonlItem]:
    for lineno, raw in enumerate(stream, start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            yield JsonlItem(lineno, {
                "_invalid": True,
                "error": f"invalid JSON: {exc.msg}",
            })
            continue
        if not isinstance(value, dict):
            yield JsonlItem(lineno, {
                "_invalid": True,
                "error": "JSONL item must be an object",
            })
            continue
        yield JsonlItem(lineno, value)


def write_jsonl(data: Mapping[str, object]) -> None:
    print(json.dumps(dict(data), ensure_ascii=False), flush=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode_text(value: object) -> bytes:
    if not isinstance(value, str):
        raise ValueError("content_b64 must be a string")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except Exception as exc:
        raise ValueError(f"invalid content_b64: {exc}") from exc


def record_text(record: Mapping[str, object], *keys: str) -> Optional[str]:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def materialize_record_source(
    record: Mapping[str, object],
    *,
    remote_path: str,
) -> tuple[Optional[str], Optional[str]]:
    """Return a local path for a batch upload record and optional temp path."""
    local = record_text(record, "local", "path", "file")
    if local:
        return local, None
    if "content_b64" not in record:
        return None, None
    data = b64decode_text(record["content_b64"])
    suffix = Path(remote_path).suffix
    if suffix not in {".py", ".mpy", ".bin"}:
        suffix = ".bin"
    fd, temp_path = tempfile.mkstemp(prefix="pyrite-jsonl-", suffix=suffix)
    with open(fd, "wb", closefd=True) as handle:
        handle.write(data)
    return temp_path, temp_path


def cleanup_paths(paths: Iterable[str]) -> None:
    for path in paths:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

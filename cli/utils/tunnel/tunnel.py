"""Host-side helpers for Pyrite tunnel commands.

The tunnel protocol is intentionally small: device-side helpers print framed
JSON requests, and the host responds with framed JSON lines on stdin.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import socket
import sys
import time
from dataclasses import dataclass, field, replace
from importlib import resources
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

FRAME_PREFIX = "PYRITE_TUNNEL "

ALLOWED_NETWORK_METHODS = ("GET", "POST", "PUT", "DELETE")
SAFE_RESPONSE_HEADERS = {
    "content-type",
    "content-length",
    "etag",
    "last-modified",
    "location",
}
BLOCKED_REQUEST_HEADERS = {
    "authorization",
    "cookie",
    "host",
    "proxy-authorization",
}
EXIT_KEY_BYTES = (b"\x03", b"\x1d")

__all__ = [
    "ALLOWED_NETWORK_METHODS",
    "BLOCKED_REQUEST_HEADERS",
    "FRAME_PREFIX",
    "SAFE_RESPONSE_HEADERS",
    "FrameDecoder",
    "HostKeyboard",
    "NetworkPolicy",
    "NetworkRequest",
    "TunnelError",
    "TunnelFrame",
    "TunnelProtocolError",
    "TunnelSecurityError",
    "build_network_response",
    "decode_frame_line",
    "encode_frame",
    "encode_key_event",
    "handle_network_frame",
    "is_exit_key",
    "load_device_script",
    "network_request_from_payload",
    "perform_network_request",
    "run_keyboard_tunnel",
    "run_network_tunnel",
    "run_tunnel_session",
    "sanitize_request_headers",
    "validate_network_request",
]


class TunnelError(Exception):
    """Base exception for tunnel errors."""


class TunnelProtocolError(TunnelError, ValueError):
    """Raised when a tunnel frame is malformed."""


class TunnelSecurityError(TunnelError, ValueError):
    """Raised when a tunnel request violates host-side policy."""


@dataclass(frozen=True)
class TunnelFrame:
    type: str
    id: int | str | None = None
    op: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NetworkRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body_b64: str | None = None


@dataclass(frozen=True)
class NetworkPolicy:
    allow_hosts: tuple[str, ...] = ()
    timeout: float = 10.0
    max_response_bytes: int = 64 * 1024
    allow_private: bool = False
    allowed_methods: tuple[str, ...] = ALLOWED_NETWORK_METHODS


class FrameDecoder:
    """Incrementally decode line-delimited tunnel frames."""

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, data: bytes | str) -> list[TunnelFrame]:
        if isinstance(data, bytes):
            text = data.decode("utf-8", errors="replace")
        else:
            text = data

        self._buffer += text
        frames: list[TunnelFrame] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            prefix_at = line.find(FRAME_PREFIX)
            if prefix_at < 0:
                continue
            frames.append(decode_frame_line(line[prefix_at:]))
        return frames


def encode_frame(frame: TunnelFrame) -> str:
    data: dict[str, Any] = {
        "type": frame.type,
        "payload": frame.payload,
    }
    if frame.id is not None:
        data["id"] = frame.id
    if frame.op is not None:
        data["op"] = frame.op
    return FRAME_PREFIX + json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def decode_frame_line(line: bytes | str) -> TunnelFrame:
    if isinstance(line, bytes):
        text = line.decode("utf-8", errors="replace")
    else:
        text = line
    text = text.strip()
    if not text.startswith(FRAME_PREFIX):
        raise TunnelProtocolError("missing tunnel frame prefix")
    raw_payload = text[len(FRAME_PREFIX):]
    try:
        data = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise TunnelProtocolError(f"invalid tunnel frame JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise TunnelProtocolError("tunnel frame must be a JSON object")
    frame_type = data.get("type")
    if not isinstance(frame_type, str) or not frame_type:
        raise TunnelProtocolError("tunnel frame missing type")
    op = data.get("op")
    if op is not None and not isinstance(op, str):
        raise TunnelProtocolError("tunnel frame op must be a string")
    payload = data.get("payload", {})
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise TunnelProtocolError("tunnel frame payload must be an object")
    return TunnelFrame(
        type=frame_type,
        id=data.get("id"),
        op=op,
        payload=payload,
    )


def is_exit_key(data: bytes | str) -> bool:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return data in EXIT_KEY_BYTES


def encode_key_event(data: bytes | str) -> dict[str, str] | None:
    if isinstance(data, str):
        raw = data.encode("utf-8")
    else:
        raw = data

    if is_exit_key(raw):
        return None

    key_map = {
        b"\r": {"kind": "key", "key": "enter", "text": "\n"},
        b"\n": {"kind": "key", "key": "enter", "text": "\n"},
        b"\x08": {"kind": "key", "key": "backspace", "text": ""},
        b"\x7f": {"kind": "key", "key": "backspace", "text": ""},
        b"\t": {"kind": "key", "key": "tab", "text": "\t"},
        b"\x1b[A": {"kind": "key", "key": "up", "text": ""},
        b"\x1b[B": {"kind": "key", "key": "down", "text": ""},
        b"\x1b[C": {"kind": "key", "key": "right", "text": ""},
        b"\x1b[D": {"kind": "key", "key": "left", "text": ""},
        b"\xe0H": {"kind": "key", "key": "up", "text": ""},
        b"\xe0P": {"kind": "key", "key": "down", "text": ""},
        b"\xe0M": {"kind": "key", "key": "right", "text": ""},
        b"\xe0K": {"kind": "key", "key": "left", "text": ""},
    }
    if raw in key_map:
        return key_map[raw]

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "kind": "bytes",
            "key": "bytes",
            "text": base64.b64encode(raw).decode("ascii"),
        }
    if len(text) == 1 and (text.isprintable() or text == " "):
        return {"kind": "char", "key": text, "text": text}
    return {
        "kind": "bytes",
        "key": "bytes",
        "text": base64.b64encode(raw).decode("ascii"),
    }


def validate_network_request(
    request: NetworkRequest,
    policy: NetworkPolicy,
) -> NetworkRequest:
    method = request.method.upper()
    allowed_methods = {item.upper() for item in policy.allowed_methods}
    if method not in allowed_methods:
        raise TunnelSecurityError(f"method {request.method!r} is not allowed")

    parsed = urlparse(request.url)
    if parsed.scheme not in {"http", "https"}:
        raise TunnelSecurityError("URL scheme must be http or https")
    if not parsed.hostname:
        raise TunnelSecurityError("URL must include a host")

    hostname = parsed.hostname.lower()
    if policy.allow_hosts and not _host_allowed(hostname, policy.allow_hosts):
        raise TunnelSecurityError(f"host {hostname!r} is not in allowlist")
    if not policy.allow_private and _is_private_host(hostname):
        raise TunnelSecurityError(f"private host {hostname!r} is not allowed")
    if not policy.allow_private:
        _reject_private_resolution(hostname, parsed.port or _default_port(parsed.scheme))
    if policy.timeout <= 0:
        raise TunnelSecurityError("timeout must be greater than 0")
    if policy.max_response_bytes < 0:
        raise TunnelSecurityError("max response bytes must be >= 0")

    if method != request.method:
        return replace(request, method=method)
    return request


def sanitize_request_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    sanitized: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name).strip()
        if not name:
            continue
        lower = name.lower()
        if lower in BLOCKED_REQUEST_HEADERS:
            continue
        if lower.startswith("proxy-") or lower.startswith("x-api-key"):
            continue
        sanitized[name] = str(raw_value)
    return sanitized


def build_network_response(
    *,
    status_code: int,
    headers: Mapping[str, str],
    body: bytes,
    max_body_bytes: int,
) -> dict[str, Any]:
    if max_body_bytes < 0:
        raise ValueError("max_body_bytes must be >= 0")
    capped_body = body[:max_body_bytes]
    header_summary: dict[str, str] = {}
    for name, value in headers.items():
        lower = str(name).lower()
        if lower in SAFE_RESPONSE_HEADERS:
            header_summary[lower] = str(value)
    return {
        "status": int(status_code),
        "headers": header_summary,
        "body_b64": base64.b64encode(capped_body).decode("ascii"),
        "truncated": len(body) > max_body_bytes,
        "size": len(body),
    }


def network_request_from_payload(payload: Mapping[str, Any]) -> NetworkRequest:
    method = payload.get("method")
    url = payload.get("url")
    headers = payload.get("headers", {})
    body_b64 = payload.get("body_b64")

    if not isinstance(method, str) or not method:
        raise TunnelProtocolError("network request missing method")
    if not isinstance(url, str) or not url:
        raise TunnelProtocolError("network request missing url")
    if headers is None:
        headers = {}
    if not isinstance(headers, dict):
        raise TunnelProtocolError("network request headers must be an object")
    if body_b64 is not None and not isinstance(body_b64, str):
        raise TunnelProtocolError("network request body_b64 must be a string")

    return NetworkRequest(
        method=method,
        url=url,
        headers={str(k): str(v) for k, v in headers.items()},
        body_b64=body_b64,
    )


def perform_network_request(
    request: NetworkRequest,
    policy: NetworkPolicy,
) -> dict[str, Any]:
    request = validate_network_request(request, policy)
    try:
        import requests
    except ImportError as exc:
        raise TunnelError("requests is required for tunnel network") from exc

    body = None
    if request.body_b64:
        try:
            body = base64.b64decode(request.body_b64)
        except ValueError as exc:
            raise TunnelProtocolError("invalid request body_b64") from exc

    response = requests.request(
        request.method,
        request.url,
        headers=sanitize_request_headers(request.headers),
        data=body,
        timeout=policy.timeout,
        allow_redirects=False,
        stream=True,
    )

    chunks: list[bytes] = []
    observed_size = 0
    truncated = False
    limit = policy.max_response_bytes
    for chunk in response.iter_content(chunk_size=8192):
        if not chunk:
            continue
        observed_size += len(chunk)
        remaining = limit - sum(len(item) for item in chunks)
        if remaining > 0:
            chunks.append(chunk[:remaining])
        if observed_size > limit:
            truncated = True
            break

    body_bytes = b"".join(chunks)
    payload = build_network_response(
        status_code=response.status_code,
        headers=response.headers,
        body=body_bytes,
        max_body_bytes=limit,
    )
    payload["truncated"] = truncated or payload["truncated"]
    payload["size"] = _response_size(response.headers, observed_size)
    return payload


def handle_network_frame(
    frame: TunnelFrame,
    policy: NetworkPolicy,
) -> TunnelFrame:
    try:
        request = network_request_from_payload(frame.payload)
        payload = perform_network_request(request, policy)
        return TunnelFrame(
            type="response",
            id=frame.id,
            op=frame.op or "http",
            payload=payload,
        )
    except Exception as exc:
        return TunnelFrame(
            type="error",
            id=frame.id,
            op=frame.op or "http",
            payload={
                "error": type(exc).__name__,
                "message": str(exc),
            },
        )


def load_device_script(name: str) -> str:
    package = "cli.device.tunnel_scripts"
    return resources.files(package).joinpath(name).read_text(encoding="utf-8")


def run_tunnel_session(
    mp: Any,
    script: str,
    handler: Callable[[TunnelFrame], TunnelFrame | None],
    *,
    poll_interval: float = 0.01,
) -> None:
    """Run a streaming tunnel session against an already connected device."""
    decoder = FrameDecoder()
    mp._enter_raw_repl()
    if not script.endswith("\n"):
        script += "\n"
    mp._write(script)
    mp._write(b"\x04")

    while True:
        transport = mp.transport
        if transport.in_waiting:
            chunk = transport.read(transport.in_waiting)
            if not chunk:
                time.sleep(poll_interval)
                continue
            if b"\x04" in chunk:
                before, _sep, _after = chunk.partition(b"\x04")
                chunk = before
                if chunk:
                    _dispatch_frames(mp, decoder.feed(chunk), handler)
                return
            _dispatch_frames(mp, decoder.feed(chunk), handler)
        else:
            time.sleep(poll_interval)


def run_keyboard_tunnel(mp: Any, *, poll_interval: float = 0.02) -> None:
    script = load_device_script("kb.py")
    with HostKeyboard() as keyboard:
        def handler(frame: TunnelFrame) -> TunnelFrame | None:
            if frame.type != "request" or frame.op not in {"kb.read", "kb.poll"}:
                return None
            blocking = frame.op == "kb.read"
            timeout = _keyboard_timeout(frame.payload.get("timeout_ms"))
            event = keyboard.read(
                blocking=blocking,
                poll_interval=poll_interval,
                timeout=timeout,
            )
            return TunnelFrame(
                type="response",
                id=frame.id,
                op=frame.op,
                payload={"event": event},
            )

        run_tunnel_session(mp, script, handler, poll_interval=poll_interval)


def run_network_tunnel(mp: Any, policy: NetworkPolicy) -> None:
    script = load_device_script("network.py")

    def handler(frame: TunnelFrame) -> TunnelFrame | None:
        if frame.type != "request" or frame.op not in {"http", "network"}:
            return None
        return handle_network_frame(frame, policy)

    run_tunnel_session(mp, script, handler)


class HostKeyboard:
    """Small cross-platform raw keyboard reader."""

    def __init__(self) -> None:
        self._win = os.name == "nt"
        self._old_tty: Any = None

    def __enter__(self) -> "HostKeyboard":
        if not self._win:
            import termios
            import tty

            fd = sys.stdin.fileno()
            self._old_tty = termios.tcgetattr(fd)
            tty.setraw(fd)
        return self

    def __exit__(self, *args: Any) -> None:
        if not self._win and self._old_tty is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_tty)

    def read(
        self,
        *,
        blocking: bool,
        poll_interval: float = 0.02,
        timeout: float | None = None,
    ) -> dict[str, str] | None:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            data = self._read_once()
            if data:
                if is_exit_key(data):
                    raise KeyboardInterrupt
                return encode_key_event(data)
            if not blocking:
                return None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                time.sleep(min(poll_interval, remaining))
            else:
                time.sleep(poll_interval)

    def _read_once(self) -> bytes | None:
        if self._win:
            import msvcrt

            if not msvcrt.kbhit():
                return None
            first = msvcrt.getwch()
            if first in ("\x00", "\xe0"):
                second = msvcrt.getwch()
                return (first + second).encode("latin1")
            return first.encode("utf-8")

        import select

        fd = sys.stdin.fileno()
        if not select.select([sys.stdin], [], [], 0)[0]:
            return None
        data = os.read(fd, 1)
        if data == b"\x1b":
            for _ in range(8):
                if not select.select([sys.stdin], [], [], 0.001)[0]:
                    break
                data += os.read(fd, 1)
                if data.endswith((b"A", b"B", b"C", b"D", b"~")):
                    break
        return data


def _dispatch_frames(
    mp: Any,
    frames: Sequence[TunnelFrame],
    handler: Callable[[TunnelFrame], TunnelFrame | None],
) -> None:
    for frame in frames:
        response = handler(frame)
        if response is not None:
            mp._write(encode_frame(response))


def _keyboard_timeout(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return float(value) / 1000.0
    return None


def _host_allowed(hostname: str, allow_hosts: Sequence[str]) -> bool:
    for raw_pattern in allow_hosts:
        pattern = raw_pattern.strip().lower()
        if not pattern:
            continue
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if hostname.endswith(suffix) and hostname != pattern[2:]:
                return True
        if hostname == pattern or hostname.endswith("." + pattern):
            return True
    return False


def _is_private_host(hostname: str) -> bool:
    host = hostname.strip("[]").lower()
    if host in {"localhost", "localhost.localdomain"}:
        return True
    if host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _reject_private_resolution(hostname: str, port: int) -> None:
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise TunnelSecurityError(f"unable to resolve host {hostname!r}: {exc}") from exc

    if not infos:
        raise TunnelSecurityError(f"unable to resolve host {hostname!r}")

    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        if not sockaddr:
            continue
        address = str(sockaddr[0]).strip("[]")
        if _is_private_host(address):
            raise TunnelSecurityError(
                f"private resolved address {address!r} is not allowed"
            )


def _default_port(scheme: str) -> int:
    if scheme == "https":
        return 443
    return 80


def _response_size(headers: Mapping[str, str], observed_size: int) -> int:
    for name, value in headers.items():
        if str(name).lower() == "content-length":
            try:
                return int(value)
            except (TypeError, ValueError):
                break
    return observed_size

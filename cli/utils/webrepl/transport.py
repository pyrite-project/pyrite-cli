"""
WebREPL WebSocket 传输实现。

通过 WebSocket 连接 MicroPython WebREPL，完成 SHA256 挑战认证后
进入透传模式，所有数据通过 WebSocket 二进制帧传输。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from getpass import getpass
from typing import Optional

from ..log import get_logger
from ..transport.base import Transport

log = get_logger(__name__)
WEBREPL_DIGEST_HEX_CHARS = 9
MAX_HANDSHAKE_LINE_BYTES = 4096
MAX_EARLY_DATA_BYTES = 64 * 1024
MAX_WEBSOCKET_MESSAGE_BYTES = 1024 * 1024

try:
    import websocket
except ImportError:
    websocket = None  # type: ignore[assignment]


class WebREPLTransport(Transport):
    """基于 WebSocket 的 WebREPL 传输实现。

    协议：
    1. WebSocket 连接 ws://host:port/
    2. 服务器发送 JSON 挑战: ``{"uid":"...","nb":9}\\n``
    3. 客户端回复 SHA256(password+uid) 的前 nb 个十六进制字符
    4. 认证成功进入透传模式
    """

    def __init__(
        self,
        url: str,
        password: Optional[str] = None,
        timeout: float = 10,
    ) -> None:
        super().__init__()
        if timeout <= 0:
            raise ValueError("WebREPL timeout must be greater than zero")
        self.url = url
        self._password = password
        self.timeout = timeout
        self.ws: Optional[websocket.WebSocket] = None  # type: ignore[valid-type]
        self._line_buf = bytearray()

    def _resolve_password(self) -> str:
        if self._password:
            return self._password
        env_pw = os.environ.get("PYRITE_WEBREPL_PASSWORD")
        if env_pw:
            return env_pw
        return getpass("WebREPL 密码: ")

    def connect(self) -> None:
        if websocket is None:
            raise ImportError(
                "缺少 websocket-client 库，请安装: pip install websocket-client"
            )

        self.disconnect()
        pw = self._resolve_password()
        log.debug("连接 WebREPL: %s", self.url)
        self.ws = websocket.create_connection(
            self.url,
            timeout=self.timeout,
            redirect_limit=0,
        )
        try:
            getstatus = getattr(self.ws, "getstatus", None)
            if callable(getstatus) and getstatus() != 101:
                raise ConnectionError("WebREPL redirect or non-upgrade response rejected")
            self.ws.settimeout(min(0.05, self.timeout))

            challenge = self._recv_line(timeout=self.timeout)
            try:
                data = json.loads(challenge)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ConnectionError("invalid WebREPL challenge") from exc
            if not isinstance(data, dict) or set(data) != {"uid", "nb"}:
                raise ConnectionError("invalid WebREPL challenge")
            uid = data["uid"]
            nb = data["nb"]
            if (
                not isinstance(uid, str)
                or not 1 <= len(uid.encode("utf-8")) <= 128
                or not uid.isascii()
                or not uid.isprintable()
                or type(nb) is not int
                or nb != WEBREPL_DIGEST_HEX_CHARS
            ):
                raise ConnectionError("invalid WebREPL challenge")

            digest = hashlib.sha256(
                pw.encode("utf-8") + uid.encode("utf-8")
            ).hexdigest()
            self.ws.send((digest[:nb] + "\n").encode())

            response = self._recv_line(
                timeout=self.timeout,
                max_tail_bytes=MAX_EARLY_DATA_BYTES,
            )
            if not response.startswith(":"):
                raise ConnectionError("WebREPL authentication failed")
            if self._line_buf:
                self._rx_buf += bytes(self._line_buf)
                self._line_buf.clear()
            log.debug("WebREPL 认证成功")
        except Exception:
            self.disconnect()
            raise

    def disconnect(self) -> None:
        if self.ws is not None:
            log.debug("断开 WebREPL: %s", self.url)
            try:
                self.ws.close()
            except Exception as e:
                log.trace("断开 WebREPL 时忽略异常: %s", e)
            self.ws = None
        self._line_buf.clear()
        super().disconnect()

    def _recv_line(
        self,
        *,
        timeout: Optional[float] = None,
        max_bytes: int = MAX_HANDSHAKE_LINE_BYTES,
        max_tail_bytes: int = 0,
    ) -> str:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while self.ws is not None:
            newline = self._line_buf.find(b"\n")
            if newline >= 0:
                line_end = newline + 1
                if line_end > max_bytes:
                    raise ConnectionError("WebREPL handshake line exceeds safety limit")
                if len(self._line_buf) - line_end > max_tail_bytes:
                    raise ConnectionError("WebREPL handshake tail exceeds safety limit")
                line = bytes(self._line_buf[:line_end])
                del self._line_buf[:line_end]
                try:
                    return line.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ConnectionError("invalid WebREPL handshake encoding") from exc
            if len(self._line_buf) > max_bytes:
                raise ConnectionError("WebREPL handshake line exceeds safety limit")
            if time.monotonic() >= deadline:
                raise TimeoutError("WebREPL handshake timed out")
            try:
                chunk = self.ws.recv()
                if not chunk:
                    raise ConnectionError("WebREPL peer closed during handshake")
                if isinstance(chunk, bytes):
                    self._line_buf.extend(chunk)
                else:
                    self._line_buf.extend(chunk.encode("utf-8"))
            except websocket.WebSocketTimeoutException:  # type: ignore[misc]
                continue
        raise ConnectionError("WebREPL disconnected during handshake")

    def _raw_write(self, data: bytes) -> None:
        if self.ws is None:
            raise ConnectionError("WebREPL 未连接")
        self.ws.send(data, websocket.ABNF.OPCODE_BINARY)  # type: ignore[union-attr]

    def _raw_read(self, size: int) -> bytes:
        return b""

    def _raw_in_waiting(self) -> int:
        return 0

    def _fill_buf(self) -> None:
        if self.ws is None:
            return
        try:
            data = self.ws.recv()
            if not data:
                raise ConnectionError("WebREPL peer closed")
            if isinstance(data, str):
                data = data.encode("utf-8")
            if len(data) > MAX_WEBSOCKET_MESSAGE_BYTES:
                raise ConnectionError("WebREPL message exceeds safety limit")
            self._rx_buf += data
        except websocket.WebSocketTimeoutException:  # type: ignore[misc]
            pass
        except Exception:
            self.disconnect()
            raise

    @property
    def is_connected(self) -> bool:
        return self.ws is not None and bool(getattr(self.ws, "connected", True))

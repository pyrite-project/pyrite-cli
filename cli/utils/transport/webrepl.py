"""
WebREPL WebSocket 传输实现。

通过 WebSocket 连接 MicroPython WebREPL，完成 SHA256 挑战认证后
进入透传模式，所有数据通过 WebSocket 二进制帧传输。
"""

from __future__ import annotations

import hashlib
import json
import os
from getpass import getpass
from typing import Optional

from ..log import get_logger
from .base import Transport

log = get_logger(__name__)

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

    def __init__(self, url: str, password: Optional[str] = None) -> None:
        super().__init__()
        self.url = url
        self._password = password
        self.ws: Optional[websocket.WebSocket] = None  # type: ignore[valid-type]

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
        self.ws = websocket.create_connection(self.url, timeout=10)
        self.ws.settimeout(0.05)

        challenge = self._recv_line()
        data = json.loads(challenge)
        uid = data["uid"]
        nb = data["nb"]

        digest = hashlib.sha256(
            pw.encode("utf-8") + uid.encode("utf-8")
        ).hexdigest()
        self.ws.send((digest[:nb] + "\n").encode())

        response = self._recv_line()
        if not response.startswith(":"):
            raise ConnectionError(f"WebREPL 认证失败: {response.strip()}")
        log.debug("WebREPL 认证成功")

    def disconnect(self) -> None:
        if self.ws is not None:
            log.debug("断开 WebREPL: %s", self.url)
            try:
                self.ws.close()
            except Exception as e:
                log.trace("断开 WebREPL 时忽略异常: %s", e)
            self.ws = None
        super().disconnect()

    def _recv_line(self) -> str:
        buf = b""
        while self.ws is not None:
            try:
                chunk = self.ws.recv()
                if isinstance(chunk, bytes):
                    buf += chunk
                else:
                    buf += chunk.encode()
                if b"\n" in buf:
                    break
            except websocket.WebSocketTimeoutException:  # type: ignore[misc]
                continue
        return buf.decode("utf-8")

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
            if data:
                if isinstance(data, str):
                    data = data.encode("utf-8")
                self._rx_buf += data
        except websocket.WebSocketTimeoutException:  # type: ignore[misc]
            pass
        except Exception:
            pass

    @property
    def is_connected(self) -> bool:
        return self.ws is not None

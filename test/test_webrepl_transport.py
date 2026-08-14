from __future__ import annotations

from types import SimpleNamespace

import pytest
import websocket

from cli.utils.webrepl.transport import WebREPLTransport


class FakeWebSocket:
    def __init__(self, chunks=(), *, status: int = 101) -> None:
        self.chunks = list(chunks)
        self.status = status
        self.connected = True
        self.closed = 0
        self.sent = []
        self.timeout = None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def getstatus(self) -> int:
        return self.status

    def recv(self):
        if not self.chunks:
            raise websocket.WebSocketTimeoutException("idle")
        item = self.chunks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def send(self, data, opcode=None) -> None:
        self.sent.append((data, opcode))

    def close(self) -> None:
        self.closed += 1
        self.connected = False


def _install_fake_websocket(monkeypatch, fake: FakeWebSocket):
    created = {}

    def create_connection(url, **kwargs):
        created.update({"url": url, **kwargs})
        return fake

    monkeypatch.setattr(
        "cli.utils.webrepl.transport.websocket.create_connection",
        create_connection,
    )
    return created


def test_handshake_line_has_total_timeout() -> None:
    transport = WebREPLTransport("ws://device.local:8266", password="secret")
    transport.ws = FakeWebSocket()

    with pytest.raises(TimeoutError, match="handshake.*timed out"):
        transport._recv_line(timeout=0.001)


def test_handshake_line_rejects_oversized_device_input() -> None:
    transport = WebREPLTransport("ws://device.local:8266", password="secret")
    transport.ws = FakeWebSocket([b"x" * 9])

    with pytest.raises(ConnectionError, match="handshake line exceeds"):
        transport._recv_line(timeout=1, max_bytes=8)


@pytest.mark.parametrize("nb", [-1, 0, 8, 10, 64, "9", True])
def test_connect_rejects_non_protocol_digest_length(monkeypatch, nb) -> None:
    fake = FakeWebSocket([
        ('{"uid":"a1b2c3","nb":' + repr(nb).lower() + '}\n').encode(),
        b":ok\n",
    ])
    _install_fake_websocket(monkeypatch, fake)
    transport = WebREPLTransport("ws://device.local:8266", password="secret")

    with pytest.raises(ConnectionError, match="invalid WebREPL challenge"):
        transport.connect()

    assert transport.is_connected is False
    assert fake.closed == 1
    assert fake.sent == []


def test_authentication_failure_closes_socket(monkeypatch) -> None:
    fake = FakeWebSocket([
        b'{"uid":"a1b2c3","nb":9}\n',
        b"denied\n",
    ])
    _install_fake_websocket(monkeypatch, fake)
    transport = WebREPLTransport("ws://device.local:8266", password="secret")

    with pytest.raises(ConnectionError, match="authentication failed"):
        transport.connect()

    assert transport.is_connected is False
    assert fake.closed == 1


def test_connect_preserves_bounded_raw_data_after_auth_line(monkeypatch) -> None:
    fake = FakeWebSocket([
        b'{"uid":"a1b2c3","nb":9}\n',
        b":ok\nraw REPL; CTRL-B to exit\r\n>",
    ])
    created = _install_fake_websocket(monkeypatch, fake)
    transport = WebREPLTransport(
        "ws://device.local:8266",
        password="secret",
        timeout=3,
    )

    transport.connect()

    assert transport.read() == b"raw REPL; CTRL-B to exit\r\n>"
    assert created["timeout"] == 3
    assert created["redirect_limit"] == 0


def test_fill_buf_disconnects_on_peer_error() -> None:
    transport = WebREPLTransport("ws://device.local:8266", password="secret")
    fake = FakeWebSocket([ConnectionResetError("peer closed")])
    transport.ws = fake

    with pytest.raises(ConnectionResetError, match="peer closed"):
        transport._fill_buf()

    assert transport.is_connected is False
    assert fake.closed == 1


def test_is_connected_checks_underlying_socket_state() -> None:
    transport = WebREPLTransport("ws://device.local:8266", password="secret")
    transport.ws = SimpleNamespace(connected=False)

    assert transport.is_connected is False

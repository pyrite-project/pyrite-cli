import base64
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cli.main import app
from cli.utils.tunnel import (
    FrameDecoder,
    NetworkPolicy,
    NetworkRequest,
    TunnelFrame,
    TunnelSecurityError,
    build_network_response,
    decode_frame_line,
    encode_frame,
    encode_key_event,
    is_exit_key,
    load_device_script,
    run_tunnel_session,
    validate_network_request,
)


runner = CliRunner()


def test_frame_decoder_handles_noise_and_partial_lines():
    line = encode_frame(
        TunnelFrame(
            type="request",
            id=7,
            op="http",
            payload={"method": "GET", "url": "https://example.com/data"},
        )
    )
    decoder = FrameDecoder()

    frames = decoder.feed("boot noise\n" + line[:16])
    assert frames == []

    frames = decoder.feed(line[16:] + "plain output\n")
    assert frames == [
        TunnelFrame(
            type="request",
            id=7,
            op="http",
            payload={"method": "GET", "url": "https://example.com/data"},
        )
    ]

    assert decode_frame_line(line).payload["url"] == "https://example.com/data"


def test_keyboard_event_encoding_common_keys_and_reserved_exit_keys():
    assert encode_key_event(b"a") == {"kind": "char", "key": "a", "text": "a"}
    assert encode_key_event(b"\r") == {"kind": "key", "key": "enter", "text": "\n"}
    assert encode_key_event(b"\x7f") == {"kind": "key", "key": "backspace", "text": ""}
    assert encode_key_event(b"\x1b[A") == {"kind": "key", "key": "up", "text": ""}
    assert encode_key_event(b"\x1b[D") == {"kind": "key", "key": "left", "text": ""}

    assert is_exit_key(b"\x03")
    assert is_exit_key(b"\x1d")
    assert encode_key_event(b"\x03") is None


def test_network_policy_validates_method_allowlist_and_private_targets():
    policy = NetworkPolicy(allow_hosts=("example.com",))
    request = NetworkRequest(
        method="GET",
        url="https://api.example.com/data",
    )

    with patch(
        "cli.utils.tunnel.tunnel.socket.getaddrinfo",
        return_value=[
            (None, None, None, "", ("93.184.216.34", 443)),
        ],
    ):
        assert validate_network_request(request, policy) == request

    with pytest.raises(TunnelSecurityError, match="allowlist"):
        validate_network_request(
            NetworkRequest(method="GET", url="https://other.test/"),
            policy,
        )

    with pytest.raises(TunnelSecurityError, match="private"):
        validate_network_request(
            NetworkRequest(method="GET", url="http://127.0.0.1:8080/"),
            NetworkPolicy(allow_hosts=(), allow_private=False),
        )

    with pytest.raises(TunnelSecurityError, match="method"):
        validate_network_request(
            NetworkRequest(method="PATCH", url="https://example.com/"),
            policy,
        )


def test_network_policy_rejects_private_dns_resolution():
    with patch(
        "cli.utils.tunnel.tunnel.socket.getaddrinfo",
        return_value=[
            (None, None, None, "", ("127.0.0.1", 443)),
        ],
    ):
        with pytest.raises(TunnelSecurityError, match="private"):
            validate_network_request(
                NetworkRequest(method="GET", url="https://api.example.com/"),
                NetworkPolicy(allow_hosts=("example.com",), allow_private=False),
            )


def test_tunnel_session_enters_raw_repl_before_streaming_script():
    calls = []

    class Transport:
        def __init__(self):
            self.chunks = [b"\x04"]

        @property
        def in_waiting(self):
            return len(self.chunks[0]) if self.chunks else 0

        def read(self, _size):
            return self.chunks.pop(0)

    def enter_raw():
        calls.append(("raw", None))

    def write(data):
        calls.append(("write", data))

    mp = SimpleNamespace(
        transport=Transport(),
        _enter_raw_repl=enter_raw,
        _write=write,
    )

    run_tunnel_session(mp, "print('hello')", lambda _frame: None)

    assert calls[0] == ("raw", None)
    assert calls[1] == ("write", "print('hello')\n")
    assert calls[2] == ("write", b"\x04")


def test_network_response_caps_body_and_header_summary():
    payload = b"abcdef"

    response = build_network_response(
        status_code=200,
        headers={
            "Content-Type": "text/plain",
            "Content-Length": str(len(payload)),
            "Set-Cookie": "token=secret",
            "X-Debug": "ignored",
        },
        body=payload,
        max_body_bytes=4,
    )

    assert response == {
        "status": 200,
        "headers": {
            "content-type": "text/plain",
            "content-length": "6",
        },
        "body_b64": base64.b64encode(b"abcd").decode("ascii"),
        "truncated": True,
        "size": 6,
    }


def test_cli_exposes_tunnel_help():
    for args in (
        ["tunnel", "--help"],
        ["tunnel", "kb", "--help"],
        ["tunnel", "network", "--help"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.stdout
        assert "tunnel" in result.stdout.lower()


def test_packaged_network_helper_dispatches_stdin_commands():
    script = load_device_script("network.py")

    assert "cmd = line.split(None, 2)" in script
    assert 'request(method, url)' in script
    assert 'request(method, url, body_b64=body_b64)' in script

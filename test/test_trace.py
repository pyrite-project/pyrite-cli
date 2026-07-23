from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from cli.main import app
from cli.utils.trace import (
    TRACE_SCHEMA_VERSION,
    TraceRecorder,
    format_trace_summary,
    format_trace_view,
    load_trace,
    redact_value,
    render_control_bytes,
    summarize_trace,
)


runner = CliRunner()


def test_render_control_bytes_uses_protocol_names() -> None:
    text = render_control_bytes(b"\x01print('x')\x03\x04\x04>")

    assert text == "<RAW>print('x')<C><D><D>>"
    assert "\x01" not in text
    assert "\x04" not in text


def test_trace_recorder_writes_schema_phase_and_redacted_metadata(tmp_path: Path) -> None:
    trace_path = tmp_path / "session.pyrite-trace"
    recorder = TraceRecorder(
        trace_path,
        operation="flash",
        port="COM3",
        session_id="trace-test",
        metadata={"password": "secret", "baudrate": 115200},
    )

    recorder.event("phase_start", phase="raw_repl")
    recorder.traffic("TX", b"\x01print('x')\x04", phase="raw_repl")
    recorder.close(status="ok")

    records = load_trace(trace_path)
    assert records[0]["type"] == "trace_event"
    assert records[0]["schema_version"] == TRACE_SCHEMA_VERSION
    assert records[0]["session_id"] == "trace-test"
    assert records[0]["event"] == "session_start"
    assert records[0]["metadata"]["password"] == "<redacted>"

    tx = next(record for record in records if record["event"] == "traffic")
    assert tx["phase"] == "raw_repl"
    assert tx["direction"] == "TX"
    assert tx["byte_count"] == len(b"\x01print('x')\x04")
    assert tx["text"] == "<RAW>print('x')<D>"


def test_redact_value_handles_sensitive_keys_and_inline_values() -> None:
    redacted = redact_value({
        "password": "abc123",
        "api_token": "token-123",
        "port": "COM3",
        "url": "ws://host/repl?password=abc123&token=token-123",
        "headers": "Authorization: Bearer token-123\nsecret = abc123",
    })

    rendered = json.dumps(redacted, ensure_ascii=False)
    assert redacted["password"] == "<redacted>"
    assert redacted["api_token"] == "<redacted>"
    assert redacted["port"] == "COM3"
    assert "abc123" not in rendered
    assert "token-123" not in rendered


def test_summarize_trace_reports_counts_tail_and_failures(tmp_path: Path) -> None:
    trace_path = tmp_path / "failed.pyrite-trace"
    recorder = TraceRecorder(trace_path, operation="flash", port="COM4", session_id="sum-test")
    recorder.traffic("TX", b"\x01", phase="raw_repl")
    recorder.traffic("RX", b"OK\x04", phase="raw_repl")
    recorder.failure(RuntimeError("upload failed password=secret"), phase="transfer")
    recorder.close(status="error")

    summary = summarize_trace(trace_path, tail=3)

    assert summary["session_id"] == "sum-test"
    assert summary["operation"] == "flash"
    assert summary["status"] == "error"
    assert summary["traffic"]["TX"]["bytes"] == 1
    assert summary["traffic"]["RX"]["bytes"] == 3
    assert summary["phases"]["raw_repl"]["traffic_events"] == 2
    assert summary["failures"][0]["error_type"] == "RuntimeError"
    assert "secret" not in summary["failures"][0]["message"]
    assert summary["recommendations"][0]["id"] == "attach_trace_on_failure"
    assert len(summary["tail"]) == 3

    text = format_trace_summary(summary)
    assert "sum-test" in text
    assert "RuntimeError" in text
    assert "recommendations:" in text
    assert "secret" not in text


def test_trace_view_and_summarize_commands(tmp_path: Path) -> None:
    trace_path = tmp_path / "view.pyrite-trace"
    recorder = TraceRecorder(trace_path, operation="flash", port="COM5", session_id="view-test")
    recorder.traffic("TX", b"\x01", phase="raw_repl")
    recorder.traffic("RX", b"OK\x04", phase="raw_repl")
    recorder.close(status="ok")

    view = format_trace_view(load_trace(trace_path))
    assert "view-test" in view
    assert "TX" in view
    assert "<RAW>" in view

    result = runner.invoke(app, ["trace", "view", str(trace_path)])
    assert result.exit_code == 0
    assert "<RAW>" in result.stdout

    result = runner.invoke(app, ["trace", "summarize", str(trace_path), "--format", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["session_id"] == "view-test"


def test_flash_trace_option_attaches_recorder_and_writes_session(tmp_path: Path) -> None:
    local_file = tmp_path / "main.py"
    local_file.write_text("print('x')\n", encoding="utf-8")
    trace_path = tmp_path / "flash.pyrite-trace"
    mp = MagicMock()
    mp.config = SimpleNamespace(board_tags={})
    mp.baudrate = 460800
    mp.timeout = 23

    with patch("cli.main._mp_factory", return_value=mp):
        result = runner.invoke(app, [
            "flash",
            "COM3",
            str(local_file),
            "/main.py",
            "--target",
            "ESP32",
            "--no-compile",
            "--force",
            "--trace",
            "--trace-path",
            str(trace_path),
            "--no-safe-main",
        ])

    assert result.exit_code == 0
    mp.set_trace_recorder.assert_called_once()
    mp.flash_file.assert_called_once()

    records = load_trace(trace_path)
    assert records[0]["event"] == "session_start"
    assert records[0]["operation"] == "flash"
    assert records[0]["metadata"]["baudrate"] == 460800
    assert records[0]["metadata"]["timeout"] == 23
    assert records[-1]["event"] == "session_end"
    assert records[-1]["status"] == "ok"

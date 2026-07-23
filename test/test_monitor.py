import json
import time
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from cli.main import app
from cli.utils.diagnostics import (
    MonitorError,
    build_options,
    build_pin_probe_script,
    build_sampling_script,
    build_single_sample_script,
    build_streaming_sample_script,
    format_uart_monitor_header,
    format_uart_monitor_sample,
    format_monitor_header,
    format_monitor_sample,
    parse_pin_list,
    parse_sample_output,
    parse_uart_ports,
    resolve_monitor_pins,
    run_monitor,
    run_monitor_session,
    run_uart_monitor_session,
)


runner = CliRunner()


class FakeSerialTransport:
    def __init__(self, chunks, *, read_delay=0.0):
        self._chunks = list(chunks)
        self._read_delay = read_delay
        self.connected = False

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    @property
    def in_waiting(self):
        if not self._chunks:
            return 0
        return len(self._chunks[0])

    def read(self, size=-1):
        if not self._chunks:
            return b""
        if self._read_delay:
            time.sleep(self._read_delay)
        chunk = self._chunks.pop(0)
        if size < 0 or size >= len(chunk):
            return chunk
        self._chunks.insert(0, chunk[size:])
        return chunk[:size]


class FakeRawTransport:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    @property
    def in_waiting(self):
        if not self._chunks:
            return 0
        return len(self._chunks[0])

    def read(self, size=-1):
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if size < 0 or size >= len(chunk):
            return chunk
        self._chunks.insert(0, chunk[size:])
        return chunk[:size]


class FakeRawMicroPython:
    def __init__(self, chunks):
        self.transport = FakeRawTransport(chunks)
        self.writes = []
        self.entered_raw_repl = False

    def _enter_raw_repl(self):
        self.entered_raw_repl = True

    def _write(self, data):
        self.writes.append(data)

    def _record_rx(self, _data):
        pass


class TestParsePinList:
    def test_comma_separated_pins(self):
        assert parse_pin_list("0,2,4,5") == [0, 2, 4, 5]

    def test_whitespace_is_ignored(self):
        assert parse_pin_list(" 0, 2 ,4 ") == [0, 2, 4]

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_list_is_rejected(self, value):
        with pytest.raises(MonitorError, match="empty"):
            parse_pin_list(value)

    def test_empty_value_is_rejected(self):
        with pytest.raises(MonitorError, match="empty pin value at position 2"):
            parse_pin_list("0,,2")

    @pytest.mark.parametrize("value", ["A", "-1", "1.5"])
    def test_invalid_value_is_rejected(self, value):
        with pytest.raises(MonitorError, match="non-negative integer"):
            parse_pin_list(f"0,{value},2")

    def test_duplicate_value_is_rejected(self):
        with pytest.raises(MonitorError, match="duplicate pin 2"):
            parse_pin_list("0,2,02")


class TestMonitorFormatting:
    def test_text_sample_uses_pin_value_pairs(self):
        assert format_monitor_sample([0, 2], [1, 0], fmt="text", seq=3) == "0=1 2=0"

    def test_modern_text_sample_uses_readable_gpio_states(self):
        assert (
            format_monitor_sample([0, 2], [1, 0], fmt="text", seq=3, style="modern")
            == "#0003 | GPIO0: HIGH | GPIO2: LOW"
        )

    def test_monitor_header_contains_title_and_session_details(self):
        header = format_monitor_header(
            build_options([0, 2], interval=0.25, count=1),
            port="COM3",
        )

        assert "PYRITE GPIO MONITOR" in header
        assert "Port: COM3" in header
        assert "Pins: GPIO0, GPIO2" in header
        assert "Interval: 0.25s" in header
        assert "Limit: 1 sample" in header

    def test_json_sample_is_json_line_payload(self):
        line = format_monitor_sample([0, 2], [1, 0], fmt="json", seq=3)

        assert json.loads(line) == {"seq": 3, "pins": {"0": 1, "2": 0}}


class TestUartMonitorFormatting:
    def test_uart_ports_accept_comma_separated_values(self):
        assert parse_uart_ports("COM3, COM4") == ["COM3", "COM4"]

    def test_uart_header_uses_tab_separated_columns(self):
        header = format_uart_monitor_header(["COM3", "COM4"])

        assert "PYRITE UART MONITOR" in header
        assert "Port\tRaw\tHex\tString" in header

    def test_uart_sample_shows_raw_hex_and_string_columns(self):
        line = format_uart_monitor_sample("COM3", b"hello")

        assert line == "COM3\tb'hello'\t68 65 6c 6c 6f\thello"

    def test_uart_sample_marks_decode_failures(self):
        line = format_uart_monitor_sample("COM3", b"\xff")

        assert line == "COM3\tb'\\xff'\tff\t<decode failed>"


class TestProbeScript:
    def test_probe_script_checks_candidates_as_inputs_only(self):
        script = build_pin_probe_script([0, 2, 4])

        assert "_candidates=[0, 2, 4]" in script
        assert "machine.Pin(_pin,machine.Pin.IN)" in script.replace(" ", "")
        assert "Pin.OUT" not in script
        assert "PULL" not in script

    def test_default_probe_filters_to_detected_valid_pins(self):
        mp = MagicMock()
        mp.run.return_value = "PYRITE_MONITOR_PINS:0,2,4\n"

        pins = resolve_monitor_pins(mp, explicit_pins=None, candidates=[0, 1, 2, 4])

        assert pins == [0, 2, 4]
        assert mp.run.call_count == 1
        assert "_candidates=[0, 1, 2, 4]" in mp.run.call_args.args[0]

    def test_default_probe_requires_at_least_one_valid_pin(self):
        mp = MagicMock()
        mp.run.return_value = "PYRITE_MONITOR_PINS:\n"

        with pytest.raises(MonitorError, match="no usable GPIO pins"):
            resolve_monitor_pins(mp, explicit_pins=None, candidates=[0])


class TestSamplingScript:
    def test_sampling_script_reads_selected_pins_as_inputs_only(self):
        script = build_sampling_script([0, 2], fmt="text", count=1)

        compile(script, "<monitor>", "exec")
        assert "_pins=[0, 2]" in script
        assert "machine.Pin(_pin,machine.Pin.IN)" in script.replace(" ", "")
        assert "Pin.OUT" not in script
        assert "PULL" not in script

    def test_sampling_script_carries_monitor_options(self):
        script = build_sampling_script(
            [0, 2],
            fmt="json",
            interval=0.2,
            duration=1.5,
            count=3,
            edge="changed",
        )

        assert "_fmt='json'" in script
        assert "_interval=0.2" in script
        assert "_duration_ms=1500" in script
        assert "_count=3" in script
        assert "_edge_changed=True" in script
        assert '"seq":%d' in script

    def test_invalid_edge_is_rejected(self):
        with pytest.raises(MonitorError, match="edge"):
            build_sampling_script([0], edge="rising")

    def test_streaming_script_emits_marked_samples(self):
        script = build_streaming_sample_script([0, 2], interval=0.2, count=3)

        compile(script, "<monitor-stream>", "exec")
        assert "_pins=[0, 2]" in script
        assert "PYRITE_MONITOR_SAMPLE:" in script
        assert "_interval=0.2" in script
        assert "_count=3" in script


class TestSingleSampleScript:
    def test_single_sample_script_reads_inputs_once(self):
        script = build_single_sample_script([0, 2])

        compile(script, "<monitor-sample>", "exec")
        assert "_pins=[0, 2]" in script
        assert "PYRITE_MONITOR_SAMPLE:" in script
        assert "machine.Pin(_pin,machine.Pin.IN)" in script.replace(" ", "")
        assert "Pin.OUT" not in script
        assert "PULL" not in script

    def test_parse_sample_output_uses_marker_line(self):
        assert parse_sample_output("noise\nPYRITE_MONITOR_SAMPLE:1,0\n", 2) == [1, 0]

    def test_parse_sample_output_rejects_wrong_value_count(self):
        with pytest.raises(MonitorError, match="expected 2 values"):
            parse_sample_output("PYRITE_MONITOR_SAMPLE:1\n", 2)


class TestRunMonitor:
    def test_explicit_pins_run_one_sampling_script(self):
        mp = MagicMock()
        mp.run.return_value = "0=1 2=0\n"

        output = run_monitor(mp, pins="0,2", count=1)

        assert output == "0=1 2=0\n"
        assert mp.run.call_count == 1
        script = mp.run.call_args.args[0]
        assert "_pins=[0, 2]" in script

    def test_default_pins_probe_then_run_sampling_script(self):
        mp = MagicMock()
        mp.run.side_effect = ["PYRITE_MONITOR_PINS:0,2\n", "0=1 2=0\n"]

        output = run_monitor(mp, pins=None, candidates=[0, 1, 2], count=1)

        assert output == "0=1 2=0\n"
        assert mp.run.call_count == 2
        assert "_candidates=[0, 1, 2]" in mp.run.call_args_list[0].args[0]
        assert "_pins=[0, 2]" in mp.run.call_args_list[1].args[0]

    def test_session_polls_with_short_scripts_and_writes_each_sample(self):
        mp = MagicMock()
        mp.run.side_effect = [
            "PYRITE_MONITOR_SAMPLE:1,0\n",
            "PYRITE_MONITOR_SAMPLE:0,0\n",
        ]
        lines = []
        sleeps = []

        emitted = run_monitor_session(
            mp,
            pins="0,2",
            count=2,
            interval=0.2,
            write=lines.append,
            sleep=sleeps.append,
        )

        assert emitted == 2
        assert lines == ["0=1 2=0", "0=0 2=0"]
        assert sleeps == [0.2]
        assert mp.run.call_count == 2
        assert all("PYRITE_MONITOR_SAMPLE:" in call.args[0] for call in mp.run.call_args_list)

    def test_session_edge_changed_skips_repeated_values(self):
        mp = MagicMock()
        mp.run.side_effect = [
            "PYRITE_MONITOR_SAMPLE:1\n",
            "PYRITE_MONITOR_SAMPLE:1\n",
            "PYRITE_MONITOR_SAMPLE:0\n",
        ]
        lines = []

        emitted = run_monitor_session(
            mp,
            pins=[0],
            count=3,
            edge="changed",
            write=lines.append,
            sleep=lambda _seconds: None,
        )

        assert emitted == 2
        assert lines == ["0=1", "0=0"]

    def test_session_can_emit_modern_rows_after_start_callback(self):
        mp = MagicMock()
        mp.run.return_value = "PYRITE_MONITOR_SAMPLE:1,0\n"
        lines = []
        starts = []

        emitted = run_monitor_session(
            mp,
            pins="0,2",
            count=1,
            sample_style="modern",
            on_start=starts.append,
            write=lines.append,
        )

        assert emitted == 1
        assert starts[0].pins == (0, 2)
        assert lines == ["#0000 | GPIO0: HIGH | GPIO2: LOW"]

    def test_session_skips_transient_missing_marker_sample(self):
        mp = MagicMock()
        mp.run.side_effect = [
            "PYRITE_MONITOR_SAMPLE:1\n",
            "noise without marker\n",
            "PYRITE_MONITOR_SAMPLE:0\n",
        ]
        lines = []
        errors = []

        emitted = run_monitor_session(
            mp,
            pins=[0],
            count=3,
            interval=0.001,
            write=lines.append,
            sleep=lambda _seconds: None,
            on_sample_error=lambda exc, output: errors.append((str(exc), output)),
        )

        assert emitted == 2
        assert lines == ["0=1", "0=0"]
        assert errors == [
            ("GPIO sample output missing monitor marker", "noise without marker\n")
        ]

    def test_session_streams_samples_without_per_sample_run_calls(self):
        mp = FakeRawMicroPython([
            b"OKPYRITE_MONITOR_SAMPLE:1\n",
            b"PYRITE_MONITOR_SAMPLE:0\n\x04",
        ])
        lines = []

        emitted = run_monitor_session(
            mp,
            pins=[0],
            count=2,
            interval=0.001,
            write=lines.append,
            stream=True,
        )

        assert emitted == 2
        assert lines == ["0=1", "0=0"]
        assert mp.entered_raw_repl is True
        assert len(mp.writes) == 2


class TestRunUartMonitor:
    def test_session_reads_each_uart_and_writes_tab_rows(self):
        transports = [
            ("COM3", FakeSerialTransport([b"hello"])),
            ("COM4", FakeSerialTransport([b"\xff"])),
        ]
        lines = []

        emitted = run_uart_monitor_session(
            transports,
            count=1,
            write=lines.append,
            sleep=lambda _seconds: None,
        )

        assert emitted == 2
        assert lines == [
            "COM3\tb'hello'\t68 65 6c 6c 6f\thello",
            "COM4\tb'\\xff'\tff\t<decode failed>",
        ]

    def test_slow_uart_read_does_not_block_other_ports(self):
        transports = [
            ("COM3", FakeSerialTransport([b"slow"], read_delay=0.2)),
            ("COM4", FakeSerialTransport([b"fast"])),
        ]
        lines = []

        started = time.monotonic()
        emitted = run_uart_monitor_session(
            transports,
            duration=0.05,
            interval=0.01,
            write=lines.append,
        )
        elapsed = time.monotonic() - started

        assert emitted >= 1
        assert "COM4\tb'fast'\t66 61 73 74\tfast" in lines
        assert elapsed < 0.15


class TestMonitorCli:
    def test_monitor_command_prints_modern_text_panel(self, monkeypatch):
        mp = MagicMock()
        mp.run.return_value = "PYRITE_MONITOR_SAMPLE:1,0\n"
        monkeypatch.setattr("cli.main._mp_factory", lambda *_args: mp)

        result = runner.invoke(app, [
            "monitor",
            "COM3",
            "--pins",
            "0,2",
            "--count",
            "1",
        ])

        assert result.exit_code == 0
        assert "PYRITE GPIO MONITOR" in result.stdout
        assert "Port: COM3" in result.stdout
        assert "#0000 | GPIO0: HIGH | GPIO2: LOW" in result.stdout
        mp.connect.assert_called_once()
        mp.disconnect.assert_called_once()

    def test_monitor_command_connects_and_prints_json_line(self, monkeypatch):
        mp = MagicMock()
        mp.run.return_value = "PYRITE_MONITOR_SAMPLE:1,0\n"
        monkeypatch.setattr("cli.main._mp_factory", lambda *_args: mp)

        result = runner.invoke(app, [
            "monitor",
            "COM3",
            "--pins",
            "0,2",
            "--count",
            "1",
            "--format",
            "json",
        ])

        assert result.exit_code == 0
        assert result.stdout.strip() == '{"seq":0,"pins":{"0":1,"2":0}}'
        mp.connect.assert_called_once()
        mp.disconnect.assert_called_once()

    def test_monitor_command_uart_reads_serial_transport_rows(self, monkeypatch):
        transports = {
            "COM3": FakeSerialTransport([b"hello"]),
            "COM4": FakeSerialTransport([b"\xff"]),
        }
        monkeypatch.setattr(
            "cli.main._serial_transport_factory",
            lambda port, *_args: transports[port],
        )

        result = runner.invoke(app, [
            "monitor",
            "COM3,COM4",
            "--uart",
            "--count",
            "1",
        ])

        assert result.exit_code == 0
        assert "PYRITE UART MONITOR" in result.stdout
        assert "Port\tRaw\tHex\tString" in result.stdout
        assert "COM3\tb'hello'\t68 65 6c 6c 6f\thello" in result.stdout
        assert "COM4\tb'\\xff'\tff\t<decode failed>" in result.stdout
        assert transports["COM3"].connected is False
        assert transports["COM4"].connected is False

    def test_monitor_command_uart_resolves_each_board_alias(self, tmp_path, monkeypatch):
        alias_file = tmp_path / "aliases.json"
        alias_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "aliases": {"left": "COM3", "right": "COM4"},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("PYRITE_BOARD_ALIAS_FILE", str(alias_file))
        transports = {
            "COM3": FakeSerialTransport([b"left"]),
            "COM4": FakeSerialTransport([b"right"]),
        }
        monkeypatch.setattr(
            "cli.main._serial_transport_factory",
            lambda port, *_args: transports[port],
        )

        result = runner.invoke(
            app,
            ["monitor", "@left,@right", "--uart", "--count", "1"],
        )

        assert result.exit_code == 0, result.output
        assert "COM3" in result.stdout
        assert "COM4" in result.stdout

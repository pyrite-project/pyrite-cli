"""Pure helpers for GPIO and UART monitoring commands."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
import json
import time
from typing import Callable, Iterable, Sequence


DEFAULT_GPIO_CANDIDATES = (
    0,
    1,
    2,
    3,
    4,
    5,
    12,
    13,
    14,
    15,
    16,
    17,
    18,
    19,
    21,
    22,
    23,
    25,
    26,
    27,
    32,
    33,
    34,
    35,
    36,
    39,
)

PROBE_MARKER = "PYRITE_MONITOR_PINS:"
SAMPLE_MARKER = "PYRITE_MONITOR_SAMPLE:"
MONITOR_TITLE = "PYRITE GPIO MONITOR"
UART_MONITOR_TITLE = "PYRITE UART MONITOR"
UART_DECODE_FAILED = "<decode failed>"


class MonitorError(ValueError):
    """Raised when monitor arguments or device probe output are invalid."""


@dataclass(frozen=True)
class MonitorOptions:
    pins: tuple[int, ...]
    fmt: str = "text"
    interval: float = 0.5
    duration: float | None = None
    count: int | None = None
    edge: str | None = None


@dataclass(frozen=True)
class UartMonitorOptions:
    ports: tuple[str, ...]
    fmt: str = "text"
    interval: float = 0.05
    duration: float | None = None
    count: int | None = None
    encoding: str = "utf-8"
    read_size: int = 4096


def parse_pin_list(value: str) -> list[int]:
    """Parse a comma-separated GPIO list such as ``0,2,4,5``."""
    if value is None:
        raise MonitorError("pin list is empty")

    text = value.strip()
    if not text:
        raise MonitorError("pin list is empty")

    pins: list[int] = []
    seen: set[int] = set()
    for position, raw_token in enumerate(value.split(","), start=1):
        token = raw_token.strip()
        if not token:
            raise MonitorError(f"empty pin value at position {position}")
        if not token.isdigit():
            raise MonitorError(
                f"invalid pin {token!r} at position {position}; "
                "expected a non-negative integer"
            )
        pin = int(token, 10)
        if pin in seen:
            raise MonitorError(f"duplicate pin {pin}")
        pins.append(pin)
        seen.add(pin)
    return pins


def _normalize_pin_sequence(pins: Iterable[int], label: str = "pin list") -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for position, pin in enumerate(pins, start=1):
        if isinstance(pin, bool) or not isinstance(pin, int) or pin < 0:
            raise MonitorError(
                f"invalid {label} value {pin!r} at position {position}; "
                "expected a non-negative integer"
            )
        if pin in seen:
            raise MonitorError(f"duplicate pin {pin}")
        normalized.append(pin)
        seen.add(pin)

    if not normalized:
        raise MonitorError(f"{label} is empty")
    return normalized


def normalize_pins(pins: str | Sequence[int]) -> list[int]:
    if isinstance(pins, str):
        return parse_pin_list(pins)
    return _normalize_pin_sequence(pins)


def parse_uart_ports(value: str | Sequence[str]) -> list[str]:
    """Parse one or more serial ports for UART monitor mode."""
    if value is None:
        raise MonitorError("UART port list is empty")

    raw_ports = value.split(",") if isinstance(value, str) else list(value)
    ports: list[str] = []
    seen: set[str] = set()
    for position, raw_port in enumerate(raw_ports, start=1):
        port = str(raw_port).strip()
        if not port:
            raise MonitorError(f"empty UART port value at position {position}")
        if port in seen:
            raise MonitorError(f"duplicate UART port {port}")
        ports.append(port)
        seen.add(port)

    if not ports:
        raise MonitorError("UART port list is empty")
    return ports


def normalize_format(fmt: str) -> str:
    value = fmt.lower()
    if value == "jsonl":
        value = "json"
    if value not in {"text", "json"}:
        raise MonitorError("format must be text or json")
    return value


def normalize_edge(edge: str | None) -> str | None:
    if edge is None or edge == "":
        return None
    value = edge.lower()
    if value != "changed":
        raise MonitorError("edge must be changed")
    return value


def build_options(
    pins: Sequence[int],
    *,
    fmt: str = "text",
    interval: float = 0.5,
    duration: float | None = None,
    count: int | None = None,
    edge: str | None = None,
) -> MonitorOptions:
    normalized_pins = _normalize_pin_sequence(pins)
    normalized_format = normalize_format(fmt)
    normalized_edge = normalize_edge(edge)

    if interval <= 0:
        raise MonitorError("interval must be greater than 0")
    if duration is not None and duration <= 0:
        raise MonitorError("duration must be greater than 0")
    if count is not None and count <= 0:
        raise MonitorError("count must be greater than 0")

    return MonitorOptions(
        pins=tuple(normalized_pins),
        fmt=normalized_format,
        interval=float(interval),
        duration=float(duration) if duration is not None else None,
        count=int(count) if count is not None else None,
        edge=normalized_edge,
    )


def build_uart_options(
    ports: Sequence[str],
    *,
    fmt: str = "text",
    interval: float = 0.05,
    duration: float | None = None,
    count: int | None = None,
    encoding: str = "utf-8",
    read_size: int = 4096,
) -> UartMonitorOptions:
    normalized_ports = parse_uart_ports(ports)
    normalized_format = normalize_format(fmt)

    if interval <= 0:
        raise MonitorError("interval must be greater than 0")
    if duration is not None and duration <= 0:
        raise MonitorError("duration must be greater than 0")
    if count is not None and count <= 0:
        raise MonitorError("count must be greater than 0")
    if not encoding:
        raise MonitorError("encoding must not be empty")
    if read_size <= 0:
        raise MonitorError("read_size must be greater than 0")

    return UartMonitorOptions(
        ports=tuple(normalized_ports),
        fmt=normalized_format,
        interval=float(interval),
        duration=float(duration) if duration is not None else None,
        count=int(count) if count is not None else None,
        encoding=encoding,
        read_size=int(read_size),
    )


def _format_seconds(value: float) -> str:
    return f"{value:g}"


def _format_limit(options: MonitorOptions) -> str:
    parts: list[str] = []
    if options.count is not None:
        suffix = "sample" if options.count == 1 else "samples"
        parts.append(f"{options.count} {suffix}")
    if options.duration is not None:
        parts.append(f"{_format_seconds(options.duration)}s")
    return ", ".join(parts) if parts else "until stopped"


def format_monitor_header(options: MonitorOptions, *, port: str = "") -> str:
    """Format the human-facing monitor session header."""
    if options.fmt == "json":
        return ""

    pin_list = ", ".join(f"GPIO{pin}" for pin in options.pins)
    edge_mode = "changed" if options.edge == "changed" else "all"
    lines = [
        f"=== {MONITOR_TITLE} ===",
        f"Port: {port or '-'} | Pins: {pin_list}",
        (
            f"Interval: {_format_seconds(options.interval)}s | "
            f"Edge: {edge_mode} | Limit: {_format_limit(options)}"
        ),
    ]
    width = max(len(line) for line in lines)
    lines.append("-" * width)
    return "\n".join(lines)


def _decode_uart_data(data: bytes, encoding: str) -> str:
    try:
        decoded = data.decode(encoding)
    except UnicodeDecodeError:
        return UART_DECODE_FAILED
    except LookupError as exc:
        raise MonitorError(f"unknown encoding {encoding!r}") from exc
    return (
        decoded
        .replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def _coerce_uart_data(data: bytes | bytearray | memoryview) -> bytes:
    if isinstance(data, bytes):
        return data
    return bytes(data)


def format_uart_monitor_header(
    options_or_ports: UartMonitorOptions | Sequence[str],
    *,
    fmt: str = "text",
    interval: float = 0.05,
    duration: float | None = None,
    count: int | None = None,
    encoding: str = "utf-8",
) -> str:
    """Format the UART monitor table header."""
    if isinstance(options_or_ports, UartMonitorOptions):
        options = options_or_ports
    else:
        options = build_uart_options(
            options_or_ports,
            fmt=fmt,
            interval=interval,
            duration=duration,
            count=count,
            encoding=encoding,
        )
    if options.fmt == "json":
        return ""

    ports = ", ".join(options.ports)
    lines = [
        f"=== {UART_MONITOR_TITLE} ===",
        (
            f"Ports: {ports} | Encoding: {options.encoding} | "
            f"Interval: {_format_seconds(options.interval)}s | "
            f"Limit: {_format_limit(options)}"
        ),
        "Port\tRaw\tHex\tString",
    ]
    return "\n".join(lines)


def format_uart_monitor_sample(
    port: str,
    data: bytes | bytearray | memoryview,
    *,
    fmt: str = "text",
    seq: int = 0,
    encoding: str = "utf-8",
) -> str:
    """Format one UART byte chunk as raw, hex, and decoded string columns."""
    normalized_format = normalize_format(fmt)
    raw_data = _coerce_uart_data(data)
    hex_text = raw_data.hex(" ")
    string_text = _decode_uart_data(raw_data, encoding)

    if normalized_format == "json":
        return json.dumps(
            {
                "seq": int(seq),
                "port": str(port),
                "raw": repr(raw_data),
                "hex": hex_text,
                "string": string_text,
            },
            separators=(",", ":"),
        )
    return f"{port}\t{raw_data!r}\t{hex_text}\t{string_text}"


def format_uart_monitor_rows(
    samples: Sequence[tuple[str, bytes | bytearray | memoryview | None]],
    *,
    fmt: str = "text",
    seq: int = 0,
    encoding: str = "utf-8",
) -> str:
    """Format one table row per UART port for screen refresh mode."""
    normalized_format = normalize_format(fmt)
    rows: list[str] = []
    for port, data in samples:
        if data is None:
            if normalized_format == "json":
                continue
            rows.append(f"{port}\t-\t-\t-")
            continue
        rows.append(
            format_uart_monitor_sample(
                port,
                data,
                fmt=normalized_format,
                seq=seq,
                encoding=encoding,
            )
        )
    return "\n".join(rows)


def format_monitor_sample(
    pins: Sequence[int],
    values: Sequence[int],
    *,
    fmt: str = "text",
    seq: int = 0,
    style: str = "compact",
) -> str:
    normalized_pins = _normalize_pin_sequence(pins)
    normalized_format = normalize_format(fmt)
    normalized_style = style.lower()
    if normalized_style not in {"compact", "modern"}:
        raise MonitorError("sample style must be compact or modern")
    if len(normalized_pins) != len(values):
        raise MonitorError("pin and value counts must match")

    int_values = [int(value) for value in values]
    if normalized_format == "json":
        return json.dumps(
            {
                "seq": int(seq),
                "pins": {
                    str(pin): int_values[index]
                    for index, pin in enumerate(normalized_pins)
                },
            },
            separators=(",", ":"),
        )
    if normalized_style == "modern":
        states = []
        for index, pin in enumerate(normalized_pins):
            state = "HIGH" if int_values[index] else "LOW"
            states.append(f"GPIO{pin}: {state}")
        return f"#{int(seq):04d} | " + " | ".join(states)
    return " ".join(
        f"{pin}={int_values[index]}"
        for index, pin in enumerate(normalized_pins)
    )


def build_pin_probe_script(
    candidates: Sequence[int] = DEFAULT_GPIO_CANDIDATES,
) -> str:
    candidate_pins = _normalize_pin_sequence(candidates, label="candidate list")
    return f"""\
import machine
_candidates={candidate_pins!r}
_valid=[]
for _pin in _candidates:
    try:
        _obj=machine.Pin(_pin,machine.Pin.IN)
        _obj.value()
        _valid.append(_pin)
    except Exception:
        pass
print({PROBE_MARKER!r}+','.join(str(_pin) for _pin in _valid))
"""


def parse_probe_output(output: str) -> list[int]:
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(PROBE_MARKER):
            payload = stripped[len(PROBE_MARKER):]
            if not payload:
                return []
            return parse_pin_list(payload)
    raise MonitorError("GPIO probe output missing monitor marker")


def build_single_sample_script(pins: Sequence[int]) -> str:
    """Build a short script that reads each pin once as input."""
    pin_list = _normalize_pin_sequence(pins)
    return f"""\
import machine
_pins={pin_list!r}
_vals=[]
for _pin in _pins:
    _vals.append(machine.Pin(_pin,machine.Pin.IN).value())
print({SAMPLE_MARKER!r}+','.join(str(int(_val)) for _val in _vals))
"""


def build_streaming_sample_script(
    pins: Sequence[int],
    *,
    interval: float = 0.5,
    duration: float | None = None,
    count: int | None = None,
    edge: str | None = None,
) -> str:
    """Build a device-side loop that streams marked GPIO samples."""
    options = build_options(
        pins,
        fmt="text",
        interval=interval,
        duration=duration,
        count=count,
        edge=edge,
    )
    duration_ms = (
        "None"
        if options.duration is None
        else str(int(options.duration * 1000))
    )
    count_value = "None" if options.count is None else str(options.count)
    edge_changed = options.edge == "changed"
    pin_list = list(options.pins)

    return f"""\
import machine,time,sys
_pins={pin_list!r}
_interval={options.interval!r}
_interval_ms={int(options.interval * 1000)}
_duration_ms={duration_ms}
_count={count_value}
_edge_changed={edge_changed!r}
_objs=[machine.Pin(_pin,machine.Pin.IN) for _pin in _pins]
_prev=None
_seq=0
_start=time.ticks_ms()
while True:
    if _count is not None and _seq >= _count:
        break
    if _duration_ms is not None and time.ticks_diff(time.ticks_ms(), _start) >= _duration_ms:
        break
    _vals=[_pin.value() for _pin in _objs]
    if (not _edge_changed) or _prev is None or _vals != _prev:
        sys.stdout.write({SAMPLE_MARKER!r}+','.join(str(int(_val)) for _val in _vals)+'\\n')
        try:
            sys.stdout.flush()
        except AttributeError:
            pass
    _prev=_vals
    _seq += 1
    if _count is not None and _seq >= _count:
        break
    if _duration_ms is not None and time.ticks_diff(time.ticks_ms(), _start) >= _duration_ms:
        break
    if hasattr(time,'sleep_ms'):
        time.sleep_ms(_interval_ms)
    else:
        time.sleep(_interval)
"""


def parse_sample_output(output: str, expected_count: int) -> list[int]:
    """Parse one monitor sample from device output."""
    if expected_count <= 0:
        raise MonitorError("expected_count must be greater than 0")

    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(SAMPLE_MARKER):
            payload = stripped[len(SAMPLE_MARKER):]
            if not payload:
                values: list[int] = []
            else:
                try:
                    values = [int(part.strip(), 10) for part in payload.split(",")]
                except ValueError as exc:
                    raise MonitorError("GPIO sample contains a non-integer value") from exc
            if len(values) != expected_count:
                raise MonitorError(
                    f"GPIO sample expected {expected_count} values, got {len(values)}"
                )
            return values
    raise MonitorError("GPIO sample output missing monitor marker")


def resolve_monitor_pins(
    mp,
    explicit_pins: str | Sequence[int] | None,
    *,
    candidates: Sequence[int] = DEFAULT_GPIO_CANDIDATES,
) -> list[int]:
    if explicit_pins is not None:
        return normalize_pins(explicit_pins)

    try:
        output = mp.run(build_pin_probe_script(candidates))
    except Exception as exc:
        raise MonitorError(
            "unable to probe GPIO pins; ensure firmware provides machine.Pin "
            "or pass --pins"
        ) from exc

    pins = parse_probe_output(output)
    if not pins:
        raise MonitorError(
            "no usable GPIO pins detected; pass --pins with a board-specific list"
        )
    return pins


def build_sampling_script(
    pins: Sequence[int],
    *,
    fmt: str = "text",
    interval: float = 0.5,
    duration: float | None = None,
    count: int | None = None,
    edge: str | None = None,
) -> str:
    options = build_options(
        pins,
        fmt=fmt,
        interval=interval,
        duration=duration,
        count=count,
        edge=edge,
    )
    duration_ms = (
        "None"
        if options.duration is None
        else str(int(options.duration * 1000))
    )
    count_value = "None" if options.count is None else str(options.count)
    edge_changed = options.edge == "changed"
    pin_list = list(options.pins)

    return f"""\
import machine,time,sys
_pins={pin_list!r}
_fmt={options.fmt!r}
_interval={options.interval!r}
_interval_ms={int(options.interval * 1000)}
_duration_ms={duration_ms}
_count={count_value}
_edge_changed={edge_changed!r}
_objs=[machine.Pin(_pin,machine.Pin.IN) for _pin in _pins]
_prev=None
_seq=0
_start=time.ticks_ms()
def _emit(_vals):
    if _fmt == 'json':
        _parts=[]
        for _i in range(len(_pins)):
            _parts.append('"%d":%d' % (_pins[_i], _vals[_i]))
        sys.stdout.write('{{"seq":%d,"pins":{{%s}}}}\\n' % (_seq, ','.join(_parts)))
    else:
        _parts=[]
        for _i in range(len(_pins)):
            _parts.append('%d=%d' % (_pins[_i], _vals[_i]))
        sys.stdout.write(' '.join(_parts)+'\\n')
    try:
        sys.stdout.flush()
    except AttributeError:
        pass
while True:
    if _count is not None and _seq >= _count:
        break
    if _duration_ms is not None and time.ticks_diff(time.ticks_ms(), _start) >= _duration_ms:
        break
    _vals=[_pin.value() for _pin in _objs]
    if (not _edge_changed) or _prev is None or _vals != _prev:
        _emit(_vals)
    _prev=_vals
    _seq += 1
    if _count is not None and _seq >= _count:
        break
    if _duration_ms is not None and time.ticks_diff(time.ticks_ms(), _start) >= _duration_ms:
        break
    if hasattr(time,'sleep_ms'):
        time.sleep_ms(_interval_ms)
    else:
        time.sleep(_interval)
"""


def run_monitor(
    mp,
    *,
    pins: str | Sequence[int] | None = None,
    fmt: str = "text",
    interval: float = 0.5,
    duration: float | None = None,
    count: int | None = None,
    edge: str | None = None,
    candidates: Sequence[int] = DEFAULT_GPIO_CANDIDATES,
) -> str:
    resolved_pins = resolve_monitor_pins(
        mp,
        explicit_pins=pins,
        candidates=candidates,
    )
    script = build_sampling_script(
        resolved_pins,
        fmt=fmt,
        interval=interval,
        duration=duration,
        count=count,
        edge=edge,
    )
    return mp.run(script)


def _supports_raw_stream(mp: object) -> bool:
    return (
        callable(getattr(type(mp), "_write", None))
        and callable(getattr(type(mp), "_enter_raw_repl", None))
        and "transport" in getattr(mp, "__dict__", {})
    )


def _stream_raw_repl_stdout(
    mp,
    code: str,
    *,
    timeout: float | None,
    on_line: Callable[[str], None],
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    from .flash.core import SET_EXECUTE

    mp._enter_raw_repl()
    mp._write(code)
    mp._write(SET_EXECUTE)

    deadline = None if timeout is None else monotonic() + timeout
    pending = b""
    saw_ack = False

    while True:
        if deadline is not None and monotonic() >= deadline:
            break

        waiting = 0
        try:
            waiting = int(mp.transport.in_waiting)
        except Exception:
            waiting = 0

        if waiting <= 0:
            time.sleep(0.001)
            continue

        chunk = mp.transport.read(waiting)
        if not chunk:
            continue

        record_rx = getattr(mp, "_record_rx", None)
        if callable(record_rx):
            record_rx(chunk)

        pending += chunk
        if not saw_ack:
            if len(pending) < 2 and b"OK".startswith(pending):
                continue
            if pending.startswith(b"OK"):
                pending = pending[2:]
            saw_ack = True

        if SET_EXECUTE in pending:
            data, _terminator, _rest = pending.partition(SET_EXECUTE)
            pending = data
            done = True
        else:
            done = False

        while b"\n" in pending:
            raw_line, _newline, pending = pending.partition(b"\n")
            on_line(raw_line.decode("utf-8", errors="replace").rstrip("\r"))

        if done:
            if pending:
                on_line(pending.decode("utf-8", errors="replace").rstrip("\r"))
            break


def _stream_timeout_for_options(options: MonitorOptions) -> float | None:
    if options.duration is not None:
        return options.duration + 5
    if options.count is not None:
        return (options.count * options.interval) + 5
    return None


def run_monitor_session(
    mp,
    *,
    pins: str | Sequence[int] | None = None,
    fmt: str = "text",
    interval: float = 0.5,
    duration: float | None = None,
    count: int | None = None,
    edge: str | None = None,
    candidates: Sequence[int] = DEFAULT_GPIO_CANDIDATES,
    write: Callable[[str], None] = print,
    on_start: Callable[[MonitorOptions], None] | None = None,
    on_sample_error: Callable[[MonitorError, str], None] | None = None,
    max_sample_errors: int | None = 3,
    sample_style: str = "compact",
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    stream: bool = False,
) -> int:
    """Continuously monitor GPIO state."""
    resolved_pins = resolve_monitor_pins(
        mp,
        explicit_pins=pins,
        candidates=candidates,
    )
    options = build_options(
        resolved_pins,
        fmt=fmt,
        interval=interval,
        duration=duration,
        count=count,
        edge=edge,
    )
    if on_start is not None:
        on_start(options)

    if stream and _supports_raw_stream(mp):
        script = build_streaming_sample_script(
            options.pins,
            interval=options.interval,
            duration=options.duration,
            count=options.count,
            edge=options.edge,
        )
        previous: list[int] | None = None
        emitted = 0
        sample_errors = 0

        def handle_line(line: str) -> None:
            nonlocal previous, emitted, sample_errors
            try:
                values = parse_sample_output(
                    line,
                    expected_count=len(options.pins),
                )
            except MonitorError as exc:
                sample_errors += 1
                if on_sample_error is not None:
                    on_sample_error(exc, line)
                if (
                    max_sample_errors is not None
                    and sample_errors >= max_sample_errors
                ):
                    raise
                return

            sample_errors = 0
            if options.edge != "changed" or previous is None or values != previous:
                write(
                    format_monitor_sample(
                        options.pins,
                        values,
                        fmt=options.fmt,
                        seq=emitted,
                        style=sample_style,
                    )
                )
                emitted += 1
            previous = values

        _stream_raw_repl_stdout(
            mp,
            script,
            timeout=_stream_timeout_for_options(options),
            on_line=handle_line,
            monotonic=monotonic,
        )
        return emitted

    sample_script = build_single_sample_script(options.pins)
    started_at = monotonic()
    previous: list[int] | None = None
    samples = 0
    emitted = 0
    sample_errors = 0

    while True:
        if options.count is not None and samples >= options.count:
            break
        if options.duration is not None and monotonic() - started_at >= options.duration:
            break

        output = mp.run(sample_script)
        try:
            values = parse_sample_output(output, expected_count=len(options.pins))
        except MonitorError as exc:
            sample_errors += 1
            if on_sample_error is not None:
                on_sample_error(exc, output)
            if max_sample_errors is not None and sample_errors >= max_sample_errors:
                raise
            samples += 1

            if options.count is not None and samples >= options.count:
                break
            if options.duration is not None:
                remaining = options.duration - (monotonic() - started_at)
                if remaining <= 0:
                    break
                sleep(min(options.interval, remaining))
            else:
                sleep(options.interval)
            continue

        sample_errors = 0
        if options.edge != "changed" or previous is None or values != previous:
            write(
                format_monitor_sample(
                    options.pins,
                    values,
                    fmt=options.fmt,
                    seq=samples,
                    style=sample_style,
                )
            )
            emitted += 1
        previous = values
        samples += 1

        if options.count is not None and samples >= options.count:
            break
        if options.duration is not None:
            remaining = options.duration - (monotonic() - started_at)
            if remaining <= 0:
                break
            sleep(min(options.interval, remaining))
        else:
            sleep(options.interval)

    return emitted


def run_uart_monitor_session(
    transports: Sequence[tuple[str, object]],
    *,
    fmt: str = "text",
    interval: float = 0.05,
    duration: float | None = None,
    count: int | None = None,
    encoding: str = "utf-8",
    read_size: int = 4096,
    write: Callable[[str], None] = print,
    on_start: Callable[[UartMonitorOptions], None] | None = None,
    refresh: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Continuously read raw bytes from one or more UART transports."""
    options = build_uart_options(
        [port for port, _transport in transports],
        fmt=fmt,
        interval=interval,
        duration=duration,
        count=count,
        encoding=encoding,
        read_size=read_size,
    )
    if on_start is not None:
        on_start(options)

    event_queue: queue.Queue[tuple[str, bytes]] = queue.Queue()
    stop_event = threading.Event()
    reader_sleep = min(options.interval, 0.01)

    def reader_loop(port: str, transport: object) -> None:
        while not stop_event.is_set():
            try:
                waiting = int(getattr(transport, "in_waiting"))
            except Exception:
                waiting = 0

            if waiting <= 0:
                time.sleep(reader_sleep)
                continue

            try:
                data = _coerce_uart_data(
                    transport.read(min(options.read_size, waiting))
                )
            except Exception:
                time.sleep(reader_sleep)
                continue

            if data:
                event_queue.put((port, data))

    threads = [
        threading.Thread(
            target=reader_loop,
            args=(port, transport),
            daemon=True,
        )
        for port, transport in transports
    ]
    for thread in threads:
        thread.start()

    started_at = monotonic()
    polls = 0
    emitted = 0
    seq = 0
    latest: dict[str, bytes | None] = {port: None for port in options.ports}

    def handle_uart_event(port: str, data: bytes) -> None:
        nonlocal emitted, seq
        latest[port] = data
        if not refresh:
            write(
                format_uart_monitor_sample(
                    port,
                    data,
                    fmt=options.fmt,
                    seq=seq,
                    encoding=options.encoding,
                )
            )
        emitted += 1
        seq += 1

    try:
        while True:
            if options.count is not None and polls >= options.count:
                break
            if (
                options.duration is not None
                and monotonic() - started_at >= options.duration
            ):
                break

            tick_started = monotonic()
            changed = False
            while True:
                elapsed = monotonic() - tick_started
                tick_remaining = options.interval - elapsed
                if tick_remaining <= 0:
                    break

                if options.duration is not None:
                    duration_remaining = options.duration - (monotonic() - started_at)
                    if duration_remaining <= 0:
                        break
                    wait_time = min(tick_remaining, duration_remaining)
                else:
                    wait_time = tick_remaining

                try:
                    port, data = event_queue.get(timeout=wait_time)
                except queue.Empty:
                    break

                handle_uart_event(port, data)
                changed = True

                while True:
                    try:
                        port, data = event_queue.get_nowait()
                    except queue.Empty:
                        break
                    handle_uart_event(port, data)
                    changed = True

                if refresh and changed:
                    write(
                        format_uart_monitor_rows(
                            [(port, latest[port]) for port in options.ports],
                            fmt=options.fmt,
                            seq=seq,
                            encoding=options.encoding,
                        )
                    )
                    changed = False

            polls += 1
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=0.05)

    return emitted

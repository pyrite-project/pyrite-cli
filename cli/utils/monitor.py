"""Pure helpers for GPIO monitoring commands."""

from __future__ import annotations

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
    sys.stdout.flush()
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
    sample_style: str = "compact",
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Continuously poll GPIO state by injecting one short ``run()`` script per sample."""
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
    sample_script = build_single_sample_script(options.pins)
    started_at = monotonic()
    previous: list[int] | None = None
    samples = 0
    emitted = 0

    while True:
        if options.count is not None and samples >= options.count:
            break
        if options.duration is not None and monotonic() - started_at >= options.duration:
            break

        output = mp.run(sample_script)
        values = parse_sample_output(output, expected_count=len(options.pins))
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

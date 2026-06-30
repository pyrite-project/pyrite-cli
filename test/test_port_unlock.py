from __future__ import annotations

from cli.utils.transport.port_unlock import (
    PortProcess,
    is_probable_port_busy_error,
    maybe_unlock_occupied_port,
    parse_handle_output,
    parse_lsof_output,
)


def test_busy_error_detection_ignores_missing_port() -> None:
    assert is_probable_port_busy_error(
        PermissionError("could not open port COM3: Access is denied")
    )
    assert is_probable_port_busy_error(
        OSError("[Errno 16] Device or resource busy: '/dev/ttyUSB0'")
    )
    assert not is_probable_port_busy_error(
        FileNotFoundError("could not open port COM99: No such file or directory")
    )


def test_parse_lsof_output_returns_processes() -> None:
    output = """\
COMMAND   PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
python3  1234 alice    3u   CHR  188,0      0t0  123 /dev/ttyUSB0
screen   5678 bob      5u   CHR  188,0      0t0  123 /dev/ttyUSB0
"""

    processes = parse_lsof_output(output)

    assert processes == [
        PortProcess(
            pid=1234,
            name="python3",
            user="alice",
            command="/dev/ttyUSB0",
            source="lsof",
        ),
        PortProcess(
            pid=5678,
            name="screen",
            user="bob",
            command="/dev/ttyUSB0",
            source="lsof",
        ),
    ]


def test_parse_handle_output_returns_processes() -> None:
    output = """\
python.exe         pid: 2220   type: File           30C: \\Device\\USBSER000
Thonny.exe         pid: 3331   type: File           1A4: \\Device\\USBSER000
"""

    processes = parse_handle_output(output)

    assert processes == [
        PortProcess(
            pid=2220,
            name="python.exe",
            command="\\Device\\USBSER000",
            source="handle",
        ),
        PortProcess(
            pid=3331,
            name="Thonny.exe",
            command="\\Device\\USBSER000",
            source="handle",
        ),
    ]


def test_maybe_unlock_asks_twice_before_terminating() -> None:
    prompts: list[str] = []
    messages: list[str] = []
    terminated: list[PortProcess] = []
    processes = [
        PortProcess(
            pid=1234,
            name="python",
            command="pyrcli monitor COM3",
            source="test",
        ),
    ]

    def confirm(message: str, *, default: bool = False) -> bool:
        prompts.append(message)
        return True

    def scanner(port: str):
        assert port == "COM3"
        return processes, "test-backend", []

    def terminator(items):
        terminated.extend(items)
        return []

    unlocked = maybe_unlock_occupied_port(
        "COM3",
        PermissionError("Access is denied"),
        confirm=confirm,
        echo=messages.append,
        scanner=scanner,
        terminator=terminator,
        interactive=True,
    )

    assert unlocked is True
    assert len(prompts) == 2
    assert "疑似被占用" in prompts[0]
    assert "保存" in prompts[1]
    assert "PID" in "\n".join(messages)
    assert terminated == processes


def test_maybe_unlock_non_interactive_does_not_prompt() -> None:
    called = False

    def confirm(message: str, *, default: bool = False) -> bool:
        raise AssertionError("confirm should not be called")

    def scanner(port: str):
        nonlocal called
        called = True
        return [], "test-backend", []

    unlocked = maybe_unlock_occupied_port(
        "COM3",
        PermissionError("Access is denied"),
        confirm=confirm,
        scanner=scanner,
        interactive=False,
    )

    assert unlocked is False
    assert called is False

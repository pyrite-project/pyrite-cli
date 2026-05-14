# pyrite-cli Architecture & Module Responsibilities

## Overview

```
pyrite-cli/
├── cli/
│   ├── main.py              # CLI entry (Typer command definitions)
│   ├── py.typed             # PEP 561 type marker
│   └── utils/
│       ├── Flash.py         # Core: MicroPython device communication & file ops
│       ├── transport.py     # Transport layer ABC
│       ├── serial_transport.py  # Serial transport implementation
│       ├── webrepl_transport.py # WebREPL (WebSocket) transport
│       ├── types.py         # Type definitions (PyriteConfig dataclass)
│       ├── config.py        # Config loading & default config generation
│       ├── compiler.py      # mpy-cross compilation wrapper (parallel support)
│       ├── ansi.py          # ANSI terminal color constants
│       ├── preprocessor.py  # Conditional compilation (libcst CST transforms)
│       └── manifest_loader.py # Secure manifest.py parser
│   └── project/
│       ├── project.py       # Project scaffolding, interactive hardware selection
│       └── stubs.py         # GitHub API stub downloader
├── test/
│   ├── test_config.py       # Config loading edge-case tests
│   ├── test_flash_utils.py  # REPL coloring, CRC32, file hash tests
│   ├── test_protocol_helpers.py # Protocol parsing function tests
│   └── test_manifest_loader.py  # Manifest parser tests
└── docs/
    └── ...
```

---

## Transport Layer (`utils/transport.py`, `serial_transport.py`, `webrepl_transport.py`)

### Design Goal

Decouple the underlying communication method from **hardcoded pyserial** to a **Transport ABC**, so `Flash.py`'s core logic does not care whether data flows over serial or WebSocket.

### Transport ABC (`transport.py`)

Abstract base class defining the interface and providing shared receive buffering:

| Method/Property | Type | Description |
|----------------|------|-------------|
| `connect()` | abstract | Establish connection (serial open / WebSocket handshake) |
| `disconnect()` | concrete | Close connection, clear `_rx_buf` |
| `write(data)` | concrete | Write data, delegates to `_raw_write()` |
| `read(size)` | concrete | Read data, prefers internal `_rx_buf` buffer |
| `in_waiting` | property | Available bytes in receive buffer |
| `reset_input_buffer()` | concrete | Drain both transmit/receive buffers |
| `reset_output_buffer()` | concrete | Clear output buffer (serial only) |
| `is_connected` | abstract | Connection status |
| `_raw_write(data)` | abstract | Subclass-specific write |
| `_raw_read(size)` | abstract | Subclass-specific read |
| `_raw_in_waiting()` | abstract | Subclass-specific available-byte query |
| `_fill_buf()` | concrete | Pull data from underlying transport into `_rx_buf` |

**Buffer mechanism**: The base class maintains an internal `_rx_buf`. Both `read()` and `in_waiting` trigger `_fill_buf()` to pull more data before returning. This eliminates repeated `if in_waiting: read()` boilerplate.

### SerialTransport (`serial_transport.py`)

pyserial wrapper:

```
SerialTransport
├── __init__(port, baudrate, timeout)
├── connect()         → serial.Serial(port, baudrate, timeout, write_timeout)
├── disconnect()      → ser.close()
├── _raw_write(data)  → ser.write(data)
├── _raw_read(size)   → ser.read(size)
├── _raw_in_waiting() → ser.in_waiting
├── reset_output_buffer() → ser.reset_output_buffer()
└── is_connected      → ser is not None and ser.is_open
```

- `connect()` auto-calls `reset_input_buffer()` to clear startup residual data.
- `disconnect()` also clears parent `_rx_buf`.

### WebREPLTransport (`webrepl_transport.py`)

WebREPL protocol via `websocket-client`, enabling WiFi connections to MicroPython devices:

```
WebREPLTransport
├── __init__(url, password)
│   ├── url:      ws://device_ip:8266/
│   └── password: CLI arg / env var / interactive prompt
├── connect()
│   ├── 1. Create WebSocket connection
│   ├── 2. Receive JSON challenge {"uid":"...", "nb":9}
│   ├── 3. Compute SHA256(password + uid) → first nb hex chars
│   └── 4. Send auth response, verify leading ":"
├── disconnect()     → ws.close()
├── _raw_write(data) → ws.send(data, OPCODE_BINARY)
├── _raw_read(size)  → returns empty (driven by _fill_buf)
├── _fill_buf()      → ws.recv() → append to _rx_buf
└── is_connected     → ws is not None
```

**Password resolution chain**: `CLI --password` → `PYRITE_WEBREPL_PASSWORD` env var → `getpass()` interactive prompt.

---

## Type Definitions (`utils/types.py`)

### PyriteConfig Dataclass

Replaces bare `dict` for configuration, providing type safety and IDE autocompletion:

```python
@dataclass
class PyriteConfig:
    chunk_size: int = 4096          # Max bytes per write chunk
    download_threads: int = 4       # Concurrent stub downloads (1-12)
    auto_compile: bool = True       # Auto-compile .py → .mpy
    verify: str = "size"            # Verification mode: off / size / crc32
    max_retries: int = 2            # Max retries on verify failure
    board_tags: Dict[str, List[str]]  # Board tag mapping (from pyproject.toml)
```

Usage changed from `self.config["chunk_size"]` to `self.config.chunk_size`.

---

## Config System (`utils/config.py`)

Loads configuration from the filesystem, merging multiple sources:

| Function | Returns | Description |
|----------|---------|-------------|
| `_load_config()` | `PyriteConfig` | Search CWD upward for `.pyrite_config.json`, merge `pyproject.toml` board_tags |
| `create_default_config()` | `str` | Create default `.pyrite_config.json` in CWD |

**Load order**:
1. Start with `PyriteConfig()` defaults
2. Search upward for `.pyrite_config.json`, merge fields
3. Search upward for `pyproject.toml`, merge `[tool.pyrite.board_tags]`
4. Precedence: built-in < JSON file < pyproject.toml board_tags

---

## Compiler Module (`utils/compiler.py`)

Wraps `mpy-cross` as a Python API, supporting both serial and parallel compilation:

| Function | Description |
|----------|-------------|
| `_compile_to_mpy(local_path, bytecode_ver, arch)` | Compile single .py → .mpy, fall back to .py on failure |
| `_compile_files_parallel(local_paths, bytecode_ver, arch, max_workers)` | Parallel compilation using `ThreadPoolExecutor` |

- Uses the `mpy_cross` Python API, no external `mpy-cross` binary required
- On failure: prints a yellow warning and silently falls back to `.py`
- Temp files in `tempfile.mkdtemp()`, auto-cleaned on exit

---

## ANSI Color Constants (`utils/ansi.py`)

Shared terminal color constants:

| Constant | Value | Usage |
|----------|-------|-------|
| `_GREEN` | `\033[32m` | Success/completion messages |
| `_YELLOW` | `\033[33m` | Warnings/notices |
| `_RED` | `\033[31m` | Error messages |
| `_RESET` | `\033[0m` | Reset color |

---

## CLI Entry (`main.py`) Changes

### Transport Selection

Two new global options added to all device communication commands:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--ws` | `str` | `None` | WebREPL WebSocket URL, e.g. `ws://192.168.4.1:8266/` |
| `--password` | `str` | `None` | WebREPL password (omit for env/input fallback) |

### _mp_factory() Helper

Centralized `MicroPython` instance creation:

```python
def _mp_factory(port, baudrate, timeout, ws, password) -> MicroPython:
    if ws:
        transport = WebREPLTransport(ws, password)
    else:
        transport = SerialTransport(port, baudrate, timeout)
    return MicroPython(transport=transport)
```

Omitting `--ws` falls back to serial, preserving all existing behavior.

---

## Flash.py Core Refactor

### State Machine Simplification

| Removed | Replacement |
|---------|-------------|
| `_in_raw` flag | Full init on every `_enter_raw_repl()` |
| `_kbd_set` flag | Same |
| `_setup_kbd_intr()` | Integrated into `_init_device_state()` |
| `_restore_kbd_intr()` | No longer needed |
| Scattered `if not is_connected: connect()` | Unified via `_ensure_connected()` |

### New Core Methods

- `_ensure_connected(retries=3)` — Check connection, auto-reconnect with retries
- `_init_device_state()` — Full device initialization sequence:
  1. Multiple Ctrl+C to interrupt stuck programs
  2. `kbd_intr(-1)` to disable Ctrl+C soft-reboot
  3. Enter raw REPL mode (Ctrl+A)
  4. Detect device mpy version and architecture

### Transport Adaptation

All `self.ser.*` operations migrated to `self.transport.*`:

| Before | After |
|--------|-------|
| `self.ser.write(data)` | `self.transport.write(data)` |
| `self.ser.read(n)` | `self.transport.read(n)` |
| `self.ser.in_waiting` | `self.transport.in_waiting` |
| `self.ser.is_open` | `self.transport.is_connected` |
| `self.ser.close()` | `self.transport.disconnect()` |
| `serial.Serial(port, ...)` | `SerialTransport(port, ...)` |

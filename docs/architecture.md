# pyrite-cli Architecture & Module Responsibilities

## Overview

```
pyrite-cli/
|-- cli/
|   |-- main.py              # CLI entry (Typer command definitions)
|   |-- py.typed             # PEP 561 type marker
|   |-- utils/
|   |   |-- flash/            # Raw REPL, flash/verify, fs browser, bytes download
|   |   |   |-- core.py
|   |   |   |-- flash.py
|   |   |   `-- mp_scripts/
|   |   |-- transport/        # Transport ABC and serial implementation
|   |   |   |-- base.py
|   |   |   `-- serial.py
|   |   |-- webrepl/          # WebREPL transport and MicroPython adapter
|   |   |   |-- transport.py
|   |   |   `-- micropython.py
|   |   |-- build/            # mpy-cross, conditional compilation, manifest parser
|   |   |-- board_alias.py    # Local name-to-serial-port aliases and migration
|   |   |-- config/           # Config loading and PyriteConfig
|   |   |-- diagnostics/      # doctor and monitor helpers
|   |   |-- log/              # Unified logging package
|   |   |-- ui/               # ANSI, JSON output, terminal selection
|   |   |-- pkg.py            # mpremote mip planning helpers
|   |   |-- webdav_mount.py   # WebDAV mount helpers
|   |   `-- errors.py
|   `-- project/
|       |-- project.py
|       |-- stubs.py
|       `-- sync.py
|-- test/
`-- docs/
```

---

## Transport Layer (`utils/transport/`)

### Design Goal

Decouple the underlying communication method from **hardcoded pyserial** to a **Transport ABC**, so `flash.py`'s core logic does not care whether data flows over serial or WebSocket.

### Transport ABC (`transport/base.py`)

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

### SerialTransport (`transport/serial.py`)

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

### WebREPLTransport (`utils/webrepl/transport.py`)

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
    baudrate: int = 921600           # Default serial baudrate
    timeout: int = 10                # Serial connection and I/O timeout
    delta_flash: str = "auto"       # Delta policy: off / auto / on
    precheck: str = "basic"          # Pre-flash check: off / basic / strict
    precheck_compat: str = "warn"    # Compatibility policy: warn / error / off
    precheck_mp_version: str = ""    # Optional target firmware version
    board_tags: Dict[str, List[str]] = field(default_factory=dict)
```

Usage changed from `self.config["chunk_size"]` to `self.config.chunk_size`.

---

## Config System (`utils/config/`)

Loads configuration from the filesystem, merging multiple sources:

| Function | Returns | Description |
|----------|---------|-------------|
| `_load_config()` | `PyriteConfig` | Search CWD upward for `.pyrite_config.json`, merge `pyproject.toml` board_tags |
| `create_default_config()` | `str` | Create default `.pyrite_config.json` in CWD |

`.pyrite_config.json` is the only project-config object. Its settings are flat, at the top level. Legacy `profile` and `profiles` keys are ignored with a warning; there is no profile overlay, and `delta_min_size` is not an active setting.

**Load order**:
1. Start with `PyriteConfig()` defaults
2. Search upward for `.pyrite_config.json`, merge fields
3. Search upward for `pyproject.toml`, merge `[tool.pyrite.board_tags]`
4. Precedence: built-in < JSON file < pyproject.toml board_tags

`baudrate` and `timeout` use a separate runtime precedence because Typer supplies CLI options and their environment variables before the connection factory runs:

```text
explicit CLI option / environment variable > .pyrite_config.json > built-in default
```

The relevant environment variables are `PYRITE_BAUDRATE` and `PYRITE_TIMEOUT`. `project new --port` and `project init --port` forward these optional baudrate and timeout values to device probing; omitted values fall back through Project Config to built-in defaults.

`pyrcli test` is intentionally separate. Its `--timeout` / `PYRITE_TIMEOUT` pair controls device-test execution, while `--connect-timeout` / `PYRITE_CONNECT_TIMEOUT` controls the connection and I/O timeout. An omitted connection timeout still falls back through Project Config to the built-in default.

---

## Board Alias, Project Config, and Target

These mechanisms have separate ownership:

| Mechanism | Owns | Source |
|-----------|------|--------|
| Board Alias | Local `name -> serial port` mapping | `.pyrite_board_aliases.json` |
| Project Config | Flat build, transfer, verification, and connection settings | `.pyrite_config.json` |
| Target | Board identity and active tags for manifests and conditional compilation | `--target` plus `board_tags` |

### Board Alias (`utils/board_alias.py`)

`BoardAlias` contains only `name` and `port`. `BoardAliasStore` writes schema version 1:

```json
{
  "version": 1,
  "aliases": {
    "lab-esp32": "COM3"
  }
}
```

The default path is `.pyrite_board_aliases.json` in the current directory. `PYRITE_BOARD_ALIAS_FILE` overrides it, while `--alias-file` selects a file for `pyrcli board` management commands. The command group exposes `register`, `list`, `show`, `remove`, and `resolve`.

All serial entry points resolve a leading `@alias` before connecting or building an `mpremote` command. Ordinary port strings pass through unchanged. Shell completion includes both detected ports and saved aliases.

If the canonical file does not exist, `.pyrite_board_profiles.json` is a read-only migration fallback. The loader extracts only each profile's name and port. The first modifying command writes the canonical alias schema and leaves the legacy file untouched; an existing canonical file always wins.

### Target and Manifest Locks

`--target` selects a board identity. Its uppercase key and configured `board_tags` become active tags for Manifest filtering and the `@target(...)` / `with target(...)` conditional-build syntax. It does not select a serial port or a Project Config overlay.

`pyrcli manifest plan --target TARGET` produces a resolved plan without writing, and `pyrcli manifest lock --target TARGET` writes `pyrite.lock`. Lockfile schema version 2 stores the value under `target`. The loader accepts version 1 lockfiles containing `profile`, normalizes that value to `target`, and compares the normalized payload. The hidden `--profile` CLI spelling is accepted only to ease command migration; it is deprecated, and conflicting `--target` and `--profile` values fail.

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
def _mp_factory(port, baudrate=None, timeout=None, ws=None, password=None) -> MicroPython:
    baudrate, timeout = resolve_connection_settings(
        baudrate, timeout, _load_config()
    )
    if ws:
        return WebREPLMicroPython(url=ws, password=password, timeout=timeout)
    return MicroPython(
        port=resolve_port_alias(port),
        baudrate=baudrate,
        timeout=timeout,
    )
```

WebREPLMicroPython is a subclass of MicroPython that internally creates a `WebREPLTransport` — `flash.py` has no WebREPL awareness at all.
Omitting `--ws` falls back to serial, preserving all existing behavior.

### remount

`pyrcli remount` is a thin wrapper around `mpremote mount`.

```bash
pyrcli remount COM3 .
```

The command validates the local directory, resolves the `mpremote` executable, and starts:

```bash
mpremote connect COM3 mount <local-dir>
```

`mpremote` mounts the host directory in the device VFS as `/remote` and implicitly keeps a REPL open when no follow-up action is supplied. Pyrite CLI does not reimplement this protocol and does not route `remount` through `flash.py`.

### pkg

`pyrcli pkg` is a thin planning and execution layer around `mpremote mip install`.

```bash
pyrcli pkg install COM3 aioble --target /lib --dry-run
pyrcli pkg install-offline COM3 ./package.json
```

`cli/utils/pkg.py` builds auditable plans for `install`, `cache`, and `install-offline`. Dry-runs do not call subprocesses or connect to the device. Non-dry-run install commands delegate package resolution and transfer to `mpremote`, keeping MicroPython package ecosystem behavior in one upstream tool instead of reimplementing MIP.

`pkg cache` currently plans cache paths and audits local `package.json` files; it does not perform network downloads.

### monitor

`pyrcli monitor` uses `cli/utils/monitor.py` to parse GPIO lists, build probe/sample scripts, and run a host-side polling loop.

The monitor path only initializes pins as `machine.Pin(pin, machine.Pin.IN)`. It does not set pull resistors or output mode. Each sample is collected through a short `MicroPython.run()` script so it does not reuse or alter the file flashing protocol.

---

## flash Package Refactor

The former monolithic `cli/utils/flash.py` is now a package:

| Path | Responsibility |
|------|----------------|
| `cli/utils/flash/core.py` | Raw REPL protocol, file transfer, verification, filesystem helpers |
| `cli/utils/flash/facade.py` | Public command-facing facade |
| `cli/utils/flash/__init__.py` | Public import surface |
| `cli/utils/flash/mp_scripts/` | Device-side upload scripts packaged with `cli.utils.flash` |

`from cli.utils.flash import MicroPython` and helper imports such as `_strip_repl_trailer` remain supported.

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

### Project Logic Separation

All project-level operations (hash-based incremental sync, status comparison, batch file pull) were moved out of `flash.py` into `cli/project/sync.py`:

| Moved Method (was on MicroPython) | Now on `ProjectSyncManager` |
|-----------------------------------|----------------------------|
| `project_scan()` | `scan()` |
| `project_flash()` | `flash()` |
| `project_status()` | `status()` |
| `project_pull()` | `pull()` |
| `_collect_project_files()` | static / `_collect_project_files()` |
| `_check_device_files()` | `_check_device_files()` |
| `_discover_device_files()` | `_discover_device_files()` |
| `_compute_file_hash()` | module-level `compute_file_hash()` |

`ProjectSyncManager` wraps a `MicroPython` instance and works through its public API (`run()`, `flash_file()`, `_enter_raw_repl()`, etc.):

```python
from .project.sync import ProjectSyncManager

mp = MicroPython(port="COM3")
mp.connect()
syncer = ProjectSyncManager(mp)
syncer.scan("./my_project")
syncer.flash("./my_project", "/app")
syncer.status("./my_project", "/app")
syncer.pull("./my_project", "/")
```

`flash.py` no longer imports `json`, `hashlib`, `HASH_CONFIG_FILE`, or `_HASH_VERSION`.

---

## WebREPLMicroPython (`utils/webrepl/micropython.py`)

A thin subclass of `MicroPython` that uses `WebREPLTransport` instead of `SerialTransport`:

```python
class WebREPLMicroPython(MicroPython):
    def __init__(self, url, password=None, timeout=10, transport=None):
        t = transport or WebREPLTransport(url, password)
        super().__init__(port=url, timeout=timeout, transport=t)
```

- Inherits all high-level operations (`flash_file`, `fs_ls`, `run`, `reset`, etc.)
- Overrides `connect()` to WebSocket handshake (ignores serial-specific port/baudrate)
- `flash.py` has zero WebREPL awareness — the separation is enforced by the transport layer

---

## Shell Completion & CLI Enhancements

### Auto-Completion

The main Typer app registers `--install-completion` and `--show-completion` for bash/zsh/PowerShell. All commands with a `port` argument have a serial-port auto-completion callback (`_complete_port`) that combines ports from `MicroPython.scan_ports()` with saved `@alias` values.

### `fs ls` Enhancements

The device file browser command supports three new flags:

| Flag | Description |
|------|-------------|
| `--recursive` / `-r` | Recursively list all sub-directories via device-side `os.walk()` |
| `--sort` | Sort by `name` (default), `size`, or `type`; prefix `-` for descending |
| `--paginate` / `-p` | Page output with 20 lines per page, Enter to continue, q to quit |

### `fs_ls_recursive()` Method

A new method on `MicroPython` that walks the directory tree on the device side in a single round-trip, outputting `size|type|path` for each entry. The device-side Python script uses recursive `os.listdir()` + `os.stat()` calls.

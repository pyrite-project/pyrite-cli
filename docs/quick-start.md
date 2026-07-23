# Quick Start

This guide takes you from installation to flashing your first MicroPython program. It covers pyrite-cli installation, core concepts, common commands, and configuration. It is intended for users who are new to pyrite-cli.

---

## 1. Installation and Environment

### 1.1 Standard Installation

```bash
pip install pyrite-cli
```

Core dependencies are installed automatically: `typer`, `pyserial`, `requests`, `tqdm`, `mpy-cross`, `mpremote`, and `libcst`.

### 1.2 Development Installation

```bash
git clone <repository-url>
cd pyrite-cli
pip install -e .
```

Editable mode makes local code changes take effect immediately.

### 1.3 Verify the Installation

```bash
pyrcli --help
pyrcli scan --version
```

---

## 2. Core Architecture Overview

pyrite-cli is organized in layers:

```text
CLI entry point (main.py)       <- commands you run
    |
    +-- Transport layer         <- serial or WebSocket
    |   +-- transport/: Transport ABC / SerialTransport
    |   +-- webrepl/: WebREPLTransport / WebREPLMicroPython
    |
    +-- Flash engine (flash/)   <- raw REPL protocol and file transfer
    |
    +-- Board aliases           <- local name-to-serial-port mappings
    |
    +-- Project sync (sync.py)  <- SHA256 incremental flashing and status diffing
    |
    +-- Dev sessions and tests  <- project dev, device tests, traceback mapping
    |
    +-- Snapshots and rollback  <- device filesystem snapshot, diff, restore
    |
    +-- Host capability tunnel  <- tunnel kb / tunnel network
    |
    +-- Conditional builds      <- libcst transform before flashing
    |
    +-- Unified logging         <- six levels, JSONL records, operation timing
    |
    +-- JSON output             <- --format json for scripts and CI
```

Module responsibilities:

| Module | Responsibility |
|--------|----------------|
| `main.py` | CLI entry point and command definitions |
| `board_alias.py` | Local Board Alias storage, legacy migration, and `@alias` resolution |
| `flash/` | Serial connection, raw REPL communication, flashing, device file browsing |
| `sync.py` | SHA256-based incremental sync, status comparison, bulk pull |
| `transport/` | Transport abstraction and serial implementation; WebREPL lives in `webrepl/` |
| `device_tests.py` | Upload and run device-side tests, parse `PYRITE_TEST` result frames |
| `snapshot.py` | Store device snapshots locally, calculate diffs and restore plans |
| `tunnel.py` / `device/tunnel_scripts/` | Keyboard events and restricted HTTP(S) request forwarding |
| `compiler.py` | `mpy-cross` wrapper |
| `preprocessor.py` | Conditional compilation macro preprocessing |

For the deeper mechanics, see [Device Flashing and Project Sync](device-flashing-and-project-sync.md).

---

## 3. Command Tour

### 3.1 Device Discovery

```bash
pyrcli scan                       # Scan serial devices
pyrcli scan -a                    # Show all devices, including entries without VID/PID
pyrcli debug board-info COM3      # Show firmware, CPU, flash, and other board details
```

### 3.2 Board Aliases

```bash
pyrcli board register COM3 --name lab # Save lab -> COM3
pyrcli board list                     # List aliases
pyrcli board show @lab                # Inspect one alias
pyrcli board resolve @lab             # Print COM3
pyrcli board remove lab               # Remove it
```

Board Alias stores only a local name and serial port. It does not store tags, firmware metadata, or connection settings. Any device command that accepts a serial `PORT` can use `@alias`, including `pkg install`, `pkg install-offline`, and `project new/init --port`:

```bash
pyrcli flash @lab main.py /main.py
pyrcli pkg install @lab aioble
pyrcli project new my-project --port @lab
```

Aliases use `.pyrite_board_aliases.json` in the current directory by default. `PYRITE_BOARD_ALIAS_FILE` selects another store, and `pyrcli board ... --alias-file PATH` selects one for that management command. When the new file is absent, pyrite-cli can read `.pyrite_board_profiles.json` as a migration source. It extracts only `name` and `port`; the next modifying command writes the new alias schema without deleting the legacy file.

### 3.3 Interactive REPL

```bash
pyrcli repl COM3                  # Interactive MicroPython REPL
# In the REPL: import machine; print(machine.freq())
pyrcli reset COM3                 # Soft-reset the device
```

### 3.4 File Operations

```bash
pyrcli fs ls COM3 /                    # List a directory
pyrcli fs ls COM3 /lib -r              # Recursive listing
pyrcli fs ls COM3 / --sort size        # Sort by size
pyrcli fs ls COM3 / -p                 # Page output
pyrcli fs cat COM3 /main.py            # Print file contents
pyrcli fs put COM3 local.py /remote.py # Upload a file
pyrcli fs get COM3 /remote.py backup.py # Download a file
pyrcli fs rm COM3 /remote.py           # Remove a file
```

### 3.5 Flashing and Manifest Targets

```bash
pyrcli flash COM3 main.py /main.py                  # Single file
pyrcli flash-program COM3 src/ /app                 # Whole directory
pyrcli flash-program COM3 . / --manifest manifest.py --target esp32_s3
pyrcli manifest plan --target esp32_s3              # Preview the resolved manifest plan
pyrcli manifest lock --target esp32_s3              # Write pyrite.lock
```

A Target identifies the board for manifest filtering and conditional compilation. It activates the target's `board_tags` plus any explicit feature tags; it is separate from Board Alias and connection config. New lockfiles use schema version 2 and store `target`. Version 1 lockfiles that stored the same value as `profile` remain readable for compatibility.

### 3.6 Package Installation

```bash
pyrcli pkg install COM3 aioble --target /lib --dry-run # Show mpremote mip install plan
pyrcli pkg install COM3 aioble --target /lib           # Install through mpremote mip
pyrcli pkg cache aioble --version latest --dry-run     # Plan local cache paths
pyrcli pkg install-offline COM3 .pyrite/pkg-cache/aioble
```

### 3.7 Project Management

```bash
pyrcli project new my-project        # Create a project interactively
pyrcli project new . --platform esp32 # Specify the MicroPython platform
pyrcli project new . --port COM3     # Auto-detect through serial
pyrcli project new . --port COM3 --baudrate 115200 --timeout 15
pyrcli project init --port COM3 --baudrate 115200 --timeout 15
pyrcli project hash .                # Calculate SHA256 hashes
pyrcli project flash COM3 . /        # Incremental flash
pyrcli project status COM3 . /       # Preview differences
pyrcli project pull COM3 . /         # Pull files from the device
```

With `project new/init --port`, `--baudrate` / `PYRITE_BAUDRATE` and `--timeout` / `PYRITE_TIMEOUT` control device probing. Omitted values fall back to `.pyrite_config.json`, then built-in defaults.

### 3.8 Development Sessions and Device Tests

```bash
pyrcli test COM3 test_device/ --timeout 15
pyrcli test COM3 test_device/ --timeout 15 --connect-timeout 4
pyrcli test COM3 test_device/ --keep-files
pyrcli project dev COM3 . /app --lens
pyrcli project dev COM3 . /app --test-on-save=all --test-path test_device/
pyrcli project dev COM3 . /app --once --no-repl --test-on-save=all
```

`pyrcli test` uploads local test files to a temporary directory on the device, runs them there, and parses only `PYRITE_TEST` result frames. Its `--timeout` / `PYRITE_TIMEOUT` value limits device-test execution. Its separate `--connect-timeout` / `PYRITE_CONNECT_TIMEOUT` value controls the device connection and I/O timeout; when omitted, it falls back to Project Config and then the built-in default. Normal `print()` output from tests is shown as ordinary device output. `project dev --test-on-save` can run device tests automatically after a successful sync, and `--lens` expands tracebacks into local source context.

### 3.9 Snapshots and Restore

```bash
pyrcli snapshot save COM3 before-change --remote-path /app
pyrcli snapshot list
pyrcli snapshot diff COM3 before-change --remote-path /app
pyrcli snapshot restore COM3 before-change
pyrcli snapshot restore COM3 before-change --apply --yes
pyrcli project flash COM3 . /app --snapshot-before before-flash
```

`snapshot restore` is a dry run by default; writing back to the device requires `--apply`, and unattended runs should also pass `--yes`. During diffing, pyrite-cli downloads device files and calculates SHA256 on the host, avoiding a dependency on `hashlib.sha256` in older firmware. If `--remote-path` is not specified, restore scans only the common parent directory from the snapshot files, so unrelated device paths are not pulled into the delete plan.

### 3.10 Host Capability Tunnel

```bash
pyrcli tunnel kb COM3
pyrcli tunnel network COM3 --allow example.com
pyrcli tunnel network COM3 --allow 127.0.0.1 --allow-private
pyrcli tunnel network COM3 --ws ws://192.168.4.1:8266/ --allow example.com --allow-webrepl
```

`tunnel kb` forwards host keyboard events to a device helper. `tunnel network` lets the device send restricted HTTP(S) requests through the host and requires explicit `--allow` entries. Access to localhost, private addresses, or link-local addresses also requires `--allow-private`. Starting a network tunnel over WebREPL requires `--allow-webrepl`.

### 3.11 Mounting

```bash
pyrcli mount COM3             # Browse the device filesystem from the PC file manager
pyrcli remount COM3 .         # Let the device access the current host directory as /remote
pyrcli remount COM3 src/ --unsafe-links
```

`mount` is a PC-side WebDAV bridge: the host accesses the device filesystem. `remount` is the reverse direction: it delegates to `mpremote mount` so the device can access a host directory.

### 3.12 GPIO Monitoring

```bash
pyrcli monitor COM3 --pins 0,2,4,5 --interval 0.2 --count 20
pyrcli monitor COM3 --pins 0,2 --format json --count 5
pyrcli monitor COM3 --pins 0,2,4 --edge changed --duration 10
```

`monitor` only reads input state with `machine.Pin(pin, machine.Pin.IN)`. It does not switch pins to output mode or configure pulls. If `--pins` is omitted, pyrite-cli conservatively probes common GPIO numbers.

### 3.13 WebREPL Connections

All device commands can use a WebSocket connection instead of serial:

```bash
pyrcli debug board-info COM3 --ws ws://192.168.4.1:8266/ --password mypass
pyrcli flash COM3 main.py /main.py --ws ws://esp32.local:8266
```

Password resolution order: `--password`, then `PYRITE_WEBREPL_PASSWORD`, then interactive input.

### 3.14 Serial Port Occupancy Handling

When opening a serial port fails with an error that looks like permission denied, already occupied, or resource busy, an interactive terminal asks whether to scan and release the holder. Windows prefers Sysinternals `handle.exe` for precise handle lookup and falls back to command-line heuristics. Linux and macOS use `lsof` or `fuser`. pyrite-cli asks again before terminating a process, and non-interactive sessions skip this flow.

---

## 4. Quick Example: Hello World

### 4.1 Create a Device File

```python
# hello.py
import time

while True:
    print("Hello from MicroPython!")
    time.sleep(1)
```

### 4.2 Scan Serial Ports

```bash
pyrcli scan
# Example output:
# COM3 - USB Serial Device (VID:PID 10C4:EA60)
```

### 4.3 Flash the File

```bash
pyrcli flash COM3 hello.py /hello.py
```

### 4.4 Run and Verify

```bash
pyrcli repl COM3
# In the REPL: exec(open('/hello.py').read())
# -> Hello from MicroPython!
# -> Hello from MicroPython!
```

### 4.5 Inspect the Device File

```bash
pyrcli fs ls COM3 /
# -> hello.py  31  F
pyrcli fs cat COM3 /hello.py
# -> import time
# -> ...
```

---

## 5. Configuration

### 5.1 `.pyrite_config.json`

Create this file at the project root. pyrite-cli searches upward from the current directory and stops at the first match. If the file does not exist, all defaults still work.

```json
{
  "chunk_size": 4096,
  "download_threads": 4,
  "auto_compile": true,
  "verify": "size",
  "delta_flash": "auto",
  "precheck": "basic",
  "precheck_compat": "warn",
  "precheck_mp_version": "",
  "max_retries": 2,
  "baudrate": 921600,
  "timeout": 10
}
```

Keep settings in this one flat top-level object. Legacy `profile` and `profiles` keys are ignored with a warning.

`pyrcli test` is the naming exception: `--timeout` / `PYRITE_TIMEOUT` controls device-test execution, while `--connect-timeout` / `PYRITE_CONNECT_TIMEOUT` controls its connection timeout.

| Field | Default | Meaning |
|-------|---------|---------|
| `chunk_size` | `4096` | Maximum bytes written per host-side transfer chunk |
| `download_threads` | `4` | Stub download worker count, clamped to 1-12 |
| `auto_compile` | `true` | Whether to compile `.py` to `.mpy` automatically |
| `verify` | `"size"` | Post-flash verification: `off`, `size`, or `crc32` |
| `delta_flash` | `"auto"` | Single-file delta flashing policy: `off`, `auto`, or `on` |
| `precheck` | `"basic"` | Pre-flash code check: `off`, `basic`, or `strict` |
| `precheck_compat` | `"warn"` | Strict compatibility handling: `warn`, `error`, or `off` |
| `precheck_mp_version` | `""` | Optional target MicroPython firmware version, for example `1.20.0` |
| `max_retries` | `2` | Maximum retries after verification failure or disconnect; `0` disables retries |
| `baudrate` | `921600` | Default serial baudrate; tune per board stability |
| `timeout` | `10` | Default serial connection and I/O timeout in seconds |

Generate a default config with:

```bash
pyrcli config
```

### 5.2 `pyproject.toml` Board Tags

Extend device tag mappings in `pyproject.toml`. They are merged with the built-in defaults:

```toml
[tool.pyrite.board_tags]
ESP32_S3 = ["ESP32", "wifi"]
C3 = ["ESP32", "wifi", "ble"]
MY_BOARD = ["MY_BOARD", "wifi", "sdcard"]
```

These tags affect automatic feature activation for conditional compilation. See [Conditional Compilation: Practical Guide](conditional-compilation-guide.md).

### 5.3 Configuration Load Order

For ordinary serial connection settings, the highest-precedence available value wins:

```text
explicit --baudrate/--timeout or PYRITE_BAUDRATE/PYRITE_TIMEOUT
    |
    v
.pyrite_config.json
    |
    v
built-in defaults
```

For `pyrcli test`, substitute `--connect-timeout` / `PYRITE_CONNECT_TIMEOUT` for the connection-timeout part of this chain. Its `--timeout` / `PYRITE_TIMEOUT` pair is reserved for test execution.

Project data loads in this order, with custom `board_tags` extending the built-in mapping:

```text
PyriteConfig() built-in defaults
    |
    v
.pyrite_config.json found by upward search
    |
    v
pyproject.toml [tool.pyrite.board_tags] found by upward search
```

---

## 6. Further Reading

| Direction | Document |
|-----------|----------|
| Flashing internals and incremental sync | [Device Flashing and Project Sync](device-flashing-and-project-sync.md) |
| Conditional compilation for multi-platform source trees | [Conditional Compilation: Practical Guide](conditional-compilation-guide.md) |
| Desktop file-manager access to device files | [WebDAV Mount](webdav-mount.md) |
| Runtime-observable firmware capabilities | [MicroPython Firmware Feature Probes](micropython-firmware-feature-probes.md) |

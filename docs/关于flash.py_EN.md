# `flash.py` — MicroPython Device Flashing Tool

Communicates with MicroPython devices (ESP32, ESP8266, RP2040, etc.) via serial (UART) **raw REPL mode**, supporting file upload, directory upload, interactive REPL terminal, automatic `.py → .mpy` compilation, and conditional compilation.

---

## Configuration File `.pyrite_config.json`

Place it in the **project root directory** to control flash behavior and download thread count. The tool searches upward from the current directory and uses the first one found.

```json
{
  "chunk_size": 4096,
  "download_threads": 4,
  "auto_compile": true
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `chunk_size` | `4096` | Max data per write (bytes). Larger values reduce REPL round-trips but increase per-buffer pressure. |
| `download_threads` | `4` | Concurrent stub download threads, range 1–12 |
| `auto_compile` | `true` | Whether to auto-compile `.py` → `.mpy`; set to `false` to disable |

The tool works fine without this file; defaults are used.

### board_tags Supplemental Configuration (`pyproject.toml`)

Additional device identification tag mappings can be defined in `pyproject.toml`, merged with built-in defaults:

```toml
[tool.pyrite.board_tags]
ESP32_S3 = ["ESP32", "wifi"]
C3 = ["ESP32", "wifi"]
```

---

## Module-Level Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `CONFIG_FILE` | `".pyrite_config.json"` | Configuration filename |
| `DEFAULT_CHUNK_SIZE` | `4096` | Default chunk size (bytes) |
| `_DEFAULT_BOARD_TAGS` | built-in dict | Default board tag mappings |
| `ENTER_RAW_REPL` | `b'\x01'` | Ctrl+A — enter raw REPL |
| `EXIT_RAW_REPL` | `b'\x02'` | Ctrl+B — exit raw REPL |
| `SET_RESET` | `b'\x03'` | Ctrl+C — interrupt/reset |
| `SET_EXECUTE` | `b'\x04'` | Ctrl+D — execute |
| `ENTER_RAW_PASTE` | `b'\x05'` | Ctrl+E — enter paste mode |

---

## Module-Level Functions

### `_load_config()`

Searches upward from the current directory for `.pyrite_config.json`, also scans `pyproject.toml` for `[tool.pyrite.board_tags]` and merges into built-in tags.

- Returns: `dict` with keys `chunk_size`, `download_threads`, `auto_compile`, `board_tags`

### `_compile_to_mpy(local_path, bytecode_ver=None, arch=None)`

Compiles `.py` → `.mpy` using the `mpy_cross` Python API.

| Parameter | Description |
|-----------|-------------|
| `local_path` | Path to the local `.py` file |
| `bytecode_ver` | Target device mpy bytecode version (optional, auto-read from device) |
| `arch` | Target architecture, e.g. `xtensawin`, `armv7m` (optional, auto-read from device) |

- Returns: `(tmp_mpy_path, tmp_dir)`, or `(None, None)` on failure
- Fallback: prints a warning on compile failure and falls back to raw `.py`

### `create_default_config()`

Creates a default `.pyrite_config.json` in the working directory (with `chunk_size`, `download_threads`, `auto_compile`).

---

## `class MicroPython`

MicroPython device operation class. Encapsulates serial port scanning, connection, raw REPL communication, kbd_intr protection, file flashing, interactive REPL terminal, and more.

### Constructor

```python
MicroPython(port=None, baudrate=115200, timeout=10)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `port` | `str` | `None` | Serial port name, e.g. `"COM3"`, `"/dev/ttyUSB0"` |
| `baudrate` | `int` | `115200` | Baud rate |
| `timeout` | `int` | `10` | Serial read/write timeout (seconds) |

Configuration is auto-loaded on construction (`_load_config`).

**Instance attributes**:

| Attribute | Type | Description |
|-----------|------|-------------|
| `config` | `dict` | Merged configuration |
| `ser` | `Serial` or `None` | pySerial serial object |
| `port` | `str` | Serial port name |
| `baudrate` | `int` | Baud rate |
| `timeout` | `int` | Timeout |
| `_in_raw` | `bool` | Whether in raw REPL mode |
| `_kbd_set` | `bool` | Whether `kbd_intr(-1)` has been set |
| `transport` | `Transport` | Transport layer instance (SerialTransport / WebREPLTransport) |

---

### Static Method: Port Scanning

#### `MicroPython.scan_ports(vid=None, pid=None, keyword=None, require_vid=True)`

Scans all system serial ports with optional filtering.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `vid` | `None` | Filter by VID (decimal) |
| `pid` | `None` | Filter by PID (decimal) |
| `keyword` | `None` | Filter by description keyword (case-insensitive) |
| `require_vid` | `True` | Set to `False` to include devices without VID/PID |

- Returns: `list[dict]`, each item contains `device`, `description`, `hwid`, `vid`, `pid`, `serial_number`

---

### Connection Management

#### `connect(port=None, baudrate=None)`

Opens a serial connection to the device. If already connected, disconnects first before reconnecting. Waits 300ms for the device serial port to be ready, then clears RX/TX buffers.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `port` | No | Override the port specified at construction |
| `baudrate` | No | Override the baud rate specified at construction |

- Returns: `True`
- Raises: `ValueError` — no port specified; `serial.SerialException` — connection failed

#### `disconnect()`

Disconnects from the device. Delegates to `self.transport.disconnect()`. Serial transport will:
1. Exit raw REPL mode (if in it)
2. Restore `kbd_intr(3)` (if `kbd_intr(-1)` was previously set)
3. Close the serial port

#### `is_connected` (property)

`bool` — whether connected and the serial port is open.

---

### Raw REPL Protocol (Internal Methods)

The following methods are internal but can be called for extended functionality.

#### `_enter_raw_repl()`

Enters MicroPython raw REPL mode.

Procedure:
1. Send `Ctrl+C` (`\x03`) twice to interrupt any running program
2. Clear the input buffer
3. Send `Ctrl+A` (`\x01`) to enter raw REPL
4. Wait for the device to return the `>` prompt
5. If `>` does not appear (device loop cannot be interrupted), send `Ctrl+D` (`\x04`) for a soft reboot and retry

Raises:
- `RuntimeError` — unable to enter raw REPL after repeated attempts

#### `_exit_raw_repl()`

Exits raw REPL back to normal REPL. Sends `Ctrl+B` (`\x02`).

#### `_write(data)`

Writes data to the serial port.

| Parameter | Description |
|-----------|-------------|
| `data` | `str` or `bytes`; `str` is auto-encoded as UTF-8 |

#### `_read_until(terminator=b"\x04", timeout=None)`

Reads from the serial port until the terminator is encountered.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `terminator` | `b"\x04"` | Terminator byte sequence |
| `timeout` | Instance `timeout` | Timeout in seconds |

- Returns: all `bytes` data including the terminator

#### `_execute(code, timeout=10)`

Executes Python code in raw REPL and returns device stdout.

Procedure:
1. Write the code text
2. Send `Ctrl+D` (`\x04`) to trigger execution
3. Read until trailing `\x04` is received
4. Strip trailing `\x04`, decode to text
5. Strip leading `"OK"` (MicroPython's execution status marker)
6. Check output for `"Traceback"`

| Parameter | Default | Description |
|-----------|---------|-------------|
| `code` | — | Python code string or `bytes` |
| `timeout` | `10` | Max seconds to wait for device response |

- Returns: `str` — device output text
- Raises: `RuntimeError` — device returned a Traceback during execution

---

### Unified Logging System

Logging is now handled by `cli/utils/log.py` — a unified logging system shared across all modules.

The `MicroPython` class uses `get_logger("cli.flash")` for structured logging with:

- 6 log levels: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR`, `FATAL`
- Console output with ANSI colors and JSONL file recording
- `Logger.operation()` context manager for automatic start/end/duration logging
- `TrafficMonitor` for serial/WebSocket flow capture (recorded as JSONL with `type: "traffic"`)
- Control characters replaced with readable names (`\x01` → `<RAW>`, `\x03` → `<C>`, etc.)

The old `_repl_log_ctx()`, `_open_repl_log()`, `_close_repl_log()`, `_drain_rx_log()`, and `_log_repl_data()` methods have been **removed** in favor of the centralized logging system.

**Usage**:

```python
from cli.utils.log import get_logger

log = get_logger("cli.flash")
log.info("Flashing file %s", path)

with log.operation("flash_file", path=path, size=size):
    # operation timing automatically recorded
    ...
```

---

### kbd_intr Protection

During flashing, data streams may contain byte `0x03` (Ctrl+C). By default, MicroPython triggers a `KeyboardInterrupt` and resets the device upon receiving `\x03`, causing flash failure.

Solution: set `kbd_intr(-1)` before flashing to disable interrupts, then restore `kbd_intr(3)` after completion.

#### `_setup_kbd_intr()`

Executes `import micropython; micropython.kbd_intr(-1)`.

#### `_restore_kbd_intr()`

Executes `import micropython; micropython.kbd_intr(3)`.

- If `_setup_kbd_intr()` was never called (`_kbd_set` is `False`), returns immediately.
- Silently ignores failures.

---

### File Flashing

#### `flash_file(local_path, remote_path=None, compile=None, bytecode_ver=None, arch=None, active_tags=None)`

Uploads a single local file to the MicroPython device. Supports preprocessing, `.mpy` compilation, and flashing in one pipeline.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `local_path` | Yes | Absolute or relative path to local file |
| `remote_path` | No | Destination path on device; defaults to filename |
| `compile` | No | Override `config["auto_compile"]` |
| `bytecode_ver` | No | Target mpy bytecode version (auto-read from device) |
| `arch` | No | Target architecture (auto-read from device) |
| `active_tags` | No | Set of conditional compilation tags |

Full procedure:
1. If `active_tags` is non-empty and the file is `.py`, run the preprocessor for conditional compilation
2. `manifest.py` is skipped; `main.py`/`boot.py` and `.pyi` files are not compiled
3. If compilation is enabled and the file is `.py`, call `_compile_to_mpy()`; on success, flash the `.mpy` instead
4. Send the `FLASH` template script (with `FSIZE` placeholder for file size); the device opens `FILE`, `wb` and loops `read(BFSIZE)` until the specified byte count is reached
5. Send raw bytes in `DEFAULT_CHUNK_SIZE` chunks
6. Print the flash result

Flash flow:

```
Connect → Enter raw REPL → Preprocess (conditional compilation) → Compile (optional) → Send FLASH template (with file size) → Send chunks → Device counts and writes → Done
```

- Raises: `FileNotFoundError` — local file does not exist

#### `flash_program(local_dir, remote_prefix="", bytecode_ver=None, arch=None, active_tags=None, manifest_path=None)`

Uploads an entire directory tree to the MicroPython device in batch mode (one script injection + one data stream, instead of flashing files individually).

| Parameter | Required | Description |
|-----------|----------|-------------|
| `local_dir` | Yes | Local directory path |
| `remote_prefix` | No | Remote path prefix on device (e.g. `"lib"`) |
| `bytecode_ver` | No | Target mpy bytecode version |
| `arch` | No | Target architecture |
| `active_tags` | No | Set of conditional compilation tags |
| `manifest_path` | No | Path to manifest.py (controls which files are flashed) |

Procedure:
1. **Collect file list** — if `manifest_path` is provided, parse via `manifest_loader`; otherwise recursively scan directory for all `.py` files
2. **One-pass preprocessing** — run conditional compilation and `.py → .mpy` compilation on all files in a single pass (skipping manifest.py and .pyi)
3. **Batch directory creation** — create all required subdirectories on the device in a single `_execute()` call (saving N-1 serial round-trips)
4. **Send FLASH_PROGRAM template** — inject the `FLASH_PROGRAM` script with `FILES` replaced by `[(size, remote_path), ...]` tuples; the device sequentially `open()` + `read()` + writes each file
5. **Single streaming transfer** — concatenate all file content and stream in `DEFAULT_CHUNK_SIZE` chunks

- Returns: `list[tuple(local_path, remote_path, success)]` — result for each file

---

### Device Information Detection

#### `get_mpy_version() -> tuple[int, str] | tuple[None, None]`

Reads the mpy bytecode version and architecture from the device.

- Executes code on the device to read `sys.implementation.mpy`
- Extracts version number (low 8 bits) and architecture name (high 6-bit mapping)
- Returns `(ver, arch)` e.g. `(6, 'xtensawin')`; on failure returns `(None, None)`

#### `detect_tags() -> set`

Reads board information from the device and returns the active tag set.

- Reads `os.uname().machine` and `sys.platform`
- Matches against `config["board_tags"]` and returns matching tags (e.g. `{"ESP32", "wifi"}`)
- Also adds the `sys.platform` value to the tag set

---

### Other Methods

#### `run(code)`

Executes arbitrary Python code on the device and returns the output. Shortcut for `_enter_raw_repl()` + `_execute()`.

| Parameter | Description |
|-----------|-------------|
| `code` | Python code string |

- Returns: `str` — device output

```python
mp.run("import machine; print(machine.freq())")
```

#### `reset()`

Soft-reboots the device (`machine.reset()`). Restores `kbd_intr(3)` first, then sends the reboot command.

---

### Interactive REPL

#### `repl_()`

Interactive terminal connected to the MicroPython device (serial passthrough mode). Serial output is displayed in real-time, and keyboard input is forwarded to the device.

**Design**: single-loop architecture with no separate read/write threads and no `input()` calls. Both directions (device→terminal and keyboard→serial) are handled in one serial loop.

**Cross-platform non-blocking keyboard input**:

| Platform | Non-blocking detection | Key reading |
|----------|------------------------|-------------|
| Windows | `msvcrt.kbhit()` | `msvcrt.getch()` |
| macOS/Linux | `select.select()` | `os.read(fd, 1)` |

- On Unix, temporarily switches the terminal to cbreak mode (disables `ECHO`/`ICANON`/`ISIG`); restores on exit

**ANSI error highlighting**:

Calls `_colorize_repl_output()` to scan serial output in real-time:

| Scenario | Effect |
|----------|--------|
| Complete Traceback → Error line in a single packet | Precise truncation, only error portion rendered in red |
| Traceback and Error line arrive across multiple packets | Tracks `in_error` state continuously, red closes automatically when Error line arrives |

**Windows extended key mapping**:

`\xe0`-prefixed sequences for arrow and editing keys are mapped to ANSI escape sequences before forwarding:

| Raw key | Mapped output |
|---------|---------------|
| ↑ / ↓ / → / ← | `\x1b[A` / `\x1b[B` / `\x1b[C` / `\x1b[D` |
| Home / End | `\x1b[H` / `\x1b[F` |
| Delete | `\x1b[3~` |
| Insert | `\x1b[2~` |

**Loop logic**:

```
Interrupt device → enter normal REPL → while connected:
    serial has data → read → error highlight → output to terminal
    keyboard has input → read → forward to serial
    sleep(0.01)
```

**Exit**: on Unix, `Ctrl+C` exits the session; on Windows, there is no special exit key — use `Ctrl+Break` or close the window.

---

### Context Manager

```python
with MicroPython(port="COM3") as mp:
    mp.flash_file("boot.py")
    mp.flash_file("main.py")
# auto disconnect()
```

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Serial port doesn't exist / is busy | `serial.SerialException` |
| Cannot auto-discover device | Prompts user to specify `--port` |
| Device returns Traceback in raw REPL | `RuntimeError`, outputs full Traceback |
| Connection drops mid-flash | `serial.SerialException`; file on device may be unclosed |
| Local file does not exist | `FileNotFoundError` |
| Invalid directory | `NotADirectoryError` |
| Cannot enter raw REPL | `RuntimeError` with raw device response |

---

### Device File Management

#### `fs_ls(remote_path="/")`

List files and directories on the device. Calls `os.stat()` twice per entry to ensure stable directory sizes on some MicroPython ports.

- Returns: `list[dict]` with keys `name`, `type` (`F`/`D`), `size`

#### `fs_df()`

Get device filesystem usage via `os.statvfs('/')`.

- Returns: `dict {'total': int, 'used': int, 'free': int}`

#### `project_pull(local_dir, remote_prefix, ..., dry_run=False)`

Download project files from device (batch transfer, similar to `flash_program`):

1. Collect all file paths (auto-discover from device if local dir is empty)
2. Send one script: device stats all files, outputs sizes + concatenated raw bytes
3. Host splits by size and writes each file locally

Markers: `[INFO]` `[PREVIEW]` `[SKIP]` `[ERROR]` — status: `✓` green / `✗` red

#### `_discover_device_files(remote_prefix)`

Recursively discover all files on the device. Device outputs `size|path` per line, host parses line by line.

- Returns: `list[(full_remote_path, size)]`

---

## FLASH Template Script (Single File)

Python script injected into the device for single-file flashing, responsible for receiving data and writing to file:

```python
import sys,micropython

micropython.kbd_intr(-1)
usb = sys.stdin.buffer

f_size = FSIZE
with open("FILE", 'wb') as f:
    while f_size:
        ln = usb.read(BFSIZE)
        if ln:
            f.flush()
            f.write(ln)
            f_size -= len(ln)
            print(f_size)
micropython.kbd_intr(3)
```

- `FSIZE` is replaced with the total file size, `FILE` with the device target path, `BFSIZE` with `DEFAULT_CHUNK_SIZE`
- The device counts bytes precisely: each chunk reduces the remaining count; when it reaches 0, the file is closed
- The remaining size is printed after each chunk; the PC side could use this to track progress (currently unused)

## FLASH_PROGRAM Template Script (Batch Flash)

Script injected for directory-wide flashing, processing multiple files at once:

```python
import sys, micropython

micropython.kbd_intr(-1)
usb = sys.stdin.buffer

entries = FILES
for file_size, file_path in entries:
    with open(file_path, 'wb') as f:
        remaining = file_size
        while remaining:
            chunk = usb.read(min(remaining, BFSIZE))
            if chunk:
                f.write(chunk)
                remaining -= len(chunk)
micropython.kbd_intr(3)
```

- `FILES` is replaced with a list of `[(size, remote_path), ...]` tuples
- The PC side concatenates all file content into a continuous byte stream; the device splits it by the sizes in `FILES` and writes each file sequentially
- No per-file round-trips required — one script injection + one data stream flashes the entire directory

---

## Frequently Asked Questions

**Q: Why is `kbd_intr(-1)` necessary?**

Binary files (and some text files) may contain bytes with value `0x03`. By default, MicroPython treats `0x03` as Ctrl+C and triggers a `KeyboardInterrupt` that resets the device. `kbd_intr(-1)` disables this behavior to ensure data is written completely.

**Q: What chunk size (`chunk_size`) should I use?**

- Smaller chunks (512–1024): lower memory usage, good for RAM-constrained devices; but more REPL round-trips, slower.
- Larger chunks (4096–8192): fewer round-trips, faster; requires sufficient device buffer space.
- ESP32 recommended: 4096, ESP8266 recommended: 2048.

**Q: How do I find the serial port?**

```bash
pyrcli scan
```

**Q: Which MicroPython devices are supported?**

Any MicroPython device that supports raw REPL (Ctrl+A) over a serial connection, including but not limited to ESP32, ESP8266, RP2040 (Raspberry Pi Pico), PYBoard, etc.

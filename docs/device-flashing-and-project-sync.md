# Device Flashing and Project Sync

This document explains how pyrite-cli communicates with MicroPython devices, how the file flashing protocols work, how batch flashing differs from single-file flashing, and how incremental project sync and device file management are implemented. It is intended for users who need to understand flashing mechanics or use the `project` command group.

Prerequisite: [Quick Start](quick-start.md) for basic commands and configuration.

---

## 1. Communication Basics

### 1.1 UART Raw REPL Protocol

pyrite-cli communicates with MicroPython devices through the serial **raw REPL mode**. Raw REPL is a binary protocol mode provided by MicroPython: the host sends Python code and the device returns stdout byte by byte.

Control characters:

| Character | Value | Purpose |
|-----------|-------|---------|
| `Ctrl+A` | `\x01` | Enter raw REPL mode |
| `Ctrl+B` | `\x02` | Exit raw REPL and return to friendly REPL |
| `Ctrl+C` | `\x03` | Interrupt the running program |
| `Ctrl+D` | `\x04` | Execute the buffered code |
| `Ctrl+E` | `\x05` | Enter paste mode |

A typical device operation lifecycle:

```text
connect()            -> open the serial port
_enter_raw_repl()    -> Ctrl+C interrupt, then Ctrl+A raw REPL
_operate()           -> execute code or transfer files
_exit_raw_repl()     -> Ctrl+B leaves raw REPL
disconnect()         -> close the serial port
```

### 1.2 `kbd_intr` Protection

Binary files may contain byte value `0x03`, which is also `Ctrl+C`. MicroPython treats `0x03` as a `KeyboardInterrupt` signal by default; receiving it during a transfer can reset the device and break the flash operation.

Before flashing, pyrite-cli runs `micropython.kbd_intr(-1)` on the device to disable the interrupt character, then restores `kbd_intr(3)` after flashing.

### 1.3 REPL Serial Logs

When debugging flashing problems, all serial traffic is recorded under `./log/flash_YYYYMMDD_HHMMSS.log`. Control characters are rendered with readable names (`\x01` -> `<RAW>`, `\x03` -> `<C>`, `\x04` -> `<D>`), which makes raw protocol failures easier to inspect.

---

## 2. Single-File Flashing

### 2.1 Flow Overview

```text
connect -> enter raw REPL -> preprocess conditional code -> compile .py to .mpy if enabled
    -> send FLASH template script with file size -> send raw bytes in chunks
    -> device receives and writes -> verify -> complete
```

### 2.2 FLASH Template Script

For single-file flashing, the PC injects a Python script like the following into the device. The device executes it, receives raw data, and writes the target file:

```python
import sys,micropython
try:
    micropython.kbd_intr(-1)
    usb = sys.stdin.buffer
    sys.stdout.write('READY')
    f_size = FSIZE          # replaced with total file size
    with open(FILE, 'wb'):   # FILE is replaced with the device path
        while f_size:
            want = min(BFSIZE, f_size)   # BFSIZE is replaced with chunk_size
            d = b''
            while len(d) < want:
                c = usb.read(min(64, want - len(d)))
                if c:
                    d += c
            f.write(d)
            f.flush()
            f_size -= len(d)
            sys.stdout.write('+')
    micropython.kbd_intr(3)
    rec = b''
    while not (b'ok' in rec):
        rec = (rec + (usb.read(1) or b''))[-16:]
    sys.stdout.write('ok')
except Exception as e:
    sys.stdout.write('FLASH_ERR:' + str(e) + '\\n')
    raise
```

Device-side protocol: `READY` means the receiver is ready, `+` marks progress after each written chunk, and `ok` confirms completion.

The inner read loop requests at most 64 bytes from the device-side stream each time and accumulates data until it reaches `want`. This is a reliability guard for MicroPython REPL stdin buffering across boards.

### 2.3 Chunked Transfer

On the PC side, the file is split by `chunk_size` (default `4096`, configurable in `.pyrite_config.json`) and sent chunk by chunk. The device writes each chunk and reports progress.

| `chunk_size` | Tradeoff |
|--------------|----------|
| 512-1024 | Lower memory pressure for small-RAM devices; more REPL round trips and slower transfer |
| 4096-8192 | Fewer round trips and faster transfer; requires enough device-side buffering |

### 2.4 Verification

Post-flash verification is controlled by the `verify` config field:

| Mode | Behavior | Use case |
|------|----------|----------|
| `off` | No verification | Optimize for speed on small transfers |
| `size` | Compare file size | Default; detects obvious failures quickly |
| `crc32` | Compare file size and CRC32 | Strict verification, catches data corruption |

Verification failures are retried automatically according to `max_retries`.

---

## 3. Batch Flashing

### 3.1 Difference from Single-File Flashing

`flash-program` transfers a whole directory tree to the device with one device-side script and one continuous data stream:

| | Single-file flash | Batch flash |
|-|-------------------|-------------|
| Script injection count | Once per file | Once total |
| Data stream | One independent transfer per file | Continuous byte stream |
| Compilation | Single-file compile | Parallel multi-file compile |

### 3.2 Three-Stage Flow

**Stage 1: collect and preprocess**

Collect `.py` files, apply conditional compilation preprocessing if active tags are specified, and filter out `manifest.py` and `.pyi` files.

**Stage 2: parallel compile**

Use `ThreadPoolExecutor` to invoke `mpy-cross` concurrently and compile `.py` files to `.mpy`. Files that fail to compile silently fall back to the original `.py` source and do not abort the whole batch.

**Stage 3: batch write**

Create the directory structure on the device, send the `FLASH_PROGRAM` script, then stream all file bytes continuously.

### 3.3 FLASH_PROGRAM Template Script

```python
import sys, micropython

# Disable Ctrl+C interrupts so 0x03 bytes in the data stream do not reset the device.
try:
    micropython.kbd_intr(-1)
    usb = sys.stdin.buffer
    sys.stdout.write('READY')

    # FILES format: [(size, remote_path), ...], replaced by the PC before execution.
    entries = FILES
    for file_size, file_path in entries:
        with open(file_path, 'wb') as f:
            remaining = file_size
            while remaining:
                blk = min(remaining, BFSIZE)
                d = usb.read(blk)
                if d:
                    f.write(d)
                    f.flush()
                    remaining -= len(d)
                    sys.stdout.write('+')
    micropython.kbd_intr(3)
except Exception as e:
    sys.stdout.write('FLASH_ERR:' + str(e) + '\\n')
    raise
```

The PC concatenates all file contents into one continuous byte stream. The device uses the sizes from `FILES` to split the stream back into individual files, writing each file in order and emitting `+` progress markers.

### 3.4 Manifest System

`manifest.py` controls which files are included during batch flashing and supports feature-based filtering.

Syntax:

```python
module("main.py")                                # single file, unconditional
module("lib/utils.py", features=["wifi"])        # only when the wifi tag is active
package("lib/drivers")                           # every .py under the directory
package("lib/net", features=["wifi"])            # entire directory only for wifi
```

- `module(filename, remote=None, features=None)` selects one file.
- `package(dirname, remote=None, features=None)` selects a directory recursively.

Parameters:

| Parameter | Meaning |
|-----------|---------|
| `remote` | Target path on the device; defaults to `filename` |
| `features` | Tag list; the entry is included if any listed tag is active. `None` means unconditional |

Manifests are parsed by a safe AST parser based on the `ast` module, not by `exec`. Only `module()` and `package()` calls are allowed, and arguments must be literals.

Manifest filtering can be combined with source-level conditional compilation. See [manifest.py Combined Filtering](conditional-compilation-guide.md#manifestpy-combined-filtering).

---

## 4. Incremental Sync with `project`

### 4.1 Design Motivation

Full directory flashing is wasteful for large projects or fast iteration. The `project` command group compares SHA256 hashes and flashes **only added or changed files**.

### 4.2 Hash Configuration

Hash state is stored in `pyrite_file_config.json` at the project root:

```json
{
  "version": 1,
  "hash_algorithm": "sha256",
  "files": {
    "main.py": "e3b0c44298fc...",
    "lib/utils.py": "01ba4719c80b..."
  }
}
```

### 4.3 `project hash`: Offline Scan

Scan the project directory, calculate SHA256 hashes for flashable files, and save the config file. No serial connection is required.

```bash
pyrcli project hash .
pyrcli project hash ./src --manifest manifest.py
```

### 4.4 `project flash`: Incremental Flash

Load the hash config, compare current local hashes, flash only added or changed files, then update the hash config.

```bash
pyrcli project flash COM3 . /
pyrcli project flash COM3 . / --target ESP32
pyrcli project flash COM3 . / --feature sdcard
```

### 4.5 `project status`: Difference Preview

Compare local hashes and device-side file sizes, print differences, and do not flash:

```text
[ADD] lib/utils.py        # exists locally, missing on device
[MOD] main.py             # local hash changed
[DEL] old_config.py       # removed from the project
```

```bash
pyrcli project status COM3 . /
```

### 4.6 `project pull`: Pull from Device

Download files from the device in bulk:

```bash
pyrcli project pull COM3 . /
pyrcli project pull COM3 . / --dry-run
```

If the local directory is empty, pyrite-cli recursively discovers files on the device. Transfer uses one script injection plus batch transfer, mirroring the `flash-program` flow.

---

## 5. Device File Management

### 5.1 Bytes Download Protocol

Reading files from the device uses a two-stage protocol:

1. Use `run()` to get the file size as text.
2. Execute a byte-output script in raw REPL and let the PC receive exactly the known size.

```python
# Approximate device-side script
import sys
size = os.stat(path)[6]
sys.stdout.write(str(size) + "\n")
sys.stdout.buffer.write(open(path, 'rb').read())
```

### 5.2 Enhanced `fs ls`

```bash
pyrcli fs ls COM3 /
pyrcli fs ls COM3 / -r
pyrcli fs ls COM3 / --sort size
pyrcli fs ls COM3 / -p
```

`fs_ls_recursive()` runs recursive `os.listdir()` and `os.stat()` on the device to traverse the directory tree in one operation, avoiding repeated host/device round trips.

---

## 6. Error Handling

| Scenario | Typical result |
|----------|----------------|
| Serial port missing or occupied | `serial.SerialException` |
| Device returns a traceback in raw REPL | `RuntimeError` with the full traceback |
| Disconnect during flashing | `serial.SerialException`; the device-side file may not be closed |
| Local file missing | `FileNotFoundError` |
| Invalid directory | `NotADirectoryError` |
| Cannot enter raw REPL | `RuntimeError` with raw device response |
| Device file read timeout or incomplete transfer | `RuntimeError` with received byte count |
| Cannot identify device target | Prompt to use `--target` manually |

---

Next step: [Conditional Compilation: Practical Guide](conditional-compilation-guide.md).

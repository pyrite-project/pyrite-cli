# Pyrite CLI

[English](README.md) | [中文](README.zh-CN.md)

Pyrite CLI is a MicroPython device toolkit for flashing code or , browsing files, opening a REPL, syncing projects, and mounting the device filesystem in your system. It talks to boards through UART raw REPL or WebREPL over WebSocket, so the same command surface works over USB serial and Wi-Fi.

It is built for the everyday loop of MicroPython development: find the board, push code, inspect files, run a quick command, and repeat without juggling several small tools.

## Features/Highlights

| Need | Command | What Pyrite CLI does |
|------|---------|----------------------|
| Find boards | `pyrcli scan` | Lists serial devices, with optional board probing and JSON output |
| Flash one file | `pyrcli flash COM3 main.py /main.py` | Preprocesses, optionally compiles to `.mpy`, transfers, and verifies |
| Sync a project | `pyrcli project flash COM3 . /app` | Uses hashes to upload only changed files |
| Browse files | `pyrcli fs ls COM3 /` | Lists, uploads, downloads, removes, moves, and copies device files |
| Mount files | `pyrcli mount COM3` | Exposes the device filesystem through a local WebDAV bridge |
| Debug live | `pyrcli repl COM3` | Opens an interactive MicroPython REPL |
| Use Wi-Fi | `--ws ws://esp32.local:8266` | Routes device commands through WebREPL |
| Flash firmware | `pyrcli firmware flash COM3 firmware.bin` | Wraps `esptool` for firmware operations |

## Install

```bash
pip install pyrite-cli
```

Firmware flashing is optional and uses `esptool`:

```bash
pip install esptool
```

For local development from this repository:

```bash
pip install -e .
```

Core runtime dependencies include `typer`, `pyserial`, `requests`, `tqdm`, `mpy-cross`, `libcst`, and `websocket-client`.

## Quick Start

```bash
# Find connected boards
pyrcli scan
pyrcli scan -i

# Inspect a board
pyrcli board-info COM3

# Flash a file
pyrcli flash COM3 main.py /main.py

# Run a quick command
pyrcli run COM3 "import machine; print(machine.freq())"

# Open an interactive REPL
pyrcli repl COM3

# List and transfer files
pyrcli fs ls COM3 /
pyrcli fs put COM3 local.py /remote.py
pyrcli fs get COM3 /remote.py local_copy.py

# Mount the filesystem in your desktop file manager
pyrcli mount COM3
```

Use WebREPL instead of serial by adding `--ws`. The positional `PORT` is kept for a consistent CLI shape; the WebSocket URL is the real transport target.

```bash
pyrcli board-info COM3 --ws ws://192.168.4.1:8266 --password mypass
pyrcli flash COM3 main.py /main.py --ws ws://esp32.local:8266
pyrcli mount COM3 --ws ws://esp32.local:8266 --password mypass
```

If `--password` is omitted, WebREPL password resolution is: CLI option, `PYRITE_WEBREPL_PASSWORD`, then interactive prompt.

## Main Features

**Fast flashing loop**

Pyrite CLI enters raw REPL, transfers code in chunks, verifies the result, and restores the device session. Python files can be compiled to `.mpy` automatically before upload.

**Project-level incremental sync**

`pyrcli project hash` records local SHA256 hashes. `pyrcli project flash` compares that state and uploads only added or changed files.

**Conditional builds**

Use `@feature("wifi")`, `@target("esp32")`, `with feature(...)`, and `with target(...)` to keep one source tree for several boards or firmware variants. Pyrite CLI rewrites the syntax with `libcst`, not regular expressions.

**Manifest-based flashing**

`manifest.py` can select modules and packages, remap remote paths, and filter files by feature tags. It is parsed with `ast` and does not execute arbitrary code.

**Desktop filesystem bridge**

`pyrcli mount` starts a local WebDAV server and maps desktop file-manager actions to MicroPython file operations. Windows can map a drive letter; Linux and macOS open the WebDAV location in the default file manager.

**Transport abstraction**

Serial and WebREPL share the same high-level MicroPython operations. Most device commands accept `--ws` and `--password`.

## Common Workflows

### Create a project

```bash
pyrcli project new my-project
pyrcli project new my-project --platform COM3
```

The project helper can detect the board, download matching MicroPython type stubs, and prepare editor configuration.

### Flash a directory

```bash
pyrcli flash-program COM3 src/ /app
pyrcli flash-program COM3 src/ /app --manifest manifest.py
```

### Incremental project sync

```bash
pyrcli project hash .
pyrcli project status COM3 . /app
pyrcli project flash COM3 . /app
pyrcli project pull COM3 . /app
```

### Browse and mount files

```bash
pyrcli fs ls COM3 / -r
pyrcli fs ls COM3 / --sort size
pyrcli fs cat COM3 /main.py
pyrcli mount COM3 --readonly
pyrcli mount COM3 --drive P
```

### Flash firmware

```bash
pyrcli firmware flash COM3 firmware.bin
pyrcli firmware erase COM3
pyrcli firmware info COM3
pyrcli firmware verify COM3 firmware.bin
pyrcli firmware read COM3 0x100000 -o backup.bin
```

## Configuration

Pyrite CLI looks for `.pyrite_config.json` from the current directory upward.

```json
{
  "chunk_size": 4096,
  "download_threads": 4,
  "auto_compile": true,
  "verify": "crc32",
  "max_retries": 2
}
```

| Field | Default | Meaning |
|-------|---------|---------|
| `chunk_size` | `4096` | Maximum bytes per write |
| `download_threads` | `4` | Stub download concurrency, clamped to 1-12 |
| `auto_compile` | `true` | Compile `.py` to `.mpy` automatically |
| `verify` | `"size"` | `off`, `size`, or `crc32` verification |
| `max_retries` | `2` | Retry count after verification or connection failure |

Board tags can be extended in `pyproject.toml`:

```toml
[tool.pyrite.board_tags]
ESP32_S3 = ["ESP32", "wifi"]
C3 = ["ESP32", "wifi"]
```

## Documentation

Start here if you want a guided path:

| Topic | Document |
|-------|----------|
| First steps, commands, config | [快速上手](docs/快速上手.md) |
| Flashing protocol and project sync | [设备刷入与项目同步](docs/设备刷入与项目同步.md) |
| Conditional compilation guide | [Conditional Compilation: Practical Guide](docs/conditional-compilation-guide.md) |
| Conditional compilation, Chinese | [条件编译实战](docs/条件编译实战.md) |
| WebDAV desktop mount | [WebDAV 挂载](docs/WebDAV挂载.md) |
| Architecture notes | [Architecture](docs/architecture.md) |
| `flash.py` internals | [`flash.py` internals](docs/关于flash.py_EN.md) |

## Command Map

### Top-level commands

| Command | Purpose |
|---------|---------|
| `scan` | Scan serial devices, with filters and JSON output |
| `flash` | Flash one local file to the device |
| `flash-program` | Recursively flash a local directory |
| `run` | Execute Python code on the device |
| `repl` | Open an interactive REPL |
| `reset` | Soft-reset the device through raw REPL |
| `board-info` | Print firmware, CPU, memory, flash, and filesystem info |
| `mount` | Mount the device filesystem through local WebDAV |
| `config` | Create a default `.pyrite_config.json` |

### `pyrcli project`

| Command | Purpose |
|---------|---------|
| `project new` | Create a project and download stubs |
| `project init` | Add MicroPython stubs to an existing project |
| `project hash` / `project scan` | Calculate local file hashes |
| `project flash` | Upload only changed files |
| `project status` | Show local/device differences |
| `project pull` | Pull files from the device |
| `project run` | Sync, then enter REPL monitoring |

### `pyrcli fs`

| Command | Purpose |
|---------|---------|
| `fs ls` | List files, with recursion, sorting, and paging |
| `fs cat` | Print a device text file |
| `fs put` | Upload a local file |
| `fs get` | Download a device file |
| `fs rm` | Remove a file or directory |
| `fs tree` | Show a tree view |
| `fs mv` | Move or rename |
| `fs cp` | Copy |

### `pyrcli firmware`

| Command | Purpose |
|---------|---------|
| `firmware flash` | Flash a firmware `.bin` |
| `firmware erase` | Erase flash |
| `firmware info` | Read chip and flash information |
| `firmware verify` | Verify firmware contents |
| `firmware read` | Read flash contents to a file |

## Under the Hood

Pyrite CLI uses MicroPython raw REPL as the common execution layer.

- Raw REPL is entered with `Ctrl+A` (`0x01`), then small Python snippets are executed on the device.
- Single-file flashing injects a device-side receiver that reads binary chunks from `sys.stdin.buffer`.
- Batch flashing sends one manifest of file sizes and paths, followed by one continuous data stream.
- Downloads use a two-stage bytes protocol: query size first, then read a known number of bytes.
- REPL trailers such as `\x04\x04>` are stripped by protocol helpers.
- `kbd_intr(-1)` is set before flashing so data bytes containing `0x03` do not trigger a keyboard interrupt.
- `fs ls` intentionally calls `os.stat(p)` twice because some MicroPython boards can return stale data on the first call.
- Logs are written through the unified logger, including JSONL operation records and serial/WebSocket traffic traces.

## Development

Run the pure logic tests without a device:

```bash
pytest test/test_protocol_helpers.py test/test_flash_utils.py test/test_config.py test/test_manifest_loader.py test/test_logger.py test/test_output.py test/test_webdav_mount.py -v
```

Useful smoke checks:

```bash
python -c "from cli.main import app; from cli.utils.flash import MicroPython"
pyrcli --help
pyrcli scan
pyrcli scan --version
```

Device validation requires an attached MicroPython board:

```bash
pyrcli board-info COM3
pyrcli flash COM3 main.py /main.py
pyrcli run COM3 "print('hello')"
```

## License

See [LICENSE](LICENSE).

<h1 align="center">Pyrite CLI</h1>

<p align="center">
  <a href="README.md">English</a> | <a href="README.zh-CN.md">中文</a>
</p>

Pyrite CLI is a focused command-line toolkit for MicroPython boards. It helps you find devices, flash files, sync projects, inspect the device filesystem, install packages through `mpremote mip`, monitor GPIO inputs, open a REPL, mount device files through WebDAV, and reverse-mount host files through `mpremote` from one `pyrcli` command surface. It's also friendly to agents like Codex and Claude Code that coding with your device will be much easier.

It talks to boards through UART raw REPL by default, and can use WebREPL over WebSocket when you add `--ws`.

## At a Glance

| Task | Command | Notes |
|------|---------|-------|
| Find boards | `pyrcli scan` | Serial scan, optional board probing, JSON output |
| Flash one file | `pyrcli flash COM3 main.py /main.py` | Preprocess, optionally compile to `.mpy`, transfer, verify |
| Sync a project | `pyrcli project flash COM3 . /app` | Upload only added or changed files |
| Browse files | `pyrcli fs ls COM3 /` | List, upload, download, remove, move, and copy files |
| Install packages | `pyrcli pkg install COM3 aioble` | Delegate host-side package install to `mpremote mip install` |
| Monitor GPIO | `pyrcli monitor COM3 --pins 0,2,4 --count 10` | Read GPIO inputs without changing pulls or output mode |
| Mount files | `pyrcli mount COM3` | Expose the device filesystem through local WebDAV |
| Reverse mount | `pyrcli remount COM3 .` | Expose a host directory to the device as `/remote` through `mpremote` |
| Debug live | `pyrcli repl COM3` | Open an interactive MicroPython REPL |
| Use WebREPL | `--ws ws://XXX:XXXX` | Route device commands through WebREPL |

## Install

Pyrite CLI requires Python 3.10 or newer.

```bash
pip install pyrite-cli
```

`mpremote` is installed as a runtime dependency and is used by `pyrcli remount` and `pyrcli pkg`.

For local development from this repository:

```bash
pip install -e .
```

## Quick Start

Use the examples with your own serial port in place of `COM3`.

```bash
# Find connected boards
pyrcli scan
pyrcli scan -i

# Inspect a board
pyrcli board-info COM3

# Flash and run
pyrcli flash COM3 main.py /main.py
pyrcli run COM3 "import machine; print(machine.freq())"

# Work with files
pyrcli fs ls COM3 /
pyrcli fs put COM3 local.py /remote.py
pyrcli fs get COM3 /remote.py local_copy.py

# Open an interactive REPL
pyrcli repl COM3
```

Mount the device filesystem in your desktop file manager:

```bash
pyrcli mount COM3
```

Reverse-mount the current host directory so code on the device can import and open files from `/remote`:

```bash
pyrcli remount COM3 .
```

Install a MicroPython package through the host-side `mpremote mip` path, or inspect the plan first:

```bash
pyrcli pkg install COM3 aioble --target /lib --dry-run
pyrcli pkg install COM3 aioble --target /lib
```

Monitor GPIO inputs:

```bash
pyrcli monitor COM3 --pins 0,2,4,5 --interval 0.2 --count 20
pyrcli monitor COM3 --pins 0,2 --format json --count 5
```

Use WebREPL instead of serial by adding `--ws`. The positional `PORT` stays in place for a consistent CLI shape; the WebSocket URL is the actual transport target.

```bash
pyrcli board-info COM3 --ws ws://192.168.4.1:8266 --password mypass
pyrcli flash COM3 main.py /main.py --ws ws://esp32.local:8266
pyrcli mount COM3 --ws ws://esp32.local:8266 --password mypass
```

If `--password` is omitted, WebREPL password resolution is: CLI option, `PYRITE_WEBREPL_PASSWORD`, then interactive prompt.

## Core Capabilities

### Fast Flashing

Pyrite CLI connects your device, transfers code in chunks, verifies the result, and restores the device session. Python files can be compiled to `.mpy` automatically before upload(Expect the main.py and boot.py). Pyrite CLI will check the update the code with smaller blocks that flashing can be much faster.

### Incremental Project Sync

`pyrcli project hash` records local SHA256 hashes. `pyrcli project flash` compares that state and uploads only added or changed files.

### Conditional Builds

Use `@feature("wifi")`, `@target("esp32")`, `with feature(...)`, and `with target(...)` to keep one source tree for several boards or firmware variants. Pyrite CLI rewrites the syntax with `libcst`, not regular expressions.

### Manifest-Based Flashing

`manifest.py` can select modules and packages, remap remote paths, and filter files by feature tags. It is parsed with `ast` and does not execute arbitrary code.

### Micropython Filesystem Bridge

`pyrcli mount` starts a local WebDAV server and maps desktop file-manager actions to MicroPython file operations. Windows can map a drive letter; Linux and macOS open the WebDAV location in the default file manager.

### Reverse Host Mount

`pyrcli remount` delegates to `mpremote mount` so the device sees a host directory at `/remote`. Pyrite CLI does not reimplement the mount protocol; it validates local arguments, locates `mpremote`, and hands the interactive session to it.

### Host-Side Package Install

`pyrcli pkg install` delegates to `mpremote mip install`, keeping package resolution and downloads on the host side. `--dry-run` prints an auditable plan without connecting to the device. `pyrcli pkg cache` currently plans cache locations and audits local `package.json` metadata; it does not perform network downloads itself.

### GPIO Monitor

`pyrcli monitor` samples GPIO pins as `machine.Pin(pin, machine.Pin.IN)` only. You can pass `--pins`, `--count`, `--duration`, `--edge changed`, and `--format json` for scriptable runs.

### Shared Transport Layer

Serial and WebREPL share the same high-level MicroPython operations. Most device commands accept `--ws` and `--password`.

## Common Workflows

Create a project and prepare editor support:

```bash
pyrcli project new my-project
pyrcli project new my-project --platform COM3
```

Flash a directory:

```bash
pyrcli flash-program COM3 src/ /app
pyrcli flash-program COM3 src/ /app --manifest manifest.py
```

Sync a project incrementally:

```bash
pyrcli project hash .
pyrcli project status COM3 . /app
pyrcli project flash COM3 . /app
pyrcli project pull COM3 . /app
```

Browse and mount files:

```bash
pyrcli fs ls COM3 ./
pyrcli fs cat COM3 /main.py
pyrcli mount COM3
pyrcli remount COM3 .
```

Install packages and watch GPIO:

```bash
pyrcli pkg install COM3 aioble --target /lib --dry-run
pyrcli pkg install-offline COM3 .pyrite/pkg-cache/aioble
pyrcli monitor COM3 --pins 0,2,4,5 --count 20
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

| Topic | Document |
|-------|----------|
| First steps, commands, config | [快速上手](docs/快速上手.md) |
| Flashing protocol and project sync | [设备刷入与项目同步](docs/设备刷入与项目同步.md) |
| Conditional compilation guide | [Conditional Compilation: Practical Guide](docs/conditional-compilation-guide.md) |
| Conditional compilation, Chinese | [条件编译实战](docs/条件编译实战.md) |
| WebDAV desktop mount | [WebDAV 挂载](docs/WebDAV挂载.md) |
| v0.0.3 implementation notes | [v0.0.3 计划](docs/v0.0.3-plan.md) |
| Architecture notes | [Architecture](docs/architecture.md) |

## Command Reference

### Top-Level Commands

| Command | Purpose |
|---------|---------|
| `scan` | Scan serial devices, with filters and JSON output |
| `flash` | Flash one local file to the device |
| `flash-program` | Recursively flash a local directory |
| `run` | Execute Python code on the device |
| `repl` | Open an interactive REPL |
| `reset` | Soft-reset the device through raw REPL |
| `board-info` | Print firmware, CPU, memory, flash, and filesystem info |
| `monitor` | Monitor GPIO input state |
| `mount` | Mount the device filesystem through local WebDAV |
| `remount` | Reverse-mount a host directory to device `/remote` through `mpremote` |
| `pkg` | Install MicroPython packages via `mpremote mip` |
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

### `pyrcli pkg`

| Command | Purpose |
|---------|---------|
| `pkg install` | Run or dry-run `mpremote mip install` for a package name or URL |
| `pkg cache` | Plan local cache paths and audit local package metadata |
| `pkg install-offline` | Install a local `package.json` or package directory through `mpremote mip` |

## v0.0.3 Status

`pyrcli` v0.0.3 removes firmware flashing, adds `remount`, `pkg`, and `monitor`, and splits flash/transport internals into smaller packages. `pkg cache` is an audit/planning surface for now; package installation itself is delegated to `mpremote mip install`.

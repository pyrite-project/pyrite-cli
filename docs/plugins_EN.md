# Plugin Development Guide

pyrite-cli supports third-party plugins that register custom commands. This guide covers writing, installing, and distributing plugins both remotely (via pip) and locally.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Plugin Structure](#plugin-structure)
- [Entry Point Convention](#entry-point-convention)
- [Installation Methods](#installation-methods)
  - [Method 1: pip-installed plugins](#method-1-pip-installed-plugins)
  - [Method 2: Local plugins](#method-2-local-plugins)
  - [Method 3: Development with editable install](#method-3-development-with-editable-install)
- [Plugin Metadata](#plugin-metadata)
- [API Access](#api-access)
- [Publishing](#publishing)
- [Full Example](#full-example)
- [Troubleshooting](#troubleshooting)

---

## Architecture Overview

```
third-party plugin package              pyrite-cli host
─────────────────────                   ──────────────
                                          cli/main.py
  pyproject.toml                              │
  └── [project.entry-points.                  │
        "pyrite.commands"]                     │
        ota = "pyrite_ota:app"                ▼
                                   cli/plugin_manager.py
  pyrite_ota/                       ┌──────────────────────┐
  ├── __init__.py  ──►app──►        │ discover_plugins()   │
  └── commands.py                   │   ↓                  │
                                    │ load_plugin(ep)      │
                                    │   ↓                  │
                                    │ main_app.add_typer() │
                                    └──────────────────────┘
```

The plugin system uses **setuptools entry points** (standard Python packaging mechanism) for discovery. No separate plugin registry or configuration file is needed for pip-installed plugins.

---

## Plugin Structure

Every plugin is a standard Python package with a `typer.Typer` instance:

```
my_plugin/
├── __init__.py          # Exports `app: typer.Typer`
├── commands.py          # Command implementations
└── pyproject.toml       # Package metadata + entry point
```

### Minimal `__init__.py`

```python
import typer

app = typer.Typer(help="My plugin commands")

__plugin_name__ = "my-plugin"
__plugin_version__ = "0.1.0"
```

### Minimal `pyproject.toml`

```toml
[project]
name = "pyrite-my-plugin"
version = "0.1.0"
dependencies = ["pyrite-cli>=0.1.0"]

[project.entry-points."pyrite.commands"]
my-plugin = "my_plugin:app"
```

### Command file (`commands.py`)

```python
from . import app

@app.command()
def hello(
    name: str = typer.Argument("world", help="Name to greet"),
):
    """Greet someone"""
    print(f"Hello, {name}!")
```

---

## Entry Point Convention

The entry point **value** must be in the form `module_path:attribute`, pointing to a `typer.Typer` instance.

```toml
[project.entry-points."pyrite.commands"]
ota = "pyrite_ota:app"
mqtt = "pyrite_mqtt:app"
ble = "pyrite_ble:app"
```

The **key** (`ota`, `mqtt`, `ble`) becomes the subcommand name users type:

```bash
pyrcli ota push COM3 firmware.bin
pyrcli mqtt pub COM3 topic msg
```

> **Naming rules**: Use lowercase letters, digits, and hyphens only. Hyphens are allowed in `pyproject.toml` but will be normalized by the shell.

---

## Installation Methods

### Method 1: pip-installed plugins

The standard way. Plugins are published to PyPI and installed like any Python package.

```bash
pip install pyrite-ota
pip install pyrite-mqtt
```

After installation, the plugin is automatically available — no configuration needed:

```bash
pyrcli ota push COM3 firmware.bin
pyrcli plugin list
# → ota  0.1.0  OTA wireless firmware update
```

**How it works**: `importlib.metadata.entry_points(group="pyrite.commands")` scans all installed packages at startup and loads matching entry points.

### Method 2: Local plugins

For development or private plugins not published to PyPI. pyrite-cli automatically scans two fixed directories for local plugins:

| Scope | Directory | Description |
|---|---|---|
| **Global** | `<pyrite-root>/plugin/` | Shared across all projects, alongside pyrite-cli itself |
| **Local** | `<CWD>/plugin/` | Project-specific, in your project's working directory |

#### Directory structure

Each subdirectory inside `plugin/` is a plugin package:

```
plugin/                              ← global or local plugin directory
├── ota-tool/
│   ├── __init__.py                  # must expose `app: typer.Typer`
│   ├── commands.py
│   └── requirements.txt             # optional, install deps manually
└── ble-scan/
    └── __init__.py
```

The subdirectory name is used as the plugin name unless `__plugin_name__` is set.

#### Example layout

```
# Global plugins:  <pyrite-root>/plugin/
pyrite-cli/
├── cli/
├── plugin/
│   └── company-tools/
│       └── __init__.py

# Local plugins:  <CWD>/plugin/
my-project/
├── main.py
├── plugin/
│   └── project-helper/
│       └── __init__.py
└── .pyrite_config.json
```

#### Verification

```bash
pyrcli plugin list
# → company-tools   0.1.0  Company internal tools
# → project-helper  0.1.0  Project-specific helpers
```

> **Tip**: Use global `plugin/` for tools you use across many projects. Use local `plugin/` for project-specific automation that you want to commit to version control.

### Method 3: Development with editable install

During active development of a pip-packaged plugin, use `pip install -e` so the plugin is registered via entry points and changes take effect immediately:

```bash
cd pyrite-ota/
pip install -e .
```

This is the recommended workflow for active development — it combines automatic discovery (via entry points) with hot-reload (editable install). Changes to Python files are picked up immediately; only `pyproject.toml` changes require re-running `pip install -e`.

---

## Plugin Metadata

Define these attributes at module level in your `__init__.py`:

| Attribute | Type | Default | Description |
|---|---|---|---|
| `__plugin_name__` | `str` | entry point key | Display name in `pyrcli plugin list` |
| `__plugin_version__` | `str` | `"0.0.0"` | Version shown in `pyrcli plugin info` |

```python
import typer

app = typer.Typer(help="OTA wireless firmware update")

__plugin_name__ = "ota"
__plugin_version__ = "1.2.0"
```

---

## API Access

Plugins can import and use any pyrite-cli module:

```python
from cli.utils.flash import MicroPython
from cli.utils.webrepl_micropython import WebREPLMicroPython
from cli.utils.transport import Transport
from cli.utils.config import _load_config
from cli.project.sync import ProjectSyncManager
```

> **Important**: pyrite-cli's internal API is not yet stable. Pin your plugin to a specific pyrite-cli version in `dependencies`:

```toml
[project]
dependencies = ["pyrite-cli>=0.1.0,<0.5.0"]
```

---

## Publishing

To make your plugin available to others via pip:

1. **Choose a name**: Prefix with `pyrite-` for discoverability (e.g., `pyrite-ota`, `pyrite-mqtt`).

2. **Build**:
   ```bash
   pip install build
   python -m build
   ```

3. **Upload to PyPI**:
   ```bash
   pip install twine
   twine upload dist/*
   ```

4. **Users install**:
   ```bash
   pip install pyrite-ota
   pyrcli plugin list
   ```

---

## Full Example

A complete plugin package that adds `pyrcli greet hello` and `pyrcli greet goodbye` commands:

```
pyrite-greet/
├── __init__.py
├── commands.py
├── pyproject.toml
└── README.md
```

### `__init__.py`

```python
"""pyrite-greet: Greeting commands for pyrite-cli."""

import typer

app = typer.Typer(help="Greeting commands")

__plugin_name__ = "greet"
__plugin_version__ = "0.1.0"
```

### `commands.py`

```python
import typer
from . import app


@app.command()
def hello(
    name: str = typer.Argument("world", help="Who to greet"),
    count: int = typer.Option(1, "--count", "-c", help="Number of times"),
):
    """Say hello"""
    for i in range(count):
        print(f"Hello, {name}! ({i + 1}/{count})")


@app.command()
def goodbye(name: str = typer.Argument(..., help="Who to say goodbye to")):
    """Say goodbye"""
    print(f"Goodbye, {name}!")
```

### `pyproject.toml`

```toml
[project]
name = "pyrite-greet"
version = "0.1.0"
description = "Greeting commands for pyrite-cli"
requires-python = ">=3.8"
dependencies = ["pyrite-cli>=0.1.0"]

[project.entry-points."pyrite.commands"]
greet = "pyrite_greet:app"
```

### Usage

```bash
# pip installed
pip install pyrite-greet

# or local development — place in plugin/ directory
# mkdir -p plugin/greet && cp -r * plugin/greet/

# use it
pyrcli greet hello Claude
# → Hello, Claude! (1/1)

pyrcli greet goodbye Claude
# → Goodbye, Claude!

pyrcli plugin list
# → greet  0.1.0  Greeting commands
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Plugin not shown in `pyrcli plugin list` | Entry point not registered | Check `pyproject.toml` `[project.entry-points."pyrite.commands"]` section |
| `ImportError` when running plugin commands | Missing dependency | Install dependencies for pip plugins; for local plugins, install manually via `pip install -r requirements.txt` |
| Local plugin not found | Not in the right directory | Place the plugin inside `plugin/` (global: alongside pyrite-cli; local: in CWD). Verify `plugin/<name>/__init__.py` exists |
| Plugin command crashes | Internal error in plugin | Check the error message; pyrite-cli wraps each plugin in try/except so other plugins still work |
| Editable install changes not reflected | Need to reinstall | Run `pip install -e .` again after changing `pyproject.toml`; code changes are picked up automatically |

---

> **Note**: The plugin system supports three installation methods: pip-installed packages (via entry points), global local plugins (`<pyrite-root>/plugin/`), and project local plugins (`<CWD>/plugin/`). For production use, prefer pip-installed plugins. For development or private tools, local plugins are more convenient.

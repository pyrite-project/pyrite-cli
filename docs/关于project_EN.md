# `cli/project/` — MicroPython Project Initialization & Type Stubs

Provides MicroPython project scaffolding creation and type stub (`.pyi`) download functionality, supporting both interactive selection and serial port auto-detection.

---

## Module Structure

```
cli/
├── project/
│   ├── __init__.py        # Empty (no exports)
│   ├── project.py         # High-level API: init_project / init_stubs / new_project_interactive
│   ├── stubs.py           # Stub query, matching, download (multi-threaded), VS Code config
│   ├── sync.py            # ProjectSyncManager — hash-based incremental sync,
│   │                          status comparison, batch file pull from device
│   └── feature_stub.pyi   # .pyi stubs for preprocessor feature/target
└── utils/
    ├── selector.py        # Interactive selection list (arrow key navigation)
    ├── preprocessor.py    # Conditional compilation macro preprocessor (libcst AST transforms)
    └── manifest_loader.py # manifest.py loader
```

### `__init__.py`

Empty file with no exports. Public symbols of `cli.project` are imported via `project.py` (`from .stubs import *`).

---

## `project.py` — High-Level API

### `init_project(proj_name: str)`

Creates a new MicroPython project directory.

| Parameter | Description |
|-----------|-------------|
| `proj_name` | Directory name; a folder with this name is created in the current working directory |

Automatically performs after creation:
1. `os.mkdir(proj_name)` creates the directory
2. Copies `feature_stub.pyi` → `{proj_name}/feature_stub.pyi` (preprocessor type hints)
3. Writes `{proj_name}/manifest.py` template

### `detect_device_info(port, baudrate=115200, timeout=10) -> tuple[str, str]`

Connects to a device via serial port and auto-detects hardware type and firmware version.

| Parameter | Description |
|-----------|-------------|
| `port` | Serial port name, e.g. `COM3` or `/dev/ttyUSB0` |

- Connects to device and executes `import sys;print(sys.version);print(sys.platform)`
- Returns `(hardware, version)` tuple, e.g. `('esp32', '1.22.2')`
- Raises: `RuntimeError` — connection failed or output parsing failed

### `new_project_interactive(proj_name: str, platform: str | None = None)`

Interactively creates a new MicroPython project and downloads stubs. The recommended entry point (called by the `pyrcli new` command).

**Auto-detect mode** (when `--platform` is specified):
1. Calls `init_project()` to create the directory
2. Reads hardware and version via `detect_device_info()`
3. Queries available stubs, auto-matches and downloads
4. If no exact match, tries the nearest version
5. Configures VS Code

**Interactive selection mode** (when `--platform` is not specified):
1. Calls `init_project()` to create the directory
2. Fetches available hardware list from GitHub API → user selects via keyboard
3. Filters versions based on selected hardware → user selects
4. If variants exist (e.g. `ESP32_GENERIC`) → user selects
5. Downloads stubs + VS Code configuration

### `init_stubs(hardware=None, version=None, variant=None, platform=None)`

Initializes MicroPython type stubs in an existing project.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `hardware` | No | Hardware type, e.g. `esp32`, `rp2` (can be omitted when using `--platform`) |
| `version` | No | Firmware version, e.g. `1.20.0` (can be omitted when using `--platform`) |
| `variant` | No | Specific hardware variant, e.g. `ESP32_GENERIC`, `PICO_W` |
| `platform` | No | Serial port name; auto-detects hardware and downloads matching stubs |

Full procedure:
1. If `platform` is specified, calls `detect_device_info()` for auto-detection
2. Calls `list_stub_dirs()` to fetch all available stub directories from the GitHub API
3. Calls `find_stub_dir()` to find the best matching stub directory
4. On failure, tries `_find_nearest_version()` to match the closest version
5. Calls `download_stubs()` to download all `.pyi` files in that directory (multi-threaded)
6. Calls `create_vscode_config()` to configure VS Code Pylance type checking

### Internal Helper Functions

#### `_get_versions_for_hardware(dirs, hardware) -> list[str]`

Extracts available firmware versions for a given hardware type, sorted in descending version order.

#### `_get_variants_for_hw_version(dirs, hardware, version) -> list[str]`

Extracts available firmware variants for a specific hardware + version combination.

#### `_find_nearest_version(target, available) -> str | None`

Finds the closest version to the target in the available list (same major version only, minimum absolute difference).

---

## `stubs.py` — Stub Query, Matching, Download

### Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `SOURCE` | `"https://api.github.com/repos/josverl/micropython-stubs"` | Upstream stub repository API URL |
| `VSCODE_DIR` | `".vscode"` | VS Code config directory name |
| `VSCODE_SETTINGS` | `"settings.json"` | VS Code config filename |

### Utility Functions

#### `version_to_dir(v: str) -> str`

Converts a version string to the directory name format used in the GitHub repository.

| Input | Output |
|-------|--------|
| `"1.20.0"` | `"v1_20_0"` |
| `"1.19.1"` | `"v1_19_1"` |

#### `_get_download_threads() -> int`

Reads the concurrent download thread count from `.pyrite_config.json`, range 1–12, default 4.

#### `_request_with_retry(url, max_retries=3, **kwargs)`

HTTP GET request with retry logic, encapsulating a unified error handling strategy.

**Retry strategy:**

| Scenario | Behavior |
|----------|----------|
| Connection error / timeout | Exponential backoff retry (1s, 2s, 4s), up to 3 attempts |
| HTTP 5xx (server error) | Exponential backoff retry, up to 3 attempts |
| HTTP 403 (API rate limit) | Exit immediately, no retry |
| Other HTTP errors | Raise exception directly, no retry |

### Stub Listing & Query

#### `list_stub_dirs() -> list[str]`

Lists all available stub directory names from the `stubs/` directory of the `josverl/micropython-stubs` repository.

- Internally calls `_request_with_retry()` for retry-capable API requests
- Returns a list of directory names, e.g. `["micropython-v1_20_0-esp32", "micropython-v1_20_0-esp32-ESP32_GENERIC", ...]`
- **Note**: GitHub API has rate limits (60 req/h unauthenticated); frequent calls may trigger 403

#### `get_hardware_types(dirs: list[str]) -> set[str]`

Extracts all available hardware types from the list of stub directory names.

- Parse rule: for directory names starting with `micropython-v`, the third `-`-delimited segment is the hardware type
- e.g. `micropython-v1_20_0-esp32` → `esp32`, `micropython-v1_20_0-rp2` → `rp2`

#### `list_all_hardware(dirs: list[str]) -> None`

Prints all available MicroPython hardware types.

#### `list_available(dirs: list[str], hardware: str) -> None`

Prints all available stubs for a given hardware type.

- Match rule: directory name contains `-{hardware}` or ends with `{hardware}`
- If no matches found, displays all available hardware types for reference

### Stub Directory Matching

#### `find_stub_dir(dirs, hardware, version, variant=None) -> str | None`

Finds the stub directory that best matches the user-specified criteria.

**Match priority (highest to lowest):**

1. **Exact match**: `micropython-{vdir}-{hardware}` (no variant) or `micropython-{vdir}-{hardware}-{variant}` (with variant)
2. **Merged variant**: `{exact}-merged`
3. **Fuzzy match**: any directory starting with the exact prefix, lexicographically first

**Examples:**

| hardware | version | variant | Matched Directory |
|----------|---------|---------|-------------------|
| `esp32` | `1.20.0` | — | `micropython-v1_20_0-esp32` |
| `esp32` | `1.20.0` | `ESP32_GENERIC` | `micropython-v1_20_0-esp32-ESP32_GENERIC` |
| `rp2` | `1.19.1` | — | `micropython-v1_19_1-rp2` |

### Stub Download

#### `download_stubs(stub_dir, output_dir, max_workers=None) -> tuple[int, Path]`

Downloads all `.pyi` files from a specified stub directory (multi-threaded).

| Parameter | Description |
|-----------|-------------|
| `stub_dir` | Stub directory name in the upstream repository |
| `output_dir` | Local output directory (stubs are saved in a subdirectory) |
| `max_workers` | Download thread count; `None` reads from `.pyrite_config.json` |

**Procedure:**
1. Calls `_request_with_retry()` to get the directory listing (GitHub API)
2. Creates the local directory at `output_dir/stub_dir`
3. Filters for all `.pyi` files
4. Downloads concurrently using `ThreadPoolExecutor`
5. Supports `tqdm` progress bar (optional dependency)

Returns `(downloaded_count, output_path)`.

### VS Code Configuration

#### `create_vscode_config(stub_path, hardware, version) -> Path`

Creates or updates `.vscode/settings.json` in the project root to configure Pylance type checking with the downloaded stubs.

**Configuration items:**

```json
{
  "python.analysis.extraPaths": ["./micropython-v1_20_0-esp32"],
  "python.languageServer": "Pylance",
  "python.analysis.typeCheckingMode": "basic",
  "python.analysis.stubPath": "."
}
```

- If the file already exists, existing config is read and new entries are appended (existing settings are not overwritten)
- Automatically creates the `.vscode/` directory if it doesn't exist

### Standalone Entry Point

`stubs.py` also supports running as a standalone script (via `argparse`), used before CLI integration. Its functionality is essentially the same as `init_stubs`, but after integration with `pyrcli init`, the CLI entry point is recommended.

---

## `utils/selector.py` — Interactive Selection List

A keyboard-navigated terminal selector, used by `new_project_interactive()` for hardware/version selection.

### `interactive_select(options: list[str], title: str) -> str`

Displays a scrollable interactive selection list.

- Arrow key navigation, Enter to confirm, Ctrl+C to exit
- Fullwidth/halfwidth character alignment (CJK-width aware)
- Window-style border display
- Auto-selects when there is only one option

### `_get_key() -> str | None`

Cross-platform single key read, returns a normalized key name (`"up"`, `"down"`, `"enter"`, `None`).

### `_display_width(s: str) -> int`

Returns the display column width of a string in the terminal. CJK fullwidth characters occupy 2 columns.

---

## `utils/preprocessor.py` — Conditional Compilation Macro Preprocessor

A libcst-based AST transformation tool supporting conditional compilation with `feature()` / `target()` macros. Called by `flash_file()` before flashing.

### Supported Macro Syntax

```python
# Function decorator: keep the function when tags match
@feature("wifi")
def connect_wifi():
    import network
    ...

# with statement block: keep the block when tags match
with target("ESP32"):
    from machine import Pin
    esp32_specific()
```

### `preprocess(source, active_tags, filename="") -> str`

Performs conditional compilation transformation on source code.

| Parameter | Description |
|-----------|-------------|
| `source` | Source code string |
| `active_tags` | Set of currently active tags |
| `filename` | Filename (for warning output) |

Transformation rules:
- `@feature("x")` / `@target("x")` decorated functions: wrap in `if False:` when tags don't match
- `with feature("x"):` / `with target("x"):` blocks: convert to `if False:` when tags don't match
- Also outputs warnings about bare calls to non-matching functions that could cause `NameError` at runtime

### Internal Classes

- `_Transformer` — libcst CSTTransformer, performs AST transforms (`with` → `if`, decorator → `if False`)
- `_Analyzer` — libcst CSTVisitor, statically analyzes call relationships to detect potential runtime errors

---

## `utils/manifest_loader.py` — manifest.py Loader

Parses the manifest.py file and filters files to flash based on active tags.

### `load_manifest(manifest_path, active_tags, base_dir=None) -> list[tuple[str, str]]`

| Parameter | Description |
|-----------|-------------|
| `manifest_path` | Path to the manifest.py file |
| `active_tags` | Set of active tags |
| `base_dir` | Base directory for file paths (defaults to manifest.py's directory) |

manifest.py supports two DSL directives:

```python
# module: single file, optional features filtering
module("main.py")
module("lib/utils.py", features=["wifi"])

# package: recursive directory
package("lib")
```

- Empty or absent `features` always matches
- Non-empty `features` matches if there is any intersection with `active_tags`
- `package` recursively adds all `.py` files in the directory

---

## Data Flow

```
pyrcli new <name> [--platform COM3]
  └── new_project_interactive(name, platform)
        ├── init_project(name)
        │     ├── os.mkdir(name)
        │     ├── copy feature_stub.pyi
        │     └── create manifest.py
        │
        ├── [--platform mode]
        │     ├── detect_device_info(port) → (hardware, version)
        │     ├── list_stub_dirs()
        │     ├── find_stub_dir(dirs, hardware, version)
        │     ├── download_stubs(stub_dir, '')
        │     └── create_vscode_config(...)
        │
        └── [interactive mode]
              ├── list_stub_dirs()
              ├── interactive_select(hardware)
              ├── interactive_select(version)
              ├── [optional] interactive_select(variant)
              ├── download_stubs(stub_dir, '')
              └── create_vscode_config(...)

pyrcli init <hardware> <version> [--variant <V>] [--platform COM3]
  └── init_stubs(hardware, version, variant, platform)
        ├── [--platform] detect_device_info(port) → auto-detect
        ├── list_stub_dirs()
        ├── find_stub_dir(...) → best match
        ├── [_find_nearest_version()] → fallback match
        ├── download_stubs(best match, "")  (multi-threaded ThreadPoolExecutor)
        └── create_vscode_config(stub_path, ...)

Conditional compilation during file flashing:
  flash_file(local_path, remote_path, active_tags=...)
    ├── preprocessor.py:   preprocess @feature/@target macros
    ├── _compile_to_mpy:   compile .py → .mpy
    └── FLASH template:    device-side write
```

## Error Handling

| Scenario | Behavior |
|----------|----------|
| GitHub API rate limit (403) | Prompt user to retry later, process exits |
| Network failure / timeout | Auto-retry up to 3 times (exponential backoff), raises exception after 3rd failure |
| No matching stubs found | Try the nearest version; if still failing, print expected pattern + list available stubs |
| Device connection failure | Print error message; project directory already created, can be configured manually later |
| `requests` library not installed | Prompt `pip install requests`, process exits |
| `tqdm` not installed | Degrades to simple file list output (no progress bar; multi-threaded download unaffected) |
| VS Code config JSON corrupted | Overwrite with warning |
| `libcst` not installed | Conditional compilation feature unavailable |

## Dependencies

- **`requests`** (required) — GitHub REST API calls and file downloads
- **`tqdm`** (optional) — Download progress bar; degrades to per-file output when missing
- **`libcst`** (required) — AST parsing foundation for the conditional compilation macro preprocessor

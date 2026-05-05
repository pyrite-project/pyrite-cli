# Conditional Compilation & Macro Preprocessing

pyrite-cli performs static preprocessing on `.py` files before flashing. `with feature/target(...)` blocks and `@feature/target(...)` decorators are expanded into `if True/False:`. mpy-cross's dead code elimination then removes non-matching code at compile time, reducing bytecode size.

---

## Concepts

### target vs feature

| Concept | Semantics | Source |
|---------|-----------|--------|
| `target` | Hardware platform, mutually exclusive | Auto-detected from device or manually specified via `--target` |
| `feature` | Functional feature, composable | Implicitly activated by target + appended via `--feature` |

Both are syntactically identical and can be used as `with` blocks or function decorators. The difference is only a semantic convention.

### active_tags Composition

```
active_tags = resolve_target(auto-detect | --target)
            + features implicitly activated by target (from board_tags config)
            + --feature appended
            - --no-feature removed
```

---

## Syntax

### `with` Block

```python
with target("ESP32"):
    import network          # Only compiled for ESP32

with target("RP2040"):
    import rp2              # Only compiled for RP2040

with feature("wifi"):
    connect_wifi()          # Only compiled when wifi tag is active
```

Expanded after preprocessing:

```python
if True:                    # tag matches
    import network

if False:                   # tag doesn't match, eliminated by mpy-cross
    import rp2
```

Line numbers are fully preserved — no lines are inserted or deleted.

### Function Decorator

```python
@target("ESP32")
def connect_wifi():
    ...

@target("RP2040")
def connect_wifi():
    ...
```

Matching implementations are kept (decorator removed); non-matching implementations are wrapped in `if False:`:

```python
def connect_wifi():         # ESP32 matches, decorator removed
    ...

if False:                   # RP2040 doesn't match
    def connect_wifi():
        ...
```

Multiple decorators can be stacked (logical AND):

```python
@target("ESP32")
@feature("ble")
def ble_init():
    ...
```

---

## IDE Type Stubs

`pyrcli new` automatically generates `feature_stub.pyi` in the project, preventing the IDE from reporting `feature`/`target` as undefined:

```python
class feature:
    def __init__(self, name: str) -> None: ...
    def __enter__(self) -> "feature": ...
    def __exit__(self, *a: object) -> None: ...
    def __call__(self, fn: object) -> object: ...

target = feature
```

`.pyi` files are not collected by `flash-program` (only `.py` files are), so no manual exclusion is needed.

---

## Static Warning Analysis

During preprocessing, a two-pass analysis is performed on each file, outputting yellow warnings to stderr:

**Warning 1: All implementations of a function don't match**

```
[warning] main.py: function 'connect_wifi' all implementations don't match current tags
```

Triggered when every `@target/@feature` implementation of a function name is absent from active_tags.

**Warning 2: Bare call not protected by a guard**

```
[warning] main.py: bare call 'connect_wifi()' not protected by a guard, will raise NameError at runtime
```

Triggered when a call site is not inside any `with target/feature(...)` block and the called function is fully disabled.

> Cross-file call analysis is not performed; this is a known limitation.

---

## board_tags Configuration

The tags implicitly activated by a target in `active_tags` are determined by the `board_tags` mapping table.

**Built-in defaults**:

| Keyword | Activated Tags |
|---------|----------------|
| `ESP32` | `ESP32`, `wifi` |
| `ESP8266` | `ESP8266` |
| `RP2040` | `RP2040` |
| `PICO` | `RP2040` |
| `STM32` | `STM32` |

**Extending or overriding in `pyproject.toml`**:

```toml
[tool.pyrite.board_tags]
MY_BOARD = ["MY_BOARD", "wifi", "ble"]
ESP32 = ["ESP32", "wifi", "ble"]   # override default
```

The tool searches upward from the current directory for `pyproject.toml` and stops at the first one found.

---

## CLI Parameters

### `flash` and `flash-program` Parameters

| Parameter | Description |
|-----------|-------------|
| `--target TAG` | Manually specify board target (for offline use, skips auto-detection) |
| `--feature TAG[,TAG]` | Append active tags, comma-separated |
| `--no-feature TAG[,TAG]` | Forcefully disable tags, comma-separated |
| `--manifest PATH` | Specify manifest.py path (`flash-program` only) |

**Examples**:

```bash
# Auto-detect device target
pyrcli flash /dev/ttyUSB0 main.py

# Specify target offline
pyrcli flash /dev/ttyUSB0 main.py --target ESP32

# Append feature
pyrcli flash /dev/ttyUSB0 main.py --target ESP32 --feature ble

# Disable a feature
pyrcli flash /dev/ttyUSB0 main.py --target ESP32 --no-feature wifi

# Flash directory with manifest
pyrcli flash-program /dev/ttyUSB0 ./src --manifest ./manifest.py
```

**`--target` is required when no device is connected**, otherwise the tool exits with an error:

```
Cannot identify device target, please specify manually with --target
```

---

## manifest.py

`manifest.py` controls which files `flash-program` flashes, with optional feature-based filtering. `pyrcli new` auto-generates a commented template when creating a project.

```python
# manifest.py
module("main.py")
module("lib/utils.py", features=["wifi"])   # Only flashed when wifi tag is active
package("lib/drivers")                      # Recursively collect all .py files in directory
package("lib/net", features=["wifi"])
```

### `module(filename, remote=None, features=None)`

| Parameter | Description |
|-----------|-------------|
| `filename` | File path relative to the project directory |
| `remote` | Target path on the device; defaults to `filename` |
| `features` | Tag list; included if any tag is in active_tags; `None` means unconditional inclusion |

### `package(dirname, remote=None, features=None)`

Recursively collects all `.py` files in the directory. Parameters are the same as `module`.

---

## `preprocessor.py` — Internal API

```python
from cli.utils.preprocessor import preprocess

result = preprocess(source: str, active_tags: set[str], filename: str = "") -> str
```

- `source` — Raw Python source code
- `active_tags` — Currently active tag set
- `filename` — Filename for warning messages (optional)
- Returns the expanded source string

Preprocessing is called automatically inside `flash_file` and typically doesn't need to be invoked directly.

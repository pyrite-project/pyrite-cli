# Conditional Compilation: Practical Guide

This document focuses on real-world usage patterns, design practices, and development workflows with `feature` / `target` in MicroPython projects. For syntax reference, see [条件编译与宏预处理_EN.md](条件编译与宏预处理_EN.md).

---

## Table of Contents

1. [Core Design Philosophy](#core-design-philosophy)
2. [Scenario 1: Single Source for Multiple Boards](#scenario-1-single-source-for-multiple-boards)
3. [Scenario 2: Feature-Based Firmware Trimming](#scenario-2-feature-based-firmware-trimming)
4. [Scenario 3: Development vs Production Builds](#scenario-3-development-vs-production-builds)
5. [Common Design Patterns](#common-design-patterns)
6. [manifest.py Combined Filtering](#manifestpy-combined-filtering)
7. [Debugging & Troubleshooting](#debugging--troubleshooting)
8. [Common Pitfalls](#common-pitfalls)

---

## Core Design Philosophy

MicroPython devices have extremely limited resources (Flash is typically 1–16 MB, RAM can be as low as 128 KB). Unlike CPython, they can't dynamically load modules at runtime. Conditional compilation lets you decide **before flashing** which code reaches the device, eliminating runtime conditionals and dead code.

```
┌──────────────┐    Preprocess (libcst)    ┌──────────────────┐    Compile (mpy-cross)   ┌─────────────┐
│  Source w/   │ ────────────────────────→ │  Expanded to     │ ──────────────────────→ │  Slimmed    │
│  macros      │                           │  if True/False   │    Dead code elimination │  bytecode   │
└──────────────┘                           └──────────────────┘                          └─────────────┘
      ↑                                                                                        ↓
  with target("ESP32"):                                                                Only matching code
  @feature("ble")                                                                      makes it through
```

`target` and `feature` are syntactically identical but differ in semantics:

| | target | feature |
|--|--------|---------|
| Semantics | Hardware platform (mutually exclusive) | Functional capability (composable) |
| Source | Auto-detected from device / `--target` flag | Implicit via board_tags / `--feature` flag |
| Examples | `ESP32`, `RP2040`, `STM32` | `wifi`, `ble`, `sdcard`, `debug` |

---

## Scenario 1: Single Source for Multiple Boards

### Problem

You maintain an IoT project that needs to support both ESP32-S3 and RP2040 boards. Pin assignments, peripheral drivers, and Flash layouts differ. The traditional approach — maintaining two copies of the code (`main_esp32.py` / `main_rp2040.py`) — means duplicated logic drifts apart over time.

### Solution

Use a single `main.py` with `target()` to distinguish platforms:

```python
# main.py
import time

# ── Pin definitions ──
with target("ESP32"):
    from machine import Pin
    LED_PIN = 2
    I2C_SCL = 22
    I2C_SDA = 21

with target("RP2040"):
    from machine import Pin
    LED_PIN = 25   # RP2040 onboard LED is on GPIO 25
    I2C_SCL = 5
    I2C_SDA = 4

# ── Init (shared logic) ──
led = Pin(LED_PIN, Pin.OUT)
i2c = machine.I2C(0, scl=Pin(I2C_SCL), sda=Pin(I2C_SDA))

# ── Platform-specific drivers ──
with target("ESP32"):
    import esp32
    def deep_sleep(sec):
        esp32.wake_on_ext0(Pin(14, Pin.IN), 1)
        machine.deepsleep(sec)

with target("RP2040"):
    def deep_sleep(sec):
        # RP2040 doesn't support esp32-style deepsleep
        machine.lightsleep(sec * 1000)

# ── Main loop ──
while True:
    led.toggle()
    time.sleep(1)
```

### Flashing

```bash
# When connected, the tool auto-detects the target
pyrcli flash COM3 main.py /main.py

# Offline mode — specify target manually
pyrcli flash COM3 main.py /main.py --target ESP32
```

### Using Decorators for Multi-Platform Functions

When different platforms need identically-named entry points, use `@target` decorators:

```python
@target("ESP32")
def init_sensors():
    import dht
    return dht.DHT22(Pin(15))

@target("RP2040")
def init_sensors():
    from machine import ADC
    return ADC(4)   # internal temperature sensor

sensor = init_sensors()   # only the matching implementation survives
```

The preprocessor keeps the matching implementation as-is (decorator removed) and wraps non-matching ones in `if False:` blocks.

---

## Scenario 2: Feature-Based Firmware Trimming

### Problem

Your project has multiple optional features: WiFi networking, BLE configuration, SD card logging, OLED display. Different customers need different combinations. Compiling everything in blows past the firmware size budget.

### Solution

Use `feature()` guards around each optional capability:

```python
# config.py
SSID = "my_iot"
PASSWORD = "secret"

# ── WiFi feature ──
with feature("wifi"):
    import network
    import ujson

    def upload_data(data):
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        wlan.connect(SSID, PASSWORD)
        # ... HTTP POST ...

# ── BLE feature ──
with feature("ble"):
    import bluetooth

    def ble_config():
        # ... BLE service ...
        pass

# ── SD card logging ──
with feature("sdcard"):
    import os

    def log_to_sdcard(msg):
        with open("/sd/log.txt", "a") as f:
            f.write(msg + "\n")

# ── Main logic ──
with feature("wifi"):
    upload_data({"temp": 25.5})

with feature("sdcard"):
    log_to_sdcard("boot complete")
```

### Choosing Combinations at Flash Time

```bash
# WiFi-only (ESP32 activates "wifi" by default)
pyrcli flash-program COM3 src/ --target ESP32

# WiFi + SD card
pyrcli flash-program COM3 src/ --target ESP32 --feature sdcard

# All features
pyrcli flash-program COM3 src/ --target ESP32 --feature sdcard,ble,oled

# Disable a feature
pyrcli flash-program COM3 src/ --target ESP32 --no-feature wifi
```

### Automatic Feature Activation via board_tags

Define board-to-feature associations in `pyproject.toml`:

```toml
[tool.pyrite.board_tags]
# target=ESP32  → "wifi" activated automatically
# target=ESP32_S3 → "wifi" and "ble" activated automatically
ESP32_S3 = ["ESP32_S3", "wifi", "ble"]
ESP32_C3 = ["ESP32_C3", "wifi", "ble"]
```

With this, `--target ESP32_S3` automatically activates `wifi` and `ble` — no need for `--feature`.

---

## Scenario 3: Development vs Production Builds

### Problem

During development you need debug logs, mock data, and interactive testing. The production build needs none of that, but should include watchdog timers and error reporting. Manually commenting things out is error-prone and easy to forget.

### Solution

Use `feature("debug")` and `feature("prod")` to distinguish:

```python
# utils.py
with feature("debug"):
    def log(msg):
        print(f"[DEBUG] {msg}")

    def simulate_sensor():
        return {"temp": 25.0, "hum": 60.0}

with feature("prod"):
    import machine
    WDT_TIMEOUT = 30000

    def init_watchdog():
        wdt = machine.WDT(timeout=WDT_TIMEOUT)
        return wdt
```

### Workflow

```bash
# Development: enable debug
pyrcli flash-program COM3 src/ --feature debug

# Release: enable production features, disable debug
pyrcli flash-program COM3 src/ --no-feature debug --feature prod
```

### Dependency Injection via Decorators

```python
# Development sensor (simulated)
@feature("debug")
def get_temperature():
    import random
    return round(20 + random.random() * 10, 1)

# Production sensor (real hardware)
@feature("prod")
def get_temperature():
    from machine import ADC
    adc = ADC(4)
    voltage = adc.read_u16() * 3.3 / 65535
    return round(27 - (voltage - 0.706) / 0.001721, 1)

# Call site doesn't care which build it is
temp = get_temperature()
```

---

## Common Design Patterns

### Pattern 1: Platform Abstraction Layer (PAL)

Consolidate platform-specific code into a single file:

```python
# pal.py — Platform Abstraction Layer
import machine

# ── Sleep mode ──
@target("ESP32")
def sleep_ms(ms):
    machine.sleep(ms)

@target("RP2040")
def sleep_ms(ms):
    # RP2040 has no machine.sleep()
    import time
    time.sleep_ms(ms)

# ── Unique device ID ──
@target("ESP32")
def device_id():
    import esp32
    return ''.join(f"{b:02x}" for b in esp32.mac())

@target("RP2040")
def device_id():
    from machine import unique_id
    return ''.join(f"{b:02x}" for b in unique_id())
```

Business code in `main.py` calls `pal.sleep_ms(100)` with zero knowledge of the underlying platform.

### Pattern 2: Module-Level Feature Toggles

Isolate entire features into separate files and use manifest.py to decide what gets flashed:

```python
# manifest.py
module("main.py")
module("pal.py")

# Flashed only when the corresponding feature is active
module("wifi_manager.py", features=["wifi"])
module("ble_service.py", features=["ble"])
module("sdcard_logger.py", features=["sdcard"])
package("drivers/", features=["sdcard"])
```

This provides file-level elimination — non-matching features never reach the device at all.

### Pattern 3: Layered Feature Design

Use stacked features for granular control:

```python
# ── Logging level features ──
with feature("log_minimal"):
    def log_error(msg): print(f"[E] {msg}")

with feature("log_verbose"):
    def log_error(msg):   print(f"[E] {msg}")
    def log_info(msg):    print(f"[I] {msg}")
    def log_debug(msg):   print(f"[D] {msg}")

# At flash time:
# --feature log_verbose   → verbose logging
# --feature log_minimal   → errors only
# neither                 → no logging (most space-efficient)
```

---

## manifest.py Combined Filtering

The `features` parameter in manifest.py works together with source-level `feature()` blocks, creating two layers of filtering:

| Layer | Mechanism | Effect |
|-------|-----------|--------|
| File level | manifest.py `features` kwarg | Non-matching files never reach the device |
| Code level | `with feature()` / `@feature()` in source | Within flashed files, non-matching blocks are eliminated |

```python
# manifest.py
module("sensor_drivers.py", features=["sensor"])

# sensor_drivers.py
with feature("sensor_temperature"):
    class TemperatureSensor:
        ...

with feature("sensor_humidity"):
    class HumiditySensor:
        ...
```

Flashing with `--feature sensor,sensor_temperature`:
- `sensor_drivers.py` **is** flashed (file-level: `sensor` matches)
- Only `TemperatureSensor` is kept in the bytecode (code-level filtering)
- `HumiditySensor` is eliminated

---

## Debugging & Troubleshooting

### Preview Preprocessing Results

Check what a file looks like after preprocessing without connecting a device:

```bash
python -c "
from cli.utils.preprocessor import preprocess
src = open('main.py').read()
result = preprocess(src, {'ESP32', 'wifi'}, 'main.py')
print(result)
"
```

### Verify active_tags

When a device is connected, `pyrite-cli` outputs the detected target:

```bash
pyrcli board-info COM3
```

The project's `.pyrite_hash.json` also records a snapshot of the active_tags used during each flash (used with `project flash`).

### Check Which Code Made It Through

After flashing, verify functions exist on the device:

```bash
pyrcli run COM3 "import sensor; print(dir(sensor))"
```

If a function is undefined, check:
1. Is the function wrapped in a `feature/target` guard?
2. Does the current active_tags include the required tag?
3. Did you see a static warning about "all implementations don't match"?

---

## Common Pitfalls

### Pitfall 1: Bare Calls to Decorated Functions

```python
@feature("wifi")
def connect_wifi():
    ...

# Connect to WiFi
connect_wifi()           # ← Warning: bare call
```

If `wifi` is not active, `connect_wifi` is eliminated, but the call site remains — causing `NameError` at runtime.

**Fix**: Wrap the call site in a guard too:

```python
with feature("wifi"):
    connect_wifi()
```

The preprocessor emits yellow warnings for these cases — keep an eye on them.

### Pitfall 2: return/break/continue Inside `with`

`with target(...)` becomes `if True/False:` after preprocessing. `return` / `break` / `continue` inside `if` blocks are valid MicroPython, but verify the semantics are what you expect — especially when the `with` block is at the top level of a function.

### Pitfall 3: Cross-File References Without Guards

```python
# utils.py
@feature("wifi")
def scan(): ...

# main.py
from utils import scan

# No guard here — if wifi is inactive, scan was eliminated in utils.py
from utils import scan   # ← ImportError or NameError
```

The preprocessor **does not** perform cross-file analysis. Wrap imports and call sites in `with feature("wifi")` in `main.py` as well.

### Pitfall 4: String Case Sensitivity

Tag matching is **exact string matching**. Note:
- `target` is automatically uppercased: `--target esp32` → `ESP32`
- `feature` is passed as-is: `--feature SdCard` requires `feature("SdCard")` in code

### Pitfall 5: Mismatched File-Level and Code-Level Filtering

If manifest.py filters with `features=["wifi"]` but the file also contains `with feature("ble"):`, the file is flashed (wifi matches) but the ble block inside is eliminated. This is usually intended. However, if the file has no code that matches any active tag, you flash an empty (or nearly empty) file — wasting device space. Either filter at the manifest level **or** guard at the code level, but avoid unclear hybrid dependencies.

---

## Summary

| Scenario | Recommended Tool | Effect |
|----------|----------------|--------|
| Multi-platform with shared codebase | `@target("...")` decorators | Same function name, platform-specific implementation |
| Optional feature toggles | `with feature("..."):` blocks | Keep or eliminate code blocks by tag |
| Dev vs production builds | `feature("debug")` / `feature("prod")` | One-flag build mode switching |
| File-level trimming | manifest.py `features` kwarg | Non-matching files not flashed |
| Platform abstraction | `@target` + `with target` in a PAL module | Zero platform coupling in business code |

The core value of conditional compilation: **one source, multiple builds, on-demand composition**. Combined with pyrite-cli's project commands (`project flash` / `project status` / `project pull`) and hash-based incremental flashing, you can manage firmware builds for different hardware platforms and feature configurations from a single project directory.

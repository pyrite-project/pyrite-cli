# MicroPython Firmware Feature Probes

This document lists firmware capabilities that `pyrcli debug doctor` can read from the MicroPython surface or verify through lightweight runtime probes. C macros that cannot be read reliably from the runtime surface, VM internals, native emitters, optimization switches, and board-level pin/clock macros are intentionally excluded.

Key principle: `doctor` reports **runtime-observable capabilities**. It must not claim that it has read C macro values. Macro names are source-code trace hints only.

## Information Sources

- Official documentation: `docs.micropython.org/en/latest/develop/porting.html`
- Official library index: `docs.micropython.org/en/latest/library/index.html`
- Official source references: `py/mpconfig.h`, `extmod/*`, `ports/*`

## Probe Levels

| Level | Meaning | Example |
|-------|---------|---------|
| direct-read | Read directly from an object attribute or function return value | `sys.implementation`, `os.uname()` |
| import-probe | Check whether a module can be imported | `import ssl`, `import network` |
| hasattr-probe | Check whether an attribute, function, or class exists | `hasattr(sys, "settrace")` |
| behaviour-probe | Use a safe behavior test | Create/read/delete a temp file, `eval("1+1")` |

## Default High-Priority Probes

These capabilities are the best candidates for the default `pyrcli debug doctor COM3` summary.

| Capability | Macro hint | Probe | Level |
|------------|------------|-------|-------|
| MicroPython version | `MICROPY_VERSION_*` | `sys.implementation`, `sys.version` | direct-read |
| Platform name | `MICROPY_PY_SYS_PLATFORM` | `sys.platform` | direct-read |
| Board/MCU identity | `MICROPY_HW_BOARD_NAME`, `MICROPY_HW_MCU_NAME` | `os.uname()` | direct-read |
| Raw REPL | `MICROPY_HELPER_REPL` | Enter/exit raw REPL | behaviour-probe |
| KeyboardInterrupt control | `MICROPY_KBD_EXCEPTION` | `import micropython; hasattr(micropython, "kbd_intr")` | hasattr-probe |
| Writable filesystem | `MICROPY_VFS_WRITABLE` | Create, read, delete a temporary file | behaviour-probe |
| Filesystem capacity | `MICROPY_PY_OS_STATVFS` | `hasattr(os, "statvfs")`, then call it | hasattr-probe |
| External file import | `MICROPY_ENABLE_EXTERNAL_IMPORT` | Write a temporary `.py` file, then `import` it | behaviour-probe |
| Basic GC | `MICROPY_PY_GC` | `import gc`, `hasattr(gc, "mem_free")` | import/hasattr |
| Memory info | `MICROPY_PY_MICROPYTHON_MEM_INFO` | `hasattr(micropython, "mem_info")` | hasattr-probe |
| Compiler/eval | `MICROPY_ENABLE_COMPILER`, `MICROPY_PY_BUILTINS_EVAL_EXEC` | `eval("1+1")` | behaviour-probe |
| `compile()` | `MICROPY_PY_BUILTINS_COMPILE` | `hasattr(builtins, "compile")` or call it directly | behaviour-probe |
| Code tracing `sys.settrace` | `MICROPY_PY_SYS_SETTRACE` | `hasattr(sys, "settrace")` | hasattr-probe |
| traceback limit | `MICROPY_PY_SYS_TRACEBACKLIMIT` | `hasattr(sys, "tracebacklimit")` | hasattr-probe |
| `async/await` syntax | `MICROPY_PY_ASYNC_AWAIT` | Compile `async def f(): pass` | behaviour-probe |
| `asyncio` | `MICROPY_PY_ASYNCIO` | `import asyncio` | import-probe |
| `machine` | `MICROPY_PY_MACHINE` | `import machine` | import-probe |
| Network module | `MICROPY_PY_NETWORK` | `import network` | import-probe |
| socket | `MICROPY_PY_SOCKET`, `MICROPY_PY_LWIP` | `import socket` | import-probe |
| TLS | `MICROPY_PY_SSL` | `import ssl` | import-probe |
| Bluetooth | `MICROPY_PY_BLUETOOTH` | `import bluetooth` | import-probe |
| WebREPL | `MICROPY_PY_WEBREPL` | `import webrepl` | import-probe |
| WebSocket | `MICROPY_PY_WEBSOCKET` | `import websocket` | import-probe |

## Identity and Environment

| Capability | Probe code | Notes |
|------------|------------|-------|
| Implementation info | `sys.implementation` | Usually includes name, version, and `_machine` |
| Version string | `sys.version` | Useful for firmware version reports |
| Platform name | `sys.platform` | For example `esp32`, `rp2`, `pyboard` |
| Board info | `os.uname()` | Common fields include sysname, nodename, release, version, machine |
| Platform module | `import platform` | If available, may expose more detailed platform info |
| Built-in module list | `help("modules")` | Output format is unstable; useful as auxiliary info |

## Filesystem and Imports

| Capability | Macro hint | Recommended probe |
|------------|------------|-------------------|
| `os` module | `MICROPY_PY_OS` | `import os` |
| Directory listing | `MICROPY_PY_OS` | `hasattr(os, "listdir")` |
| File stat | `MICROPY_PY_OS` | `hasattr(os, "stat")` |
| Filesystem capacity | `MICROPY_PY_OS_STATVFS` | `hasattr(os, "statvfs")` |
| Sync to storage | `MICROPY_PY_OS_SYNC` | `hasattr(os, "sync")` |
| Random bytes | `MICROPY_PY_OS_URANDOM` | `hasattr(os, "urandom")` |
| uname | `MICROPY_PY_OS_UNAME` | `hasattr(os, "uname")` |
| File write | `MICROPY_VFS_WRITABLE` | `open(path, "wb").write(...)` |
| File read | `MICROPY_VFS` | `open(path, "rb").read()` |
| File delete | `MICROPY_VFS_WRITABLE` | `os.remove(path)` |
| External module import | `MICROPY_ENABLE_EXTERNAL_IMPORT` | Write a temporary module and `import` it |

## Runtime Control and Debugging

| Capability | Macro hint | Recommended probe |
|------------|------------|-------------------|
| `micropython` module | `MICROPY_PY_MICROPYTHON` | `import micropython` |
| Interrupt character control | `MICROPY_KBD_EXCEPTION` | `hasattr(micropython, "kbd_intr")` |
| Memory info | `MICROPY_PY_MICROPYTHON_MEM_INFO` | `hasattr(micropython, "mem_info")` |
| Stack usage | `MICROPY_PY_MICROPYTHON_STACK_USE` | `hasattr(micropython, "stack_use")` |
| Heap lock state | `MICROPY_PY_MICROPYTHON_HEAP_LOCKED` | `hasattr(micropython, "heap_locked")` |
| RingIO | `MICROPY_PY_MICROPYTHON_RINGIO` | `hasattr(micropython, "RingIO")` |
| Scheduler function | `MICROPY_ENABLE_SCHEDULER` | `hasattr(micropython, "schedule")` |
| `sys.settrace` | `MICROPY_PY_SYS_SETTRACE` | `hasattr(sys, "settrace")` |
| traceback limit | `MICROPY_PY_SYS_TRACEBACKLIMIT` | `hasattr(sys, "tracebacklimit")` |
| stdin/stdout/stderr | `MICROPY_PY_SYS_STDFILES` | `hasattr(sys, "stdin")`, etc. |
| stdio buffer | `MICROPY_PY_SYS_STDIO_BUFFER` | `hasattr(sys.stdin, "buffer")` |

## Memory and GC

| Capability | Macro hint | Recommended probe |
|------------|------------|-------------------|
| `gc` module | `MICROPY_PY_GC` | `import gc` |
| Free memory | `MICROPY_PY_GC` | `hasattr(gc, "mem_free")` |
| Allocated memory | `MICROPY_PY_GC` | `hasattr(gc, "mem_alloc")` |
| Manual collection | `MICROPY_PY_GC` | `hasattr(gc, "collect")` |
| GC threshold | `MICROPY_GC_ALLOC_THRESHOLD` | `hasattr(gc, "threshold")` |

## Compiler and Syntax Capabilities

| Capability | Macro hint | Recommended probe |
|------------|------------|-------------------|
| `eval` | `MICROPY_PY_BUILTINS_EVAL_EXEC` | `eval("1+1") == 2` |
| `exec` | `MICROPY_PY_BUILTINS_EVAL_EXEC` | `exec("x=1", ns)` |
| `compile` | `MICROPY_PY_BUILTINS_COMPILE` | `compile("1+1", "<probe>", "eval")` |
| f-string | `MICROPY_PY_FSTRINGS` | `eval("f'{1+1}'") == "2"` |
| Assignment expression | `MICROPY_PY_ASSIGN_EXPR` | `compile("(x:=1)", "<probe>", "eval")` |
| `async/await` syntax | `MICROPY_PY_ASYNC_AWAIT` | `compile("async def f():\\n return 1", "<probe>", "exec")` |
| complex | `MICROPY_PY_BUILTINS_COMPLEX` | `hasattr(builtins, "complex")` or compile `1j` |
| bytearray | `MICROPY_PY_BUILTINS_BYTEARRAY` | `hasattr(builtins, "bytearray")` |
| memoryview | `MICROPY_PY_BUILTINS_MEMORYVIEW` | `hasattr(builtins, "memoryview")` |
| set | `MICROPY_PY_BUILTINS_SET` | `hasattr(builtins, "set")` |
| frozenset | `MICROPY_PY_BUILTINS_FROZENSET` | `hasattr(builtins, "frozenset")` |
| property | `MICROPY_PY_BUILTINS_PROPERTY` | `hasattr(builtins, "property")` |
| `dir` | `MICROPY_PY_BUILTINS_DIR` | `hasattr(builtins, "dir")` |
| `help` | `MICROPY_PY_BUILTINS_HELP` | `hasattr(builtins, "help")` |

## Standard Library and Common Modules

These modules are suitable for `import-probe`. A successful import only means the module entry point exists; subfeatures still need `hasattr` or behavior probes.

| Module | Macro hint | Secondary probe suggestion |
|--------|------------|----------------------------|
| `array` | `MICROPY_PY_ARRAY` | `import array` |
| `asyncio` | `MICROPY_PY_ASYNCIO` | `import asyncio` |
| `binascii` | `MICROPY_PY_BINASCII` | `hasattr(binascii, "crc32")` |
| `btree` | `MICROPY_PY_BTREE` | `import btree` |
| `cmath` | `MICROPY_PY_CMATH` | `import cmath` |
| `collections` | `MICROPY_PY_COLLECTIONS` | `hasattr(collections, "deque")` |
| `cryptolib` | `MICROPY_PY_CRYPTOLIB` | `import cryptolib` |
| `deflate` | `MICROPY_PY_DEFLATE` | `import deflate` |
| `errno` | `MICROPY_PY_ERRNO` | `hasattr(errno, "errorcode")` |
| `framebuf` | `MICROPY_PY_FRAMEBUF` | `import framebuf` |
| `hashlib` | `MICROPY_PY_HASHLIB` | `hasattr(hashlib, "sha256")`, `md5`, `sha1` |
| `heapq` | `MICROPY_PY_HEAPQ` | `import heapq` |
| `io` | `MICROPY_PY_IO` | `hasattr(io, "BytesIO")` |
| `json` | `MICROPY_PY_JSON` | `json.loads("{}")` |
| `marshal` | `MICROPY_PY_MARSHAL` | `import marshal` |
| `math` | `MICROPY_PY_MATH` | `hasattr(math, "isclose")`, `factorial` |
| `openamp` | `MICROPY_PY_OPENAMP` | `import openamp` |
| `platform` | `MICROPY_PY_PLATFORM` | `import platform` |
| `random` | `MICROPY_PY_RANDOM` | `hasattr(random, "randint")` |
| `re` | `MICROPY_PY_RE` | `hasattr(re, "sub")` |
| `select` | `MICROPY_PY_SELECT` | `hasattr(select, "poll")`, `select` |
| `struct` | `MICROPY_PY_STRUCT` | `struct.pack("B", 1)` |
| `_thread` | `MICROPY_PY_THREAD` | `import _thread` |
| `time` | `MICROPY_PY_TIME` | `hasattr(time, "time")`, `ticks_ms`, `gmtime` |
| `uctypes` | `MICROPY_PY_UCTYPES` | `import uctypes` |
| `vfs` | `MICROPY_PY_VFS` | `import vfs` |
| `weakref` | `MICROPY_PY_WEAKREF` | `import weakref` |
| `_onewire` | `MICROPY_PY_ONEWIRE` | `import _onewire` |

## Visible `sys` Capabilities

| Capability | Macro hint | Recommended probe |
|------------|------------|-------------------|
| `sys.maxsize` | `MICROPY_PY_SYS_MAXSIZE` | `hasattr(sys, "maxsize")` |
| `sys.modules` | `MICROPY_PY_SYS_MODULES` | `hasattr(sys, "modules")` |
| `sys.exc_info` | `MICROPY_PY_SYS_EXC_INFO` | `hasattr(sys, "exc_info")` |
| `sys.executable` | `MICROPY_PY_SYS_EXECUTABLE` | `hasattr(sys, "executable")` |
| `sys.intern` | `MICROPY_PY_SYS_INTERN` | `hasattr(sys, "intern")` |
| `sys.exit` | `MICROPY_PY_SYS_EXIT` | `hasattr(sys, "exit")` |
| `sys.atexit` | `MICROPY_PY_SYS_ATEXIT` | `hasattr(sys, "atexit")` |
| `sys.path` | `MICROPY_PY_SYS_PATH` | `hasattr(sys, "path")` |
| `sys.argv` | `MICROPY_PY_SYS_ARGV` | `hasattr(sys, "argv")` |
| `sys.ps1/ps2` | `MICROPY_PY_SYS_PS1_PS2` | `hasattr(sys, "ps1")`, `ps2` |
| `sys.getsizeof` | `MICROPY_PY_SYS_GETSIZEOF` | `hasattr(sys, "getsizeof")` |

## Visible `machine` Capabilities

By default, only check for existence so probing does not change hardware state by constructing peripheral objects.

| Capability | Macro hint | Recommended probe |
|------------|------------|-------------------|
| `machine` module | `MICROPY_PY_MACHINE` | `import machine` |
| reset/reset_cause | `MICROPY_PY_MACHINE_RESET` | `hasattr(machine, "reset")`, `reset_cause` |
| bootloader | `MICROPY_PY_MACHINE_BOOTLOADER` | `hasattr(machine, "bootloader")` |
| IRQ control | `MICROPY_PY_MACHINE_DISABLE_IRQ_ENABLE_IRQ` | `hasattr(machine, "disable_irq")`, `enable_irq` |
| freq | `MICROPY_PY_MACHINE` | `hasattr(machine, "freq")` |
| mem8/mem16/mem32 | `MICROPY_PY_MACHINE_MEMX` | `hasattr(machine, "mem8")`, etc. |
| Pin | `MICROPY_PY_MACHINE_PIN_*` | `hasattr(machine, "Pin")` |
| ADC | `MICROPY_PY_MACHINE_ADC` | `hasattr(machine, "ADC")` |
| DAC | `MICROPY_PY_MACHINE_DAC` | `hasattr(machine, "DAC")` |
| I2C | `MICROPY_PY_MACHINE_I2C` | `hasattr(machine, "I2C")` |
| SoftI2C | `MICROPY_PY_MACHINE_SOFTI2C` | `hasattr(machine, "SoftI2C")` |
| SPI | `MICROPY_PY_MACHINE_SPI` | `hasattr(machine, "SPI")` |
| SoftSPI | `MICROPY_PY_MACHINE_SOFTSPI` | `hasattr(machine, "SoftSPI")` |
| PWM | `MICROPY_PY_MACHINE_PWM` | `hasattr(machine, "PWM")` |
| Timer | `MICROPY_PY_MACHINE_TIMER` | `hasattr(machine, "Timer")` |
| UART | `MICROPY_PY_MACHINE_UART` | `hasattr(machine, "UART")` |
| I2S | `MICROPY_PY_MACHINE_I2S` | `hasattr(machine, "I2S")` |
| CAN | `MICROPY_PY_MACHINE_CAN` | `hasattr(machine, "CAN")` |
| SDCard | `MICROPY_PY_MACHINE_SDCARD` | `hasattr(machine, "SDCard")` |
| WDT | `MICROPY_PY_MACHINE_WDT` | `hasattr(machine, "WDT")` |
| Signal | `MICROPY_PY_MACHINE_SIGNAL` | `hasattr(machine, "Signal")` |
| bitstream | `MICROPY_PY_MACHINE_BITSTREAM` | `hasattr(machine, "bitstream")` |
| time_pulse_us | `MICROPY_PY_MACHINE_PULSE` | `hasattr(machine, "time_pulse_us")` |

## Network, TLS, Bluetooth, and WebREPL

| Capability | Macro hint | Recommended probe |
|------------|------------|-------------------|
| `network` module | `MICROPY_PY_NETWORK` | `import network` |
| WLAN | `MICROPY_PY_NETWORK_WLAN` | `hasattr(network, "WLAN")` |
| LAN | `MICROPY_PY_NETWORK_LAN` | `hasattr(network, "LAN")` |
| PPP | `MICROPY_PY_NETWORK_PPP_LWIP` | `hasattr(network, "PPP")` |
| CYW43 | `MICROPY_PY_NETWORK_CYW43` | Module/class existence check |
| WIZNET5K | `MICROPY_PY_NETWORK_WIZNET5K` | `hasattr(network, "WIZNET5K")` |
| NINAW10 | `MICROPY_PY_NETWORK_NINAW10` | Class existence check |
| socket | `MICROPY_PY_SOCKET`, `MICROPY_PY_LWIP` | `import socket` |
| raw socket constant | `MICROPY_PY_LWIP_SOCK_RAW` | `hasattr(socket, "SOCK_RAW")` |
| ssl | `MICROPY_PY_SSL` | `import ssl` |
| ssl wrap_socket | `MICROPY_PY_SSL` | `hasattr(ssl, "wrap_socket")` |
| bluetooth | `MICROPY_PY_BLUETOOTH` | `import bluetooth` |
| BLE class | `MICROPY_PY_BLUETOOTH` | `hasattr(bluetooth, "BLE")` |
| WebREPL | `MICROPY_PY_WEBREPL` | `import webrepl` |
| WebSocket | `MICROPY_PY_WEBSOCKET` | `import websocket` |

## Port-Specific but Probeable Modules

These are not universal capabilities, but they can be checked from the runtime surface with `import` or `hasattr`.

| Port/range | Module/capability | Recommended probe |
|------------|-------------------|-------------------|
| ESP8266/ESP32 | `esp` | `import esp` |
| ESP32 | `esp32` | `import esp32` |
| ESP8266/ESP32 | ESP-NOW | `import espnow` |
| STM32 / pyboard | `pyb` | `import pyb` |
| STM32 | `stm` | `import stm` |
| Renesas RA | `ra` | `import ra` |
| nRF legacy | `ubluepy` | `import ubluepy` |
| nRF | `nrf` | `import nrf` |
| Zephyr | `zephyr` | `import zephyr` |
| Zephyr | `zsensor` | `import zsensor` |
| Unix/Windows | `termios` | `import termios` |
| Unix | `ffi` | `import ffi` |
| WebAssembly/JS | `js`, `jsffi` | `import js`, `import jsffi` |

## Suggested JSON Schema

```json
{
  "firmware_features": {
    "items": [
      {
        "id": "sys.settrace",
        "macro_hint": "MICROPY_PY_SYS_SETTRACE",
        "category": "debug",
        "status": "supported | unsupported | skipped | error",
        "confidence": "hasattr-probe",
        "probe": "hasattr(sys, 'settrace')"
      }
    ]
  }
}
```

## Implementation Notes

- Use the field name `macro_hint`, not `macro_value`.
- `unsupported` means the runtime surface did not show the capability. It does not mean a C macro value was read as 0.
- Use `unknown` only when the probe cannot run safely or the result cannot be interpreted.
- Network, TLS, and Bluetooth probes should only check modules, classes, or constants; do not actively connect to networks.
- For `machine`, default to `hasattr` checks so probing does not construct peripheral objects.
- Behavior probes should use temporary files and clean them up after success or failure when possible.

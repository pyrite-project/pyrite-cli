# MicroPython 表层可探测固件能力清单

本文整理 `pyrcli debug doctor` 可以从 MicroPython 表层读取或通过轻量运行时探针验证的固件能力。不可从表层可靠读取的 C macro、VM 内部实现、native emitter、优化开关、板级引脚/时钟宏已经删除。

> 关键原则：`doctor` 报告“运行时可观察能力”，不要声称读取到了 C macro 值。宏名只作为源码追踪参考。

## 信息来源

- 官方文档：`docs.micropython.org/en/latest/develop/porting.html`
- 官方库索引：`docs.micropython.org/en/latest/library/index.html`
- 官方源码参考：`py/mpconfig.h`、`extmod/*`、`ports/*`

## 可探测等级

| 等级 | 含义 | 示例 |
| --- | --- | --- |
| direct-read | 可直接从对象属性或函数返回值读取 | `sys.implementation`、`os.uname()` |
| import-probe | 通过 `import` 判断模块是否存在 | `import ssl`、`import network` |
| hasattr-probe | 通过 `hasattr` 判断属性/函数/类是否存在 | `hasattr(sys, "settrace")` |
| behaviour-probe | 通过安全行为测试判断能力 | 写/读/删临时文件、`eval("1+1")` |

## 注册模型

运行时可观察的板卡能力在代码中注册稳定 feature id 和探测方法。CLI 功能单独注册自己依赖哪些 board feature。必需依赖缺失时命令会在继续执行前报错；可选依赖缺失时输出注册的 fallback 标签，例如 `FallbackToSizeVerify`。

## 默认优先探测项

这些能力最适合进入 `pyrcli debug doctor COM3` 的默认摘要。

| 能力 | 相关宏参考 | 探测方式 | 等级 |
| --- | --- | --- | --- |
| MicroPython 版本 | `MICROPY_VERSION_*` | `sys.implementation`、`sys.version` | direct-read |
| 平台名 | `MICROPY_PY_SYS_PLATFORM` | `sys.platform` | direct-read |
| 板卡/MCU 标识 | `MICROPY_HW_BOARD_NAME`、`MICROPY_HW_MCU_NAME` | `os.uname()` | direct-read |
| Raw REPL | `MICROPY_HELPER_REPL` | 进入/退出 raw REPL | behaviour-probe |
| KeyboardInterrupt 控制 | `MICROPY_KBD_EXCEPTION` | `import micropython; hasattr(micropython, "kbd_intr")` | hasattr-probe |
| 文件系统可写 | `MICROPY_VFS_WRITABLE` | 创建、读取、删除临时文件 | behaviour-probe |
| 文件系统容量 | `MICROPY_PY_OS_STATVFS` | `hasattr(os, "statvfs")` 后调用 | hasattr-probe |
| 外部文件导入 | `MICROPY_ENABLE_EXTERNAL_IMPORT` | 写临时 `.py` 后 `import` | behaviour-probe |
| GC 基础能力 | `MICROPY_PY_GC` | `import gc`、`hasattr(gc, "mem_free")` | import/hasattr |
| 内存信息 | `MICROPY_PY_MICROPYTHON_MEM_INFO` | `hasattr(micropython, "mem_info")` | hasattr-probe |
| 编译器/eval | `MICROPY_ENABLE_COMPILER`、`MICROPY_PY_BUILTINS_EVAL_EXEC` | `eval("1+1")` | behaviour-probe |
| `compile()` | `MICROPY_PY_BUILTINS_COMPILE` | `hasattr(builtins, "compile")` 或直接调用 | behaviour-probe |
| 代码跟踪`sys.settrace` | `MICROPY_PY_SYS_SETTRACE` | `hasattr(sys, "settrace")` | hasattr-probe |
| traceback limit | `MICROPY_PY_SYS_TRACEBACKLIMIT` | `hasattr(sys, "tracebacklimit")` | hasattr-probe |
| `async/await` 语法 | `MICROPY_PY_ASYNC_AWAIT` | 编译 `async def f(): pass` | behaviour-probe |
| `asyncio` | `MICROPY_PY_ASYNCIO` | `import asyncio` | import-probe |
| `machine` | `MICROPY_PY_MACHINE` | `import machine` | import-probe |
| 网络模块 | `MICROPY_PY_NETWORK` | `import network` | import-probe |
| socket | `MICROPY_PY_SOCKET`、`MICROPY_PY_LWIP` | `import socket` | import-probe |
| TLS | `MICROPY_PY_SSL` | `import ssl` | import-probe |
| 蓝牙 | `MICROPY_PY_BLUETOOTH` | `import bluetooth` | import-probe |
| WebREPL | `MICROPY_PY_WEBREPL` | `import webrepl` | import-probe |
| WebSocket | `MICROPY_PY_WEBSOCKET` | `import websocket` | import-probe |

## 身份与环境

| 能力 | 探测代码 | 备注 |
| --- | --- | --- |
| 实现信息 | `sys.implementation` | 通常含 name、version、_machine |
| 版本字符串 | `sys.version` | 可用于报告固件版本 |
| 平台名 | `sys.platform` | 如 `esp32`、`rp2`、`pyboard` |
| 板卡信息 | `os.uname()` | 常见字段含 sysname、nodename、release、version、machine |
| 平台模块 | `import platform` | 若存在，可读更细的平台信息 |
| 可用内建模块列表 | `help("modules")` | 输出格式不稳定，适合作辅助信息 |

## 文件系统与导入

| 能力 | 相关宏参考 | 推荐探针 |
| --- | --- | --- |
| `os` 模块 | `MICROPY_PY_OS` | `import os` |
| 目录列举 | `MICROPY_PY_OS` | `hasattr(os, "listdir")` |
| 文件状态 | `MICROPY_PY_OS` | `hasattr(os, "stat")` |
| 文件系统容量 | `MICROPY_PY_OS_STATVFS` | `hasattr(os, "statvfs")` |
| 同步刷盘 | `MICROPY_PY_OS_SYNC` | `hasattr(os, "sync")` |
| 随机字节 | `MICROPY_PY_OS_URANDOM` | `hasattr(os, "urandom")` |
| uname | `MICROPY_PY_OS_UNAME` | `hasattr(os, "uname")` |
| 文件写入 | `MICROPY_VFS_WRITABLE` | `open(path, "wb").write(...)` |
| 文件读取 | `MICROPY_VFS` | `open(path, "rb").read()` |
| 文件删除 | `MICROPY_VFS_WRITABLE` | `os.remove(path)` |
| 外部模块导入 | `MICROPY_ENABLE_EXTERNAL_IMPORT` | 写临时模块并 `import` |

## 运行时控制与调试

| 能力 | 相关宏参考 | 推荐探针 |
| --- | --- | --- |
| `micropython` 模块 | `MICROPY_PY_MICROPYTHON` | `import micropython` |
| 中断字符控制 | `MICROPY_KBD_EXCEPTION` | `hasattr(micropython, "kbd_intr")` |
| 内存信息 | `MICROPY_PY_MICROPYTHON_MEM_INFO` | `hasattr(micropython, "mem_info")` |
| stack 使用量 | `MICROPY_PY_MICROPYTHON_STACK_USE` | `hasattr(micropython, "stack_use")` |
| heap 锁状态 | `MICROPY_PY_MICROPYTHON_HEAP_LOCKED` | `hasattr(micropython, "heap_locked")` |
| RingIO | `MICROPY_PY_MICROPYTHON_RINGIO` | `hasattr(micropython, "RingIO")` |
| 调度函数 | `MICROPY_ENABLE_SCHEDULER` | `hasattr(micropython, "schedule")` |
| `sys.settrace` | `MICROPY_PY_SYS_SETTRACE` | `hasattr(sys, "settrace")` |
| traceback limit | `MICROPY_PY_SYS_TRACEBACKLIMIT` | `hasattr(sys, "tracebacklimit")` |
| stdin/stdout/stderr | `MICROPY_PY_SYS_STDFILES` | `hasattr(sys, "stdin")` 等 |
| stdio buffer | `MICROPY_PY_SYS_STDIO_BUFFER` | `hasattr(sys.stdin, "buffer")` |

## 内存与 GC

| 能力 | 相关宏参考 | 推荐探针 |
| --- | --- | --- |
| `gc` 模块 | `MICROPY_PY_GC` | `import gc` |
| 剩余内存 | `MICROPY_PY_GC` | `hasattr(gc, "mem_free")` |
| 已分配内存 | `MICROPY_PY_GC` | `hasattr(gc, "mem_alloc")` |
| 主动回收 | `MICROPY_PY_GC` | `hasattr(gc, "collect")` |
| GC threshold | `MICROPY_GC_ALLOC_THRESHOLD` | `hasattr(gc, "threshold")` |

## 编译器与语法能力

| 能力 | 相关宏参考 | 推荐探针 |
| --- | --- | --- |
| `eval` | `MICROPY_PY_BUILTINS_EVAL_EXEC` | `eval("1+1") == 2` |
| `exec` | `MICROPY_PY_BUILTINS_EVAL_EXEC` | `exec("x=1", ns)` |
| `compile` | `MICROPY_PY_BUILTINS_COMPILE` | `compile("1+1", "<probe>", "eval")` |
| f-string | `MICROPY_PY_FSTRINGS` | `eval("f'{1+1}'") == "2"` |
| assignment expression | `MICROPY_PY_ASSIGN_EXPR` | `compile("(x:=1)", "<probe>", "eval")` |
| async/await 语法 | `MICROPY_PY_ASYNC_AWAIT` | `compile("async def f():\\n return 1", "<probe>", "exec")` |
| complex | `MICROPY_PY_BUILTINS_COMPLEX` | `hasattr(builtins, "complex")` 或 `1j` 编译 |
| bytearray | `MICROPY_PY_BUILTINS_BYTEARRAY` | `hasattr(builtins, "bytearray")` |
| memoryview | `MICROPY_PY_BUILTINS_MEMORYVIEW` | `hasattr(builtins, "memoryview")` |
| set | `MICROPY_PY_BUILTINS_SET` | `hasattr(builtins, "set")` |
| frozenset | `MICROPY_PY_BUILTINS_FROZENSET` | `hasattr(builtins, "frozenset")` |
| property | `MICROPY_PY_BUILTINS_PROPERTY` | `hasattr(builtins, "property")` |
| `dir` | `MICROPY_PY_BUILTINS_DIR` | `hasattr(builtins, "dir")` |
| `help` | `MICROPY_PY_BUILTINS_HELP` | `hasattr(builtins, "help")` |

## 标准库与通用模块

这些模块适合用 `import-probe`。导入成功只能说明模块入口存在，子功能仍需 `hasattr` 或行为测试确认。

| 模块 | 相关宏参考 | 二级探测建议 |
| --- | --- | --- |
| `array` | `MICROPY_PY_ARRAY` | `import array` |
| `asyncio` | `MICROPY_PY_ASYNCIO` | `import asyncio` |
| `binascii` | `MICROPY_PY_BINASCII` | `hasattr(binascii, "crc32")` |
| `ubinascii.crc32` | `MICROPY_PY_BINASCII` | `hasattr(ubinascii, "crc32")` |
| `btree` | `MICROPY_PY_BTREE` | `import btree` |
| `cmath` | `MICROPY_PY_CMATH` | `import cmath` |
| `collections` | `MICROPY_PY_COLLECTIONS` | `hasattr(collections, "deque")` |
| `cryptolib` | `MICROPY_PY_CRYPTOLIB` | `import cryptolib` |
| `deflate` | `MICROPY_PY_DEFLATE` | `import deflate` |
| `zlib` | `MICROPY_PY_DEFLATE` | `import zlib` |
| `errno` | `MICROPY_PY_ERRNO` | `hasattr(errno, "errorcode")` |
| `framebuf` | `MICROPY_PY_FRAMEBUF` | `import framebuf` |
| `hashlib` | `MICROPY_PY_HASHLIB` | `hasattr(hashlib, "sha256")`、`md5`、`sha1` |
| `heapq` | `MICROPY_PY_HEAPQ` | `import heapq` |
| `io` | `MICROPY_PY_IO` | `hasattr(io, "BytesIO")` |
| `json` | `MICROPY_PY_JSON` | `json.loads("{}")` |
| `marshal` | `MICROPY_PY_MARSHAL` | `import marshal` |
| `math` | `MICROPY_PY_MATH` | `hasattr(math, "isclose")`、`factorial` |
| `openamp` | `MICROPY_PY_OPENAMP` | `import openamp` |
| `platform` | `MICROPY_PY_PLATFORM` | `import platform` |
| `random` | `MICROPY_PY_RANDOM` | `hasattr(random, "randint")` |
| `re` | `MICROPY_PY_RE` | `hasattr(re, "sub")` |
| `select` | `MICROPY_PY_SELECT` | `hasattr(select, "poll")`、`select` |
| `struct` | `MICROPY_PY_STRUCT` | `struct.pack("B", 1)` |
| `_thread` | `MICROPY_PY_THREAD` | `import _thread` |
| `time` | `MICROPY_PY_TIME` | `hasattr(time, "time")`、`ticks_ms`、`gmtime` |
| `uctypes` | `MICROPY_PY_UCTYPES` | `import uctypes` |
| `vfs` | `MICROPY_PY_VFS` | `import vfs` |
| `weakref` | `MICROPY_PY_WEAKREF` | `import weakref` |
| `_onewire` | `MICROPY_PY_ONEWIRE` | `import _onewire` |

## `sys` 可见能力

| 能力 | 相关宏参考 | 推荐探针 |
| --- | --- | --- |
| `sys.maxsize` | `MICROPY_PY_SYS_MAXSIZE` | `hasattr(sys, "maxsize")` |
| `sys.modules` | `MICROPY_PY_SYS_MODULES` | `hasattr(sys, "modules")` |
| `sys.exc_info` | `MICROPY_PY_SYS_EXC_INFO` | `hasattr(sys, "exc_info")` |
| `sys.executable` | `MICROPY_PY_SYS_EXECUTABLE` | `hasattr(sys, "executable")` |
| `sys.intern` | `MICROPY_PY_SYS_INTERN` | `hasattr(sys, "intern")` |
| `sys.exit` | `MICROPY_PY_SYS_EXIT` | `hasattr(sys, "exit")` |
| `sys.atexit` | `MICROPY_PY_SYS_ATEXIT` | `hasattr(sys, "atexit")` |
| `sys.path` | `MICROPY_PY_SYS_PATH` | `hasattr(sys, "path")` |
| `sys.argv` | `MICROPY_PY_SYS_ARGV` | `hasattr(sys, "argv")` |
| `sys.ps1/ps2` | `MICROPY_PY_SYS_PS1_PS2` | `hasattr(sys, "ps1")`、`ps2` |
| `sys.getsizeof` | `MICROPY_PY_SYS_GETSIZEOF` | `hasattr(sys, "getsizeof")` |

## `machine` 可见能力

默认只做存在性检查，避免实例化时改变硬件状态。

| 能力 | 相关宏参考 | 推荐探针 |
| --- | --- | --- |
| `machine` 模块 | `MICROPY_PY_MACHINE` | `import machine` |
| reset/reset_cause | `MICROPY_PY_MACHINE_RESET` | `hasattr(machine, "reset")`、`reset_cause` |
| bootloader | `MICROPY_PY_MACHINE_BOOTLOADER` | `hasattr(machine, "bootloader")` |
| IRQ 控制 | `MICROPY_PY_MACHINE_DISABLE_IRQ_ENABLE_IRQ` | `hasattr(machine, "disable_irq")`、`enable_irq` |
| freq | `MICROPY_PY_MACHINE` | `hasattr(machine, "freq")` |
| mem8/mem16/mem32 | `MICROPY_PY_MACHINE_MEMX` | `hasattr(machine, "mem8")` 等 |
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

## 网络、TLS、蓝牙、WebREPL

| 能力 | 相关宏参考 | 推荐探针 |
| --- | --- | --- |
| `network` 模块 | `MICROPY_PY_NETWORK` | `import network` |
| WLAN | `MICROPY_PY_NETWORK_WLAN` | `hasattr(network, "WLAN")` |
| LAN | `MICROPY_PY_NETWORK_LAN` | `hasattr(network, "LAN")` |
| PPP | `MICROPY_PY_NETWORK_PPP_LWIP` | `hasattr(network, "PPP")` |
| CYW43 | `MICROPY_PY_NETWORK_CYW43` | 模块/类存在性检查 |
| WIZNET5K | `MICROPY_PY_NETWORK_WIZNET5K` | `hasattr(network, "WIZNET5K")` |
| NINAW10 | `MICROPY_PY_NETWORK_NINAW10` | 类存在性检查 |
| socket | `MICROPY_PY_SOCKET`、`MICROPY_PY_LWIP` | `import socket` |
| raw socket 常量 | `MICROPY_PY_LWIP_SOCK_RAW` | `hasattr(socket, "SOCK_RAW")` |
| ssl | `MICROPY_PY_SSL` | `import ssl` |
| ssl wrap_socket | `MICROPY_PY_SSL` | `hasattr(ssl, "wrap_socket")` |
| bluetooth | `MICROPY_PY_BLUETOOTH` | `import bluetooth` |
| BLE class | `MICROPY_PY_BLUETOOTH` | `hasattr(bluetooth, "BLE")` |
| WebREPL | `MICROPY_PY_WEBREPL` | `import webrepl` |
| WebSocket | `MICROPY_PY_WEBSOCKET` | `import websocket` |

## 端口专属但可探测模块

这些不是通用能力，但可以通过 `import` 或 `hasattr` 表层探测。

| 端口/范围 | 模块/能力 | 推荐探针 |
| --- | --- | --- |
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
| WebAssembly/JS | `js`、`jsffi` | `import js`、`import jsffi` |

## 建议的 JSON schema

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

## 实现注意

- 字段名使用 `macro_hint`，不要使用 `macro_value`。
- `unsupported` 表示运行时表层没有观察到能力，不表示读取到了宏值为 0。
- `unknown` 只用于探针无法安全运行或结果无法解释的情况。
- 网络、TLS、蓝牙只做模块/类/常量存在性检查，不主动联网。
- `machine` 默认只做 `hasattr`，避免构造外设对象。
- 行为探针要使用临时文件，并在成功或失败后尽量清理。

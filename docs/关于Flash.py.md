# `Flash.py` — MicroPython 设备刷入工具

通过串口（UART）的**原始 REPL 模式**向 MicroPython 设备（ESP32、ESP8266、RP2040 等）上传文件或目录。
---


## 配置文件 `.pyrite_config.json`

放在**项目根目录**，控制刷入时的块大小：

```json
{
  "chunk_size": 4096
}
```

- `chunk_size` — 每次写入的最大数据量（字节），越大则 REPL 往返次数越少，但单次缓冲区压力越大。默认 4096。
- 不创建此文件也可正常使用，会使用默认值。
- 工具从当前目录向上逐级查找，找到第一个 `.pyrite_config.json` 即停止。

---

## 模块级常量

| 常量 | 值 | 说明 |
|------|-----|------|
| `CONFIG_FILE` | `".pyrite_config.json"` | 配置文件名 |
| `DEFAULT_CHUNK_SIZE` | `4096` | 默认块大小（字节） |

---

## 模块级函数

### `_load_config()`

从当前目录及上级目录查找并加载 `.pyrite_config.json`。如果未找到或解析失败，返回包含 `DEFAULT_CHUNK_SIZE` 的默认配置字典。

- 返回: `dict`，如 `{"chunk_size": 4096}`

### `create_default_config()`

在工作目录创建一个默认的 `.pyrite_config.json`。

---

## `class MicroPython`

MicroPython 设备操作类。封装了串口扫描、连接、原始 REPL 通信、kbd_intr 保护、文件刷入等全部功能。

### 构造方法

```python
MicroPython(port=None, baudrate=115200, timeout=10)
```

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `port` | `str` | `None` | 串口名，如 `"COM3"`、`"/dev/ttyUSB0"` |
| `baudrate` | `int` | `115200` | 波特率 |
| `timeout` | `int` | `10` | 串口读写超时（秒） |

构造时自动加载配置（`_load_config`）。

---

### 静态方法：串口扫描

#### `MicroPython.scan_ports()`

扫描系统全部串口设备。

- 返回: `list[dict]`，每个元素包含 `device`、`description`、`hwid`、`vid`、`pid`、`serial_number`

#### `MicroPython.scan_micropython_ports()`

从全部串口中筛选疑似 MicroPython 设备的列表。筛选依据：

- **VID 匹配**：CP210x (0x10C4)、CH340 (0x1A86)、FTDI (0x0403)、RP2040 (0x2E8A)、ESP32-S3 (0x303A)、MCP2221 (0x16D0)
- **描述关键词**：`cp210`、`ch340`、`ft232`、`usb serial`、`uart`、`micropython`

- 返回: `list[dict]`，每个元素包含 `device`

---

### 连接管理

#### `connect(port=None, baudrate=None)`

打开串口连接到设备。

- 如果已连接，自动断开后再连接。
- 连接后等待 300ms 让设备串口就绪，清空收发缓冲区。
- 返回: `True`

| 参数 | 必填 | 说明 |
|------|------|------|
| `port` | 否 | 覆盖初始化时指定的串口 |
| `baudrate` | 否 | 覆盖初始化时指定的波特率 |

引发:
- `ValueError` — 未指定串口
- `serial.SerialException` — 连接失败

#### `disconnect()`

断开串口连接。顺序执行：

1. 恢复 `kbd_intr(3)`（若之前已设置 `kbd_intr(-1)`）
2. 退出原始 REPL 模式
3. 关闭串口

#### `is_connected`（属性）

`bool` — 是否已连接且串口已打开。

---

### 原始 REPL 协议（内部方法）

以下方法属于内部实现，但在需要扩展功能时也可调用。

#### `_enter_raw_repl()`

进入 MicroPython 原始 REPL 模式。

流程：
1. 发送 `Ctrl+C`（`\x03`）中断可能正在运行的程序
2. 清空输入缓冲区
3. 发送 `Ctrl+A`（`\x01`）进入原始 REPL
4. 等待设备返回 `>` 提示符

引发:
- `RuntimeError` — 无法进入原始 REPL

#### `_exit_raw_repl()`

退出原始 REPL，回到普通 REPL。发送 `Ctrl+B`（`\x02`）。

#### `_write(data)`

向串口写入数据。

| 参数 | 说明 |
|------|------|
| `data` | `str` 或 `bytes`；自动将 `str` 编码为 UTF-8 |

#### `_read_until(terminator=b"\x04", timeout=None)`

从串口持续读取，直到遇到终止符。

| 参数 | 默认 | 说明 |
|------|------|------|
| `terminator` | `b"\x04"` | 终止字节序列 |
| `timeout` | 实例的 `timeout` | 超时秒数 |

- 返回: 包含终止符在内的全部 `bytes` 数据

#### `_execute(code, timeout=10)`

在原始 REPL 中执行一段 Python 代码并返回设备的标准输出。

流程：
1. 写入代码文本
2. 发送 `Ctrl+D`（`\x04`）触发执行
3. 读取直到收到尾部的 `\x04`
4. 去掉尾部 `\x04`，解码为文本
5. 检查输出中是否包含 `"Traceback"`

| 参数 | 默认 | 说明 |
|------|------|------|
| `code` | — | Python 代码字符串或 `bytes` |
| `timeout` | `10` | 等待设备响应的最大秒数 |

- 返回: `str` — 设备输出的文本
- 引发: `RuntimeError` — 执行过程中设备返回了 Traceback

#### `_write_raw_chunk(data)`

通过 `sys.stdin.buffer.read()` 将原始字节直接写入设备上已打开的文件，无需编码开销。

协议（`flash_file` 的每个数据块调用一次）：

1. 发送代码 `import sys; b=sys.stdin.buffer.read(N); f.write(b)`
2. 发送 `Ctrl+D` 触发编译
3. 等待 50ms 让设备开始执行并阻塞在 `read()` 上
4. 发送 `N` 个原始字节
5. 读取直到收到尾部的 `\x04`，检测 Traceback

| 参数 | 说明 |
|------|------|
| `data` | `bytes` — 要写入的原始数据块 |

- 引发: `RuntimeError` — 设备返回了 Traceback（常见原因：设备不支持 `sys.stdin.buffer`）

---

### kbd_intr 保护

刷入过程中，数据流中可能出现字节 `0x03`（Ctrl+C 字符）。默认情况下 MicroPython 收到 `\x03` 会触发 `KeyboardInterrupt` 并重启设备，导致刷入失败。

解决方法：在刷入前设置 `kbd_intr(-1)` 禁用中断，刷入完成后恢复 `kbd_intr(3)`。

#### `_setup_kbd_intr()`

执行 `import micropython; micropython.kbd_intr(-1)`。

#### `_restore_kbd_intr()`

执行 `import micropython; micropython.kbd_intr(3)`。

- 如果未调用过 `_setup_kbd_intr`（`_kbd_set` 为 `False`），直接返回。
- 失败时静默忽略，不中断流程。

---

### 文件刷入

#### `flash_file(local_path, remote_path=None)`

将本地单个文件上传到 MicroPython 设备。

完整流程：

1. 确保处于原始 REPL 模式，调用 `_enter_raw_repl()`（若未进入）
2. 调用 `_setup_kbd_intr()` 设置 `kbd_intr(-1)`（若未设置）
3. 在设备上执行 `f=open('remote_path','wb')` 创建/清空文件
4. 以二进制模式读取本地文件，每次最多 `chunk_size` 字节：
   - 发送 `sys.stdin.buffer.read(N)` 命令让设备等待 N 个原始字节
   - 直接通过串口发送原始字节（零编码开销）
   - 设备将字节写入打开的文件
5. 执行 `f.close()` 关闭文件
6. 打印 `✓` 表示成功

| 参数 | 必填 | 说明 |
|------|------|------|
| `local_path` | 是 | 本地文件绝对或相对路径 |
| `remote_path` | 否 | 设备上保存路径，默认使用文件名；自动 `\` 转 `/` |

- 返回: `True`
- 引发: `FileNotFoundError` — 本地文件不存在
- 引发: `RuntimeError` — 设备执行错误（如文件系统满或不支持 `sys.stdin.buffer`）

如果中途出错，会尝试在设备端执行 `f.close()` 关闭文件。

---

#### `flash_program(local_dir, remote_prefix="")`

将整个目录树递归上传到 MicroPython 设备。

流程：

1. 扫描本地目录，收集所有文件及相对路径
2. 在设备上创建所需的所有子目录（`os.mkdir()`）
3. 逐文件调用 `flash_file()`

| 参数 | 必填 | 说明 |
|------|------|------|
| `local_dir` | 是 | 本地目录路径 |
| `remote_prefix` | 否 | 设备上的远程路径前缀（如 `"lib"`） |

- 返回: `list[tuple(local_path, remote_path, success)]` — 每个文件的刷入结果

---

#### `run(code)`

在设备上执行任意 Python 代码并返回输出。

- 短接 `_enter_raw_repl()` + `_execute()`

| 参数 | 说明 |
|------|------|
| `code` | Python 代码字符串 |

- 返回: `str` — 设备输出

示例:

```python
mp.run("import machine; print(machine.freq())")
```

---

#### `reset()`

软重启设备（`machine.reset()`）。

先恢复 `kbd_intr(3)`，然后发送重启指令。重启后连接将断开，但串口对象会自然失效。

---

### 上下文管理器

```python
with MicroPython(port="COM3") as mp:
    mp.flash_file("boot.py")
    mp.flash_file("main.py")
# 自动调用 disconnect()
```

---

## 错误处理

| 场景 | 表现 |
|------|------|
| 串口不存在/被占用 | `serial.SerialException` |
| 无法自动发现设备 | 提示用户指定 `--port` |
| 设备在原始 REPL 中返回 Traceback | `RuntimeError`，输出完整 Traceback |
| 刷入中途断开 | `serial.SerialException`，文件对象在设备端可能未关闭 |
| 本地文件不存在 | `FileNotFoundError` |
| 目录无效 | `NotADirectoryError` |

---

## 刷入流程示意图

```
连接 ──▶ 进入原始 REPL ──▶ kbd_intr(-1) ──▶ 分块传输 ──▶ kbd_intr(3) ──▶ 断开
                                              │
                                    ┌─────────┴──────────────────┐
                                    │ 发送代码: sys.stdin        │
                                    │   .buffer.read(N)          │
                                    │ 发送原始 N 字节            │
                                    │ 等待设备 \x04 确认         │
                                    └────────────────────────────┘
```

---

## 常见问题

**Q: 为什么需要 `kbd_intr(-1)`？**

二进制文件（或某些文本文件）中可能包含值 `0x03` 的字节。MicroPython 默认将 `0x03` 视为 Ctrl+C，收到后会触发 `KeyboardInterrupt` 并重启设备。`kbd_intr(-1)` 关闭此行为，确保数据完整写入。

**Q: 块大小 (`chunk_size`) 设多大合适？**

- 较小的块（512–1024）：内存占用低，适合 RAM 紧张的设备；但 REPL 往返次数多，速度慢。
- 较大的块（4096–8192）：往返少、速度快；但需要设备有足够缓冲区。
- ESP32 建议 4096，ESP8266 建议 2048。

**Q: 如何知道串口号？**

```bash
python utils/Flash.py --scan
```

**Q: 支持哪些 MicroPython 设备？**

任何支持原始 REPL（Ctrl+A）并通过串口连接的 MicroPython 设备均可，包括但不限于 ESP32、ESP8266、RP2040 (Raspberry Pi Pico)、PYBoard 等。

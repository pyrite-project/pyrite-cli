# `Flash.py` — MicroPython 设备刷入工具

通过串口（UART）的**原始 REPL 模式**向 MicroPython 设备（ESP32、ESP8266、RP2040 等）上传文件或目录，并提供交互式 REPL 终端、自动编译 `.py → .mpy`、条件编译等能力。

---

## 配置文件 `.pyrite_config.json`

放在**项目根目录**，控制刷入行为和下载线程数。从当前目录向上逐级查找，找到即停止。

```json
{
  "chunk_size": 4096,
  "download_threads": 4,
  "auto_compile": true
}
```

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `chunk_size` | `4096` | 每次写入的最大数据量（字节），越大 REPL 往返越少，但单次缓冲区压力越大 |
| `download_threads` | `4` | 存根下载并发线程数，范围 1–12 |
| `auto_compile` | `true` | 是否自动编译 `.py` → `.mpy`，设为 `false` 可关闭 |

不创建此文件也可正常使用，会使用默认值。

### board_tags 补充配置 (`pyproject.toml`)

可在 `pyproject.toml` 中追加设备识别标签映射，会与内置默认标签合并：

```toml
[tool.pyrite.board_tags]
ESP32_S3 = ["ESP32", "wifi"]
C3 = ["ESP32", "wifi"]
```

---

## 模块级常量

| 常量 | 值 | 说明 |
|------|-----|------|
| `CONFIG_FILE` | `".pyrite_config.json"` | 配置文件名 |
| `DEFAULT_CHUNK_SIZE` | `4096` | 默认块大小（字节） |
| `_DEFAULT_BOARD_TAGS` | 内置字典 | 默认板级标签映射 |
| `ENTER_RAW_REPL` | `b'\x01'` | Ctrl+A — 进入原始 REPL |
| `EXIT_RAW_REPL` | `b'\x02'` | Ctrl+B — 退出原始 REPL |
| `SET_RESET` | `b'\x03'` | Ctrl+C — 中断/复位 |
| `SET_EXECUTE` | `b'\x04'` | Ctrl+D — 执行 |
| `ENTER_RAW_PASTE` | `b'\x05'` | Ctrl+E — 进入粘贴模式 |

---

## 模块级函数

### `_load_config()`

从当前目录及上级目录查找并加载 `.pyrite_config.json`，同时扫描 `pyproject.toml` 中的 `[tool.pyrite.board_tags]` 并合并到内置标签。

- 返回: `dict`，包含 `chunk_size`、`download_threads`、`auto_compile`、`board_tags`

### `_compile_to_mpy(local_path, bytecode_ver=None, arch=None)`

编译 `.py` → `.mpy`，使用 `mpy_cross` Python API。

| 参数 | 说明 |
|------|------|
| `local_path` | 本地 `.py` 文件路径 |
| `bytecode_ver` | 目标设备 mpy 字节码版本（可选，自动从设备读取） |
| `arch` | 目标架构，如 `xtensawin`、`armv7m`（可选，自动从设备读取） |

- 返回: `(tmp_mpy_path, tmp_dir)`，失败返回 `(None, None)`
- 回退策略：编译失败时静默打印警告并回退到原始 `.py`

### `create_default_config()`

在工作目录创建一个默认的 `.pyrite_config.json`（含 `chunk_size`、`download_threads`、`auto_compile`）。

---

## `class MicroPython`

MicroPython 设备操作类。封装了串口扫描、连接、原始 REPL 通信、kbd_intr 保护、文件刷入、REPL 交互终端等全部功能。

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

**实例属性**：

| 属性 | 类型 | 说明 |
|------|------|------|
| `config` | `dict` | 合并后的配置 |
| `ser` | `Serial` 或 `None` | pySerial 串口对象 |
| `port` | `str` | 串口号 |
| `baudrate` | `int` | 波特率 |
| `timeout` | `int` | 超时 |
| `_in_raw` | `bool` | 是否处于原始 REPL 模式 |
| `_kbd_set` | `bool` | 是否已设置 `kbd_intr(-1)` |

---

### 静态方法：串口扫描

#### `MicroPython.scan_ports(vid=None, pid=None, keyword=None, require_vid=True)`

扫描系统全部串口设备，支持多种过滤条件。

| 参数 | 默认 | 说明 |
|------|------|------|
| `vid` | `None` | 按 VID 过滤（十进制） |
| `pid` | `None` | 按 PID 过滤（十进制） |
| `keyword` | `None` | 按描述关键字过滤（大小写不敏感） |
| `require_vid` | `True` | 设为 `False` 时包含无 VID/PID 的设备 |

- 返回: `list[dict]`，每个元素包含 `device`、`description`、`hwid`、`vid`、`pid`、`serial_number`

---

### 连接管理

#### `connect(port=None, baudrate=None)`

打开串口连接到设备。如果已连接，自动断开后再连接。连接后等待 300ms 让设备串口就绪，清空收发缓冲区。

| 参数 | 必填 | 说明 |
|------|------|------|
| `port` | 否 | 覆盖初始化时指定的串口 |
| `baudrate` | 否 | 覆盖初始化时指定的波特率 |

- 返回: `True`
- 引发: `ValueError` — 未指定串口；`serial.SerialException` — 连接失败

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
1. 连发两次 `Ctrl+C`（`\x03`）中断可能正在运行的程序
2. 清空输入缓冲区
3. 发送 `Ctrl+A`（`\x01`）进入原始 REPL
4. 等待设备返回 `>` 提示符
5. 如果 `>` 未出现（设备程序循环无法中断），发送 `Ctrl+D`（`\x04`）软重启后重试

引发:
- `RuntimeError` — 连续尝试后仍无法进入原始 REPL

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
5. 去掉输出开头的 `"OK"`（MicroPython 自带的执行状态标记）
6. 检查输出中是否包含 `"Traceback"`

| 参数 | 默认 | 说明 |
|------|------|------|
| `code` | — | Python 代码字符串或 `bytes` |
| `timeout` | `10` | 等待设备响应的最大秒数 |

- 返回: `str` — 设备输出的文本
- 引发: `RuntimeError` — 执行过程中设备返回了 Traceback

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

#### `flash_file(local_path, remote_path=None, compile=None, bytecode_ver=None, arch=None, active_tags=None)`

将本地单个文件上传到 MicroPython 设备。支持预处理、编译到 `.mpy`、刷入一条龙。

| 参数 | 必填 | 说明 |
|------|------|------|
| `local_path` | 是 | 本地文件绝对或相对路径 |
| `remote_path` | 否 | 设备上保存路径，默认使用文件名 |
| `compile` | 否 | 覆盖 `config["auto_compile"]` 设置 |
| `bytecode_ver` | 否 | 目标 mpy 字节码版本（自动从设备读取） |
| `arch` | 否 | 目标架构（自动从设备读取） |
| `active_tags` | 否 | 条件编译 tags 集合 |

完整流程：
1. 如果 `active_tags` 非空且文件为 `.py`，调用预处理器进行条件编译
2. `manifest.py` 跳过上传；`main.py`/`boot.py` 以及 `.pyi` 文件不编译
3. 若启用了编译且为 `.py`，调用 `_compile_to_mpy()`，成功则改刷 `.mpy`
4. 发送两遍 `Ctrl+C` 中断设备
5. 发送 `FLASH` 模板脚本（在设备端 `open(FILE, 'wb')` 并循环读取 stdin）
6. 按 `DEFAULT_CHUNK_SIZE` 分块发送原始字节，以 `[fuck!]\n` 标记结束
7. 打印刷入结果

刷入流程（新）：

```
连接 → 进入原始REPL → 预处理(条件编译) → 编译(可选) → 发送FLASH模板脚本
→ 分块发送原始字节 → 设备端写入文件 → 完成
```

- 引发: `FileNotFoundError` — 本地文件不存在

#### `flash_program(local_dir, remote_prefix="", bytecode_ver=None, arch=None, active_tags=None, manifest_path=None)`

将整个目录树递归上传到 MicroPython 设备。

| 参数 | 必填 | 说明 |
|------|------|------|
| `local_dir` | 是 | 本地目录路径 |
| `remote_prefix` | 否 | 设备上的远程路径前缀（如 `"lib"`） |
| `bytecode_ver` | 否 | 目标 mpy 字节码版本 |
| `arch` | 否 | 目标架构 |
| `active_tags` | 否 | 条件编译 tags 集合 |
| `manifest_path` | 否 | manifest.py 路径（控制哪些文件刷入） |

流程：
1. 如提供 `manifest_path`，通过 `manifest_loader` 解析刷入清单
2. 否则扫描目录，收集所有 `.py` 文件
3. 在设备上创建所需的所有子目录（`os.mkdir()`）
4. 逐个文件调用 `flash_file()`

- 返回: `list[tuple(local_path, remote_path, success)]` — 每个文件的刷入结果

---

### 设备信息检测

#### `get_mpy_version() -> tuple[int, str] | tuple[None, None]`

从设备读取 mpy 字节码版本号和架构。

- 在设备上执行代码读取 `sys.implementation.mpy`
- 解析出版本号（低 8 位）和架构名称（高 6 位映射）
- 返回 `(ver, arch)`，如 `(6, 'xtensawin')`；失败返回 `(None, None)`

#### `detect_tags() -> set`

从设备读取 board 信息，返回活跃 tags 集合。

- 读取 `os.uname().machine` 和 `sys.platform`
- 与 `config["board_tags"]` 匹配，返回匹配的 tags（如 `{"ESP32", "wifi"}`）
- 同时将 `sys.platform` 返回值加入 tags

---

### 其他方法

#### `run(code)`

在设备上执行任意 Python 代码并返回输出。短接 `_enter_raw_repl()` + `_execute()`。

| 参数 | 说明 |
|------|------|
| `code` | Python 代码字符串 |

- 返回: `str` — 设备输出

```python
mp.run("import machine; print(machine.freq())")
```

#### `reset()`

软重启设备（`machine.reset()`）。先恢复 `kbd_intr(3)`，然后发送重启指令。

---

### 交互式 REPL

#### `repl_()`

连接到 MicroPython 设备的交互式终端。将串口输出实时显示到终端，同时支持非阻塞键盘输入命令。

**设计特点**：单循环架构，不依赖独立读线程/写线程，无 `input()` 调用。

**跨平台键盘输入**：

| 平台 | 非阻塞检测 | 读键 |
|------|-----------|------|
| Windows | `msvcrt.kbhit()` | `msvcrt.getch()` |
| macOS/Linux | `select.select()` | `os.read(fd, 1)` |

- Unix 下临时切换终端为 cbreak 模式（关闭 `ECHO`/`ICANON`/`ISIG`），退出时自动恢复

**ANSI 高亮**：

| 场景 | 效果 |
|------|------|
| Traceback 到错误行 | 红色字（`\033[31m`），覆盖完整错误行（`NameError: message`） |
| Traceback + 错误行在同一串口包 | 精准截断，仅错误区域红色 |
| Traceback 和错误行跨多包 | 状态持续，错误行到来后自动关闭红色 |

**串口输出净化**：

- 过滤原始 REPL 控制字符：`\x01`（Ctrl+A）、`\x02`（Ctrl+B）、`\x04`（Ctrl+D）
- 在发送命令后等待 `OK` 响应（`_expect_ok`），将其从输出中移除

**快捷键**：

| 按键 | 行为 |
|------|------|
| `Enter` | 发送输入缓存的代码 + `Ctrl+D` 到原始 REPL 执行 |
| `Ctrl+C` | 向设备发送 `\x03` 中断，清空输入缓存 |
| `Ctrl+D` | 退出 REPL 会话 |
| `Backspace` | 删除输入缓存末尾字符 |
| `↑ / ↓` | 历史命令导航（输入保留未提交内容） |

**循环逻辑**：

```
while 已连接:
    读取串口数据 → 净化 → 错误高亮 → 输出到终端
    如有输入缓存 → 重绘输入行
    非阻塞检测键盘 → 有按键则处理（Enter/Ctrl+C/字符等）
    sleep(0.01)
```

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
| 无法进入原始 REPL | `RuntimeError`，含设备原始响应 |

---

## FLASH 模板脚本

刷入时注入到设备的 Python 脚本，负责在设备端接收数据并写入文件：

```python
import sys,micropython
micropython.kbd_intr(-1)
usb = sys.stdin.buffer
buf = b''
with open(FILE, 'wb') as f:
    while True:
        ln = usb.read(BFSIZE)
        if ln:
            f.flush()
            if (buf + ln).endswith(b'[fuck!]\n') or ln.endswith(b'[fuck!]\n'):
                f.write(ln[:-8])
                break
            f.write(ln)
            buf = ln
micropython.kbd_intr(3)
```

- `BFSIZE` 替换为 `DEFAULT_CHUNK_SIZE`，`FILE` 替换为设备上目标路径
- 以 `[fuck!]\n` 标记数据结束

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
pyrcli scan
```

**Q: 支持哪些 MicroPython 设备？**

任何支持原始 REPL（Ctrl+A）并通过串口连接的 MicroPython 设备均可，包括但不限于 ESP32、ESP8266、RP2040 (Raspberry Pi Pico)、PYBoard 等。

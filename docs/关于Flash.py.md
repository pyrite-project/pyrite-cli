# `Flash.py` — MicroPython 设备刷入工具

通过串口（UART）的**原始 REPL 模式**向 MicroPython 设备（ESP32、ESP8266、RP2040 等）上传文件或目录，并提供交互式 REPL 终端、自动编译 `.py → .mpy`、条件编译、增量刷入等能力。

---

## 配置文件 `.pyrite_config.json`

放在**项目根目录**，控制刷入行为和下载线程数。从当前目录向上逐级查找，找到即停止。

```json
{
  "chunk_size": 4096,
  "download_threads": 4,
  "auto_compile": true,
  "verify": "size",
  "max_retries": 2
}
```

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `chunk_size` | `4096` | 每次写入的最大数据量（字节），越大 REPL 往返越少，但单次缓冲区压力越大 |
| `download_threads` | `4` | 存根下载并发线程数，范围 1–12 |
| `auto_compile` | `true` | 是否自动编译 `.py` → `.mpy`，设为 `false` 可关闭 |
| `verify` | `"size"` | 刷入后校验模式: `off`=不校验, `size`=文件大小, `crc32`=文件大小+CRC32 |
| `max_retries` | `2` | 校验失败时最大重试次数，设为 `0` 关闭重试 |

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
| `HASH_CONFIG_FILE` | `"pyrite_file_config.json"` | 哈希配置文件名 |
| `_HASH_VERSION` | `1` | 哈希配置文件格式版本 |
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

- 返回: `dict`，包含 `chunk_size`、`download_threads`、`auto_compile`、`verify`、`max_retries`、`board_tags`

### `_compile_to_mpy(local_path, bytecode_ver=None, arch=None)`

编译 `.py` → `.mpy`，使用 `mpy_cross` Python API。

| 参数 | 说明 |
|------|------|
| `local_path` | 本地 `.py` 文件路径 |
| `bytecode_ver` | 目标设备 mpy 字节码版本（可选，自动从设备读取） |
| `arch` | 目标架构，如 `xtensawin`、`armv7m`（可选，自动从设备读取） |

- 返回: `(tmp_mpy_path, tmp_dir)`，失败返回 `(None, None)`
- 回退策略：编译失败时静默打印警告并回退到原始 `.py`

### `_compile_files_parallel(local_paths, bytecode_ver=None, arch=None, max_workers=4)`

并行编译多个 `.py` → `.mpy`（`ThreadPoolExecutor`）。在 `flash_program` 中使用以加速批量刷入前的编译阶段。

| 参数 | 说明 |
|------|------|
| `local_paths` | 本地 `.py` 路径列表 |
| `bytecode_ver` | mpy 字节码版本 |
| `arch` | 目标架构 |
| `max_workers` | 最大并行数 |

- 返回: `dict {local_path: (mpy_path, tmp_dir)}`，编译失败值为 `(None, None)`

### `_grep_size_after_ok(buf)`

从串口原始字节流中查找 `OK<size>\n` 并解析文件大小。用于 bytes 协议下载。

### `_grep_raw_start(buf)`

返回原始数据在字节流中的起始下标（跳过 `OK<size>\n` 协议前缀）。

### `_extract_raw_bytes(buf, expected_size)`

从串口字节流中提取原始文件数据，去除协议前缀和尾部 `\x04` 标记。

### `create_default_config()`

在工作目录创建一个默认的 `.pyrite_config.json`（含 `chunk_size`、`download_threads`、`auto_compile`、`verify`、`max_retries`）。

---

## `class MicroPython`

MicroPython 设备操作类。封装了串口扫描、连接、原始 REPL 通信、kbd_intr 保护、文件刷入、REPL 交互终端、增量刷入、设备文件管理等全部功能。

### 构造方法

```python
MicroPython(port=None, baudrate=115200, timeout=10)
```

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `port` | `str` | `None` | 串口名，如 `"COM3"`、`"/dev/ttyUSB0"` |
| `baudrate` | `int` | `115200` | 波特率 |
| `timeout` | `int` | `10` | 串口读写超时（秒） |

构造时自动加载配置（`_load_config`），`ser` 初始化为未打开的 `serial.Serial()` 对象。

**实例属性**：

| 属性 | 类型 | 说明 |
|------|------|------|
| `config` | `dict` | 合并后的配置 |
| `ser` | `Serial` | pySerial 串口对象（始终有效，`is_open` 判断是否已连接） |
| `port` | `str` | 串口号 |
| `baudrate` | `int` | 波特率 |
| `timeout` | `int` | 超时 |
| `_in_raw` | `bool` | 是否处于原始 REPL 模式 |
| `_kbd_set` | `bool` | 是否已设置 `kbd_intr(-1)` |
| `_repl_log_file` | `TextIO` 或 `None` | REPL 原始数据日志文件句柄 |

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

`bool` — 串口对象 `ser.is_open` 的快捷访问。

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

### REPL 串口日志系统

用于调试和排查刷入问题，自动记录所有串口收发数据到 `./log/` 目录。

#### `_open_repl_log()`

在 `./log/` 目录下创建以时间戳命名的日志文件（`flash_YYYYMMDD_HHMMSS.log`），以写模式打开。

- 返回: `Path` — 日志文件路径

#### `_close_repl_log()`

关闭当前日志文件句柄（如果打开）。

#### `_repl_log_ctx()`（上下文管理器）

自动管理日志生命周期：进入时打开日志（如果尚未打开），退出时自动关闭。日志文件首次创建时打印文件路径。

#### `_drain_rx_log()`

非阻塞读取串口 RX 缓冲区中所有剩余数据并记录到日志。用于清空不必要的残留数据同时保留日志记录。

#### `_log_repl_data(direction, data)`

记录单次串口收发数据到日志文件。

| 参数 | 说明 |
|------|------|
| `direction` | `"tx"` 表示写入，`"rx"` 表示读取 |
| `data` | 原始 `bytes` 数据 |

- 日志格式：`[HH:MM:SS] >>（tx）或 <<（rx）文本内容`
- 控制字符（`\x01`–`\x05`）替换为可读名称 `<RAW>`、`<B>`、`<C>`、`<D>`、`<E>`
- 纯控制字符/空白行额外输出 hex 表示

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

### 哈希工具（内部）

#### `_compute_file_hash(filepath)`（static）

计算文件的 SHA256 哈希值（1MB 分块读取）。

- 返回: `str` — 十六进制哈希字符串

#### `_collect_project_files(local_dir, active_tags=None, manifest_path=None)`

收集项目中可刷入的文件列表（与 `flash_program` 规则一致）。支持 manifest 和目录递归两种模式，自动过滤 `manifest.py` 和 `.pyi` 文件。

- 返回: `list[(local_abs_path, remote_path)]`

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
4. 发送 `FLASH` 模板脚本（含 `FSIZE` 文件大小占位符），设备端代为 `open(FILE, 'wb')` 并循环 `read(BFSIZE)` 直到收齐指定字节数
5. 按 `DEFAULT_CHUNK_SIZE` 分块发送原始字节
6. 刷入后校验（依据 `verify` 配置）

刷入流程：

```
连接 → 进入原始REPL → 预处理(条件编译) → 编译(可选) → 发送FLASH模板脚本(含文件大小)
→ 分块发送原始字节 → 设备端计数接收并写入 → 校验 → 完成
```

- 引发: `FileNotFoundError` — 本地文件不存在

#### `flash_program(local_dir, remote_prefix="", bytecode_ver=None, arch=None, active_tags=None, manifest_path=None)`

将整个目录树批量上传到 MicroPython 设备（一次脚本注入 + 一次数据流）。

| 参数 | 必填 | 说明 |
|------|------|------|
| `local_dir` | 是 | 本地目录路径 |
| `remote_prefix` | 否 | 设备上的远程路径前缀（如 `"lib"`） |
| `bytecode_ver` | 否 | 目标 mpy 字节码版本 |
| `arch` | 否 | 目标架构 |
| `active_tags` | 否 | 条件编译 tags 集合 |
| `manifest_path` | 否 | manifest.py 路径（控制哪些文件刷入） |

流程（三阶段）：
1. **收集与预处理** — 收集 `.py` 文件清单，条件编译预处理
2. **并行编译** — 使用 `_compile_files_parallel()` 多线程并发 `mpy-cross`
3. **批量写入** — 批量创建目录 → 发送 `FLASH_PROGRAM` 脚本 → 一次流式传输所有文件

- 返回: `list[tuple(local_path, remote_path, success)]` — 每个文件的刷入结果

---

### 增量刷入（project 功能）

#### `project_scan(local_dir, hash_config_path=None, active_tags=None, manifest_path=None)`

扫描项目目录，计算所有可刷入文件的 SHA256 哈希并保存到 `pyrite_file_config.json`。无需串口连接。

| 参数 | 默认 | 说明 |
|------|------|------|
| `local_dir` | — | 项目目录路径 |
| `hash_config_path` | `local_dir/pyrite_file_config.json` | 哈希配置文件输出路径 |
| `active_tags` | `None` | 条件编译 tags |
| `manifest_path` | `None` | manifest.py 路径 |

- 返回: 配置文件路径

配置文件格式：

```json
{
  "version": 1,
  "hash_algorithm": "sha256",
  "files": {
    "main.py": "e3b0c44298fc...",
    "lib/utils.py": "01ba4719c80b..."
  }
}
```

#### `project_flash(local_dir, remote_prefix, hash_config_path=None, bytecode_ver=None, arch=None, active_tags=None, manifest_path=None)`

加载哈希配置，比对当前哈希，仅刷入新增或已更改的文件。需先 `connect()`。

流程：
1. 加载配置 → 扫描当前项目 → 计算哈希并比对
2. 标记 [新增] 或 [已更改] 的文件
3. 逐文件调用 `flash_file()` 刷入
4. 更新哈希配置（仅记录成功刷入的文件）

- 返回: `list[tuple(local_path, remote_path, success)]`

#### `project_status(local_dir, remote_prefix, hash_config_path=None, active_tags=None, manifest_path=None)`

比对本地哈希和设备端文件大小，显示差异清单（不刷入）。需先 `connect()`。

显示状态：`[ADD]`（本地有/设备无）、`[MOD]`（本地哈希变化）、`[DEL]`（配置中有/项目中已移除）。

#### `project_pull(local_dir, remote_prefix, hash_config_path=None, active_tags=None, manifest_path=None, dry_run=False)`

从设备下载项目文件到本地（批量传输）。类似 `flash_program` 的批处理逻辑：

1. 收集所有文件路径
2. 发送一个脚本到设备，设备一次性 stat 所有文件，输出 `SZ:[...]` + 全部文件内容拼接
3. 主机端按文件大小分割数据块，逐个写入本地文件

如果本地目录为空或不存在，自动调用 `_discover_device_files()` 从设备递归发现文件清单。

支持 `dry_run=True` 预览模式。

- 输出标记: `[INFO]` `[PREVIEW]` `[SKIP]` `[ERROR]`
- 成功/失败状态: `✓` 绿色 / `✗` 红色

#### `_discover_device_files(remote_prefix)`

递归发现设备上的所有文件。设备端逐行输出 `size|path`，主机端按行解析。

- 返回: `list[(full_remote_path, size)]`

#### `fs_df()`

获取设备文件系统使用情况。通过 `os.statvfs('/')` 获取 total/used/free 字节数。

- 返回: `dict {'total': int, 'used': int, 'free': int}`

---

### 设备文件管理

以下方法用于操作设备上的文件系统。

#### `_read_device_file(remote_path)`

从设备读取文件内容（原始字节传输）。协议：设备先输出文件大小（文本行），再通过 `stdout.buffer` 输出原始字节。PC 端读取全部串口数据后解析。

- 返回: `bytes` — 完整的文件内容

#### `_check_device_files(remote_paths)`

批量检查设备文件存在性和大小。

- 返回: `dict {remote_path: size}`，不存在的文件 `size = -1`

#### `fs_ls(remote_path="/")`

列出设备目录下的文件和子目录，返回包含 `name`、`type`（`F`/`D`）、`size` 的字典列表。

- 对每个条目连续两次 `os.stat()`，解决 MicroPython 部分端口首次读取目录大小不稳定的问题。

#### `fs_rm(remote_path)`

删除设备上的文件或空目录。

- 返回: `bool` — 是否成功

#### `fs_cat(remote_path)`

读取设备上文本文件的内容。

- 返回: `str`

#### `fs_get(remote_path, local_path)`

从设备下载文件到本地路径。调用 `_read_device_file()` 获取原始字节后写入本地。

- 返回: `int` — 文件大小（字节）

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

连接到 MicroPython 设备的交互式终端（串口透传模式）。将串口输出实时显示到终端，键盘输入透传到设备。

**设计特点**：单循环架构，不依赖独立读线程/写线程。一次串口循环内同时处理设备→终端和键盘→串口两个方向的数据流。

**跨平台非阻塞键盘输入**：

| 平台 | 非阻塞检测 | 读键 |
|------|-----------|------|
| Windows | `msvcrt.kbhit()` | `msvcrt.getch()` |
| macOS/Linux | `select.select()` | `os.read(fd, 1)` |

- Unix 下临时切换终端为 cbreak 模式（关闭 `ECHO`/`ICANON`/`ISIG`），退出时自动恢复

**ANSI 错误高亮**：

调用 `_colorize_repl_output()` 实时扫描串口输出：

| 场景 | 效果 |
|------|------|
| 单包内包含完整的 Traceback → Error 行 | 精准截断，仅错误部分渲染红色 |
| Traceback 和 Error 行跨多包到达 | 状态持续跟踪 `in_error` 标志，Error 行到达后自动关闭红色 |

**Windows 扩展键映射**：

方向键和编辑键的 `\xe0` 前缀序列映射为 ANSI 转义序列后透传：

| 原始键 | 映射发送 |
|--------|---------|
| ↑ / ↓ / → / ← | `\x1b[A` / `\x1b[B` / `\x1b[C` / `\x1b[D` |
| Home / End | `\x1b[H` / `\x1b[F` |
| Delete | `\x1b[3~` |
| Insert | `\x1b[2~` |

**循环逻辑**：

```
中断设备进入普通 REPL → while 已连接:
    串口有数据 → 读取 → 错误高亮 → 输出到终端
    键盘有按键 → 读取 → 透传到串口
    sleep(0.01)
```

**退出**：Unix 下 `Ctrl+C` 退出会话；Windows 下无特殊退出键，直接 `Ctrl+Break` 或关闭窗口。

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
| 设备文件读取超时/不完整 | `RuntimeError`，含已接收字节数 |

---

## FLASH 模板脚本（单文件刷入）

刷入单个文件时注入到设备的 Python 脚本，负责在设备端接收数据并写入文件：

```python
import sys,micropython

micropython.kbd_intr(-1)
usb = sys.stdin.buffer

f_size = FSIZE
with open("FILE", 'wb') as f:
    while f_size:
        ln = usb.read(BFSIZE)
        if ln:
            f.flush()
            f.write(ln)
            f_size -= len(ln)
            print(f_size)
micropython.kbd_intr(3)
```

- `FSIZE` 替换为文件总字节数，`FILE` 替换为设备上目标路径，`BFSIZE` 替换为 `DEFAULT_CHUNK_SIZE`
- 设备端精确计数接收：每收到一块数据减去长度，剩余 0 时关闭文件
- 每块写入后打印剩余大小，PC 端可借此判断进度（当前未利用此信息）

## FLASH_PROGRAM 模板脚本（批量刷入）

刷入整个目录时注入的脚本，一次处理多个文件：

```python
import sys, micropython

micropython.kbd_intr(-1)
usb = sys.stdin.buffer

entries = FILES
for file_size, file_path in entries:
    with open(file_path, 'wb') as f:
        remaining = file_size
        while remaining:
            chunk = usb.read(min(remaining, BFSIZE))
            if chunk:
                f.write(chunk)
                remaining -= len(chunk)
micropython.kbd_intr(3)
```

- `FILES` 替换为 `[(size, remote_path), ...]` 元组列表
- PC 端将所有文件内容拼接为连续字节流，设备端按 `FILES` 中的尺寸依次切分写入
- 无需逐个文件往返，一次脚本注入 + 一次数据流即可刷完整个目录

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

**Q: `ser` 为什么不再初始化为 `None`？**

VSCode 类型检查会将 `self.ser = None` 推导为 `Optional[Serial]`，后续所有 `self.ser.xxx` 调用都需要判空或 `# type: ignore`。改用 `self.ser = serial.Serial()`（未打开的串口对象）后类型恒为 `Serial`，消除冗余的类型标注。

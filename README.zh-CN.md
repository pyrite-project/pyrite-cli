<h1 align="center">Pyrite CLI</h1>

<p align="center">
  <a href="README.md">English</a> | <a href="README.zh-CN.md">中文</a>
</p>

Pyrite CLI 是一个面向 MicroPython 开发板的精简命令行工具箱。它通过统一的 `pyrcli` 命令界面，帮助你发现设备、刷入文件、同步项目、查看设备文件系统、打开 REPL、通过 WebDAV 挂载文件，以及执行固件相关任务。

它默认通过 UART raw REPL 与设备通信；添加 `--ws` 后，可以改用 WebREPL over WebSocket。

## 一览

| 任务 | 命令 | 说明 |
|------|------|------|
| 发现设备 | `pyrcli scan` | 扫描串口设备，可选板级探测和 JSON 输出 |
| 刷入单文件 | `pyrcli flash COM3 main.py /main.py` | 预处理、可选编译为 `.mpy`、传输、校验 |
| 同步项目 | `pyrcli project flash COM3 . /app` | 只上传新增或变更文件 |
| 浏览文件 | `pyrcli fs ls COM3 /` | 列出、上传、下载、删除、移动和复制文件 |
| 挂载文件 | `pyrcli mount COM3` | 通过本地 WebDAV 暴露设备文件系统 |
| 实时调试 | `pyrcli repl COM3` | 打开交互式 MicroPython REPL |
| 使用 WebREPL | `--ws ws://XXX:XXXX` | 通过 WebREPL 路由设备命令 |
| 烧录固件 | `pyrcli firmware flash COM3 firmware.bin` | 封装 `esptool` 固件操作 |

## 安装

Pyrite CLI 需要 Python 3.10 或更高版本。

```bash
pip install pyrite-cli
```

固件烧录是可选能力，需要 `esptool`：

```bash
pip install esptool
```

从当前仓库开发安装：

```bash
pip install -e .
```

## 快速上手

把示例里的 `COM3` 替换成你的串口。

```bash
# 发现已连接的开发板
pyrcli scan
pyrcli scan -i

# 查看板级信息
pyrcli board-info COM3

# 刷入并运行
pyrcli flash COM3 main.py /main.py
pyrcli run COM3 "import machine; print(machine.freq())"

# 操作文件
pyrcli fs ls COM3 /
pyrcli fs put COM3 local.py /remote.py
pyrcli fs get COM3 /remote.py local_copy.py

# 打开交互式 REPL
pyrcli repl COM3
```

在桌面文件管理器中挂载设备文件系统：

```bash
pyrcli mount COM3
```

给设备命令添加 `--ws` 即可改用 WebREPL。位置参数 `PORT` 会保留，用来保持 CLI 形状一致；真正的传输目标是 WebSocket URL。

```bash
pyrcli board-info COM3 --ws ws://192.168.4.1:8266 --password mypass
pyrcli flash COM3 main.py /main.py --ws ws://esp32.local:8266
pyrcli mount COM3 --ws ws://esp32.local:8266 --password mypass
```

省略 `--password` 时，WebREPL 密码解析顺序为：命令行参数、`PYRITE_WEBREPL_PASSWORD` 环境变量、交互输入。

## 核心能力

### 快速刷入

Pyrite CLI 会进入 raw REPL、分块传输代码、校验结果，并恢复设备会话。Python 文件可在上传前自动编译为 `.mpy`。

### 增量项目同步

`pyrcli project hash` 记录本地 SHA256 哈希。`pyrcli project flash` 对比该状态，只上传新增或变更文件。

### 条件构建

使用 `@feature("wifi")`、`@target("esp32")`、`with feature(...)` 和 `with target(...)`，可以用同一套源码管理多块开发板或多个固件变体。Pyrite CLI 使用 `libcst` 重写语法，而不是正则替换。

### Manifest 刷入

`manifest.py` 可以选择模块和包、重映射远端路径，并按 feature 标签过滤文件。它通过 `ast` 解析，不会执行任意代码。

### 桌面文件系统桥接

`pyrcli mount` 会启动本地 WebDAV 服务，把文件管理器操作映射为 MicroPython 文件操作。Windows 可映射盘符；Linux 和 macOS 会在默认文件管理器中打开 WebDAV 位置。

### 共享传输层

串口和 WebREPL 共用同一组高层 MicroPython 操作。大多数设备命令都支持 `--ws` 和 `--password`。

## 常见工作流

创建项目并准备编辑器支持：

```bash
pyrcli project new my-project
pyrcli project new my-project --platform COM3
```

刷入目录：

```bash
pyrcli flash-program COM3 src/ /app
pyrcli flash-program COM3 src/ /app --manifest manifest.py
```

增量同步项目：

```bash
pyrcli project hash .
pyrcli project status COM3 . /app
pyrcli project flash COM3 . /app
pyrcli project pull COM3 . /app
```

浏览和挂载文件：

```bash
pyrcli fs ls COM3 / -r
pyrcli fs ls COM3 / --sort size
pyrcli fs cat COM3 /main.py
pyrcli mount COM3 --readonly
pyrcli mount COM3 --drive P
```

烧录固件：

```bash
pyrcli firmware flash COM3 firmware.bin
pyrcli firmware erase COM3
pyrcli firmware info COM3
pyrcli firmware verify COM3 firmware.bin
pyrcli firmware read COM3 0x100000 -o backup.bin
```

## 配置

Pyrite CLI 会从当前目录向上查找 `.pyrite_config.json`。

```json
{
  "chunk_size": 4096,
  "download_threads": 4,
  "auto_compile": true,
  "verify": "crc32",
  "max_retries": 2
}
```

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `chunk_size` | `4096` | 每次写入的最大字节数 |
| `download_threads` | `4` | 存根下载并发数，限制在 1-12 |
| `auto_compile` | `true` | 自动将 `.py` 编译为 `.mpy` |
| `verify` | `"size"` | 校验模式：`off`、`size` 或 `crc32` |
| `max_retries` | `2` | 校验失败或断线后的重试次数 |

可以在 `pyproject.toml` 中扩展设备标签：

```toml
[tool.pyrite.board_tags]
ESP32_S3 = ["ESP32", "wifi"]
C3 = ["ESP32", "wifi"]
```

## 文档

| 主题 | 文档 |
|------|------|
| 入门、命令、配置 | [快速上手](docs/快速上手.md) |
| 刷入协议与项目同步 | [设备刷入与项目同步](docs/设备刷入与项目同步.md) |
| 条件编译指南 | [Conditional Compilation: Practical Guide](docs/conditional-compilation-guide.md) |
| 条件编译中文实战 | [条件编译实战](docs/条件编译实战.md) |
| WebDAV 桌面挂载 | [WebDAV 挂载](docs/WebDAV挂载.md) |
| 架构说明 | [Architecture](docs/architecture.md) |

## 命令参考

### 顶层命令

| 命令 | 作用 |
|------|------|
| `scan` | 扫描串口设备，支持过滤和 JSON 输出 |
| `flash` | 刷入单个本地文件到设备 |
| `flash-program` | 递归刷入本地目录 |
| `run` | 在设备上执行 Python 代码 |
| `repl` | 打开交互式 REPL |
| `reset` | 通过 raw REPL 软重启设备 |
| `board-info` | 输出固件、CPU、内存、Flash 和文件系统信息 |
| `mount` | 通过本地 WebDAV 挂载设备文件系统 |
| `config` | 创建默认 `.pyrite_config.json` |

### `pyrcli project`

| 命令 | 作用 |
|------|------|
| `project new` | 创建项目并下载存根 |
| `project init` | 为已有项目添加 MicroPython 存根 |
| `project hash` / `project scan` | 计算本地文件哈希 |
| `project flash` | 只上传变更文件 |
| `project status` | 查看本地与设备差异 |
| `project pull` | 从设备拉取文件 |
| `project run` | 同步后进入 REPL 监控 |

### `pyrcli fs`

| 命令 | 作用 |
|------|------|
| `fs ls` | 列出文件，支持递归、排序和分页 |
| `fs cat` | 打印设备端文本文件 |
| `fs put` | 上传本地文件 |
| `fs get` | 下载设备文件 |
| `fs rm` | 删除文件或目录 |
| `fs tree` | 显示树形视图 |
| `fs mv` | 移动或重命名 |
| `fs cp` | 复制 |

### `pyrcli firmware`

| 命令 | 作用 |
|------|------|
| `firmware flash` | 烧录固件 `.bin` |
| `firmware erase` | 擦除 Flash |
| `firmware info` | 读取芯片和 Flash 信息 |
| `firmware verify` | 验证固件内容 |
| `firmware read` | 读取 Flash 内容到文件 |

# Pyrite CLI

[English](README.md) | [中文](README.zh-CN.md)

Pyrite CLI 是一个 MicroPython 设备工具箱，用来刷入代码、浏览文件、打开 REPL、同步项目，并把设备文件系统挂载到文件系统。它可以通过 UART raw REPL 与设备通信，也可以通过 WebREPL WebSocket 走 Wi-Fi 通道；同一套命令同时覆盖 USB 串口和远程 WebREPL。

它面向 MicroPython 日常开发循环：发现设备、推送代码、查看文件、运行一段命令，然后快速重复。

## 功能/亮点

| 需求 | 命令 | Pyrite CLI 做什么 |
|------|------|-------------------|
| 发现设备 | `pyrcli scan` | 扫描串口设备，可探测板级信息，可输出 JSON |
| 刷入单文件 | `pyrcli flash COM3 main.py /main.py` | 预处理、可选编译 `.mpy`、传输并校验 |
| 同步项目 | `pyrcli project flash COM3 . /app` | 基于哈希只上传新增或变更文件 |
| 浏览文件 | `pyrcli fs ls COM3 /` | 列表、上传、下载、删除、移动、复制设备文件 |
| 桌面挂载 | `pyrcli mount COM3` | 通过本地 WebDAV 将MicroPython设备的文件系统桥接到系统文件管理器 |
| 交互调试 | `pyrcli repl COM3` | 打开交互式 MicroPython REPL |
| Wi-Fi 连接 | `--ws ws://esp32.local:8266` | 将设备命令切换到 WebREPL |
| 固件烧录 | `pyrcli firmware flash COM3 firmware.bin` | 封装 `esptool` 固件操作 |

## 安装

```bash
pip install pyrite-cli
```

固件烧录是可选能力，需要额外安装 `esptool`：

```bash
pip install esptool
```

从当前仓库开发安装：

```bash
pip install -e .
```

核心运行依赖包括 `typer`、`pyserial`、`requests`、`tqdm`、`mpy-cross`、`libcst` 和 `websocket-client`。

## 快速上手

```bash
# 发现设备
pyrcli scan
pyrcli scan -i

# 查看板级信息
pyrcli board-info COM3

# 刷入文件
pyrcli flash COM3 main.py /main.py

# 执行一段命令
pyrcli run COM3 "import machine; print(machine.freq())"

# 打开交互式 REPL
pyrcli repl COM3

# 列出和传输文件
pyrcli fs ls COM3 /
pyrcli fs put COM3 local.py /remote.py
pyrcli fs get COM3 /remote.py local_copy.py

# 在桌面文件管理器中挂载设备文件系统
pyrcli mount COM3
```

给设备命令添加 `--ws` 即可改用 WebREPL。位置参数 `PORT` 会保留，用来保持 CLI 形状一致；真正的连接目标是 WebSocket URL。

```bash
pyrcli board-info COM3 --ws ws://192.168.4.1:8266 --password mypass
pyrcli flash COM3 main.py /main.py --ws ws://esp32.local:8266
pyrcli mount COM3 --ws ws://esp32.local:8266 --password mypass
```

省略 `--password` 时，WebREPL 密码解析顺序为：命令行参数、`PYRITE_WEBREPL_PASSWORD` 环境变量、交互输入。

## 主要功能

**快速刷入链路**

Pyrite CLI 会进入 raw REPL、分块传输代码、校验结果，并恢复设备会话。Python 文件可在上传前自动编译为 `.mpy`。

**项目级增量同步**

`pyrcli project hash` 记录本地 SHA256 哈希。`pyrcli project flash` 对比状态，只上传新增或变更文件。

**条件编译**

使用 `@feature("wifi")`、`@target("esp32")`、`with feature(...)` 和 `with target(...)`，可以用同一套源码管理多块开发板或多个固件变体。Pyrite CLI 使用 `libcst` 做语法树级重写，不用正则替换。

**Manifest 刷入清单**

`manifest.py` 可以选择模块和包、重映射远端路径，并按 feature 标签过滤文件。解析器基于 `ast`，不会执行任意代码。

**桌面文件系统桥接**

`pyrcli mount` 启动本地 WebDAV 服务，把文件管理器操作映射为 MicroPython 文件操作。Windows 可映射盘符；Linux 和 macOS 会在默认文件管理器中打开 WebDAV 位置。

**传输层抽象**

串口和 WebREPL 共用同一组高层 MicroPython 操作。大多数设备命令都支持 `--ws` 和 `--password`。

## 常见工作流

### 创建项目

```bash
pyrcli project new my-project
pyrcli project new my-project --platform COM3
```

项目助手可以检测开发板、下载匹配的 MicroPython 类型存根，并准备编辑器配置。

### 刷入目录

```bash
pyrcli flash-program COM3 src/ /app
pyrcli flash-program COM3 src/ /app --manifest manifest.py
```

### 增量同步项目

```bash
pyrcli project hash .
pyrcli project status COM3 . /app
pyrcli project flash COM3 . /app
pyrcli project pull COM3 . /app
```

### 浏览和挂载文件

```bash
pyrcli fs ls COM3 / -r
pyrcli fs ls COM3 / --sort size
pyrcli fs cat COM3 /main.py
pyrcli mount COM3 --readonly
pyrcli mount COM3 --drive P
```

### 固件烧录

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

## 文档导航

想按路径阅读，可以从这里进入：

| 主题 | 文档 |
|------|------|
| 入门、命令、配置 | [快速上手](docs/快速上手.md) |
| 刷入协议与项目同步 | [设备刷入与项目同步](docs/设备刷入与项目同步.md) |
| 条件编译实战 | [条件编译实战](docs/条件编译实战.md) |
| 条件编译英文指南 | [Conditional Compilation: Practical Guide](docs/conditional-compilation-guide.md) |
| WebDAV 桌面挂载 | [WebDAV 挂载](docs/WebDAV挂载.md) |
| 架构说明 | [Architecture](docs/architecture.md) |
| `flash.py` 内部说明 | [`flash.py` internals](docs/关于flash.py_EN.md) |

## 命令地图

### 顶层命令

| 命令 | 作用 |
|------|------|
| `scan` | 扫描串口设备，支持过滤和 JSON 输出 |
| `flash` | 刷入单个本地文件 |
| `flash-program` | 递归刷入本地目录 |
| `run` | 在设备上执行 Python 代码 |
| `repl` | 打开交互式 REPL |
| `reset` | 通过 raw REPL 软重启 |
| `board-info` | 输出固件、CPU、内存、Flash、文件系统信息 |
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
| `fs ls` | 列文件，支持递归、排序、分页 |
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

## 微硬核：内部工作方式

Pyrite CLI 以 MicroPython raw REPL 作为统一执行层。

- 通过 `Ctrl+A` (`0x01`) 进入 raw REPL，然后在设备上执行小段 Python。
- 单文件刷入会注入设备端接收脚本，从 `sys.stdin.buffer` 读取二进制块。
- 批量刷入先发送文件大小和路径清单，再发送连续数据流。
- 下载采用两阶段 bytes 协议：先查询大小，再按已知大小接收字节。
- 协议助手会剥离 `\x04\x04>` 等 REPL 尾部标记。
- 刷入前设置 `kbd_intr(-1)`，避免数据流中的 `0x03` 被误当成键盘中断。
- `fs ls` 会故意执行两次 `os.stat(p)`，因为部分 MicroPython 板子首次调用可能返回过期数据。
- 统一日志系统会写入 JSONL 操作记录，并可记录串口或 WebSocket 流量。

## 开发

运行无需设备的纯逻辑测试：

```bash
pytest test/test_protocol_helpers.py test/test_flash_utils.py test/test_config.py test/test_manifest_loader.py test/test_logger.py test/test_output.py test/test_webdav_mount.py -v
```

常用冒烟检查：

```bash
python -c "from cli.main import app; from cli.utils.flash import MicroPython"
pyrcli --help
pyrcli scan
pyrcli scan --version
```

实机验证需要连接 MicroPython 开发板：

```bash
pyrcli board-info COM3
pyrcli flash COM3 main.py /main.py
pyrcli run COM3 "print('hello')"
```

## License

见 [LICENSE](LICENSE)。

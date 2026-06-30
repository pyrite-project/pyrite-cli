<h1 align="center">Pyrite CLI</h1>
<p align="center">
  <img src="./docs/img/icon.png" width="128" alt="Pyrcli-icon" align="center">
</p>

<p align="center">
  <a href="README.md">English</a> | <a href="README.zh-CN.md">中文</a>
</p>

Pyrite CLI 是一个面向 MicroPython 开发板的精简命令行工具箱。它通过统一的 `pyrcli` 命令界面，帮助你发现设备、刷入文件、同步项目、查看设备文件系统、安装包、监控 GPIO 输入、打开 REPL、通过 WebDAV 挂载设备文件，以及通过 `mpremote` 把上位机目录反向挂载给设备。

它默认通过 UART raw REPL 与设备通信；添加 `--ws` 后，可以改用 WebREPL over WebSocket。

## 一览

| 任务 | 命令 | 说明 |
|------|------|------|
| 发现设备 | `pyrcli scan` | 扫描串口设备，可选板级探测和 JSON 输出 |
| 刷入单文件 | `pyrcli flash COM3 main.py /main.py` | 预处理、可选编译为 `.mpy`、传输、校验 |
| 同步项目 | `pyrcli project flash COM3 . /app` | 只上传新增或变更文件 |
| 开发会话 | `pyrcli project dev COM3 . /app --lens` | 监听、增量刷入、traceback 映射和设备端测试 |
| 设备测试 | `pyrcli test COM3 test_device/` | 上传并运行设备端测试，解析结果帧 |
| 快照恢复 | `pyrcli snapshot save COM3 before-change` | 保存、对比和恢复设备文件系统快照 |
| 主机透传 | `pyrcli tunnel network COM3 --allow example.com` | 键盘输入和受限 HTTP(S) 网络跳板 |
| 浏览文件 | `pyrcli fs ls COM3 /` | 列出、上传、下载、删除、移动和复制文件 |
| 安装包 | `pyrcli pkg install COM3 aioble` | 委托 `mpremote mip install` 在上位机侧完成包安装 |
| 监控 GPIO | `pyrcli monitor COM3 --pins 0,2,4 --count 10` | 只读输入状态，不设置上下拉或输出模式 |
| 挂载文件 | `pyrcli mount COM3` | 通过本地 WebDAV 暴露设备文件系统 |
| 反向挂载 | `pyrcli remount COM3 .` | 通过 `mpremote` 把上位机目录暴露为设备端 `/remote` |
| 实时调试 | `pyrcli repl COM3` | 打开交互式 MicroPython REPL |
| 使用 WebREPL | `--ws ws://XXX:XXXX` | 通过 WebREPL 路由设备命令 |

## 安装

Pyrite CLI 需要 Python 3.10 或更高版本。

```bash
pip install pyrite-cli
```

`mpremote` 会作为运行时依赖安装，`pyrcli remount` 和 `pyrcli pkg` 会使用它完成反向挂载和包安装。

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
pyrcli debug board-info COM3

# 刷入，然后进入 REPL 运行代码
pyrcli flash COM3 main.py /main.py
pyrcli repl COM3
# 在 REPL 中输入：import machine; print(machine.freq())

# 监听项目变化并展开 traceback 本地源码上下文
pyrcli project dev COM3 . /app --lens
pyrcli project dev COM3 . /app --test-on-save=all --test-path test_device/

# 操作文件
pyrcli fs ls COM3 /
pyrcli fs put COM3 local.py /remote.py
pyrcli fs get COM3 /remote.py local_copy.py
```

在桌面文件管理器中挂载设备文件系统：

```bash
pyrcli mount COM3
```

把当前上位机目录反向挂载给设备，设备端可通过 `/remote` 访问：

```bash
pyrcli remount COM3 .
```

通过上位机侧 `mpremote mip` 路径安装 MicroPython 包，也可以先查看计划：

```bash
pyrcli pkg install COM3 aioble --target /lib --dry-run
pyrcli pkg install COM3 aioble --target /lib
```

监控 GPIO 输入：

```bash
pyrcli monitor COM3 --pins 0,2,4,5 --interval 0.2 --count 20
pyrcli monitor COM3 --pins 0,2 --format json --count 5
```

保存设备文件系统快照并在需要时恢复：

```bash
pyrcli snapshot save COM3 before-change --remote-path /app
pyrcli snapshot diff COM3 before-change --remote-path /app
pyrcli snapshot restore COM3 before-change
pyrcli snapshot restore COM3 before-change --apply --yes
```

使用主机能力透传：

```bash
pyrcli tunnel kb COM3
pyrcli tunnel network COM3 --allow example.com
```

给设备命令添加 `--ws` 即可改用 WebREPL。位置参数 `PORT` 会保留，用来保持 CLI 形状一致；真正的传输目标是 WebSocket URL。

```bash
pyrcli debug board-info COM3 --ws ws://192.168.4.1:8266 --password mypass
pyrcli flash COM3 main.py /main.py --ws ws://esp32.local:8266
pyrcli mount COM3 --ws ws://esp32.local:8266 --password mypass
```

省略 `--password` 时，WebREPL 密码解析顺序为：命令行参数、`PYRITE_WEBREPL_PASSWORD` 环境变量、交互输入。

## 核心能力

### 快速刷入

Pyrite CLI 会进入 raw REPL、分块传输代码、校验结果，并恢复设备会话。Python 文件可在上传前自动编译为 `.mpy`。

### 增量项目同步

`pyrcli project hash` 记录本地 SHA256 哈希。`pyrcli project flash` 对比该状态，只上传新增或变更文件。

### 开发会话与设备端测试

`pyrcli project dev` 持续监听项目变化并增量刷入；`--lens` 会把设备 traceback 展开到本地源码上下文，`--test-on-save=all|changed|off` 可在刷入后自动运行设备端测试。`pyrcli test` 可单独上传并执行 `test_device/`。

### 快照与恢复

`pyrcli snapshot save/list/diff/restore` 可保存设备文件系统状态、预览差异并恢复。`restore` 默认只 dry-run，真正写回设备需要 `--apply`；对比阶段在 host 侧计算 SHA256，兼容旧固件。

### 条件构建

使用 `@feature("wifi")`、`@target("esp32")`、`with feature(...)` 和 `with target(...)`，可以用同一套源码管理多块开发板或多个固件变体。Pyrite CLI 使用 `libcst` 重写语法，而不是正则替换。

### Manifest 刷入

`manifest.py` 可以选择模块和包、重映射远端路径，并按 feature 标签过滤文件。它通过 `ast` 解析，不会执行任意代码。

### 桌面文件系统桥接

`pyrcli mount` 会启动本地 WebDAV 服务，把文件管理器操作映射为 MicroPython 文件操作。Windows 可映射盘符；Linux 和 macOS 会在默认文件管理器中打开 WebDAV 位置。

### 反向上位机挂载

`pyrcli remount` 委托 `mpremote mount` 工作，让设备端把上位机目录看作 `/remote`。Pyrite CLI 不重新实现挂载协议，只负责校验本地参数、定位 `mpremote` 并把交互会话交给它。

### 上位机侧包安装

`pyrcli pkg install` 委托 `mpremote mip install`，让包解析和下载发生在上位机侧。`--dry-run` 只输出可审计计划，不连接设备。`pyrcli pkg cache` 当前用于规划缓存路径并审计本地 `package.json` 元数据，不自行执行网络下载。

### GPIO 监控

`pyrcli monitor` 只用 `machine.Pin(pin, machine.Pin.IN)` 读取输入状态。可配合 `--pins`、`--count`、`--duration`、`--edge changed` 和 `--format json` 做脚本化采样。

### 主机能力透传

`pyrcli tunnel kb` 把上位机键盘事件透传给设备端 helper。`pyrcli tunnel network` 让设备通过上位机发起受限 HTTP(S) 请求，必须显式配置 `--allow`，访问私有地址还需要 `--allow-private`。

### 串口占用处理

当串口疑似被其他进程占用时，交互式终端会询问是否扫描并结束占用进程。Windows 优先使用 Sysinternals `handle.exe`，Linux/macOS 使用 `lsof` 或 `fuser`；非交互环境会跳过。

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
pyrcli project flash COM3 . /app --snapshot-before before-flash
pyrcli project pull COM3 . /app
pyrcli project dev COM3 . /app --lens --test-on-save=all
```

浏览和挂载文件：

```bash
pyrcli fs ls COM3 / -r
pyrcli fs ls COM3 / --sort size
pyrcli fs cat COM3 /main.py
pyrcli mount COM3 --readonly
pyrcli mount COM3 --drive P
pyrcli remount COM3 .
```

安装包和监控 GPIO：

```bash
pyrcli pkg install COM3 aioble --target /lib --dry-run
pyrcli pkg install-offline COM3 .pyrite/pkg-cache/aioble
pyrcli monitor COM3 --pins 0,2,4,5 --count 20
```

## 配置

Pyrite CLI 会从当前目录向上查找 `.pyrite_config.json`。

```json
{
  "chunk_size": 4096,
  "download_threads": 4,
  "auto_compile": true,
  "verify": "crc32",
  "delta_flash": "auto",
  "precheck": "basic",
  "precheck_compat": "warn",
  "precheck_mp_version": "",
  "max_retries": 2,
  "baudrate": 921600
}
```

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `chunk_size` | `4096` | 每次写入的最大字节数 |
| `download_threads` | `4` | 存根下载并发数，限制在 1-12 |
| `auto_compile` | `true` | 自动将 `.py` 编译为 `.mpy` |
| `verify` | `"size"` | 校验模式：`off`、`size` 或 `crc32` |
| `delta_flash` | `"auto"` | 单文件增量刷入策略：`off`、`auto` 或 `on` |
| `precheck` | `"basic"` | 刷入前代码预检查：`off`、`basic` 或 `strict` |
| `precheck_compat` | `"warn"` | strict 兼容性问题处理：`warn`、`error` 或 `off` |
| `precheck_mp_version` | `""` | 可选目标 MicroPython 固件版本 |
| `max_retries` | `2` | 校验失败或断线后的重试次数 |
| `baudrate` | `921600` | 默认串口波特率 |

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
| 条件编译实战 | [条件编译实战](docs/条件编译实战.md) |
| WebDAV 桌面挂载 | [WebDAV 挂载](docs/WebDAV挂载.md) |
| MicroPython 固件能力探测 | [MicroPython 表层可探测固件能力清单](docs/mp_firm_featswitch.md) |

## 命令参考

### 顶层命令

| 命令 | 作用 |
|------|------|
| `scan` | 扫描串口设备，支持过滤和 JSON 输出 |
| `flash` | 刷入单个本地文件到设备 |
| `flash-program` | 递归刷入本地目录 |
| `repl` | 打开交互式 REPL |
| `reset` | 通过 raw REPL 软重启设备 |
| `test` | 上传并运行设备端 MicroPython 测试 |
| `debug board-info` | 输出固件、CPU、内存、Flash 和文件系统信息 |
| `debug doctor` | 运行串口、Raw REPL、文件系统、内存和运行时特性诊断 |
| `monitor` | 监控 GPIO 输入状态 |
| `mount` | 通过本地 WebDAV 挂载设备文件系统 |
| `remount` | 通过 `mpremote` 把上位机目录反向挂载为设备端 `/remote` |
| `snapshot` | 保存、对比和恢复设备文件系统快照 |
| `tunnel` | 透传键盘输入和受限 HTTP(S) 请求 |
| `pkg` | 通过 `mpremote mip` 安装 MicroPython 包 |
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
| `project dev` | 监听项目变化、增量刷入、REPL、traceback 映射和测试联动 |

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

### `pyrcli pkg`

| 命令 | 作用 |
|------|------|
| `pkg install` | 对包名或 URL 执行或预览 `mpremote mip install` |
| `pkg cache` | 规划本地缓存路径并审计本地包元数据 |
| `pkg install-offline` | 通过 `mpremote mip` 安装本地 `package.json` 或包目录 |

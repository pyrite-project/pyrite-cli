# ✦ Pyrite-CLI

MicroPython 设备串口工具集。通过 UART raw REPL 协议或 WebREPL (WebSocket) 与设备通信，支持文件刷入、交互式 REPL、自动/条件编译、增量同步、项目脚手架、固件烧录、设备文件浏览器，以及通过 WebDAV 把设备文件系统挂到系统默认文件管理器。

---

## 安装

```bash
pip install pyrite-cli
# 固件烧录需额外安装 esptool：
pip install esptool
```

核心依赖：`typer`、`pyserial`、`requests`、`tqdm`、`mpy-cross`、`libcst`、`websocket-client`

---

## 文档导航

读者可根据经验层级选择文档：

| 层级 | 文档 | 适合读者 |
|------|------|----------|
| **入门** | [快速上手](docs/快速上手.md) | 首次使用，从安装到第一个刷入示例 |
| **进阶** | [设备刷入与项目同步](docs/设备刷入与项目同步.md) | 深入刷入原理、增量同步、manifest 清单 |
| **进阶** | [条件编译实战](docs/条件编译实战.md) | 一份源码管理多平台、按功能裁剪固件 |
| **桌面集成** | [WebDAV 挂载](docs/WebDAV挂载.md) | 用 Windows Explorer / Finder / Linux 文件管理器浏览设备 |

---

## 能力地图

| 场景 | 推荐命令 | 说明 |
|------|----------|------|
| 发现设备 | `pyrcli scan` | 扫描串口、输出 VID/PID、可选 JSON |
| 单文件刷入 | `pyrcli flash COM3 main.py /main.py` | 自动编译、条件编译、校验 |
| 批量刷入 | `pyrcli project flash COM3 . /` | 基于哈希的增量同步 |
| 文件浏览 | `pyrcli fs ls COM3 /` | 命令行文件浏览器 |
| 桌面挂载 | `pyrcli mount COM3` | PC 侧 WebDAV 桥接到默认文件管理器 |
| 交互调试 | `pyrcli repl COM3` | 进入 MicroPython REPL |
| 固件烧录 | `pyrcli firmware flash COM3 firmware.bin` | esptool 封装 |

---

## 命令概览

### 基础命令

| 命令 | 作用 |
|------|------|
| `scan` | 扫描串口设备，支持 VID/PID/关键字过滤 |
| `flash` | 刷入单个文件（自动编译 + 条件编译） |
| `flash-program` | 递归刷入整个本地目录，支持 manifest 清单过滤 |
| `repl` | 交互式 MicroPython REPL |
| `reset` | 通过原始 REPL 软重启设备 |
| `board-info` | 获取固件、CPU、内存、Flash 等详细板级信息 |
| `mount` | 通过 PC 侧 WebDAV 桥把设备文件系统挂到默认文件管理器 |
| `config` | 生成默认 `.pyrite_config.json` |

### 项目命令组 `pyrcli project`

| 命令 | 作用 |
|------|------|
| `project new` | 交互式创建新项目 + 自动下载存根 |
| `project init` | 为已有项目下载 MicroPython 类型存根 |
| `project hash` | 离线扫描项目目录，计算 SHA256 哈希 |
| `project flash` | 根据哈希配置增量刷入新增/变更的文件 |
| `project status` | 比对本地哈希与设备文件，显示差异清单 |
| `project pull` | 从设备拉取文件到本地目录 |
| `project run` | 增量刷入项目后进入 REPL 监控 |

### 文件浏览命令组 `pyrcli fs`

| 命令 | 作用 |
|------|------|
| `fs ls` | 列出设备目录（支持 `-r` 递归、`--sort` 排序、`-p` 分页） |
| `fs rm` | 删除设备文件 |
| `fs cat` | 打印设备文本文件内容 |
| `fs put` | 上传本地文件到设备（支持编译 + 条件编译） |
| `fs get` | 从设备下载文件到本地 |

### 固件命令组 `pyrcli firmware`（需 esptool）

| 命令 | 作用 |
|------|------|
| `firmware flash` | 烧录固件 .bin |
| `firmware erase` | 擦除整个 Flash |
| `firmware info` | 读取芯片和 Flash 信息 |
| `firmware verify` | 验证固件烧录结果 |
| `firmware read` | 从 Flash 读取内容到文件 |

---

## 快速上手

```bash
# 查看可用设备
pyrcli scan
pyrcli scan -a                    # 显示所有设备（含无 VID/PID 的）
pyrcli scan -i                    # 连设备并显示板子信息
pyrcli scan --format json         # JSON 格式输出（适合脚本/CI）

# 获取设备详细信息
pyrcli board-info COM3
pyrcli board-info COM3 --baudrate 921600

# 创建项目（交互式选择硬件/版本）
pyrcli project new my-project
pyrcli project new my-project --platform COM3   # 串口自动检测

# 刷入文件（自动编译 .py → .mpy + 条件编译）
pyrcli flash COM3 main.py /main.py
pyrcli flash COM3 main.py /main.py -f wifi,esp32-s3   # 激活 feature

# 刷入整个目录
pyrcli flash-program COM3 . / --manifest manifest.py

# 增量刷入（基于哈希比对）
pyrcli project hash .                   # 先计算哈希
pyrcli project flash COM3 . /

# 查看设备文件
pyrcli fs ls COM3 /
pyrcli fs ls COM3 /lib -r              # 递归
pyrcli fs ls COM3 / --sort size        # 按大小排序
pyrcli fs ls COM3 / -p                 # 分页

# 用默认文件管理器浏览设备文件系统
pyrcli mount COM3
pyrcli mount COM3 --drive P            # Windows 指定盘符
pyrcli mount COM3 --readonly           # 只读挂载

# 上传/下载文件
pyrcli fs put COM3 local.py /remote.py
pyrcli fs get COM3 /remote.py backup.py

# 固件烧录
pyrcli firmware flash COM3 firmware.bin
pyrcli firmware info COM3

# 通过 WebREPL 连接（而非串口）
pyrcli flash COM3 main.py /main.py --ws ws://192.168.4.1:8266 --password mypass
pyrcli repl COM3 --ws ws://esp32.local:8266

# 打开交互式 REPL
pyrcli repl COM3
```

---

## 特性

**刷入链路** — 连接设备 → 预处理条件编译 → 编译 `.py` → `.mpy` → 分块传输写入

**自动适配** — 从设备读取字节码版本和架构，编译时传入正确的 `-march` 参数

**条件编译** — 通过 `@feature("wifi")` / `@target("esp32")` 宏控制哪些代码刷入设备，使用 libcst CST 转换（非正则）

**增量同步** — `project hash` 扫描本地文件计算 SHA256，`project flash` 仅刷入新增/变更的文件

**项目脚手架** — 交互式硬件/版本选择，多线程下载 `.pyi` 类型存根，自动配置 VS Code

**设备文件浏览器** — `fs ls/rm/cat/put/get` 完整文件操作，支持递归列表、排序、分页

**桌面文件管理器挂载** — `mount` 在 PC 侧启动 WebDAV 桥，Windows 映射盘符，Linux/macOS 打开默认文件管理器，无需固件 MTP 支持

**固件烧录** — 通过 esptool 子进程封装，支持烧录/擦除/验证/读取

**传输层抽象** — 支持串口 (pyserial) 和 WebREPL (WebSocket) 两种传输方式，所有命令均可通过 `--ws` 切换

**Manifest 清单系统** — 使用安全 AST 解析器（非 exec）控制刷入文件清单，支持 `remote` 路径重映射和 `features` 标签过滤

**Shell 自动补全** — 串口号 Tab 自动补全（`pyrcli flash <TAB>`）

**JSON 输出模式** — 部分命令支持 `--format json`，可集成到脚本/CI 流水线

**MSYS2 兼容** — 自动检测 Git Bash 路径转换，恢复原始设备路径，对claude code等工具友好

**统一日志系统** — 6 级日志、JSONL 文件记录、操作计时上下文管理器、串口/WebSocket 流量监控

---

## 条件编译示例

```python
@feature("wifi")
def connect_wifi():
    import network
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

with target("esp32-s3"):
    from machine import Pin
    rgb = Pin(48, Pin.OUT)
```

```bash
# 刷入时激活 wifi 和 esp32-s3 特性
pyrcli flash COM3 main.py /main.py -f wifi,esp32-s3
```

---

## 配置

项目根目录创建 `.pyrite_config.json`（从 CWD 向上逐级搜索）：

```json
{
  "chunk_size": 4096,
  "download_threads": 4,
  "auto_compile": true,
  "verify": "crc32",
  "max_retries": 2
}
```

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `chunk_size` | `4096` | 每次写入的最大数据量（字节） |
| `download_threads` | `4` | 存根下载并发数，范围 1–12 |
| `auto_compile` | `true` | 自动编译 .py → .mpy |
| `verify` | `"size"` | 校验模式: `off` / `size` / `crc32` |
| `max_retries` | `2` | 校验失败/断线重试次数 |

设备识别标签可在 `pyproject.toml` 中扩展：

```toml
[tool.pyrite.board_tags]
ESP32_S3 = ["ESP32", "wifi"]
```

---

## WebREPL

所有需要串口的命令均支持 `--ws` 参数切换为 WebREPL 连接：

```bash
# 通过 WebSocket 连接设备
pyrcli board-info COM3 --ws ws://192.168.4.1:8266 --password mypass
pyrcli flash COM3 main.py /main.py --ws ws://esp32.local:8266
```

WebREPL 传输基于 `websocket-client` 库，协议兼容 MicroPython 官方 webrepl 固件。

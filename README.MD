# ✦ Pyrite-CLI

MicroPython 设备串口工具集。通过 UART raw REPL 协议与设备通信，支持文件刷入、交互式 REPL、自动编译、条件编译、项目脚手架等。

---

## 安装

```bash
pip install pyrite-cli
```

依赖：`typer`、`pyserial`、`requests`、`tqdm`、`mpy-cross`、`libcst`

---

## 命令概览

| 命令 | 作用 |
|------|------|
| `scan` | 扫描串口设备，支持 VID/PID/关键字过滤 |
| `flash` | 刷入单个文件（自动编译 + 条件编译） |
| `flash-program` | 刷入整个目录，支持 manifest 控制清单 |
| `repl` | 交互式 MicroPython REPL（跨平台，历史记录） |
| `run` | 在设备上执行一段 Python 代码 |
| `reset` | 软重启设备 |
| `board-info` | 获取固件、CPU、内存、Flash 等详细板级信息 |
| `new` | 交互式创建新项目 + 自动下载存根 |
| `init` | 为已有项目初始化 MicroPython 存根 |
| `config` | 生成默认 `.pyrite_config.json` |

---

## 快速上手

```bash
# 查看可用设备
pyrcli scan

# 获取设备详细信息
pyrcli board-info COM3

# 创建项目（交互式选择硬件/版本）
pyrcli new my-project

# 或者通过串口自动检测创建
pyrcli new my-project --platform COM3

# 刷入文件（自动编译 .py → .mpy）
pyrcli flash COM3 main.py /main.py

# 刷入整个项目目录
pyrcli flash-program COM3 . / --manifest manifest.py

# 打开交互式 REPL
pyrcli repl COM3

# 执行单行代码
pyrcli run COM3 "import machine; print(machine.freq())"
```

---

## 特性

**刷入链路** — 连接设备 → 预处理条件编译 → 编译 `.py` → `.mpy` → 分块传输写入

**自动适配** — 从设备读取字节码版本和架构，编译时传入正确的 `-march` 参数

**条件编译** — 通过 `@feature("wifi")` / `@target("esp32")` 宏控制哪些代码刷入设备

**项目脚手架** — 交互式硬件/版本选择，多线程下载 `.pyi` 类型存根，自动配置 VS Code

**一键自动检测** — 连接设备即自动识别硬件型号和固件版本，匹配最佳存根

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

项目根目录创建 `.pyrite_config.json`：

```json
{
  "chunk_size": 4096,
  "download_threads": 4,
  "auto_compile": true
}
```

设备识别标签也可在 `pyproject.toml` 中扩展：

```toml
[tool.pyrite.board_tags]
ESP32_S3 = ["ESP32", "wifi"]
```

---

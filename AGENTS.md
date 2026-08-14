# AGENTS.md

## 性能优化取舍提醒

- 不要默认建议把设备端 `usb.read(min(64, ...))` 放大；64B 读取窗口是为兼容不同 MicroPython 板卡和 REPL stdin 行为保留的稳定性设计。
- 不要默认建议移除批量刷入中的 host 侧 `all_data = b"".join(...)` 合并；当前一次性连续数据流是为简化设备端协议和进度统计保留的设计。
- 不要默认建议缩短串口连接/DTR/RTS 固定等待；这些等待用于提高 ESP32/ESP8266 等板卡复位和 Raw REPL 初始化稳定性。

pyrite-cli 是一个 MicroPython 设备串口工具，通过 UART 原始 REPL 协议与设备通信，提供文件刷入、交互式 REPL、自动编译、条件编译、增量刷入、项目脚手架等功能。CLI 入口为 `pyrcli`（定义在 `pyproject.toml` 中 `cli.main:main`）。

## 测试与修复流程

编写或修改代码后，严格按照以下循环进行：

```
步骤 1 ── 用户提出问题/需求
步骤 2 ── 编写或运行首次失败测试（纯逻辑测试，无需硬件）
步骤 3 ── 添加调试日志（如打印变量、捕获 REPL 原始流量至 log/ 目录）
步骤 4 ── 第 N 次测试
          ├── 仍失败 → 回到步骤 3（分析日志，调整调试信息）
          └── 通过 → 进入步骤 5
步骤 5 ── 命令冒烟测试：确认模块导入、CLI --help、无设备命令均可正常执行
步骤 6 ── 实机验证（仅当用户要求刷入时）
```

### 测试分层

1. **纯逻辑测试**（无需硬件）：`pytest test/` — 协议解析、配置边界、CRC/SHA 计算、着色逻辑、manifest 解析、日志系统、JSON 输出（共 110+ 项）

2. **命令冒烟测试**（无需设备，修复语法/导入错误后必做）：
   - `python -c "from cli.main import app; from cli.utils.flash import MicroPython; from cli.utils.config import _load_config; from cli.utils.build import preprocess; from cli.utils.log import get_logger; from cli.utils.ui import print_json"` — 验证所有模块可正常导入
   - `pyrcli --help` — CLI 入口正常
  - `pyrcli flash --help` / `pyrcli debug --help` / `pyrcli debug board-info --help` / `pyrcli debug doctor --help` / `pyrcli fs --help` / `pyrcli project --help` / `pyrcli pkg --help` / `pyrcli monitor --help` / `pyrcli remount --help` — 各命令/子命令组正常
   - `pyrcli scan`（不需要设备，无设备时正常退出即可）
   - `pyrcli scan --version`

3. **实机验证**（仅当用户要求刷入时才执行，需 ESP32/其他设备）：
   - 基础连通：`pyrcli scan` → `pyrcli debug board-info COM3`
   - 刷入测试：`pyrcli flash COM3 <local> <remote>` → `pyrcli repl COM3` 后手动执行代码验证
   - 文件操作：`pyrcli fs ls/cat/put/get/rm COM3`
   - 批量操作：`pyrcli flash-program COM3 <dir> <prefix>`
   - 项目命令：`pyrcli project flash/status/pull/scan`

### 调试手段

- 统一日志系统自动写入 JSONL 文件（`./log/` 目录），含操作计时和流量监控
- 串口数据写日志时对控制字符做替换（`0x01`→`<RAW>`、`0x03`→`<C>`、`0x04`→`<D>` 等）
- 可使用 `python -c "from cli.utils.flash import MicroPython; ..."` 快速在实机复现
- 对于断线问题，在重试循环中检查 `self.is_connected` 并触发 `self.connect()`

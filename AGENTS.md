# AGENTS.md

## 性能优化取舍提醒

- 不要默认建议把设备端 `usb.read(min(64, ...))` 放大；64B 读取窗口是为兼容不同 MicroPython 板卡和 REPL stdin 行为保留的稳定性设计。
- 不要默认建议移除批量刷入中的 host 侧 `all_data = b"".join(...)` 合并；当前一次性连续数据流是为简化设备端协议和进度统计保留的设计。
- 不要默认建议缩短串口连接/DTR/RTS 固定等待；这些等待用于提高 ESP32/ESP8266 等板卡复位和 Raw REPL 初始化稳定性。

pyrite-cli 是一个 MicroPython 设备串口工具，通过 UART 原始 REPL 协议与设备通信，提供文件刷入、交互式 REPL、自动编译、条件编译、增量刷入、项目脚手架等功能。CLI 入口为 `pyrcli`（定义在 `pyproject.toml` 中 `cli.main:main`）。

## 环境

```powershell
# 开发安装
pip install -e .

# 运行测试（仅跑纯逻辑测试，不连设备）
pytest test/test_protocol_helpers.py test/test_flash_utils.py test/test_config.py test/test_manifest_loader.py test/test_logger.py test/test_output.py -v

# 实机测试 — 连上 ESP32/其他 MicroPython 设备
pyrcli scan
pyrcli board-info COM3
pyrcli flash COM3 main.py /main.py
pyrcli run COM3 "print('hello')"
pyrcli repl COM3
pyrcli fs ls COM3 /
pyrcli fs cat COM3 /main.py
pyrcli fs put COM3 local.py /remote.py
pyrcli fs get COM3 /remote.py local_copy.py
pyrcli fs rm COM3 /remote.py

# 批量和项目命令
pyrcli flash-program COM3 src/ /app
pyrcli project flash COM3 src/ /app
pyrcli project status COM3 src/ /app
pyrcli project pull COM3 src/ /app
pyrcli project scan src/

# fs ls 增强功能
pyrcli fs ls COM3 / -r              # 递归列出
pyrcli fs ls COM3 / --sort size      # 按文件大小排序
pyrcli fs ls COM3 / -p              # 分页显示

# 反向挂载（设备端通过 /remote 访问上位机目录，委托 mpremote）
pyrcli remount COM3 .
pyrcli remount COM3 src/ --unsafe-links

# 包安装与 GPIO 监控
pyrcli pkg install COM3 aioble --target /lib --dry-run
pyrcli pkg install COM3 aioble --target /lib
pyrcli monitor COM3 --pins 0,2,4,5 --count 20

# 构建
pip install build
python -m build
```

## 架构

```
cli/
  main.py                  # Typer CLI 入口 — scan, flash, flash-program, repl,
                           # run, reset, board-info, new, init, config
                           # 子命令组: project, fs, pkg
                           # 顶层命令: mount, remount, monitor
                           # 含 MSYS2 路径修复 _norm_path()、串口自动补全
  utils/
    flash/                 # MicroPython 串口类与刷入核心包
      core.py              # 原始 REPL、文件刷入/校验、批量刷入、设备文件浏览器、
                           # bytes 协议下载、递归 fs_ls_recursive
      flash.py             # 命令可触达的公开 facade
      mp_scripts/          # 设备端刷入脚本
    transport/             # 传输层包
      base.py              # Transport 抽象基类（ABC）
      serial.py            # pyserial 串口传输实现 SerialTransport
      webrepl.py           # WebSocket WebREPL 传输实现 WebREPLTransport
    serial_transport.py    # 旧导入兼容层
    webrepl_transport.py   # 旧导入兼容层
    webrepl_micropython.py # WebREPLMicroPython — 通过 WebREPL 连接设备的
                           # MicroPython 子类（继承所有高级操作）
    pkg.py                 # mpremote mip install/cache/install-offline 计划与执行
    monitor.py             # GPIO 监控参数解析、采样脚本和 host 侧轮询
    config.py              # 配置加载 _load_config()、create_default_config()
                           # 常量: CONFIG_FILE, DEFAULT_CHUNK_SIZE 等
    types.py               # PyriteConfig 数据类定义
    compiler.py            # _compile_to_mpy() / _compile_files_parallel()
                           # mpy-cross Python API 封装
    ansi.py                # ANSI 颜色常量 _GREEN, _YELLOW, _RED, _RESET
    log.py                 # 统一日志系统 — 6 级日志、JSONL 文件、操作计时、流量监控
    logger.py              # 日志兼容层 — 重导出 log.py（旧 import 无需改动）
    output.py              # 输出工具 — JSON 格式输出、TTY 检测
    preprocessor.py        # 条件编译 — libcst CST 转换 @feature/@target
    manifest_loader.py     # 安全 manifest.py 解析器（AST 非 exec）
    selector.py            # 交互式终端选择 UI（键盘导航）
  project/
    project.py             # 项目脚手架、交互式硬件选择、自动检测
    stubs.py               # GitHub API 存根下载、多线程、VS Code 配置
    sync.py                # ProjectSyncManager — 哈希增量刷入、状态比对、
                           # 文件拉取（封装设备无关的项目逻辑）
test/
  test_protocol_helpers.py # 尾部标记剥离测试（8 个）
  test_flash_utils.py      # REPL 着色、CRC32、文件哈希测试（26 个）
  test_config.py           # 配置加载边界值测试（18 个）
  test_manifest_loader.py  # manifest 解析器测试（3 个）
  test_logger.py           # 统一日志系统测试（359 行）
  test_output.py           # JSON 输出/TTY 检测测试（303 行）
```

## 通信协议

- **原始 REPL 模式** (`Ctrl+A` = `0x01`)：在设备上执行 Python，捕获 stdout
- **单文件刷入**：注入 FLASH 脚本（设备端用 `sys.stdin.buffer.read()` 循环读取），PC 端逐块发送文件二进制
- **批量刷入**：一次注入 FLASH_PROGRAM 脚本 + 连续数据流，设备按 `[(size, path), ...]` 分文件写入
- **Bytes 下载协议**（两阶段）：先通过 `run()` 获取文件大小，再在 Raw REPL 中执行字节输出脚本并用 `\x04` 确认执行，PC 端按已知大小直接接收
- **REPL 尾部标记**：设备返回以 `\x04\x04>`、`\x04\x04` 或 `\x04` 结尾，`_strip_repl_trailer` 负责去除

## 关键模式

- `MicroPython`（串口）和 `WebREPLMicroPython`（WebSocket）均支持上下文管理器
- 每次设备操作流程：connect → enter raw repl → operate → exit raw repl → disconnect
- 统一日志系统 (`cli/utils/log.py`) 提供 6 级日志、JSONL 文件记录、操作计时、串口/WebSocket 流量监控
- `Logger.operation()` 上下文管理器自动记录开始/结束/耗时/成败
- 流量数据写入 JSONL 文件（`type` 字段为 `"traffic"`）用于调试
- `kbd_intr(-1)` 在刷入前设置，防止数据流中的 `0x03` 字节触发设备重启
- `fs_ls` 发送 `os.stat(p)` **执行两次** — 某些 MicroPython 板子首次调用可能返回过期数据
- `_norm_path()` 检测 MSYS2 路径转换并恢复原始设备路径

## 配置系统

从 `.pyrite_config.json` 加载（从 CWD 向上逐级搜索），并与 `pyproject.toml` 的 `[tool.pyrite.board_tags]` 合并。

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `chunk_size` | `4096` | 每次写入的最大数据量（字节） |
| `download_threads` | `4` | 存根下载并发数，范围 1–12 |
| `auto_compile` | `true` | 自动编译 .py → .mpy |
| `verify` | `"size"` | 校验模式: `off`/`size`/`crc32` |
| `max_retries` | `2` | 校验失败/断线重试次数 |

`pyproject.toml` 补充 board_tags 示例：

```toml
[tool.pyrite.board_tags]
ESP32_S3 = ["ESP32", "wifi"]
C3 = ["ESP32", "wifi"]
```

## 条件编译

用 `libcst`（CST 级转换，非正则）将 `@feature("name")`、`@target("esp32")` 装饰器和 `with feature()/with target()` 上下文管理器重写为 `if True:` / `if False:` 块。包含静态分析，警告对被禁用函数的裸调用。

## Manifest 系统

`manifest.py` 使用 `ast` 安全解析器（非 `exec`），仅允许 `module()` 和 `package()` 调用，且参数必须为字面量。支持 `remote`（路径重映射）和 `features`（标签过滤）关键字参数。有限制：最大嵌套深度 15 层、最多 500 条目。

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
   - `python -c "from cli.main import app; from cli.utils.flash import MicroPython; from cli.utils.config import _load_config; from cli.utils.preprocessor import preprocess; from cli.utils.log import get_logger; from cli.utils.output import print_json"` — 验证所有模块可正常导入
   - `pyrcli --help` — CLI 入口正常
  - `pyrcli flash --help` / `pyrcli fs --help` / `pyrcli project --help` / `pyrcli pkg --help` / `pyrcli monitor --help` / `pyrcli remount --help` — 各命令/子命令组正常
   - `pyrcli scan`（不需要设备，无设备时正常退出即可）
   - `pyrcli scan --version`

3. **实机验证**（仅当用户要求刷入时才执行，需 ESP32/其他设备）：
   - 基础连通：`pyrcli scan` → `pyrcli board-info COM3`
   - 刷入测试：`pyrcli flash COM3 <local> <remote>` → `pyrcli run COM3 "..."` 验证
   - 文件操作：`pyrcli fs ls/cat/put/get/rm COM3`
   - 批量操作：`pyrcli flash-program COM3 <dir> <prefix>`
   - 项目命令：`pyrcli project flash/status/pull/scan`

### 调试手段

- 统一日志系统自动写入 JSONL 文件（`./log/` 目录），含操作计时和流量监控
- 串口数据写日志时对控制字符做替换（`0x01`→`<RAW>`、`0x03`→`<C>`、`0x04`→`<D>` 等）
- 可使用 `python -c "from cli.utils.flash import MicroPython; ..."` 快速在实机复现
- 对于断线问题，在重试循环中检查 `self.is_connected` 并触发 `self.connect()`

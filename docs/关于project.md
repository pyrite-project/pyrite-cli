# `cli/project/` — MicroPython 项目初始化与类型存根

提供 MicroPython 项目的脚手架创建和类型存根（`.pyi`）下载功能，支持交互式选择和串口自动检测。

---

## 模块结构

```
cli/
├── project/
│   ├── __init__.py        # 空（无导出）
│   ├── project.py         # 高层接口：init_project / init_stubs / new_project_interactive
│   ├── stubs.py           # 存根查询、匹配、下载（多线程）、VS Code 配置
│   └── feature_stub.pyi   # 预处理器 feature/target 的 .pyi 存根
└── utils/
    ├── selector.py        # 交互式选择列表（上下键导航）
    ├── preprocessor.py    # 条件编译宏预处理器（libcst AST 转换）
    └── manifest_loader.py # manifest.py 加载器
```

### `__init__.py`

空文件，无导出。`cli.project` 的公开符号通过 `project.py`（`from .stubs import *`）引入。

---

## `project.py` — 高层接口

### `init_project(proj_name: str)`

创建新 MicroPython 项目目录。

| 参数 | 说明 |
|------|------|
| `proj_name` | 项目目录名称，会在当前工作目录下创建同名文件夹 |

创建后自动执行：
1. `os.mkdir(proj_name)` 创建目录
2. 复制 `feature_stub.pyi` → `{proj_name}/feature_stub.pyi`（预处理器类型提示）
3. 写入 `{proj_name}/manifest.py` 模板

### `detect_device_info(port, baudrate=115200, timeout=10) -> tuple[str, str]`

通过串口连接设备并自动检测硬件类型和固件版本。

| 参数 | 说明 |
|------|------|
| `port` | 串口号，如 `COM3` 或 `/dev/ttyUSB0` |

- 连接设备后执行 `import sys;print(sys.version);print(sys.platform)`
- 返回 `(hardware, version)` 元组，如 `('esp32', '1.22.2')`
- 引发: `RuntimeError` — 连接失败或输出解析失败

### `new_project_interactive(proj_name: str, platform: str | None = None)`

交互式创建新 MicroPython 项目并下载存根。推荐入口（被 `pyrcli new` 命令调用）。

**自动检测模式**（指定 `--platform` 参数时）：
1. 调用 `init_project()` 创建目录
2. 通过 `detect_device_info()` 自动读取硬件和版本
3. 查询可用存根，自动匹配并下载
4. 如未精确匹配，尝试最接近版本
5. 配置 VS Code

**交互式选择模式**（未指定 `--platform` 时）：
1. 调用 `init_project()` 创建目录
2. 从 GitHub API 获取可用硬件列表 → 用户键盘选择
3. 根据所选硬件筛选版本 → 用户选择
4. 若有变体（如 `ESP32_GENERIC`）→ 用户选择
5. 下载存根 + VS Code 配置

### `init_stubs(hardware=None, version=None, variant=None, platform=None)`

在已创建的项目中初始化 MicroPython 类型存根。

| 参数 | 必填 | 说明 |
|------|------|------|
| `hardware` | 否 | 硬件类型，如 `esp32`、`rp2`（使用 `--platform` 时可省略） |
| `version` | 否 | 固件版本，如 `1.20.0`（使用 `--platform` 时可省略） |
| `variant` | 否 | 具体硬件变体，如 `ESP32_GENERIC`、`PICO_W` |
| `platform` | 否 | 串口号，自动检测硬件并下载对应存根 |

完整流程：
1. 如指定 `platform`，调用 `detect_device_info()` 自动检测硬件和版本
2. 调用 `list_stub_dirs()` 从 GitHub API 获取所有可用存根目录列表
3. 调用 `find_stub_dir()` 查找最匹配的存根目录
4. 查找失败时尝试 `_find_nearest_version()` 匹配最接近的版本
5. 调用 `download_stubs()` 下载该目录下的所有 `.pyi` 文件（多线程）
6. 调用 `create_vscode_config()` 配置 VS Code Pylance 类型检查

### 内部辅助函数

#### `_get_versions_for_hardware(dirs, hardware) -> list[str]`

提取指定硬件类型的可用固件版本，按版本号降序排列。

#### `_get_variants_for_hw_version(dirs, hardware, version) -> list[str]`

提取特定硬件 + 版本组合对应的可用固件变体列表。

#### `_find_nearest_version(target, available) -> str | None`

在可用版本列表中查找与目标版本最接近的版本（仅限同主版本号，最小绝对差）。

---

## `stubs.py` — 存根查询、匹配、下载

### 常量

| 常量 | 值 | 说明 |
|------|-----|------|
| `SOURCE` | `"https://api.github.com/repos/josverl/micropython-stubs"` | 上游存根仓库 API 地址 |
| `VSCODE_DIR` | `".vscode"` | VS Code 配置目录名 |
| `VSCODE_SETTINGS` | `"settings.json"` | VS Code 配置文件名 |

### 工具函数

#### `version_to_dir(v: str) -> str`

将版本号字符串转换为 GitHub 仓库中的目录名格式。

| 输入 | 输出 |
|------|------|
| `"1.20.0"` | `"v1_20_0"` |
| `"1.19.1"` | `"v1_19_1"` |

#### `_get_download_threads() -> int`

从 `.pyrite_config.json` 读取并发下载线程数，范围 1–12，默认 4。

#### `_request_with_retry(url, max_retries=3, **kwargs)`

带重试的 HTTP GET 请求，封装了统一的错误处理策略。

**重试策略：**

| 场景 | 行为 |
|------|------|
| 连接错误 / 超时 | 指数退避重试（1s, 2s, 4s），最多 3 次 |
| HTTP 5xx（服务器错误） | 指数退避重试，最多 3 次 |
| HTTP 403（API 速率限制） | 立即退出，不重试 |
| 其他 HTTP 错误 | 直接抛出异常，不重试 |

### 存根列表与查询

#### `list_stub_dirs() -> list[str]`

从 `josverl/micropython-stubs` 仓库的 `stubs/` 目录下，列出所有可用存根目录名。

- 内部调用 `_request_with_retry()` 进行带重试的 API 请求
- 返回目录名列表，如 `["micropython-v1_20_0-esp32", "micropython-v1_20_0-esp32-ESP32_GENERIC", ...]`
- **注意**：GitHub API 存在速率限制（未认证 60 req/h），频繁调用会触发 403

#### `get_hardware_types(dirs: list[str]) -> set[str]`

从存根目录名列表中提取所有可用的硬件类型。

- 解析规则：以 `micropython-v` 开头的目录名，取第三个 `-` 分隔段作为硬件类型
- 如 `micropython-v1_20_0-esp32` → `esp32`，`micropython-v1_20_0-rp2` → `rp2`

#### `list_all_hardware(dirs: list[str]) -> None`

打印所有可用的 MicroPython 硬件类型列表。

#### `list_available(dirs: list[str], hardware: str) -> None`

打印指定硬件的所有可用存根。

- 匹配规则：目录名中包含 `-{hardware}` 或以 `{hardware}` 结尾
- 如果无匹配，显示所有可用的硬件类型供用户参考

### 存根目录匹配

#### `find_stub_dir(dirs, hardware, version, variant=None) -> str | None`

查找与用户指定条件最匹配的存根目录名。

**匹配优先级（从高到低）：**

1. **精确匹配**：`micropython-{vdir}-{hardware}`（无 variant）或 `micropython-{vdir}-{hardware}-{variant}`（有 variant）
2. **Merged 变体**：`{exact}-merged`
3. **模糊匹配**：以精确匹配前缀开头的任意目录，取字典序第一个

**示例：**

| hardware | version | variant | 匹配目录 |
|----------|---------|---------|----------|
| `esp32` | `1.20.0` | — | `micropython-v1_20_0-esp32` |
| `esp32` | `1.20.0` | `ESP32_GENERIC` | `micropython-v1_20_0-esp32-ESP32_GENERIC` |
| `rp2` | `1.19.1` | — | `micropython-v1_19_1-rp2` |

### 存根下载

#### `download_stubs(stub_dir, output_dir, max_workers=None) -> tuple[int, Path]`

下载指定存根目录中的所有 `.pyi` 文件（多线程）。

| 参数 | 说明 |
|------|------|
| `stub_dir` | 上游仓库中的存根目录名 |
| `output_dir` | 本地输出目录（存根文件保存在其子目录中） |
| `max_workers` | 下载线程数，`None` 时从 `.pyrite_config.json` 读取 |

**流程：**
1. 调用 `_request_with_retry()` 获取目录清单（GitHub API）
2. 在 `output_dir/stub_dir` 下创建本地目录
3. 筛选出所有 `.pyi` 文件
4. 使用 `ThreadPoolExecutor` 多线程并发下载
5. 支持 `tqdm` 进度条（可选依赖）

返回 `(下载文件数, 输出路径)`。

### VS Code 配置

#### `create_vscode_config(stub_path, hardware, version) -> Path`

在项目根目录创建或更新 `.vscode/settings.json`，配置 Pylance 类型检查指向下载的存根。

**配置项：**

```json
{
  "python.analysis.extraPaths": ["./micropython-v1_20_0-esp32"],
  "python.languageServer": "Pylance",
  "python.analysis.typeCheckingMode": "basic",
  "python.analysis.stubPath": "."
}
```

- 如果文件已存在，会读取现有配置并追加，不会覆盖已有设置
- 自动创建 `.vscode/` 目录（如果不存在）

### 独立入口

`stubs.py` 也支持作为独立脚本运行（通过 `argparse` 解析参数），在 CLI 集成之前使用。目前功能与 `init_stubs` 基本相同，但通过 `pyrcli init` 集成后推荐使用 CLI 入口。

---

## `utils/selector.py` — 交互式选择列表

终端中的键盘导航选择器，被 `new_project_interactive()` 用于硬件/版本选择。

### `interactive_select(options: list[str], title: str) -> str`

显示可滚动的交互式选择列表。

- 上下键导航，Enter 确认，Ctrl+C 退出
- 全角/半角字符对齐（CJK 宽度感知）
- 窗口式边框显示
- 唯一选项时自动选中

### `_get_key() -> str | None`

跨平台单键读取，返回标准化键名（`"up"`、`"down"`、`"enter"`、`None`）。

### `_display_width(s: str) -> int`

返回字符串在终端中的显示列宽，CJK 全角字符占 2 列。

---

## `utils/preprocessor.py` — 条件编译宏预处理器

基于 libcst 的 AST 转换工具，支持 `feature()` / `target()` 宏的条件编译，被 `flash_file()` 在刷入前调用。

### 支持的宏语法

```python
# 函数装饰器：当 tags 匹配时保留该函数
@feature("wifi")
def connect_wifi():
    import network
    ...

# with 语句块：当 tags 匹配时保留该块
with target("ESP32"):
    from machine import Pin
    esp32_specific()
```

### `preprocess(source, active_tags, filename="") -> str`

对源代码执行条件编译转换。

| 参数 | 说明 |
|------|------|
| `source` | 源代码字符串 |
| `active_tags` | 当前活跃的 tags 集合 |
| `filename` | 文件名（用于警告输出） |

转换规则：
- `@feature("x")` / `@target("x")` 装饰的函数：tags 不匹配时包裹在 `if False:` 中
- `with feature("x"):` / `with target("x"):` 语句块：tags 不匹配时转为 `if False:`
- 同时输出警告：裸调用未匹配函数可能在运行时引发 `NameError`

### 内部类

- `_Transformer` — libcst CSTTransformer，执行 AST 转换（`with` → `if`，装饰器 → `if False`）
- `_Analyzer` — libcst CSTVisitor，静态分析调用关系，检测潜在运行时错误

---

## `utils/manifest_loader.py` — manifest.py 加载器

解析 manifest.py 文件，根据活跃 tags 筛选需要刷入的文件。

### `load_manifest(manifest_path, active_tags, base_dir=None) -> list[tuple[str, str]]`

| 参数 | 说明 |
|------|------|
| `manifest_path` | manifest.py 文件路径 |
| `active_tags` | 活跃 tags 集合 |
| `base_dir` | 文件路径基准目录（默认 manifest.py 所在目录） |

manifest.py 中支持两条 DSL 指令：

```python
# module: 单个文件，可选 features 控制
module("main.py")
module("lib/utils.py", features=["wifi"])

# package: 整个目录递归
package("lib")
```

- `features` 为空或不提供时始终匹配
- `features` 非空时与 `active_tags` 有交集即匹配
- `package` 递归添加目录下所有 `.py` 文件

---

## 数据流

```
pyrcli new <name> [--platform COM3]
  └── new_project_interactive(name, platform)
        ├── init_project(name)
        │     ├── os.mkdir(name)
        │     ├── copy feature_stub.pyi
        │     └── create manifest.py
        │
        ├── [--platform 模式]
        │     ├── detect_device_info(port) → (hardware, version)
        │     ├── list_stub_dirs()
        │     ├── find_stub_dir(dirs, hardware, version)
        │     ├── download_stubs(stub_dir, '')
        │     └── create_vscode_config(...)
        │
        └── [交互模式]
              ├── list_stub_dirs()
              ├── interactive_select(hardware)
              ├── interactive_select(version)
              ├── [可选] interactive_select(variant)
              ├── download_stubs(stub_dir, '')
              └── create_vscode_config(...)

pyrcli init <hardware> <version> [--variant <V>] [--platform COM3]
  └── init_stubs(hardware, version, variant, platform)
        ├── [--platform] detect_device_info(port) → auto-detect
        ├── list_stub_dirs()
        ├── find_stub_dir(...) → 最佳匹配
        ├── [_find_nearest_version()] → 降级匹配
        ├── download_stubs(最佳匹配, "")  (多线程 ThreadPoolExecutor)
        └── create_vscode_config(存根路径, ...)

文件刷入时的条件编译:
  flash_file(local_path, remote_path, active_tags=...)
    ├── preprocessor.py:  预处理 @feature/@target 宏
    ├── _compile_to_mpy:  编译 .py → .mpy
    └── FLASH 模板脚本:   设备端写入
```

## 错误处理

| 场景 | 表现 |
|------|------|
| GitHub API 速率限制（403） | 提示用户稍后重试，进程退出 |
| 网络故障 / 超时 | 自动重试最多 3 次（指数退避），3 次后抛出异常 |
| 未找到匹配的存根 | 尝试最接近版本；仍失败则打印预期模式 + 列出可用存根 |
| 设备连接失败 | 提示失败信息，项目目录已创建可稍后手动配置 |
| `requests` 库未安装 | 提示 `pip install requests`，进程退出 |
| `tqdm` 未安装 | 退化为简单文件列表输出（多线程下载不受影响） |
| VS Code 配置 JSON 损坏 | 覆盖写入（警告提示） |
| `libcst` 未安装 | 条件编译功能不可用 |

## 依赖

- **`requests`**（必需）— GitHub REST API 调用与文件下载
- **`tqdm`**（可选）— 下载进度条，缺失时退化为逐文件输出
- **`libcst`**（必需）— 条件编译宏预处理器的 AST 解析基础

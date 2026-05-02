# `cli/project/` — MicroPython 项目初始化与类型存根

提供 MicroPython 项目的脚手架创建和类型存根（`.pyi`）下载功能。

---

## 模块结构

```
cli/project/
        ├── __init__.py       # 重新导出 stubs 模块的全部公开 API
        ├── project.py        # 高层接口：init_project / init_stubs
        └── stubs.py          # 存根查询、匹配、下载、VS Code 配置
```

### `__init__.py`

空初始化文件，执行 `from .stubs import *`，将 `stubs.py` 中的所有公开符号暴露到 `cli.project` 包级别。

---

## `project.py` — 高层接口

### `init_project(proj_name: str)`

创建新 MicroPython 项目目录。

| 参数 | 说明 |
|------|------|
| `proj_name` | 项目目录名称，会在当前工作目录下创建同名文件夹 |

实现：直接调用 `os.mkdir(proj_name)`。

### `init_stubs(hardware, version, variant=None)`

在已创建的项目中初始化 MicroPython 类型存根。

完整流程：

1. 调用 `list_stub_dirs()` 从 GitHub API 获取所有可用存根目录列表
2. 调用 `find_stub_dir()` 查找最匹配的存根目录
3. 调用 `download_stubs()` 下载该目录下的所有 `.pyi` 文件
4. 调用 `create_vscode_config()` 配置 VS Code Pylance 类型检查

| 参数 | 必填 | 说明 |
|------|------|------|
| `hardware` | 是 | 硬件类型，如 `esp32`、`rp2`、`stm32` |
| `version` | 是 | 固件版本，如 `1.20.0`、`1.19.1` |
| `variant` | 否 | 具体硬件变体，如 `ESP32_GENERIC`、`PICO_W` |

查找失败时，会打印预期目录模式并列出匹配该硬件的所有可用存根，然后退出。

---

## `stubs.py` — 存根查询、匹配、下载

### 常量

| 常量 | 值 | 说明 |
|------|-----|------|
| `API_BASE` | `"https://api.github.com/repos/josverl/micropython-stubs"` | 上游存根仓库 API 地址 |
| `VSCODE_DIR` | `".vscode"` | VS Code 配置目录名 |
| `VSCODE_SETTINGS` | `"settings.json"` | VS Code 配置文件名 |

### 工具函数

#### `version_to_dir(v: str) -> str`

将版本号字符串转换为 GitHub 仓库中的目录名格式。

| 输入 | 输出 |
|------|------|
| `"1.20.0"` | `"v1_20_0"` |
| `"1.19.1"` | `"v1_19_1"` |

#### `_request_with_retry(url, max_retries=3, **kwargs)`

带重试的 HTTP GET 请求，封装了统一的错误处理策略。

**重试策略：**

| 场景 | 行为 |
|------|------|
| 连接错误 / 超时 | 指数退避重试（1s, 2s, 4s），最多 3 次 |
| HTTP 5xx（服务器错误） | 指数退避重试，最多 3 次 |
| HTTP 403（API 速率限制） | 立即退出，不重试 |
| 其他 HTTP 错误 | 直接抛出异常，不重试 |

每次重试前打印提示信息，帮助用户了解当前状态。

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

#### `download_stubs(stub_dir, output_dir) -> tuple[int, Path]`

下载指定存根目录中的所有 `.pyi` 文件。

**流程：**

1. 调用 `_request_with_retry()` 获取目录清单（GitHub API）
2. 在 `output_dir/stub_dir` 下创建本地目录
3. 筛选出所有 `.pyi` 文件
4. 逐个下载（使用 `_request_with_retry()`，直接请求 GitHub raw URL）
5. 支持 `tqdm` 进度条（可选依赖）

| 参数 | 说明 |
|------|------|
| `stub_dir` | 上游仓库中的存根目录名 |
| `output_dir` | 本地输出目录（存根文件保存在其子目录中） |

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

### `stubs.main()` — 独立入口

`stubs.py` 也支持作为独立脚本运行（通过 `argparse` 解析参数），在 CLI 集成之前使用。目前功能与 `init_stubs` 基本相同，但通过 `pyrcli init` 集成后，推荐使用 CLI 入口。

---

## 数据流

```
pyrcli new <name>
  └── init_project(name) → os.mkdir(name)

pyrcli init <hardware> <version> [--variant <V>]
  └── init_stubs(hardware, version, variant)
        ├── list_stub_dirs()
        │     └── _request_with_retry(GitHub API) → 目录名列表
        ├── find_stub_dir(列表, hardware, version, variant) → 最佳匹配
        ├── download_stubs(最佳匹配, "")
        │     ├── _request_with_retry(GitHub API) → 文件清单
        │     └── 对每个 .pyi 文件:
        │           _request_with_retry(GitHub raw URL) → 下载内容
        └── create_vscode_config(存根路径, ...)
              └── 写入 .vscode/settings.json
```

## 错误处理

| 场景 | 表现 |
|------|------|
| GitHub API 速率限制（403） | 提示用户稍后重试，进程退出 |
| 网络故障 / 超时 | 自动重试最多 3 次（指数退避），3 次后抛出异常 |
| 未找到匹配的存根 | 打印预期目录模式 + 列出该硬件的所有可用存根 |
| `requests` 库未安装 | 提示 `pip install requests`，进程退出 |
| `tqdm` 未安装 | 退化为简单文件列表输出，功能不受影响 |
| VS Code 配置 JSON 损坏 | 覆盖写入（警告提示） |

## 依赖

- **`requests`**（必需）— GitHub REST API 调用与文件下载
- **`tqdm`**（可选）— 下载进度条，缺失时退化为逐文件输出

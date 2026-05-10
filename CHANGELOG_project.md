# project 命令组 — 统一项目脚手架与增量刷入

## 变更文件

### `cli/main.py`

原有顶层 `new`、`init` 命令保留为向后兼容包装器，同时迁移到 `project` 子命令组。

### 新增命令结构

```
pyrcli project
├── new     — 创建新 MicroPython 项目（原 pyrcli new）
├── init    — 初始化存根（原 pyrcli init）
├── hash    — 扫描项目，记录文件 SHA256 哈希
└── flash   — 根据哈希配置，增量刷入变更文件
```

顶层兼容命令依然可用（但推荐改用 `project` 子命令）：
- `pyrcli new ...` → 转发到 `project new`
- `pyrcli init ...` → 转发到 `project init`

### `cli/utils/Flash.py`

`MicroPython` 类新增 4 个方法（未修改任何已有方法）：

| 方法 | 类型 | 说明 |
|------|------|------|
| `_compute_file_hash()` | static | SHA256 文件哈希（1MB 分块读取） |
| `_collect_project_files()` | instance | 收集可刷入文件列表（manifest/目录递归，过滤 manifest.py/.pyi） |
| `project_scan()` | instance | 扫描目录 → 计算哈希 → 写入 `pyrite_file_config.json`（无需串口） |
| `project_flash()` | instance | 加载哈希配置 → 比对哈希 → 逐个刷入变更文件 → 更新配置 |

### 哈希配置文件格式 (`pyrite_file_config.json`)

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

## 使用示例

```bash
# 创建项目
pyrcli project new my-project

# 初始化存根
pyrcli project init esp32 1.24.0

# 记录哈希
pyrcli project hash . -f wifi

# 增量刷入
pyrcli project flash COM3 . /
```

## 测试结果（COM3, ESP32 设备）

| 场景 | 结果 |
|------|------|
| `project hash` 扫描项目 | 通过 |
| 新增文件 → `project flash` 检测并刷入 | 通过 |
| 无变更 → `project flash` 跳过 | 通过 |

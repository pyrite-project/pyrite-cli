# pyrite-cli 创新功能想法

本文档整理 pyrite-cli v0.0.4可以探索的产品方向和新功能。重点不放在简单压榨刷入速度上，而是围绕 MicroPython 设备开发中的真实痛点：设备差异大、失败难复现、文件系统不可见、刷入风险高、开发循环割裂。

## 更新方案一览

### 1. `pyrcli debug doctor` 设备诊断报告 (已完成)

### 2. 串口 Flight Recorder

目标：把一次设备操作中的关键事件、Raw REPL 收发、耗时、错误点记录成可分享、可分析、可回放的 trace 文件。

命令草案：

```bash
pyrcli flash COM3 src/main.py /main.py --trace
pyrcli trace view log/2026-xx-xx.pyrite-trace
pyrcli trace summarize log/2026-xx-xx.pyrite-trace
```

建议记录内容：

- 操作类型：`flash`、`flash-program`、`fs get`、`repl` 等。
- 设备端口、连接参数、配置摘要。
- Raw REPL 进入、脚本注入、数据传输、校验、退出等阶段。
- 串口/WebSocket 流量摘要，而不是无限制保存全部原始二进制。
- 控制字符可读化，例如 `<RAW>`、`<C>`、`<D>`。
- 失败时的最后 N 条收发记录和异常栈。

实现切入点：

- 扩展现有 `cli/utils/log.py` 的 traffic JSONL 能力。
- 新增 trace schema，例如 `type=trace_event`、`session_id`、`phase`。
- 增加 `cli/utils/trace.py` 做 trace 读取、摘要和脱敏。
- 后续可把真实 trace 转换成协议解析回归测试输入。

创新点：

- 这个功能不仅是日志，而是“问题样本格式”。
- 用户提交 bug 时可以附 trace，维护者不用猜测设备返回了什么。
- 长期可以积累不同板卡的兼容性样本。

### 3. `pyrcli project dev` Watch Mode (已完成)

### 4. 远端异常映射到本地源码

目标：捕获 MicroPython traceback 后，把设备路径映射回本地文件路径。

命令草案：

```bash
pyrcli repl COM3 --map-traceback
pyrcli project dev COM3 ./ / --map-traceback
```

示例效果：

```text
/lib/sensor.py:24 -> src/lib/sensor.py:24
NameError: name 'Pin' isn't defined
```

实现思路：

- 利用项目同步 manifest 或 Device Shadow FS 建立 remote -> local 映射。
- 对 traceback 行做结构化解析。
- 如果刷入的是 `.mpy`，提示对应源文件，但不假装能还原精确运行栈。
- 如果 报错文件是经过manifest后的，需要按照命令（或者自动扫描）给出的设备名称重新定位（全部按照re走）

### 5. Board Profile 与设备别名

目标：为常用开发板生成稳定身份和推荐配置。

命令草案：

```bash
pyrcli board register COM3 --name lab-esp32
pyrcli repl @lab-esp32
pyrcli project flash @lab-esp32
```

本地配置示例：

```json
{
  "name": "lab-esp32",
  "port": "COM3",
  "tags": ["ESP32", "wifi"],
  "last_firmware": "MicroPython v1.xx",
  "recommended": {
    "verify": "size",
    "chunk_size": 4096
  }
}
```

价值：

- 减少反复输入串口号。
- 和条件编译、manifest features、watch mode 形成联动。
- 可为不同板卡保存不同的同步策略。

### 6. Manifest Lockfile

目标：让项目刷入结果可复现，适合团队协作和教学材料。

命令草案：

```bash
pyrcli manifest lock
pyrcli manifest plan --profile esp32_s3
pyrcli project flash COM3 --locked
```

锁定内容：

- `manifest.py` 解析后的模块列表。
- `remote` 路径重映射结果。
- `features` 过滤结果。
- mip 包、GitHub 包、离线包版本。
- `.py` 到 `.mpy` 的构建设置。

实现切入点：

- 复用 `cli/utils/manifest_loader.py` 的安全 AST 解析。
- 新增 `pyrite.lock`，格式优先选择 JSON，方便测试和工具读取。
- `--locked` 模式下如果 manifest 变化但 lockfile 未更新，则报错。

### 7. 设备端 Mini Test Runner

目标：补齐 host 纯逻辑测试和实机验证之间的空白。

命令草案：

```bash
pyrcli test COM3
pyrcli test COM3 test_device/test_sensor.py
pyrcli test COM3 --keep-files
```

建议约定：

- 默认读取 `test_device/`。
- 临时上传测试文件到 `/.pyrite_tests/`。
- 支持简单的 `assert`、stdout 捕获和超时。
- 测试结束后默认清理远端测试文件。

价值：

- 可以测试真实 GPIO、网络、文件系统和 MicroPython 模块行为。
- 适合驱动、传感器库、教学示例项目。

### 8. Safe Main 启动保护

目标：降低刷入 `/main.py` 后设备因异常或死循环难以恢复的概率。

注：此功能为修复补丁，无命令

实现思路：

- 在启动时连发\x03强制打断设备动作以成功进入repl
- 支持覆盖原 `./main.py` (会有备份不会直接覆盖)

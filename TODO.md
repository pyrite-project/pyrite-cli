# TODO

pyrite-cli 未来发展方向与功能优化清单。

## 中优先级

- [x] **固件烧录命令** — 支持通过 `esptool` 集成或独立实现烧录 MicroPython 固件 `.bin` 文件
- [ ] **项目模板库** — 提供预置项目模板（IoT 传感器、WiFi 连接、BLE 扫描等），`pyrcli new` 时可选择模板
- [ ] **设备端文件 diff** — `project status` 增加行级 diff 显示，不止于大小/哈希比对
- [ ] **JSON 输出模式** — 为 `scan`、`board-info`、`project status` 添加 `--json` 输出，方便脚本集成
- [x] **Shell 自动补全** — 注册 Typer 原生支持的 `--install-completion`，支持 PowerShell/bash/zsh 补全
- [x] **交互式文件浏览器增强** — `fs ls` 支持分页、递归列出、文件大小排序
- [ ] **多 profile 配置** — 支持 `--profile prod` 切换不同配置（串口号、波特率、标签等）
- [ ] **环境变量插值** — 配置项支持 `${VAR}` 引用环境变量
- [ ] **API 文档生成** — 用 Sphinx 或 MkDocs 生成 Python API 文档，当前只有 README 和散落的 Markdown
- [ ] **插件系统** — 允许第三方插件注册自定义命令（如 `pyrcli ota`、`pyrcli mqtt`）

## 低优先级 / 长期

- [x] **DTR/RTS 自动复位** — 连接时通过串口信号线自动复位设备进入刷写模式
- [ ] **设备配置备份/恢复** — `pyrcli device backup` / `restore` 批量导入/导出设备文件
- [ ] **国际化（i18n）** — 统一管理中文/英文提示语
- [ ] **依赖版本锁定** — 添加 `requirements-dev.txt` 锁定开发依赖版本

# TODO

pyrite-cli 未来发展方向与功能优化清单。

## 近期 — 安全加固

- [ ] **WebREPL 密码安全存储** — 当前密码以明文传递，应支持密钥环（keyring）或加密配置文件存储
- [ ] **插件沙箱** — 第三方插件可能执行任意代码，需限制插件对文件系统/网络/子进程的访问权限
- [ ] **manifest 解析器加固** — 增加超长路径/超大文件的拒绝策略，防止畸形 manifest 导致 OOM
- [ ] **串口输入消毒** — 对用户通过 `run`/`repl` 传入的设备命令做长度/字符限制，防止注入意外控制序列
- [ ] **固件烧录完整性双重校验** — 烧录后除 esptool verify 外增加 SHA256 哈希比对
- [ ] **HTTPS 证书校验** — 存根下载（GitHub API）和 future OTA 功能需强制证书验证，禁止跳过

## 中期 — 功能添加

- [ ] **项目模板库** — 提供预置项目模板（IoT 传感器、WiFi 连接、BLE 扫描等），`pyrcli new` 时可选择模板
- [ ] **设备端文件 diff** — `project status` 增加行级 diff 显示，不止于大小/哈希比对
- [x] **JSON 输出模式** — 为 `scan`、`board-info`、`project status` 添加 `--json` 输出，方便脚本集成
- [ ] **OTA 升级** — 利用 WebREPL 通道实现远程固件与代码更新
- [ ] **多设备并行管理** — `pyrcli broadcast` 同时向多个串口/WebREPL 设备发送命令
- [ ] **mDNS 设备发现** — 局域网自动发现 MicroPython 设备，无需手动指定 IP/端口
- [ ] **API 文档生成** — 用 Sphinx 或 MkDocs 生成 Python API 文档，当前只有 README 和散落的 Markdown

## 长期 — 优化与杂项

- [ ] **设备配置备份/恢复** — `pyrcli device backup` / `restore` 批量导入/导出设备文件
- [ ] **大文件传输性能** — 当前逐块发送，探索压缩传输、并行流等方式加速
- [ ] **国际化（i18n）** — 统一管理中文/英文提示语
- [ ] **异步重构** — 将同步串口/WebSocket 调用迁移为 asyncio，提升并发吞吐
- [ ] **设备端内存优化** — 精简 FLASH/FLASH_PROGRAM 脚本，降低 MicroPython 堆内存占用
- [ ] **错误信息人性化** — 统一异常处理，对常见错误（超时、设备无响应、协议版本不匹配）提供具体建议
- [ ] **硬件在环测试框架** — 建立自动化实机测试基础设施，覆盖多款 ESP32/STM32 开发板

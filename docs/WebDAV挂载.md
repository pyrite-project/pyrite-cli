# WebDAV 挂载

`pyrcli mount` 在 PC 侧启动一个 WebDAV 服务，把系统文件管理器的文件操作转换为 MicroPython 设备上的 Raw REPL 文件操作。

这个方案不依赖设备固件的 USB MTP 支持，也不需要在设备端运行常驻服务。只要设备能通过现有串口或 WebREPL 进入 Raw REPL，就可以使用。

---

## 1. 工作方式

```
Windows Explorer / macOS Finder / Linux 文件管理器
        ↓ WebDAV
本机 WebDAV 服务: http://127.0.0.1:8765/
        ↓ pyrcli mount
UART Raw REPL / WebREPL
        ↓
MicroPython 文件系统
```

`pyrcli mount` 做三件事：

1. 连接串口设备，或通过 `--ws` 连接 WebREPL 设备。
2. 在 PC 上启动本地 WebDAV 服务。
3. 尝试把这个 WebDAV 地址交给系统默认文件管理器。

WebDAV 服务会处理常见文件管理器请求：

| WebDAV 方法 | 映射到设备操作 |
|-------------|----------------|
| `PROPFIND` | 列目录 / 查询文件信息 |
| `GET` / `HEAD` | 下载文件 |
| `PUT` | 上传或覆盖文件 |
| `DELETE` | 删除文件或目录 |
| `MKCOL` | 创建目录 |
| `MOVE` | 重命名 / 移动 |
| `COPY` | 复制 |
| `LOCK` / `UNLOCK` | 兼容文件管理器锁请求 |

---

## 2. 基本用法

```powershell
pyrcli mount COM4
```

默认行为：

- 设备根目录：`/`
- WebDAV 地址：`http://127.0.0.1:8765/`
- 自动连接默认文件管理器
- 读写模式

通过 WebREPL 挂载时，`COM4` 仍作为兼容其他设备命令的占位端口参数，实际连接地址由 `--ws` 指定：

```powershell
pyrcli mount COM4 --ws ws://192.168.4.1:8266 --password mypass
```

如果省略 `--password`，密码解析顺序与其他 WebREPL 命令一致：`PYRITE_WEBREPL_PASSWORD` 环境变量，最后交互输入。

停止服务：

```text
Ctrl+C
```

停止时会断开串口连接，并尽量清理自动创建的系统挂载。

---

## 3. 常用参数

### 指定设备端根目录

```powershell
pyrcli mount COM4 --root /app
```

文件管理器中看到的根目录会映射到设备的 `/app`。

### 指定 WebDAV 端口

```powershell
pyrcli mount COM4 --http-port 8766
```

如果默认端口 `8765` 被占用，可以换一个端口。

### 只读模式

```powershell
pyrcli mount COM4 --readonly
```

文件管理器仍可浏览和下载文件，但写入、删除、移动、复制、创建目录会返回禁止操作。

### 只启动服务，不自动打开文件管理器

```powershell
pyrcli mount COM4 --no-map
```

这时手动访问：

```text
http://127.0.0.1:8765/
```

或使用系统支持的 WebDAV 地址格式。

---

## 4. Windows

Windows 下默认使用 `net use` 映射一个驱动器盘符。

```powershell
pyrcli mount COM4
```

指定盘符：

```powershell
pyrcli mount COM4 --drive P
```

或：

```powershell
pyrcli mount COM4 --drive P:
```

停止 `pyrcli mount` 后会执行类似下面的清理：

```powershell
net use P: /delete /y
```

### Windows 注意事项

- 需要 Windows WebClient 服务可用。
- 文件管理器会频繁发送探测请求，控制台会显示 WebDAV 操作日志。
- `WinError 10053` / `WinError 10054` 是文件管理器主动断开探测连接，默认不在控制台输出。
- 如果要查看这些断连细节，可用更高日志级别：

```powershell
pyrcli -vv mount COM4
```

---

## 5. Linux

Linux 下优先使用 `gio`：

```bash
pyrcli mount /dev/ttyUSB0
```

内部会尝试：

```bash
gio mount dav://127.0.0.1:8765/
gio open dav://127.0.0.1:8765/
```

如果没有 `gio`，会退回：

```bash
xdg-open dav://127.0.0.1:8765/
```

停止服务时，如果是由 `gio mount` 成功挂载的地址，会尝试自动卸载：

```bash
gio mount -u dav://127.0.0.1:8765/
```

### Linux 注意事项

- GNOME/Nautilus、Nemo、Thunar 等文件管理器对 WebDAV 的行为可能略有差异。
- 如果自动打开失败，可以使用 `--no-map` 后手动在文件管理器地址栏输入：

```text
dav://127.0.0.1:8765/
```

---

## 6. macOS

macOS 下会尝试用 Finder 打开 WebDAV 地址：

```bash
pyrcli mount /dev/cu.usbserial-0001
```

内部会调用：

```bash
open webdav://127.0.0.1:8765/
```

### macOS 注意事项

- Finder 可能弹出连接确认窗口。
- 停止 `pyrcli mount` 后，本地 WebDAV 服务会停止；Finder 中的连接可手动断开或弹出。
- 如果自动打开失败，可以使用 `--no-map` 后在 Finder 的“连接服务器”中输入：

```text
webdav://127.0.0.1:8765/
```

---

## 7. 操作日志

`mount` 会在控制台输出每个 WebDAV 操作，同时写入默认 JSONL 日志文件。

示例：

```text
INFO [cli.utils.webdav_mount] WebDAV PROPFIND / -> / 207 3723B 0.0ms client=127.0.0.1
INFO [cli.utils.webdav_mount] WebDAV GET /main.py -> /main.py 200 128B 42.3ms client=127.0.0.1
INFO [cli.utils.webdav_mount] WebDAV PUT /main.py -> /main.py 204 0B 518.7ms client=127.0.0.1
```

字段含义：

| 字段 | 说明 |
|------|------|
| `PROPFIND` / `GET` / `PUT` | 文件管理器发出的 WebDAV 方法 |
| 第一个路径 | WebDAV URL 路径 |
| `->` 后路径 | 映射到设备端的路径 |
| 状态码 | HTTP 状态码 |
| `B` | 响应体字节数 |
| `ms` | 本次请求耗时 |
| `client` | 客户端地址 |

常见状态码：

| 状态码 | 说明 |
|--------|------|
| `200` | 成功读取 |
| `201` | 创建成功 |
| `204` | 成功且无响应体 |
| `207` | WebDAV 多状态响应，常见于目录列表 |
| `403` | 只读模式禁止写操作 |
| `404` | 文件或目录不存在 |
| `405` | 方法不适用于当前对象 |
| `412` | `Overwrite: F` 且目标已存在 |

---

## 8. 启动时空白目录问题

某些 MicroPython 板子刚进入 Raw REPL 或刚完成一次文件系统操作时，第一次目录枚举可能返回空结果。

`pyrcli mount` 已做目录缓存和空列表重试：

1. 默认会在 WebDAV 服务启动前预热根目录缓存，避免文件管理器首次访问时看到偶发空目录。
2. 使用 `--load-all` 时，WebDAV 服务挂载前会先调用 `flash.py` 内的 `fs_ls_recursive()` 读取并缓存整棵目录结构。
3. 增量目录读取仍会在 `PROPFIND Depth: 1` 遇到空列表时短重试一次，避免把偶发空结果写入缓存。

默认重试间隔约 `80ms`。后续浏览已扫描过的目录时，会优先使用 PC 侧缓存，减少串口往返和刚打开文件管理器时偶发显示空目录的情况。

执行写入、删除、移动、复制或创建目录后，目录缓存会自动失效；下一次目录访问会重新读取根目录并启动后台扫描。

---

## 9. 性能与限制

### 串口是单通道

WebDAV 文件管理器可能并发发多个请求，但设备端 UART/Raw REPL 实际只能串行处理。`pyrcli mount` 内部会串行化设备访问，避免多个请求同时读写串口。

目录结构会在 PC 侧缓存，但文件内容仍按需读取；缓存只保存路径、目录/文件类型和文件大小，不缓存文件正文。

### 大文件会比较慢

文件读写最终还是走串口：

- 读取：设备端按 Raw REPL 字节协议输出文件内容。
- 写入：PC 侧接收 WebDAV `PUT` 后，临时保存为本地文件，再通过现有 `flash_file()` 刷入设备。

### 不是 USB MTP

这是 PC 侧 WebDAV 桥，不是设备端 USB MTP。

优点是不需要固件开发；代价是 `pyrcli mount` 进程必须保持运行。

### 文件管理器可能产生临时文件

不同系统的文件管理器或编辑器可能创建临时文件，例如：

- `.~lock.*`
- `.DS_Store`
- `Thumbs.db`
- 编辑器的 swap/backup 文件

这些行为来自 PC 端应用，不是 pyrite-cli 主动创建。

---

## 10. 排障

### Windows 映射失败

先确认 WebClient 服务可用，再尝试指定盘符：

```powershell
pyrcli mount COM4 --drive P
```

如果仍失败，可以只启动 WebDAV 服务：

```powershell
pyrcli mount COM4 --no-map
```

然后手动访问：

```text
http://127.0.0.1:8765/
```

### Linux 自动打开失败

确认系统存在 `gio` 或 `xdg-open`：

```bash
which gio
which xdg-open
```

手动访问：

```text
dav://127.0.0.1:8765/
```

### macOS Finder 连接失败

手动打开 Finder 的“连接服务器”，输入：

```text
webdav://127.0.0.1:8765/
```

### 目录偶发空白

先刷新文件管理器。如果仍反复出现，带更详细日志运行：

```bash
pyrcli -vv mount COM4
```

观察是否出现：

```text
WebDAV EMPTY-LIST retry path=/ attempt=1 result=...
```

如果重试后仍为空，说明设备端当前枚举确实没有返回条目，需要检查串口稳定性、设备文件系统状态或设备是否正在执行其他任务。

---

## 11. 推荐命令

Windows：

```powershell
pyrcli mount COM4 --drive P
```

Linux：

```bash
pyrcli mount /dev/ttyUSB0
```

macOS：

```bash
pyrcli mount /dev/cu.usbserial-0001
```

只读浏览：

```bash
pyrcli mount COM4 --readonly
```

调试模式：

```bash
pyrcli -vv mount COM4
```

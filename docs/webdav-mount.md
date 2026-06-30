# WebDAV Mount

`pyrcli mount` starts a WebDAV service on the PC and translates file-manager operations into Raw REPL file operations on a MicroPython device.

This approach does not require USB MTP support in device firmware and does not require a resident service on the device. If the device can enter Raw REPL through an existing serial port or WebREPL connection, it can be mounted.

For the opposite direction, where the device accesses a host directory, use `pyrcli remount`. `remount` delegates to `mpremote mount` and exposes the local directory to the device VFS as `/remote`; it does not use the WebDAV service described here.

---

## 1. How It Works

```text
Windows Explorer / macOS Finder / Linux file manager
        | WebDAV
Local WebDAV service: http://127.0.0.1:8765/
        | pyrcli mount
UART Raw REPL / WebREPL
        |
MicroPython filesystem
```

`pyrcli mount` does three things:

1. Connects to the serial device, or connects to a WebREPL device with `--ws`.
2. Starts a local WebDAV service on the PC.
3. Tries to hand the WebDAV URL to the system file manager.

The WebDAV service handles common file-manager requests:

| WebDAV method | Device operation |
|---------------|------------------|
| `PROPFIND` | List a directory or query file metadata |
| `GET` / `HEAD` | Download a file |
| `PUT` | Upload or overwrite a file |
| `DELETE` | Delete a file or directory |
| `MKCOL` | Create a directory |
| `MOVE` | Rename or move |
| `COPY` | Copy |
| `LOCK` / `UNLOCK` | Compatibility responses for file-manager lock requests |

---

## 2. Basic Usage

```powershell
pyrcli mount COM4
```

Default behavior:

- Device root: `/`
- WebDAV URL: `http://127.0.0.1:8765/`
- Automatically opens the default file manager
- Read/write mode

When mounting through WebREPL, `COM4` remains as the positional port placeholder used by other device commands. The real transport target is provided by `--ws`:

```powershell
pyrcli mount COM4 --ws ws://192.168.4.1:8266 --password mypass
```

If `--password` is omitted, password resolution follows the same order as other WebREPL commands: `PYRITE_WEBREPL_PASSWORD`, then interactive input.

Stop the service with:

```text
Ctrl+C
```

On shutdown, pyrite-cli disconnects the serial session and tries to clean up any system mount it created automatically.

---

## 2.1 Reverse Mount: `remount`

```powershell
pyrcli remount COM4 .
```

`remount` lets the device access the PC. The device sees `/remote`, and `mpremote` also changes the current working directory to `/remote`. It is equivalent to:

```powershell
mpremote connect COM4 mount .
```

If the directory contains symbolic links and you intentionally need the device to follow links outside the mount root, opt in explicitly:

```powershell
pyrcli remount COM4 . --unsafe-links
```

`remount` supports only the serial path because it uses `mpremote` serial mounting. For WebREPL, use `pyrcli mount --ws ...` to access the device filesystem from the PC.

---

## 3. Common Options

### Device-Side Root

```powershell
pyrcli mount COM4 --root /app
```

The root visible in the file manager maps to `/app` on the device.

### WebDAV Port

```powershell
pyrcli mount COM4 --http-port 8766
```

Use another port if the default `8765` is already occupied.

### Startup Root Stability

After startup, `pyrcli mount` warms the root directory listing first. If a device has just returned from user code to Raw REPL and briefly reports an empty root directory, WebDAV returns `503 Retry-After` during the guard window so the file manager does not cache a false empty directory. The default guard window is 5 seconds:

```powershell
pyrcli mount COM4 --startup-empty-list-grace 10
```

If the device filesystem is actually empty, it will be shown normally after the guard window.

### Flash Usage

For WebDAV root `PROPFIND` responses, `pyrcli mount` returns `quota-used-bytes` and `quota-available-bytes`. These values come from device-side `os.statvfs('/')`, the same real filesystem statistics used by `pyrcli fs df`. They are not PC-side cache size and are not calculated by summing directory entries.

Different system file managers display WebDAV quota fields differently. Some show capacity and free space directly; others use the fields only in properties or mount metadata.

### Run a Script Through the Active Mount Session

`pyrcli mount` injects a protected virtual executable file at the WebDAV root. The filename depends on the host system:

| System | Filename |
|--------|----------|
| Windows | `_run.bat` |
| macOS | `_run.command` |
| Linux | `_run.sh` |

Requesting that file makes the active mount session run this on the device:

```python
execfile("/main.py")
```

The recommended command-line trigger is:

```powershell
pyrcli mount-run --path /main.py
```

If the mount service uses a custom port:

```powershell
pyrcli mount-run --http-port 8766 --path /main.py
```

While the script is running, the mount file channel pauses. New WebDAV file reads wait; writes are stored in temporary PC files and queued, then replayed to the device in order after the script exits. Script stdout and stderr are printed to the console running `pyrcli mount`. Use `pyrcli mount --run-timeout 600` to adjust the script timeout, and `--run-queue-max` / `--run-queue-max-bytes` to cap the write queue.

The virtual executable content only reports that `main.py` has started. Actual execution is performed by the mount service when the file is requested. The service calculates a SHA-256 for the file and returns it through ETag and `X-Pyrite-Run-Executable-SHA256`. `PUT`, `DELETE`, `MOVE`, `COPY`, and `MKCOL` on the virtual file are rejected so the entry point cannot be overwritten through the file manager.

### Read-Only Mode

```powershell
pyrcli mount COM4 --readonly
```

The file manager can still browse and download files, but writes, deletes, moves, copies, and directory creation are rejected.

### Start the Service Without Opening a File Manager

```powershell
pyrcli mount COM4 --no-map
```

Then open this address manually:

```text
http://127.0.0.1:8765/
```

or use the WebDAV URL format supported by your system.

---

## 4. Windows

On Windows, pyrite-cli maps a drive letter with `net use` by default:

```powershell
pyrcli mount COM4
```

Specify a drive letter:

```powershell
pyrcli mount COM4 --drive P
```

or:

```powershell
pyrcli mount COM4 --drive P:
```

After `pyrcli mount` stops, cleanup runs a command similar to:

```powershell
net use P: /delete /y
```

### Windows Notes

- The Windows WebClient service must be available.
- File Explorer sends frequent probe requests, so WebDAV operation logs appear in the console.
- `WinError 10053` and `WinError 10054` usually mean Explorer closed a probe connection; these are hidden from console output by default.
- To inspect those disconnect details, use a higher log level:

```powershell
pyrcli -vv mount COM4
```

---

## 5. Linux

On Linux, pyrite-cli prefers `gio`:

```bash
pyrcli mount /dev/ttyUSB0
```

Internally it tries:

```bash
gio mount dav://127.0.0.1:8765/
gio open dav://127.0.0.1:8765/
```

If `gio` is unavailable, it falls back to:

```bash
xdg-open dav://127.0.0.1:8765/
```

When stopping the service, if `gio mount` succeeded, pyrite-cli tries to unmount automatically:

```bash
gio mount -u dav://127.0.0.1:8765/
```

### Linux Notes

- GNOME/Nautilus, Nemo, Thunar, and other file managers may behave slightly differently with WebDAV.
- If auto-open fails, use `--no-map` and enter this in the file-manager address bar:

```text
dav://127.0.0.1:8765/
```

---

## 6. macOS

On macOS, pyrite-cli tries to open the WebDAV URL in Finder:

```bash
pyrcli mount /dev/cu.usbserial-0001
```

Internally it calls:

```bash
open webdav://127.0.0.1:8765/
```

### macOS Notes

- Finder may show a connection confirmation dialog.
- After `pyrcli mount` stops, the local WebDAV service stops. The Finder connection can be disconnected or ejected manually.
- If auto-open fails, use `--no-map` and enter this in Finder's "Connect to Server" dialog:

```text
webdav://127.0.0.1:8765/
```

---

## 7. Operation Logs

`mount` prints each WebDAV operation to the console and writes it to the default JSONL log file.

Example:

```text
INFO [cli.utils.webdav_mount] WebDAV PROPFIND / -> / 207 3723B 0.0ms client=127.0.0.1
INFO [cli.utils.webdav_mount] WebDAV GET /main.py -> /main.py 200 128B 42.3ms client=127.0.0.1
INFO [cli.utils.webdav_mount] WebDAV PUT /main.py -> /main.py 204 0B 518.7ms client=127.0.0.1
```

Field meanings:

| Field | Meaning |
|-------|---------|
| `PROPFIND` / `GET` / `PUT` | WebDAV method sent by the file manager |
| First path | WebDAV URL path |
| Path after `->` | Mapped device path |
| Status code | HTTP status code |
| `B` | Response body size in bytes |
| `ms` | Request duration |
| `client` | Client address |

Common status codes:

| Status | Meaning |
|--------|---------|
| `200` | Read succeeded |
| `201` | Created |
| `204` | Success with no response body |
| `207` | WebDAV multi-status response, common for directory listings |
| `403` | Write operation forbidden in read-only mode |
| `404` | File or directory does not exist |
| `405` | Method does not apply to the current object |
| `412` | `Overwrite: F` and target already exists |

---

## 8. Blank Directory at Startup

Some MicroPython boards may briefly return an empty directory listing right after entering Raw REPL or after a filesystem operation.

`pyrcli mount` uses directory caching and empty-list retries:

1. By default, it warms the root directory cache before the WebDAV service is mounted, avoiding occasional empty root listings during the first file-manager access.
2. With `--load-all`, it calls `fs_ls_recursive()` from `cli.utils.flash` before mounting the WebDAV service and caches the whole directory tree.
3. Incremental directory reads still do a short retry when `PROPFIND Depth: 1` gets an empty list, so transient empty results are not written into the cache.

The default retry interval is about `80ms`. After a directory has been scanned, later browsing uses the PC-side cache first, reducing serial round trips and avoiding occasional blank views when opening the file manager.

Writes, deletes, moves, copies, and directory creation invalidate the relevant directory cache. The next directory access rereads the root and starts a background scan.

---

## 9. Performance and Limits

### Serial Is Single-Channel

WebDAV file managers may send concurrent requests, but device-side UART/Raw REPL access is effectively serial. `pyrcli mount` serializes device operations internally so concurrent requests do not read and write the serial port at the same time.

The directory tree is cached on the PC, but file contents are still read on demand. The cache stores paths, directory/file type, and file size; it does not cache file bodies.

### Large Files Are Slower

File reads and writes still go through serial:

- Read: the device emits file bytes through the Raw REPL byte protocol.
- Write: the PC receives a WebDAV `PUT`, stores it in a local temporary file, then flashes it to the device through the existing `flash_file()` path.

### This Is Not USB MTP

This is a PC-side WebDAV bridge, not device-side USB MTP.

The advantage is that no firmware development is required. The tradeoff is that the `pyrcli mount` process must keep running.

### File Managers May Create Temporary Files

Different file managers and editors may create temporary files such as:

- `.~lock.*`
- `.DS_Store`
- `Thumbs.db`
- editor swap or backup files

These are created by PC-side applications, not by pyrite-cli.

---

## 10. Troubleshooting

### Windows Mapping Fails

First confirm that the WebClient service is available, then try specifying a drive letter:

```powershell
pyrcli mount COM4 --drive P
```

If it still fails, start only the WebDAV service:

```powershell
pyrcli mount COM4 --no-map
```

Then open manually:

```text
http://127.0.0.1:8765/
```

### Linux Auto-Open Fails

Check that `gio` or `xdg-open` exists:

```bash
which gio
which xdg-open
```

Open manually:

```text
dav://127.0.0.1:8765/
```

### macOS Finder Connection Fails

Open Finder's "Connect to Server" dialog and enter:

```text
webdav://127.0.0.1:8765/
```

### Directory Is Occasionally Blank

Refresh the file manager first. If the issue repeats, run with more detailed logs:

```bash
pyrcli -vv mount COM4
```

Look for:

```text
WebDAV EMPTY-LIST retry path=/ attempt=1 result=...
```

If the listing is still empty after retry, the device currently returned no entries. Check serial stability, filesystem state, or whether the device is busy running other code.

---

## 11. Recommended Commands

Windows:

```powershell
pyrcli mount COM4 --drive P
```

Linux:

```bash
pyrcli mount /dev/ttyUSB0
```

macOS:

```bash
pyrcli mount /dev/cu.usbserial-0001
```

Read-only browsing:

```bash
pyrcli mount COM4 --readonly
```

Debug mode:

```bash
pyrcli -vv mount COM4
```

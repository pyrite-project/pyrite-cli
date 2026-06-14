"""
PC 侧 WebDAV 挂载桥。

该模块不要求设备固件支持 USB MTP。它在本机启动一个 WebDAV 服务，
把文件管理器的 WebDAV 请求转换为现有 UART/Raw REPL 文件操作。
"""

from __future__ import annotations

import contextlib
import email.utils
import os
import platform
import posixpath
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .flash import MicroPython
from .log import get_logger

log = get_logger(__name__)
ET.register_namespace("D", "DAV:")
_CLIENT_DISCONNECT_WINERRORS = {10038, 10053, 10054}
_QUIET_CLIENT_DISCONNECT_WINERRORS = {10053, 10054}
_CLIENT_DISCONNECT_EXCEPTIONS = (
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionResetError,
)


@dataclass(frozen=True)
class DeviceFileStat:
    path: str
    is_dir: bool
    size: int


@dataclass(frozen=True)
class WebDavConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    root: str = "/"
    readonly: bool = False
    drive: Optional[str] = None
    map_drive: bool = True
    empty_list_retries: int = 1
    empty_list_retry_delay: float = 0.08
    startup_empty_list_retries: int = 5
    directory_cache: bool = True
    load_all: bool = False


class WebDavThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer 将客户端断开连接视为正常情况。
    Windows 资源管理器/Web 客户端经常打开本地 WebDAV 套接字，然后在探测功能时将其关闭。默认的套接字服务器行为会打印这些重置的回溯信息，虽然信息量很大，但无法采取任何行动。
    """

    daemon_threads = True

    def handle_error(self, request: object, client_address: tuple[str, int]) -> None:
        exc = sys.exc_info()[1]
        winerror = getattr(exc, "winerror", None)
        errno = getattr(exc, "errno", None)
        code = winerror or errno
        if isinstance(exc, _CLIENT_DISCONNECT_EXCEPTIONS) or winerror in _CLIENT_DISCONNECT_WINERRORS:
            if code in _QUIET_CLIENT_DISCONNECT_WINERRORS:
                log.debug("WebDAV DISCONNECT client=%s:%s reason=%s", client_address[0], client_address[1], exc)
            else:
                log.info("WebDAV DISCONNECT client=%s:%s reason=%s", client_address[0], client_address[1], exc)
            return
        super().handle_error(request, client_address)


class DevicePathMapper:
    """把 WebDAV URL 路径限制并映射到设备端根目录。"""

    def __init__(self, root: str = "/") -> None:
        root = (root or "/").replace("\\", "/")
        if not root.startswith("/"):
            root = "/" + root
        self.root = posixpath.normpath(root)
        if self.root == ".":
            self.root = "/"

    def to_remote(self, url_path: str) -> str:
        path = unquote(urlsplit(url_path).path or "/").replace("\\", "/")
        parts = [p for p in path.split("/") if p and p not in (".", "..")]
        rel = "/".join(parts)
        if self.root == "/":
            return "/" + rel if rel else "/"
        return self.root.rstrip("/") + ("/" + rel if rel else "")

    def href_for(self, remote_path: str) -> str:
        remote = posixpath.normpath(remote_path.replace("\\", "/"))
        if self.root == "/":
            rel = remote.strip("/")
        elif remote == self.root:
            rel = ""
        else:
            prefix = self.root.rstrip("/") + "/"
            rel = remote[len(prefix):] if remote.startswith(prefix) else remote.strip("/")
        href = "/" + quote(rel, safe="/")
        return href if href != "" else "/"


class MicroPythonWebDavAdapter:
    """串行化访问 MicroPython 文件系统。"""

    def __init__(self, mp: MicroPython) -> None:
        self._mp = mp
        self._lock = threading.RLock()

    def stat(self, path: str) -> Optional[DeviceFileStat]:
        script = (
            "import os\n"
            f"p={path!r}\n"
            "try:\n"
            " s=os.stat(p); s=os.stat(p)\n"
            " print(('D' if s[0]&0x4000 else 'F')+'|'+str(s[6]))\n"
            "except Exception as e:\n"
            " print('ERR|'+repr(e))\n"
        )
        with self._lock:
            out = self._mp.run(script, timeout=10)
        for line in out.strip().splitlines():
            line = line.strip()
            if line.startswith("D|") or line.startswith("F|"):
                kind, size = line.split("|", 1)
                return DeviceFileStat(path=path, is_dir=kind == "D", size=int(size))
        return None

    def list_dir(self, path: str) -> list[DeviceFileStat]:
        with self._lock:
            items = self._mp.fs_ls(path)
        result: list[DeviceFileStat] = []
        for item in items:
            name = item["name"]
            child = path.rstrip("/") + "/" + name if path != "/" else "/" + name
            size = int(item["size"]) if item["size"].isdigit() else 0
            result.append(DeviceFileStat(path=child, is_dir=item["type"] == "D", size=size))
        return result

    def list_dir_recursive(self, path: str) -> list[DeviceFileStat]:
        with self._lock:
            items = self._mp.fs_ls_recursive(path)
        result: list[DeviceFileStat] = []
        for item in items:
            item_type = item["type"]
            if item_type not in ("D", "F"):
                continue
            size = int(item["size"]) if item["size"].isdigit() else 0
            result.append(
                DeviceFileStat(
                    path=posixpath.normpath(item["name"].replace("\\", "/")),
                    is_dir=item_type == "D",
                    size=size,
                )
            )
        return result

    def read_file(self, path: str) -> bytes:
        with self._lock:
            return self._mp._read_device_file(path)

    def write_file(self, path: str, data: bytes) -> None:
        fd, local_path = tempfile.mkstemp(prefix="pyrite_webdav_", suffix=".bin")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            with self._lock:
                self._mp.flash_file(local_path, path, compile=False)
        finally:
            with contextlib.suppress(OSError):
                os.remove(local_path)

    def make_dir(self, path: str) -> None:
        script = "import os\n" f"os.mkdir({path!r})\n" "print('OK')\n"
        with self._lock:
            self._mp.run(script, timeout=10)

    def delete(self, path: str) -> None:
        stat = self.stat(path)
        if stat is None:
            raise FileNotFoundError(path)
        if stat.is_dir and self._remove_empty_dir(path):
            return
        with self._lock:
            ok = self._mp.fs_rm(path, recursive=stat.is_dir, force=False)
        if ok:
            return
        if self.stat(path) is None:
            log.debug("WebDAV delete verified removed despite missing OK path=%s", path)
            return
        raise OSError(f"failed to delete {path}")

    def _remove_empty_dir(self, path: str) -> bool:
        script = (
            "import os\n"
            f"p={path!r}\n"
            "try:\n"
            " os.rmdir(p)\n"
            " print('OK')\n"
            "except Exception as e:\n"
            " print('ERR|'+repr(e))\n"
        )
        with self._lock:
            out = self._mp.run(script, timeout=10)
        if "OK" in out:
            return True
        return self.stat(path) is None

    def move(self, src: str, dst: str) -> None:
        with self._lock:
            self._mp.fs_mv(src, dst)

    def copy(self, src: str, dst: str) -> None:
        with self._lock:
            self._mp.fs_cp(src, dst)


def _http_date() -> str:
    return email.utils.formatdate(time.time(), usegmt=True)


def _xml_response(stat: DeviceFileStat, href: str) -> ET.Element:
    response = ET.Element("{DAV:}response")
    ET.SubElement(response, "{DAV:}href").text = href + ("/" if stat.is_dir and href != "/" and not href.endswith("/") else "")
    propstat = ET.SubElement(response, "{DAV:}propstat")
    prop = ET.SubElement(propstat, "{DAV:}prop")
    ET.SubElement(prop, "{DAV:}displayname").text = _display_name(stat, href)
    resource_type = ET.SubElement(prop, "{DAV:}resourcetype")
    if stat.is_dir:
        ET.SubElement(resource_type, "{DAV:}collection")
    else:
        ET.SubElement(prop, "{DAV:}getcontentlength").text = str(stat.size)
        ET.SubElement(prop, "{DAV:}getcontenttype").text = "application/octet-stream"
    ET.SubElement(prop, "{DAV:}getlastmodified").text = _http_date()
    ET.SubElement(prop, "{DAV:}getetag").text = f'"{stat.size:x}-{abs(hash(stat.path)) & 0xffff:x}"'
    ET.SubElement(propstat, "{DAV:}status").text = "HTTP/1.1 200 OK"
    return response


def _display_name(stat: DeviceFileStat, href: str) -> str:
    if href == "/":
        return ""
    return posixpath.basename(stat.path.rstrip("/"))


def list_dir_with_empty_retry(
    adapter: object,
    path: str,
    retries: int,
    delay: float,
) -> list[DeviceFileStat]:
    children = adapter.list_dir(path)
    attempt = 0
    while not children and attempt < retries:
        attempt += 1
        if delay > 0:
            time.sleep(delay)
        children = adapter.list_dir(path)
        log.debug(
            "WebDAV EMPTY-LIST retry path=%s attempt=%d result=%d",
            path,
            attempt,
            len(children),
        )
    return children


class DirectoryCachingWebDavAdapter:
    """PC-side directory tree cache for WebDAV directory listings."""

    handles_empty_list_retry = True

    def __init__(self, adapter: object, root: str, config: WebDavConfig) -> None:
        self._adapter = adapter
        self._root = posixpath.normpath(root or "/")
        if self._root == ".":
            self._root = "/"
        self._config = config
        self._lock = threading.RLock()
        self._dirs: dict[str, list[DeviceFileStat]] = {}
        self._stats: dict[str, DeviceFileStat] = {}
        self._scan_thread: Optional[threading.Thread] = None
        self._generation = 0

    def stat(self, path: str) -> Optional[DeviceFileStat]:
        normalized = self._normalize(path)
        with self._lock:
            cached = self._stats.get(normalized)
            if cached is not None:
                log.debug("WebDAV CACHE stat-hit path=%s", normalized)
                return cached

        stat = self._adapter.stat(normalized)
        if stat is not None:
            self._cache_stat(stat)
        return stat

    def list_dir(self, path: str) -> list[DeviceFileStat]:
        normalized = self._normalize(path)
        with self._lock:
            cached = self._dirs.get(normalized)
            if cached is not None:
                log.debug("WebDAV CACHE list-hit path=%s entries=%d", normalized, len(cached))
                return list(cached)

        with self._lock:
            generation = self._generation
        children = self._fetch_and_cache_dir(normalized, generation)
        if normalized == self._root:
            self._ensure_background_scan()
        return children

    def prime_root_listing(self) -> bool:
        with self._lock:
            generation = self._generation
        children = self._fetch_and_cache_dir(
            self._root,
            generation,
            retries=max(self._config.empty_list_retries, self._config.startup_empty_list_retries),
            cache_empty=False,
        )
        if children:
            self._ensure_background_scan()
        return bool(children)

    def prime_from_recursive_listing(self) -> bool:
        if not hasattr(self._adapter, "list_dir_recursive"):
            return False
        started = time.perf_counter()
        try:
            entries = self._adapter.list_dir_recursive(self._root)
        except Exception as exc:
            log.debug("WebDAV recursive cache preload skipped root=%s reason=%s", self._root, exc)
            return False

        dirs: dict[str, list[DeviceFileStat]] = {self._root: []}
        stats: dict[str, DeviceFileStat] = {self._root: DeviceFileStat(path=self._root, is_dir=True, size=0)}
        for entry in entries:
            path = self._normalize(entry.path)
            if path == self._root or not self._is_inside_root(path):
                continue
            normalized_entry = DeviceFileStat(path=path, is_dir=entry.is_dir, size=entry.size)
            stats[path] = normalized_entry
            if normalized_entry.is_dir:
                dirs.setdefault(path, [])
            parent = self._parent_dir(path)
            dirs.setdefault(parent, []).append(normalized_entry)

        for children in dirs.values():
            children.sort(key=lambda item: (not item.is_dir, item.path.lower()))

        with self._lock:
            self._generation += 1
            self._dirs = {path: list(children) for path, children in dirs.items()}
            self._stats = stats
            generation = self._generation

        elapsed_ms = (time.perf_counter() - started) * 1000
        log.info(
            "WebDAV 目录缓存预加载完成 root=%s entries=%d dirs=%d generation=%d %.1fms",
            self._root,
            len(entries),
            len(dirs),
            generation,
            elapsed_ms,
        )
        return True

    def read_file(self, path: str) -> bytes:
        return self._adapter.read_file(path)

    def write_file(self, path: str, data: bytes) -> None:
        try:
            self._adapter.write_file(path, data)
        finally:
            self._invalidate()

    def make_dir(self, path: str) -> None:
        try:
            self._adapter.make_dir(path)
        finally:
            self._invalidate()

    def delete(self, path: str) -> None:
        try:
            self._adapter.delete(path)
        finally:
            self._invalidate()

    def move(self, src: str, dst: str) -> None:
        try:
            self._adapter.move(src, dst)
        finally:
            self._invalidate()

    def copy(self, src: str, dst: str) -> None:
        try:
            self._adapter.copy(src, dst)
        finally:
            self._invalidate()

    def close(self) -> None:
        self._invalidate()

    def wait_for_background_scan(self, timeout: Optional[float] = None) -> bool:
        with self._lock:
            thread = self._scan_thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _normalize(self, path: str) -> str:
        normalized = posixpath.normpath((path or "/").replace("\\", "/"))
        return "/" if normalized == "." else normalized

    def _is_inside_root(self, path: str) -> bool:
        if self._root == "/":
            return path.startswith("/")
        return path == self._root or path.startswith(self._root.rstrip("/") + "/")

    def _parent_dir(self, path: str) -> str:
        parent = posixpath.dirname(path.rstrip("/"))
        if not parent:
            parent = "/"
        if self._root != "/" and not self._is_inside_root(parent):
            return self._root
        return parent

    def _cache_stat(self, stat: DeviceFileStat) -> None:
        with self._lock:
            self._stats[self._normalize(stat.path)] = stat

    def _fetch_and_cache_dir(
        self,
        path: str,
        generation: Optional[int] = None,
        retries: Optional[int] = None,
        delay: Optional[float] = None,
        cache_empty: bool = True,
    ) -> list[DeviceFileStat]:
        log.debug("WebDAV CACHE list-miss path=%s", path)
        children = list_dir_with_empty_retry(
            self._adapter,
            path,
            self._config.empty_list_retries if retries is None else retries,
            self._config.empty_list_retry_delay if delay is None else delay,
        )
        with self._lock:
            if generation is not None and generation != self._generation:
                log.debug(
                    "WebDAV CACHE discard-stale path=%s generation=%d current=%d",
                    path,
                    generation,
                    self._generation,
                )
                return list(children)
            if not children and not cache_empty:
                log.debug("WebDAV CACHE skip-empty path=%s", path)
                return []
            self._dirs[path] = list(children)
            self._stats[path] = DeviceFileStat(path=path, is_dir=True, size=0)
            for child in children:
                self._stats[self._normalize(child.path)] = child
        return list(children)

    def _ensure_background_scan(self) -> None:
        with self._lock:
            if self._scan_thread and self._scan_thread.is_alive():
                return
            generation = self._generation
            self._scan_thread = threading.Thread(
                target=self._scan_directory_tree,
                args=(generation,),
                name="pyrite-webdav-cache",
                daemon=True,
            )
            self._scan_thread.start()
            log.info("WebDAV 目录缓存后台扫描已启动 root=%s", self._root)

    def _scan_directory_tree(self, generation: int) -> None:
        scanned_dirs = 0
        try:
            with self._lock:
                if generation != self._generation:
                    log.debug("WebDAV 目录缓存后台扫描已取消 root=%s", self._root)
                    return
                root_children = list(self._dirs.get(self._root, []))
            stack = [child.path for child in root_children if child.is_dir]
            seen = {self._root}
            while stack:
                with self._lock:
                    if generation != self._generation:
                        log.debug("WebDAV 目录缓存后台扫描已取消 root=%s", self._root)
                        return
                path = stack.pop()
                if path in seen:
                    continue
                seen.add(path)
                children = self._fetch_and_cache_dir(path, generation)
                scanned_dirs += 1
                stack.extend(child.path for child in children if child.is_dir)
            log.info("WebDAV 目录缓存后台扫描完成 root=%s dirs=%d", self._root, scanned_dirs)
        except Exception as exc:
            log.debug("WebDAV 目录缓存后台扫描失败 root=%s reason=%s", self._root, exc)

    def _invalidate(self) -> None:
        with self._lock:
            self._generation += 1
            self._dirs.clear()
            self._stats.clear()
            log.debug("WebDAV CACHE invalidated generation=%d", self._generation)


def make_webdav_handler(adapter: object, mapper: DevicePathMapper, config: WebDavConfig) -> type[BaseHTTPRequestHandler]:
    class WebDavHandler(BaseHTTPRequestHandler):
        server_version = "PyriteWebDAV/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            log.debug("WebDAV %s - %s", self.client_address[0], fmt % args)

        def _elapsed_ms(self) -> float:
            started = getattr(self, "_request_started_at", None)
            if started is None:
                started = time.perf_counter()
                self._request_started_at = started
            return (time.perf_counter() - started) * 1000

        def _log_access(self, status: int, body_size: int) -> None:
            try:
                remote = self._remote()
            except Exception:
                remote = "-"
            log.info(
                "WebDAV %s %s -> %s %d %dB %.1fms client=%s",
                self.command,
                urlsplit(self.path).path or "/",
                remote,
                status,
                body_size,
                self._elapsed_ms(),
                self.client_address[0],
            )

        def _send_bytes(
            self,
            status: int,
            body: bytes = b"",
            content_type: str = "application/octet-stream",
            extra_headers: Optional[dict[str, str]] = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", content_type)
            self.send_header("DAV", "1, 2")
            self.send_header("MS-Author-Via", "DAV")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self._log_access(int(status), len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def _remote(self) -> str:
            return mapper.to_remote(self.path)

        def _destination(self) -> str:
            dst = self.headers.get("Destination", "")
            if not dst:
                raise ValueError("missing Destination header")
            return mapper.to_remote(urlsplit(dst).path)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0") or "0")
            return self.rfile.read(length) if length else b""

        def _reject_readonly(self) -> bool:
            if config.readonly:
                self._send_bytes(HTTPStatus.FORBIDDEN, b"readonly")
                return True
            return False

        def do_OPTIONS(self) -> None:
            self._send_bytes(
                HTTPStatus.NO_CONTENT,
                extra_headers={
                    "Allow": "OPTIONS, PROPFIND, GET, HEAD, PUT, DELETE, MKCOL, MOVE, COPY, LOCK, UNLOCK",
                    "Public": "OPTIONS, PROPFIND, GET, HEAD, PUT, DELETE, MKCOL, MOVE, COPY, LOCK, UNLOCK",
                },
            )

        def do_PROPFIND(self) -> None:
            remote = self._remote()
            stat = adapter.stat(remote)
            if stat is None:
                self._send_bytes(HTTPStatus.NOT_FOUND, b"not found")
                return
            depth = self.headers.get("Depth", "1")
            multistatus = ET.Element("{DAV:}multistatus")
            multistatus.append(_xml_response(stat, mapper.href_for(remote)))
            if stat.is_dir and depth != "0":
                if getattr(adapter, "handles_empty_list_retry", False):
                    children = adapter.list_dir(remote)
                else:
                    children = list_dir_with_empty_retry(
                        adapter,
                        remote,
                        config.empty_list_retries,
                        config.empty_list_retry_delay,
                    )
                for child in children:
                    multistatus.append(_xml_response(child, mapper.href_for(child.path)))
            body = ET.tostring(multistatus, encoding="utf-8", xml_declaration=True)
            self._send_bytes(207, body, "application/xml; charset=utf-8")

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            stat = adapter.stat(self._remote())
            if stat is None:
                self._send_bytes(HTTPStatus.NOT_FOUND, b"not found")
                return
            if stat.is_dir:
                self._send_bytes(HTTPStatus.METHOD_NOT_ALLOWED, b"directory")
                return
            data = adapter.read_file(stat.path)
            self._send_bytes(
                HTTPStatus.OK,
                data,
                extra_headers={"Last-Modified": _http_date()},
            )

        def do_PUT(self) -> None:
            if self._reject_readonly():
                return
            remote = self._remote()
            existed = adapter.stat(remote) is not None
            adapter.write_file(remote, self._read_body())
            self._send_bytes(HTTPStatus.NO_CONTENT if existed else HTTPStatus.CREATED)

        def do_DELETE(self) -> None:
            if self._reject_readonly():
                return
            try:
                adapter.delete(self._remote())
            except FileNotFoundError:
                log.debug("WebDAV DELETE already absent path=%s", self._remote())
            self._send_bytes(HTTPStatus.NO_CONTENT)

        def do_MKCOL(self) -> None:
            if self._reject_readonly():
                return
            adapter.make_dir(self._remote())
            self._send_bytes(HTTPStatus.CREATED)

        def do_MOVE(self) -> None:
            if self._reject_readonly():
                return
            dst = self._destination()
            exists = adapter.stat(dst)
            if exists and self.headers.get("Overwrite", "T").upper() == "F":
                self._send_bytes(HTTPStatus.PRECONDITION_FAILED, b"destination exists")
                return
            if exists:
                adapter.delete(dst)
            adapter.move(self._remote(), dst)
            self._send_bytes(HTTPStatus.CREATED)

        def do_COPY(self) -> None:
            if self._reject_readonly():
                return
            dst = self._destination()
            exists = adapter.stat(dst)
            if exists and self.headers.get("Overwrite", "T").upper() == "F":
                self._send_bytes(HTTPStatus.PRECONDITION_FAILED, b"destination exists")
                return
            if exists:
                adapter.delete(dst)
            adapter.copy(self._remote(), dst)
            self._send_bytes(HTTPStatus.CREATED)

        def do_LOCK(self) -> None:
            if self._reject_readonly():
                return
            token = "opaquelocktoken:" + str(uuid.uuid4())
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<D:prop xmlns:D="DAV:"><D:lockdiscovery><D:activelock>'
                "<D:locktype><D:write/></D:locktype>"
                "<D:lockscope><D:exclusive/></D:lockscope>"
                "<D:depth>infinity</D:depth>"
                f"<D:locktoken><D:href>{token}</D:href></D:locktoken>"
                "</D:activelock></D:lockdiscovery></D:prop>"
            ).encode("utf-8")
            self._send_bytes(HTTPStatus.OK, body, "application/xml; charset=utf-8", {"Lock-Token": f"<{token}>"})

        def do_UNLOCK(self) -> None:
            self._send_bytes(HTTPStatus.NO_CONTENT)

        def do_PROPPATCH(self) -> None:
            self._send_bytes(207, b'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:"/>', "application/xml")

    return WebDavHandler


def _available_drive_letter() -> str:
    import ctypes
    import string

    drive_mask = ctypes.windll.kernel32.GetLogicalDrives()
    for letter in reversed(string.ascii_uppercase[3:]):
        bit = 1 << (ord(letter) - ord("A"))
        if not (drive_mask & bit):
            return letter + ":"
    return "P:"


def map_windows_drive(url: str, drive: Optional[str] = None) -> str:
    mountpoint = (drive or _available_drive_letter()).rstrip(":") + ":"
    parsed = urlsplit(url)
    targets = [url]
    if parsed.hostname and parsed.port:
        targets.append(f"\\\\{parsed.hostname}@{parsed.port}\\DavWWWRoot\\")
    elif parsed.hostname:
        targets.append(f"\\\\{parsed.hostname}\\DavWWWRoot\\")

    errors: list[str] = []
    for target in targets:
        result = subprocess.run(
            ["net", "use", mountpoint, target, "/persistent:no"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return mountpoint
        errors.append((result.stderr or result.stdout).strip())
    raise RuntimeError(f"net use {mountpoint} {url} 失败: {' | '.join(errors)}")


def unmap_windows_drive(mountpoint: str) -> None:
    subprocess.run(
        ["net", "use", mountpoint, "/delete", "/y"],
        capture_output=True,
        text=True,
        timeout=15,
    )


def webdav_file_manager_url(url: str, system: Optional[str] = None) -> str:
    parsed = urlsplit(url)
    target_system = system or platform.system()
    if target_system == "Linux":
        return urlunsplit(("dav", parsed.netloc, parsed.path or "/", "", ""))
    if target_system == "Darwin":
        return urlunsplit(("webdav", parsed.netloc, parsed.path or "/", "", ""))
    return url


def _run_detached(cmd: list[str]) -> None:
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )


def open_linux_file_manager(url: str) -> tuple[str, Optional[Callable[[], None]]]:
    location = webdav_file_manager_url(url, "Linux")
    gio = shutil.which("gio")
    if gio:
        mounted = False
        result = subprocess.run(
            [gio, "mount", location],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            mounted = True
        elif "already mounted" not in (result.stderr + result.stdout).lower():
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"gio mount {location} 失败: {detail}")
        _run_detached([gio, "open", location])

        def cleanup() -> None:
            if mounted:
                subprocess.run(
                    [gio, "mount", "-u", location],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )

        return location, cleanup

    xdg_open = shutil.which("xdg-open")
    if xdg_open:
        _run_detached([xdg_open, location])
        return location, None

    raise RuntimeError("未找到 gio 或 xdg-open，无法自动打开 Linux 文件管理器")


def open_macos_file_manager(url: str) -> tuple[str, Optional[Callable[[], None]]]:
    location = webdav_file_manager_url(url, "Darwin")
    opener = shutil.which("open")
    if not opener:
        raise RuntimeError("未找到 open，无法自动打开 macOS Finder")
    _run_detached([opener, location])
    return location, None


def connect_file_manager(url: str, drive: Optional[str] = None) -> tuple[str, Optional[Callable[[], None]]]:
    system = platform.system()
    if system == "Windows":
        mountpoint = map_windows_drive(url, drive)

        def cleanup() -> None:
            unmap_windows_drive(mountpoint)

        return mountpoint, cleanup
    if system == "Linux":
        return open_linux_file_manager(url)
    if system == "Darwin":
        return open_macos_file_manager(url)
    raise RuntimeError(f"暂不支持自动打开 {system} 的文件管理器，请手动访问 {url}")


def warm_up_directory_listing(adapter: object, root: str, config: WebDavConfig) -> None:
    try:
        stat = adapter.stat(root)
        if stat and stat.is_dir:
            children = list_dir_with_empty_retry(
                adapter,
                root,
                config.empty_list_retries,
                config.empty_list_retry_delay,
            )
            log.debug("WebDAV warm-up path=%s entries=%d", root, len(children))
    except Exception as exc:
        log.debug("WebDAV warm-up skipped path=%s reason=%s", root, exc)


def serve_webdav(mp: MicroPython, config: WebDavConfig) -> None:
    mapper = DevicePathMapper(config.root)
    device_adapter = MicroPythonWebDavAdapter(mp)
    adapter = (
        DirectoryCachingWebDavAdapter(device_adapter, mapper.root, config)
        if config.directory_cache
        else device_adapter
    )
    if isinstance(adapter, DirectoryCachingWebDavAdapter):
        if config.load_all:
            adapter.prime_from_recursive_listing()
        else:
            adapter.prime_root_listing()
    handler = make_webdav_handler(adapter, mapper, config)
    server = WebDavThreadingHTTPServer((config.host, config.port), handler)
    actual_host, actual_port = server.server_address
    url = f"http://{actual_host}:{actual_port}/"
    file_manager_target: Optional[str] = None
    cleanup_file_manager: Optional[Callable[[], None]] = None

    try:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        thread.start()
        if config.map_drive:
            file_manager_target, cleanup_file_manager = connect_file_manager(url, config.drive)
            log.info("已连接到默认文件管理器: %s", file_manager_target)
        log.info("WebDAV 服务已启动: %s", url)
        log.info("按 Ctrl+C 停止服务")
        while thread.is_alive():
            time.sleep(0.2)
    except KeyboardInterrupt:
        log.info("用户中断，正在停止 WebDAV 服务")
    finally:
        server.shutdown()
        with contextlib.suppress(NameError):
            thread.join(timeout=5)
        server.server_close()
        if hasattr(adapter, "close"):
            adapter.close()
        if cleanup_file_manager:
            cleanup_file_manager()
            log.info("已断开默认文件管理器: %s", file_manager_target)

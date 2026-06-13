from __future__ import annotations

import http.client
import threading

from cli.utils import webdav_mount
from cli.utils.webdav_mount import (
    DeviceFileStat,
    DevicePathMapper,
    WebDavConfig,
    WebDavThreadingHTTPServer,
    make_webdav_handler,
    open_linux_file_manager,
    open_macos_file_manager,
    warm_up_directory_listing,
    webdav_file_manager_url,
)


class FakeAdapter:
    def __init__(self):
        self.files = {
            "/flash/main.py": b"print('hello')\n",
        }
        self.dirs = {"/flash"}
        self.writes = []
        self.moves = []

    def stat(self, path: str):
        if path in self.dirs:
            return DeviceFileStat(path=path, is_dir=True, size=0)
        if path in self.files:
            return DeviceFileStat(path=path, is_dir=False, size=len(self.files[path]))
        return None

    def list_dir(self, path: str):
        assert path == "/flash"
        return [DeviceFileStat(path="/flash/main.py", is_dir=False, size=len(self.files["/flash/main.py"]))]

    def read_file(self, path: str) -> bytes:
        return self.files[path]

    def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data
        self.writes.append((path, data))

    def make_dir(self, path: str) -> None:
        self.dirs.add(path)

    def delete(self, path: str) -> None:
        self.files.pop(path, None)
        self.dirs.discard(path)

    def move(self, src: str, dst: str) -> None:
        self.moves.append((src, dst))
        if src in self.files:
            self.files[dst] = self.files.pop(src)

    def copy(self, src: str, dst: str) -> None:
        self.files[dst] = self.files[src]


def _request(server, method, path, body=b"", headers=None):
    conn = http.client.HTTPConnection(server.server_address[0], server.server_address[1], timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), data


def _serve(adapter: FakeAdapter, readonly: bool = False):
    mapper = DevicePathMapper("/flash")
    handler = make_webdav_handler(
        adapter,
        mapper,
        WebDavConfig(readonly=readonly, empty_list_retry_delay=0),
    )
    server = WebDavThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class TransientEmptyListAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.list_calls = 0

    def list_dir(self, path: str):
        self.list_calls += 1
        if self.list_calls == 1:
            return []
        return super().list_dir(path)


def test_path_mapper_keeps_requests_inside_device_root():
    mapper = DevicePathMapper("/flash")

    assert mapper.to_remote("/") == "/flash"
    assert mapper.to_remote("/main.py") == "/flash/main.py"
    assert mapper.to_remote("/dir%20name/file.txt") == "/flash/dir name/file.txt"
    assert mapper.to_remote("/../boot.py") == "/flash/boot.py"


def test_propfind_depth_one_lists_directory_children():
    adapter = FakeAdapter()
    server = _serve(adapter)
    try:
        status, headers, body = _request(server, "PROPFIND", "/", headers={"Depth": "1"})
    finally:
        server.shutdown()

    assert status == 207
    assert headers["Content-Type"].startswith("application/xml")
    text = body.decode("utf-8")
    assert "<D:href>/</D:href>" in text
    assert "<D:href>/main.py</D:href>" in text
    assert "<D:getcontentlength>15</D:getcontentlength>" in text


def test_propfind_retries_transient_empty_directory_listing():
    adapter = TransientEmptyListAdapter()
    server = _serve(adapter)
    try:
        status, _headers, body = _request(server, "PROPFIND", "/", headers={"Depth": "1"})
    finally:
        server.shutdown()

    assert status == 207
    assert adapter.list_calls == 2
    assert "<D:href>/main.py</D:href>" in body.decode("utf-8")


def test_warm_up_retries_transient_empty_root_listing():
    adapter = TransientEmptyListAdapter()

    warm_up_directory_listing(
        adapter,
        "/flash",
        WebDavConfig(empty_list_retries=1, empty_list_retry_delay=0),
    )

    assert adapter.list_calls == 2


def test_put_writes_uploaded_body_to_device_path():
    adapter = FakeAdapter()
    server = _serve(adapter)
    try:
        status, _headers, _body = _request(
            server,
            "PUT",
            "/new.txt",
            body=b"abc",
            headers={"Content-Length": "3"},
        )
    finally:
        server.shutdown()

    assert status == 201
    assert adapter.writes == [("/flash/new.txt", b"abc")]


def test_move_uses_destination_header_path():
    adapter = FakeAdapter()
    server = _serve(adapter)
    host, port = server.server_address
    try:
        status, _headers, _body = _request(
            server,
            "MOVE",
            "/main.py",
            headers={"Destination": f"http://{host}:{port}/renamed.py"},
        )
    finally:
        server.shutdown()

    assert status == 201
    assert adapter.moves == [("/flash/main.py", "/flash/renamed.py")]


def test_move_respects_overwrite_false():
    adapter = FakeAdapter()
    adapter.files["/flash/existing.py"] = b"old"
    server = _serve(adapter)
    host, port = server.server_address
    try:
        status, _headers, _body = _request(
            server,
            "MOVE",
            "/main.py",
            headers={
                "Destination": f"http://{host}:{port}/existing.py",
                "Overwrite": "F",
            },
        )
    finally:
        server.shutdown()

    assert status == 412
    assert adapter.moves == []


def test_server_suppresses_winerror_10053_and_10054_from_info(monkeypatch, capsys):
    info_messages = []
    debug_messages = []
    monkeypatch.setattr(
        webdav_mount.log,
        "info",
        lambda msg, *args, **_extra: info_messages.append(msg % args if args else msg),
    )
    monkeypatch.setattr(
        webdav_mount.log,
        "debug",
        lambda msg, *args, **_extra: debug_messages.append(msg % args if args else msg),
    )
    adapter = FakeAdapter()
    mapper = DevicePathMapper("/flash")
    handler = make_webdav_handler(adapter, mapper, WebDavConfig())
    server = WebDavThreadingHTTPServer(("127.0.0.1", 0), handler)
    try:
        for code in (10053, 10054):
            try:
                raise ConnectionResetError(code, "connection reset")
            except ConnectionResetError:
                server.handle_error(None, ("127.0.0.1", code))
    finally:
        server.server_close()

    captured = capsys.readouterr()
    assert "Exception occurred during processing of request" not in captured.err
    assert not any("WebDAV DISCONNECT client=127.0.0.1:10053" in msg for msg in info_messages)
    assert not any("WebDAV DISCONNECT client=127.0.0.1:10054" in msg for msg in info_messages)
    assert any("WebDAV DISCONNECT client=127.0.0.1:10053" in msg for msg in debug_messages)
    assert any("WebDAV DISCONNECT client=127.0.0.1:10054" in msg for msg in debug_messages)


def test_webdav_request_logs_access_at_info_level(monkeypatch):
    messages = []
    monkeypatch.setattr(
        webdav_mount.log,
        "info",
        lambda msg, *args, **_extra: messages.append(msg % args if args else msg),
    )
    adapter = FakeAdapter()
    server = _serve(adapter)
    try:
        status, _headers, _body = _request(server, "PROPFIND", "/", headers={"Depth": "0"})
    finally:
        server.shutdown()

    assert status == 207
    assert any("WebDAV PROPFIND / -> /flash 207" in msg for msg in messages)


def test_file_manager_urls_for_linux_and_macos():
    url = "http://127.0.0.1:8765/"

    assert webdav_file_manager_url(url, "Linux") == "dav://127.0.0.1:8765/"
    assert webdav_file_manager_url(url, "Darwin") == "webdav://127.0.0.1:8765/"


def test_linux_file_manager_prefers_gio(monkeypatch):
    popen_calls = []
    run_calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(webdav_mount.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "gio" else None)
    monkeypatch.setattr(webdav_mount.subprocess, "run", lambda cmd, **_kwargs: run_calls.append(cmd) or Result())
    monkeypatch.setattr(webdav_mount.subprocess, "Popen", lambda cmd, **_kwargs: popen_calls.append(cmd))

    location, cleanup = open_linux_file_manager("http://127.0.0.1:8765/")
    assert location == "dav://127.0.0.1:8765/"
    assert run_calls == [["/usr/bin/gio", "mount", "dav://127.0.0.1:8765/"]]
    assert popen_calls == [["/usr/bin/gio", "open", "dav://127.0.0.1:8765/"]]

    assert cleanup is not None
    cleanup()
    assert run_calls[-1] == ["/usr/bin/gio", "mount", "-u", "dav://127.0.0.1:8765/"]


def test_linux_file_manager_falls_back_to_xdg_open(monkeypatch):
    popen_calls = []

    def which(name):
        return "/usr/bin/xdg-open" if name == "xdg-open" else None

    monkeypatch.setattr(webdav_mount.shutil, "which", which)
    monkeypatch.setattr(webdav_mount.subprocess, "Popen", lambda cmd, **_kwargs: popen_calls.append(cmd))

    location, cleanup = open_linux_file_manager("http://127.0.0.1:8765/")

    assert location == "dav://127.0.0.1:8765/"
    assert cleanup is None
    assert popen_calls == [["/usr/bin/xdg-open", "dav://127.0.0.1:8765/"]]


def test_macos_file_manager_uses_open_with_webdav_url(monkeypatch):
    popen_calls = []
    monkeypatch.setattr(webdav_mount.shutil, "which", lambda name: "/usr/bin/open" if name == "open" else None)
    monkeypatch.setattr(webdav_mount.subprocess, "Popen", lambda cmd, **_kwargs: popen_calls.append(cmd))

    location, cleanup = open_macos_file_manager("http://127.0.0.1:8765/")

    assert location == "webdav://127.0.0.1:8765/"
    assert cleanup is None
    assert popen_calls == [["/usr/bin/open", "webdav://127.0.0.1:8765/"]]

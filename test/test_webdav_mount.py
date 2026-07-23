from __future__ import annotations

import http.client
import os
import threading
import time
from unittest.mock import MagicMock, patch

import click
import pytest
from typer.testing import CliRunner

from cli.main import app
from cli.utils import webdav_mount
from cli.utils.webdav_mount import (
    DeviceFileStat,
    DevicePathMapper,
    DirectoryListingNotReady,
    DirectoryCachingWebDavAdapter,
    MicroPythonWebDavAdapter,
    MountRunState,
    WebDavConfig,
    WebDavThreadingHTTPServer,
    make_webdav_handler,
    mount_run_executable_for_system,
    open_linux_file_manager,
    open_macos_file_manager,
    webdav_file_manager_url,
)


runner = CliRunner()


def _run_entry():
    entry = mount_run_executable_for_system()
    assert entry is not None
    return entry


class FakeAdapter:
    def __init__(self):
        self.files = {
            "/flash/main.py": b"print('hello')\n",
        }
        self.dirs = {"/flash"}
        self.writes = []
        self.moves = []
        self.operations = []
        self.usage = {"total": 1024, "used": 256, "free": 768}
        self.usage_calls = 0

    def stat(self, path: str):
        if path in self.dirs:
            return DeviceFileStat(path=path, is_dir=True, size=0)
        if path in self.files:
            return DeviceFileStat(path=path, is_dir=False, size=len(self.files[path]))
        return None

    def list_dir(self, path: str):
        assert path == "/flash"
        children = [DeviceFileStat(path=child, is_dir=True, size=0) for child in sorted(self.dirs) if child != path]
        children.extend(
            DeviceFileStat(path=file_path, is_dir=False, size=len(data))
            for file_path, data in sorted(self.files.items())
            if file_path.startswith(path.rstrip("/") + "/")
            and "/" not in file_path[len(path.rstrip("/") + "/"):]
        )
        return children

    def fs_usage(self):
        self.usage_calls += 1
        return self.usage

    def read_file(self, path: str) -> bytes:
        return self.files[path]

    def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data
        self.writes.append((path, data))
        self.operations.append(("write", path))

    def make_dir(self, path: str) -> None:
        self.dirs.add(path)
        self.operations.append(("mkdir", path))

    def delete(self, path: str) -> None:
        self.files.pop(path, None)
        self.dirs.discard(path)
        self.operations.append(("delete", path))

    def move(self, src: str, dst: str) -> None:
        self.moves.append((src, dst))
        if src in self.files:
            self.files[dst] = self.files.pop(src)
        self.operations.append(("move", src, dst))

    def copy(self, src: str, dst: str) -> None:
        self.files[dst] = self.files[src]
        self.operations.append(("copy", src, dst))


class RunCapableAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.run_calls = []

    def run_file(self, path: str) -> str:
        self.run_calls.append(path)
        return f"ran {path}\n"


class BlockingRunAdapter(RunCapableAdapter):
    def __init__(self):
        super().__init__()
        self.run_started = threading.Event()
        self.release_run = threading.Event()

    def run_file(self, path: str) -> str:
        self.run_calls.append(path)
        self.run_started.set()
        assert self.release_run.wait(2)
        return "done\n"


def _request(server, method, path, body=b"", headers=None):
    conn = http.client.HTTPConnection(server.server_address[0], server.server_address[1], timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), data


def _serve(adapter: FakeAdapter, readonly: bool = False, **config_kwargs):
    mapper = DevicePathMapper("/flash")
    handler = make_webdav_handler(
        adapter,
        mapper,
        WebDavConfig(readonly=readonly, empty_list_retry_delay=0, **config_kwargs),
    )
    server = WebDavThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _serve_with_run(adapter: FakeAdapter, config=None, run_state=None):
    mapper = DevicePathMapper("/flash")
    config = config or WebDavConfig(empty_list_retry_delay=0)
    run_state = run_state or MountRunState()
    handler = make_webdav_handler(
        adapter,
        mapper,
        config,
        run_state=run_state,
        run_controller=adapter,
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


class RepeatedTransientEmptyListAdapter(FakeAdapter):
    def __init__(self, empty_times: int):
        super().__init__()
        self.empty_times = empty_times
        self.list_calls = 0

    def list_dir(self, path: str):
        self.list_calls += 1
        if self.list_calls <= self.empty_times:
            return []
        return super().list_dir(path)


class AlwaysEmptyRootAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.list_calls = 0

    def list_dir(self, path: str):
        self.list_calls += 1
        return []


class MissingOnDeleteAdapter(FakeAdapter):
    def delete(self, path: str) -> None:
        raise FileNotFoundError(path)


class RecursiveListingAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.list_calls = 0
        self.recursive_calls = []

    def list_dir(self, path: str):
        self.list_calls += 1
        return super().list_dir(path)

    def list_dir_recursive(self, path: str):
        self.recursive_calls.append(path)
        return [
            DeviceFileStat(path="/flash/lib", is_dir=True, size=0),
            DeviceFileStat(path="/flash/lib/pkg.py", is_dir=False, size=7),
            DeviceFileStat(path="/flash/main.py", is_dir=False, size=len(self.files["/flash/main.py"])),
        ]


class CacheProbeAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.stat_calls = []
        self.list_calls = {}

    def stat(self, path: str):
        self.stat_calls.append(path)
        return super().stat(path)

    def list_dir(self, path: str):
        self.list_calls[path] = self.list_calls.get(path, 0) + 1
        return super().list_dir(path)


class DeleteReturnsFalseButRemovedMicroPython:
    def __init__(self):
        self.removed = False
        self.fs_rm_calls = []

    def run(self, script: str, timeout: int = 10):
        if "os.rmdir" in script:
            return "ERR|OSError(39)\n"
        if self.removed:
            return "ERR|OSError('ENOENT')\n"
        return "D|0\n"

    def fs_rm(self, path: str, recursive: bool = False, force: bool = False):
        self.fs_rm_calls.append((path, recursive, force))
        self.removed = True
        return False


class EmptyDirRemoveMicroPython:
    def __init__(self):
        self.removed = False
        self.fs_rm_calls = []

    def run(self, script: str, timeout: int = 10):
        if "os.rmdir" in script:
            self.removed = True
            return "OK\n"
        if self.removed:
            return "ERR|OSError('ENOENT')\n"
        return "D|0\n"

    def fs_rm(self, path: str, recursive: bool = False, force: bool = False):
        self.fs_rm_calls.append((path, recursive, force))
        return True


class BlockingTreeAdapter:
    def __init__(self):
        self.files = {
            "/flash/main.py": b"print('hello')\n",
        }
        self.dirs = {"/flash", "/flash/lib"}
        self.list_calls = {}
        self.dir_scan_started = threading.Event()
        self.release_dir_scan = threading.Event()

    def stat(self, path: str):
        if path in self.dirs:
            return DeviceFileStat(path=path, is_dir=True, size=0)
        if path in self.files:
            return DeviceFileStat(path=path, is_dir=False, size=len(self.files[path]))
        return None

    def list_dir(self, path: str):
        self.list_calls[path] = self.list_calls.get(path, 0) + 1
        if path == "/flash":
            children = []
            for child in sorted(self.dirs):
                if child != "/flash" and child.count("/") == 2:
                    children.append(DeviceFileStat(path=child, is_dir=True, size=0))
            children.append(DeviceFileStat(path="/flash/main.py", is_dir=False, size=len(self.files["/flash/main.py"])))
            return children
        if path.startswith("/flash/"):
            self.dir_scan_started.set()
            assert self.release_dir_scan.wait(2)
            child_file = path + "/pkg.py"
            return [DeviceFileStat(path=child_file, is_dir=False, size=7)] if path == "/flash/lib" else []
        raise AssertionError(path)

    def read_file(self, path: str) -> bytes:
        return self.files[path]

    def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data

    def make_dir(self, path: str) -> None:
        self.dirs.add(path)

    def delete(self, path: str) -> None:
        self.files.pop(path, None)
        self.dirs.discard(path)

    def move(self, src: str, dst: str) -> None:
        if src in self.files:
            self.files[dst] = self.files.pop(src)

    def copy(self, src: str, dst: str) -> None:
        self.files[dst] = self.files[src]


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


def test_propfind_root_reports_real_device_flash_quota():
    adapter = FakeAdapter()
    adapter.usage = {"total": 4096, "used": 1536, "free": 2560}
    server = _serve(adapter)
    try:
        status, _headers, body = _request(server, "PROPFIND", "/", headers={"Depth": "0"})
    finally:
        server.shutdown()

    text = body.decode("utf-8")
    assert status == 207
    assert "<D:quota-used-bytes>1536</D:quota-used-bytes>" in text
    assert "<D:quota-available-bytes>2560</D:quota-available-bytes>" in text


def test_directory_cache_reuses_root_filesystem_usage_for_propfind():
    adapter = FakeAdapter()
    mapper = DevicePathMapper("/flash")
    cache = DirectoryCachingWebDavAdapter(
        adapter,
        mapper.root,
        WebDavConfig(empty_list_retry_delay=0),
    )
    handler = make_webdav_handler(cache, mapper, WebDavConfig(empty_list_retry_delay=0))
    server = WebDavThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        first_status, _headers, _body = _request(server, "PROPFIND", "/", headers={"Depth": "0"})
        second_status, _headers, _body = _request(server, "PROPFIND", "/", headers={"Depth": "0"})
    finally:
        server.shutdown()

    assert first_status == 207
    assert second_status == 207
    assert adapter.usage_calls == 1


def test_directory_cache_refreshes_filesystem_usage_after_write():
    adapter = FakeAdapter()
    cache = DirectoryCachingWebDavAdapter(
        adapter,
        "/flash",
        WebDavConfig(empty_list_retry_delay=0),
    )

    assert cache.fs_usage() is not None
    assert cache.fs_usage() is not None
    cache.write_file("/flash/main.py", b"print('updated')\n")
    assert cache.fs_usage() is not None

    assert adapter.usage_calls == 2


def test_propfind_injects_run_trigger_file():
    adapter = RunCapableAdapter()
    entry = _run_entry()
    server = _serve_with_run(adapter)
    try:
        status, _headers, body = _request(server, "PROPFIND", "/", headers={"Depth": "1"})
    finally:
        server.shutdown()

    assert status == 207
    assert f"<D:href>/{entry.name}</D:href>" in body.decode("utf-8")


def test_run_trigger_uses_platform_executable_names():
    assert mount_run_executable_for_system("Windows").name == "_run.bat"
    assert mount_run_executable_for_system("Darwin").name == "_run.command"
    assert mount_run_executable_for_system("Linux").name == "_run.sh"
    assert mount_run_executable_for_system("FreeBSD") is None


def test_run_trigger_executes_default_main_py_and_returns_output():
    adapter = RunCapableAdapter()
    entry = _run_entry()
    server = _serve_with_run(adapter)
    try:
        status, headers, body = _request(server, "GET", "/" + entry.name)
    finally:
        server.shutdown()

    assert status == 200
    assert body == entry.body
    assert headers["ETag"] == f'"sha256:{entry.sha256}"'
    assert headers["X-Pyrite-Run-Executable-SHA256"] == entry.sha256
    assert adapter.run_calls == ["/main.py"]


def test_run_trigger_file_body_contains_only_start_prompt():
    entry = mount_run_executable_for_system("Linux")

    assert entry is not None
    body = entry.body.decode("utf-8")
    assert "已开始运行main.py" in body
    assert "execfile" not in body
    assert "curl" not in body
    assert "http" not in body


def test_run_trigger_rejects_webdav_mutations_to_protect_hash():
    adapter = RunCapableAdapter()
    entry = _run_entry()
    server = _serve_with_run(adapter)
    try:
        put_status, _headers, _body = _request(
            server,
            "PUT",
            "/" + entry.name,
            body=b"tampered",
            headers={"Content-Length": "8"},
        )
        move_status, _headers, _body = _request(
            server,
            "MOVE",
            "/main.py",
            headers={"Destination": f"http://{server.server_address[0]}:{server.server_address[1]}/{entry.name}"},
        )
    finally:
        server.shutdown()

    assert put_status == 403
    assert move_status == 403
    assert adapter.writes == []
    assert adapter.moves == []


def test_webdav_requests_wait_while_mount_run_is_running():
    adapter = BlockingRunAdapter()
    server = _serve_with_run(adapter)
    run_done = threading.Event()
    read_done = threading.Event()
    read_result = {}

    def run_request():
        try:
            _request(server, "GET", "/" + _run_entry().name)
        finally:
            run_done.set()

    def read_request():
        read_result["response"] = _request(server, "GET", "/main.py")
        read_done.set()

    run_thread = threading.Thread(target=run_request, daemon=True)
    read_thread = threading.Thread(target=read_request, daemon=True)
    try:
        run_thread.start()
        assert adapter.run_started.wait(1)
        read_thread.start()
        assert not read_done.wait(0.1)

        adapter.release_run.set()
        assert run_done.wait(1)
        assert read_done.wait(1)
    finally:
        server.shutdown()

    status, _headers, body = read_result["response"]
    assert status == 200
    assert body == b"print('hello')\n"


def test_write_requests_queue_and_replay_in_order_while_mount_run_is_running():
    adapter = BlockingRunAdapter()
    server = _serve_with_run(adapter)
    run_done = threading.Event()
    first_done = threading.Event()
    second_done = threading.Event()
    results = {}

    def run_request():
        try:
            _request(server, "GET", "/" + _run_entry().name)
        finally:
            run_done.set()

    def put_request(name, body, done):
        results[name] = _request(
            server,
            "PUT",
            f"/{name}.txt",
            body=body,
            headers={"Content-Length": str(len(body))},
        )
        done.set()

    run_thread = threading.Thread(target=run_request, daemon=True)
    first_thread = threading.Thread(target=put_request, args=("first", b"1", first_done), daemon=True)
    second_thread = threading.Thread(target=put_request, args=("second", b"2", second_done), daemon=True)
    try:
        run_thread.start()
        assert adapter.run_started.wait(1)
        first_thread.start()
        time.sleep(0.05)
        second_thread.start()
        assert not first_done.wait(0.05)
        assert not second_done.wait(0.05)

        adapter.release_run.set()
        assert run_done.wait(1)
        assert first_done.wait(1)
        assert second_done.wait(1)
    finally:
        server.shutdown()

    assert results["first"][0] == 201
    assert results["second"][0] == 201
    assert adapter.operations == [
        ("write", "/flash/first.txt"),
        ("write", "/flash/second.txt"),
    ]


def test_propfind_uses_unicode_displayname_for_chinese_paths():
    adapter = FakeAdapter()
    adapter.files = {"/flash/中文.txt": "你好".encode("utf-8")}
    server = _serve(adapter)
    try:
        status, _headers, body = _request(server, "PROPFIND", "/", headers={"Depth": "1"})
    finally:
        server.shutdown()

    text = body.decode("utf-8")
    assert status == 207
    assert "<D:displayname>中文.txt</D:displayname>" in text
    assert "%E4%B8%AD%E6%96%87" in text


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


def test_directory_cache_root_prime_retries_more_than_request_listing():
    adapter = RepeatedTransientEmptyListAdapter(empty_times=3)
    cache = DirectoryCachingWebDavAdapter(
        adapter,
        "/flash",
        WebDavConfig(empty_list_retries=1, startup_empty_list_retries=4, empty_list_retry_delay=0),
    )

    assert cache.prime_root_listing()

    assert adapter.list_calls == 4
    assert [child.path for child in cache.list_dir("/flash")] == ["/flash/main.py"]


def test_directory_cache_root_prime_does_not_cache_empty_root():
    adapter = AlwaysEmptyRootAdapter()
    cache = DirectoryCachingWebDavAdapter(
        adapter,
        "/flash",
        WebDavConfig(empty_list_retries=1, startup_empty_list_retries=2, empty_list_retry_delay=0),
    )

    assert not cache.prime_root_listing()
    assert "/flash" not in cache._dirs

    with pytest.raises(DirectoryListingNotReady):
        cache.list_dir("/flash")
    assert "/flash" not in cache._dirs
    assert adapter.list_calls == 6


def test_directory_cache_keeps_startup_empty_root_unpublished_until_files_appear():
    adapter = RepeatedTransientEmptyListAdapter(empty_times=6)
    cache = DirectoryCachingWebDavAdapter(
        adapter,
        "/flash",
        WebDavConfig(
            empty_list_retries=1,
            startup_empty_list_retries=2,
            empty_list_retry_delay=0,
            startup_empty_list_grace=60,
        ),
    )

    assert not cache.prime_root_listing()
    with pytest.raises(DirectoryListingNotReady):
        cache.list_dir("/flash")
    assert "/flash" not in cache._dirs

    assert [child.path for child in cache.list_dir("/flash")] == ["/flash/main.py"]
    assert [child.path for child in cache.list_dir("/flash")] == ["/flash/main.py"]


def test_directory_cache_empty_root_grace_starts_when_empty_listing_is_seen(monkeypatch):
    now = 100.0
    monkeypatch.setattr(webdav_mount.time, "monotonic", lambda: now)
    adapter = AlwaysEmptyRootAdapter()
    cache = DirectoryCachingWebDavAdapter(
        adapter,
        "/flash",
        WebDavConfig(empty_list_retries=0, startup_empty_list_retries=0, startup_empty_list_grace=5),
    )

    now = 200.0
    assert not cache.prime_root_listing()
    now = 204.0
    with pytest.raises(DirectoryListingNotReady):
        cache.list_dir("/flash")

    now = 206.0
    assert cache.list_dir("/flash") == []


def test_propfind_returns_retry_when_startup_root_listing_is_not_ready():
    adapter = AlwaysEmptyRootAdapter()
    mapper = DevicePathMapper("/flash")
    cache = DirectoryCachingWebDavAdapter(
        adapter,
        mapper.root,
        WebDavConfig(empty_list_retries=0, startup_empty_list_retries=0, startup_empty_list_grace=60),
    )
    handler = make_webdav_handler(cache, mapper, WebDavConfig(empty_list_retry_delay=0))
    server = WebDavThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, headers, body = _request(server, "PROPFIND", "/", headers={"Depth": "1"})
    finally:
        server.shutdown()

    assert status == 503
    assert headers["Retry-After"] == "1"
    assert b"root directory listing is not ready" in body


def test_directory_cache_returns_root_before_background_scan_finishes():
    adapter = BlockingTreeAdapter()
    cache = DirectoryCachingWebDavAdapter(
        adapter,
        "/flash",
        WebDavConfig(empty_list_retry_delay=0),
    )

    children = cache.list_dir("/flash")

    assert [child.path for child in children] == ["/flash/lib", "/flash/main.py"]
    assert adapter.dir_scan_started.wait(1)
    assert adapter.list_calls["/flash"] == 1

    adapter.release_dir_scan.set()
    assert cache.wait_for_background_scan(2)
    before = adapter.list_calls["/flash/lib"]
    cached_children = cache.list_dir("/flash/lib")

    assert [child.path for child in cached_children] == ["/flash/lib/pkg.py"]
    assert adapter.list_calls["/flash/lib"] == before


def test_directory_cache_updates_file_write_without_root_rescan():
    adapter = BlockingTreeAdapter()
    adapter.dirs = {"/flash"}
    cache = DirectoryCachingWebDavAdapter(
        adapter,
        "/flash",
        WebDavConfig(empty_list_retry_delay=0),
    )

    cache.list_dir("/flash")
    assert adapter.list_calls["/flash"] == 1

    cache.write_file("/flash/new.py", b"print(1)\n")
    children = cache.list_dir("/flash")

    assert adapter.list_calls["/flash"] == 1
    assert [child.path for child in children] == ["/flash/main.py", "/flash/new.py"]
    assert cache.stat("/flash/new.py") == DeviceFileStat(path="/flash/new.py", is_dir=False, size=9)


def test_directory_cache_caches_child_stats_from_first_directory_listing():
    adapter = CacheProbeAdapter()
    cache = DirectoryCachingWebDavAdapter(
        adapter,
        "/flash",
        WebDavConfig(empty_list_retry_delay=0),
    )

    assert [child.path for child in cache.list_dir("/flash")] == ["/flash/main.py"]
    assert cache.stat("/flash/main.py") == DeviceFileStat(path="/flash/main.py", is_dir=False, size=15)
    assert [child.path for child in cache.list_dir("/flash")] == ["/flash/main.py"]

    assert adapter.list_calls["/flash"] == 1
    assert adapter.stat_calls == []


def test_directory_cache_invalidates_after_mkcol_and_refreshes_parent_listing():
    adapter = CacheProbeAdapter()
    cache = DirectoryCachingWebDavAdapter(
        adapter,
        "/flash",
        WebDavConfig(empty_list_retry_delay=0),
    )

    assert [child.path for child in cache.list_dir("/flash")] == ["/flash/main.py"]
    cache.make_dir("/flash/lib")
    children = cache.list_dir("/flash")

    assert [child.path for child in children] == ["/flash/lib", "/flash/main.py"]
    assert adapter.list_calls["/flash"] == 2
    assert cache.stat("/flash/lib") == DeviceFileStat(path="/flash/lib", is_dir=True, size=0)


def test_directory_cache_invalidates_after_move_and_drops_source_stat():
    adapter = CacheProbeAdapter()
    cache = DirectoryCachingWebDavAdapter(
        adapter,
        "/flash",
        WebDavConfig(empty_list_retry_delay=0),
    )

    cache.list_dir("/flash")
    assert cache.stat("/flash/main.py") is not None
    cache.move("/flash/main.py", "/flash/renamed.py")
    children = cache.list_dir("/flash")

    assert [child.path for child in children] == ["/flash/renamed.py"]
    assert cache.stat("/flash/main.py") is None
    assert cache.stat("/flash/renamed.py") == DeviceFileStat(path="/flash/renamed.py", is_dir=False, size=15)


def test_directory_cache_invalidates_after_copy_and_refreshes_destination_stat():
    adapter = CacheProbeAdapter()
    cache = DirectoryCachingWebDavAdapter(
        adapter,
        "/flash",
        WebDavConfig(empty_list_retry_delay=0),
    )

    cache.list_dir("/flash")
    cache.copy("/flash/main.py", "/flash/copy.py")
    children = cache.list_dir("/flash")

    assert [child.path for child in children] == ["/flash/copy.py", "/flash/main.py"]
    assert cache.stat("/flash/copy.py") == DeviceFileStat(path="/flash/copy.py", is_dir=False, size=15)


def test_directory_cache_primes_from_recursive_listing_without_incremental_ls():
    adapter = RecursiveListingAdapter()
    cache = DirectoryCachingWebDavAdapter(
        adapter,
        "/flash",
        WebDavConfig(empty_list_retry_delay=0),
    )

    assert cache.prime_from_recursive_listing()

    assert adapter.recursive_calls == ["/flash"]
    assert [child.path for child in cache.list_dir("/flash")] == ["/flash/lib", "/flash/main.py"]
    assert [child.path for child in cache.list_dir("/flash/lib")] == ["/flash/lib/pkg.py"]
    assert cache.stat("/flash/lib/pkg.py") == DeviceFileStat(path="/flash/lib/pkg.py", is_dir=False, size=7)
    assert adapter.list_calls == 0


def test_directory_cache_preserves_recursive_preload_after_file_write():
    adapter = RecursiveListingAdapter()
    cache = DirectoryCachingWebDavAdapter(
        adapter,
        "/flash",
        WebDavConfig(empty_list_retry_delay=0),
    )

    assert cache.prime_from_recursive_listing()
    cache.write_file("/flash/main.py", b"print('updated')\n")

    assert [child.path for child in cache.list_dir("/flash")] == ["/flash/lib", "/flash/main.py"]
    assert cache.stat("/flash/main.py") == DeviceFileStat(path="/flash/main.py", is_dir=False, size=17)
    assert adapter.recursive_calls == ["/flash"]
    assert adapter.list_calls == 0


def test_directory_cache_discards_background_scan_after_delete_invalidation():
    adapter = BlockingTreeAdapter()
    adapter.dirs = {"/flash", "/flash/test"}
    adapter.files = {"/flash/main.py": b"print('hello')\n"}
    cache = DirectoryCachingWebDavAdapter(
        adapter,
        "/flash",
        WebDavConfig(empty_list_retry_delay=0),
    )

    root_children = cache.list_dir("/flash")
    assert [child.path for child in root_children] == ["/flash/test", "/flash/main.py"]
    assert adapter.dir_scan_started.wait(1)

    adapter.dirs.remove("/flash/test")
    cache.delete("/flash/test")
    adapter.release_dir_scan.set()
    assert cache.wait_for_background_scan(2)

    refreshed_children = cache.list_dir("/flash")

    assert [child.path for child in refreshed_children] == ["/flash/main.py"]
    assert cache.stat("/flash/test") is None


def test_device_delete_treats_missing_after_false_result_as_success():
    mp = DeleteReturnsFalseButRemovedMicroPython()
    adapter = MicroPythonWebDavAdapter(mp)

    adapter.delete("/test")

    assert mp.fs_rm_calls == [("/test", True, False)]


def test_device_delete_uses_rmdir_for_empty_directory_before_recursive_rm():
    mp = EmptyDirRemoveMicroPython()
    adapter = MicroPythonWebDavAdapter(mp)

    adapter.delete("/test")

    assert mp.fs_rm_calls == []


def test_delete_is_idempotent_when_file_manager_retries_missing_path():
    adapter = MissingOnDeleteAdapter()
    server = _serve(adapter)
    try:
        status, _headers, body = _request(server, "DELETE", "/test")
    finally:
        server.shutdown()

    assert status == 204
    assert body == b""


def test_put_writes_uploaded_body_to_device_path():
    adapter = FakeAdapter()
    server = _serve(adapter, max_upload_bytes=3)
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


def test_put_rejects_body_larger_than_upload_limit_before_tempfile():
    adapter = FakeAdapter()
    server = _serve(adapter, max_upload_bytes=3)
    try:
        with patch(
            "cli.utils.webdav_mount.tempfile.mkstemp",
            side_effect=AssertionError("oversized PUT should be rejected before tempfile"),
        ):
            status, _headers, body = _request(
                server,
                "PUT",
                "/large.bin",
                body=b"abcd",
                headers={"Content-Length": "4"},
            )
    finally:
        server.shutdown()

    assert status == 413
    assert b"upload too large" in body
    assert adapter.writes == []


def test_put_rejects_invalid_content_length_before_tempfile():
    adapter = FakeAdapter()
    server = _serve(adapter)
    try:
        with patch(
            "cli.utils.webdav_mount.tempfile.mkstemp",
            side_effect=AssertionError("invalid PUT should be rejected before tempfile"),
        ):
            status, _headers, body = _request(
                server,
                "PUT",
                "/bad.bin",
                headers={"Content-Length": "abc"},
            )
    finally:
        server.shutdown()

    assert status == 400
    assert b"invalid Content-Length" in body
    assert adapter.writes == []


def test_put_rejects_run_queue_byte_overflow_before_tempfile():
    adapter = BlockingRunAdapter()
    server = _serve_with_run(
        adapter,
        config=WebDavConfig(empty_list_retry_delay=0, max_upload_bytes=10),
        run_state=MountRunState(max_bytes=1),
    )
    run_done = threading.Event()

    def run_request():
        try:
            _request(server, "GET", "/" + _run_entry().name)
        finally:
            run_done.set()

    run_thread = threading.Thread(target=run_request, daemon=True)
    try:
        run_thread.start()
        assert adapter.run_started.wait(1)
        with patch(
            "cli.utils.webdav_mount.tempfile.mkstemp",
            side_effect=AssertionError("queue overflow should be rejected before tempfile"),
        ):
            status, _headers, body = _request(
                server,
                "PUT",
                "/queued.bin",
                body=b"ab",
                headers={"Content-Length": "2"},
            )

        adapter.release_run.set()
        assert run_done.wait(1)
    finally:
        server.shutdown()

    assert status == 503
    assert b"mount run queue is full" in body
    assert adapter.writes == []


def test_put_preserves_utf8_chinese_body_bytes():
    adapter = FakeAdapter()
    server = _serve(adapter)
    body = "中文内容".encode("utf-8")
    try:
        status, _headers, _body = _request(
            server,
            "PUT",
            "/cn.txt",
            body=body,
            headers={"Content-Length": str(len(body))},
        )
    finally:
        server.shutdown()

    assert status == 201
    assert adapter.writes == [("/flash/cn.txt", body)]


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


def test_mount_accepts_webrepl_options_and_uses_mp_factory():
    mp = MagicMock()

    with patch("cli.main._mp_factory", return_value=mp) as factory, \
         patch("cli.utils.webdav_mount.serve_webdav") as serve:
        result = runner.invoke(app, [
            "mount",
            "COM4",
            "--ws",
            "ws://esp32.local:8266",
            "--password",
            "secret",
            "--no-map",
            "--load-all",
            "--startup-empty-list-grace",
            "12.5",
            "--max-upload-bytes",
            "12345",
        ])

    assert result.exit_code == 0
    factory.assert_called_once_with(
        "COM4",
        None,
        None,
        "ws://esp32.local:8266",
        "secret",
    )
    mp.connect.assert_called_once()
    serve.assert_called_once()
    assert serve.call_args.args[1].startup_empty_list_grace == 12.5
    assert serve.call_args.args[1].max_upload_bytes == 12345
    assert serve.call_args.args[1].load_all is True
    mp.disconnect.assert_called_once()


def test_mount_run_requests_current_mount_session():
    adapter = RunCapableAdapter()
    server = _serve_with_run(adapter)
    _host, port = server.server_address
    try:
        result = runner.invoke(app, [
            "mount-run",
            "--http-port",
            str(port),
            "--path",
            "/app.py",
        ])
    finally:
        server.shutdown()

    assert result.exit_code == 0
    assert "ran /app.py\n" in result.stdout
    assert adapter.run_calls == ["/app.py"]


def test_mount_help_includes_webrepl_options():
    result = runner.invoke(app, ["mount", "--help"])
    help_text = click.utils.strip_ansi(result.stdout)

    assert result.exit_code == 0
    assert "--ws" in help_text
    assert "--password" in help_text
    assert "--max-upload-bytes" in help_text


def test_remount_invokes_mpremote_mount(monkeypatch, tmp_path):
    run_calls = []

    class Result:
        returncode = 0

    monkeypatch.setattr(
        "cli.main.shutil.which",
        lambda name: "C:\\bin\\mpremote.exe" if name == "mpremote" else None,
    )
    monkeypatch.setattr(
        "cli.main.subprocess.run",
        lambda cmd: run_calls.append(cmd) or Result(),
    )

    result = runner.invoke(app, [
        "remount",
        "COM7",
        str(tmp_path),
        "--unsafe-links",
    ])

    assert result.exit_code == 0
    assert run_calls == [[
        "C:\\bin\\mpremote.exe",
        "connect",
        "COM7",
        "mount",
        "--unsafe-links",
        os.path.abspath(str(tmp_path)),
    ]]


def test_remount_reports_missing_mpremote(monkeypatch, tmp_path):
    monkeypatch.setattr("cli.main.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "cli.main.subprocess.run",
        lambda _cmd: pytest.fail("mpremote should not be started"),
    )

    result = runner.invoke(app, ["remount", "COM7", str(tmp_path)])

    assert result.exit_code == 1
    assert "mpremote" in result.output


def test_top_level_help_lists_remount_not_firmware():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "remount" in result.stdout
    assert "firmware" not in result.stdout

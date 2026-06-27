"""Project development watch mode."""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Set

from ..utils.ui import _GREEN, _RESET, _YELLOW
from ..utils.config import DEFAULT_BAUDRATE, HASH_CONFIG_FILE
from ..utils.log import get_logger
from .sync import ProjectSyncManager

log = get_logger(__name__)
_SOFT_REBOOT = b"\x04"

_WATCH_FILE_NAMES = {"manifest.py", ".pyrite_config.json", "pyproject.toml"}
_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".pyrite_cache",
    ".venv",
    "venv",
    "env",
    "build",
    "dist",
}
_IGNORED_FILES = {HASH_CONFIG_FILE}


@dataclass
class DevOptions:
    port: str
    local_dir: str = "."
    remote_path: str = "/"
    baudrate: int = DEFAULT_BAUDRATE
    timeout: int = 10
    no_compile: bool = False
    target: Optional[str] = None
    feature: Optional[str] = None
    no_feature: Optional[str] = None
    manifest_path: Optional[str] = None
    hash_config_path: Optional[str] = None
    ws: Optional[str] = None
    password: Optional[str] = None
    dry_run: bool = False
    auto_run: bool = False
    no_repl: bool = False
    map_traceback: bool = False
    once: bool = False
    poll_interval: float = 0.3
    debounce: float = 0.5
    on_error: str = "continue"
    changed_paths: Optional[Set[str]] = field(default=None, repr=False)


class ProjectWatcher:
    """Fast mtime/size watcher for project development loops."""

    def __init__(
        self,
        root: str,
        *,
        manifest_path: Optional[str] = None,
        poll_interval: float = 0.3,
        debounce: float = 0.5,
    ) -> None:
        self.root = Path(root).resolve()
        self.manifest_path = Path(manifest_path).resolve() if manifest_path else None
        self.poll_interval = max(0.05, poll_interval)
        self.debounce = max(0.0, debounce)

    def snapshot(self) -> dict[str, tuple[int, int]]:
        result: dict[str, tuple[int, int]] = {}
        if not self.root.exists():
            return result

        for current_root, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in _IGNORED_DIRS]
            for name in files:
                if not self._should_watch_name(name):
                    continue
                path = Path(current_root) / name
                self._add_stat(result, path)

        if self.manifest_path and self.manifest_path.exists():
            self._add_stat(result, self.manifest_path)
        return result

    @staticmethod
    def changed_paths(
        before: dict[str, tuple[int, int]],
        after: dict[str, tuple[int, int]],
    ) -> Set[str]:
        changed: Set[str] = set()
        for path, stat in after.items():
            if before.get(path) != stat:
                changed.add(path)
        for path in before:
            if path not in after:
                changed.add(path)
        return changed

    def wait_for_stable_change(
        self,
        baseline: dict[str, tuple[int, int]],
        stop_event: threading.Event,
    ) -> tuple[dict[str, tuple[int, int]], Set[str]]:
        pending: Set[str] = set()
        latest = baseline
        last_change = 0.0
        while not stop_event.is_set():
            time.sleep(self.poll_interval)
            current = self.snapshot()
            changed = self.changed_paths(latest, current)
            if changed:
                pending.update(changed)
                latest = current
                last_change = time.monotonic()
                continue
            if pending and time.monotonic() - last_change >= self.debounce:
                return latest, pending
        return latest, set()

    @staticmethod
    def _should_watch_name(name: str) -> bool:
        if name in _IGNORED_FILES:
            return False
        return name.endswith(".py") or name in _WATCH_FILE_NAMES

    @staticmethod
    def _add_stat(result: dict[str, tuple[int, int]], path: Path) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        result[str(path)] = (stat.st_mtime_ns, stat.st_size)


class DevSession:
    def __init__(
        self,
        options: DevOptions,
        *,
        mp_factory: Callable[..., object],
        manager_factory: Callable[[object], ProjectSyncManager] = ProjectSyncManager,
        stderr=None,
    ) -> None:
        self.options = options
        self.mp = mp_factory(
            options.port,
            options.baudrate,
            options.timeout,
            options.ws,
            options.password,
        )
        self.manager = manager_factory(self.mp)
        self.stderr = stderr if stderr is not None else sys.stderr
        self._stop = threading.Event()
        self._sync_queue: queue.Queue[Optional[Set[str]]] = queue.Queue()
        self._watch_thread: Optional[threading.Thread] = None
        self._bytecode_ver: Optional[int] = None
        self._arch: Optional[str] = None
        self._active_tags: Optional[Set[str]] = None
        self._device_context_ready = False

    def run(self) -> None:
        if self.options.no_repl:
            self._run_without_repl()
            return

        self._ensure_connected()
        self._run_sync_with_error_policy(self.options.changed_paths)
        self._start_watcher()
        try:
            self.mp.repl_(
                command_handler=self.handle_repl_command,
                idle_hook=self.process_pending_sync,
                output_mapper=self._traceback_output_mapper(),
            )
        finally:
            self._stop.set()
            self._join_watcher()
            self._disconnect()

    def _run_without_repl(self) -> None:
        self._ensure_connected()
        try:
            self._run_sync_with_error_policy(self.options.changed_paths)
            if self.options.once:
                return
            watcher = ProjectWatcher(
                self.options.local_dir,
                manifest_path=self.options.manifest_path,
                poll_interval=self.options.poll_interval,
                debounce=self.options.debounce,
            )
            snapshot = watcher.snapshot()
            while not self._stop.is_set():
                snapshot, changed = watcher.wait_for_stable_change(snapshot, self._stop)
                if changed:
                    self._run_sync_with_error_policy(self._fast_changed_paths(changed))
        except KeyboardInterrupt:
            log.info("用户中断")
        finally:
            self._disconnect()

    def sync_once(self, changed_paths: Optional[Set[str]] = None):
        self._ensure_device_context()
        self._write_busy()
        success = False
        try:
            results = self.manager.flash(
                self.options.local_dir,
                self.options.remote_path,
                hash_config_path=self.options.hash_config_path,
                bytecode_ver=self._bytecode_ver,
                arch=self._arch,
                active_tags=self._active_tags or None,
                manifest_path=self.options.manifest_path,
                dry_run=self.options.dry_run,
                changed_paths=changed_paths,
            )
            success = all(item[2] for item in results)
            return results
        finally:
            self._return_to_repl()
            if success and self.options.auto_run and not self.options.dry_run:
                self.soft_reboot()
            self._write_ready()

    def process_pending_sync(self) -> None:
        changed = self._drain_sync_queue()
        if changed is None:
            return
        self._run_sync_with_error_policy(self._fast_changed_paths(changed))

    def handle_repl_command(self, data: bytes) -> bool:
        try:
            command = data.decode("utf-8", errors="ignore").strip()
        except Exception:
            return False
        if command not in {"run", ":run"}:
            return False
        self.soft_reboot()
        return True

    def soft_reboot(self) -> None:
        self.stderr.write(f"{_GREEN}[dev] soft reboot (boot.py/main.py){_RESET}\n")
        self.stderr.flush()
        self.mp._write(_SOFT_REBOOT)

    def _run_sync_with_error_policy(self, changed_paths: Optional[Set[str]]) -> None:
        try:
            self.sync_once(changed_paths)
        except Exception as exc:
            log.error("dev sync failed: %s", exc)
            if self.options.on_error == "stop":
                raise
            if self.options.on_error == "keep-repl":
                self._return_to_repl()

    def _start_watcher(self) -> None:
        watcher = ProjectWatcher(
            self.options.local_dir,
            manifest_path=self.options.manifest_path,
            poll_interval=self.options.poll_interval,
            debounce=self.options.debounce,
        )

        def loop() -> None:
            snapshot = watcher.snapshot()
            while not self._stop.is_set():
                snapshot, changed = watcher.wait_for_stable_change(snapshot, self._stop)
                if changed:
                    self._sync_queue.put(changed)

        self._watch_thread = threading.Thread(target=loop, daemon=True)
        self._watch_thread.start()

    def _join_watcher(self) -> None:
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=1)

    def _drain_sync_queue(self) -> Optional[Set[str]]:
        changed: Set[str] = set()
        while True:
            try:
                item = self._sync_queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                return None
            changed.update(item)
        return changed or None

    def _fast_changed_paths(self, changed: Set[str]) -> Optional[Set[str]]:
        if not changed:
            return None
        root = Path(self.options.local_dir).resolve()
        fast: Set[str] = set()
        for raw in changed:
            path = Path(raw)
            if not path.exists() or path.name in _WATCH_FILE_NAMES:
                return None
            if path.name in _IGNORED_FILES or not path.name.endswith(".py"):
                return None
            try:
                path.resolve().relative_to(root)
            except ValueError:
                return None
            fast.add(str(path))
        return fast or None

    def _ensure_connected(self) -> None:
        if not getattr(self.mp, "is_connected", False):
            self.mp.connect()

    def _ensure_device_context(self) -> None:
        self._ensure_connected()
        if self.options.no_compile:
            self.mp.config.auto_compile = False
        if self._device_context_ready:
            return
        if self.options.no_compile:
            self._bytecode_ver, self._arch = None, None
        else:
            self._bytecode_ver, self._arch = self.mp.get_mpy_version()
        self._active_tags = self._resolve_active_tags()
        self._device_context_ready = True

    def _resolve_active_tags(self) -> Set[str]:
        if self.options.target:
            target = self.options.target.upper()
            tags = set(self.mp.config.board_tags.get(target, [target]))
            tags.add(target)
        else:
            tags = set(self.mp.detect_tags())
            if not tags:
                raise RuntimeError("无法识别设备 target，请使用 --target 手动指定")
        tags.update(_split_tags(self.options.feature))
        tags.difference_update(_split_tags(self.options.no_feature))
        return tags

    def _return_to_repl(self) -> None:
        try:
            self.mp._exit_raw_repl()
        except Exception as exc:
            log.debug("return to repl skipped: %s", exc)

    def _traceback_output_mapper(self):
        if not self.options.map_traceback:
            return None
        from ..utils.traceback_map import create_traceback_output_mapper

        return create_traceback_output_mapper(
            local_dir=self.options.local_dir,
            remote_prefix=self.options.remote_path,
            manifest_path=self.options.manifest_path,
            active_tags=self._active_tags or set(),
            auto_compile=not self.options.no_compile,
        )

    def _disconnect(self) -> None:
        try:
            self.mp.disconnect()
        except Exception:
            pass

    def _write_busy(self) -> None:
        self.stderr.write(f"{_YELLOW}[dev] 正在刷入，REPL 暂不可用...{_RESET}\n")
        self.stderr.flush()

    def _write_ready(self) -> None:
        self.stderr.write(f"{_GREEN}[dev] 刷入结束，REPL 可用{_RESET}\n")
        self.stderr.flush()


def run_project_dev(
    options: DevOptions,
    *,
    mp_factory: Callable[..., object],
    manager_factory: Callable[[object], ProjectSyncManager] = ProjectSyncManager,
    stderr=None,
) -> None:
    DevSession(
        options,
        mp_factory=mp_factory,
        manager_factory=manager_factory,
        stderr=stderr,
    ).run()


def _split_tags(value: Optional[str]) -> Set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}

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
from ..utils.device_context import CommandNeeds, needs_no_mpy, prepare_device
from ..utils.device_tests import (
    DeviceTestPlan,
    DeviceTestSession,
    discover_device_tests,
    run_device_test_plan,
)
from ..utils.log import get_logger
from .sync import ProjectSyncManager

log = get_logger(__name__)
_SOFT_REBOOT = b"\x04"
DEV_NEEDS = CommandNeeds(
    connection=True,
    raw_repl=True,
    repl_preempt=True,
    device_context=True,
    active_tags=True,
    mpy_version=True,
)

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
    lens: bool = False
    open_editor: bool = False
    once: bool = False
    poll_interval: float = 0.3
    debounce: float = 0.5
    on_error: str = "continue"
    test_on_save: str = "off"
    test_path: Optional[str] = None
    test_timeout: int = 10
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
        test_runner: Callable[..., DeviceTestSession] = run_device_test_plan,
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
        self.test_runner = test_runner
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
        if self.options.once:
            self._disconnect()
            return
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
        results = []
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
            if success and self._tests_enabled() and not self.options.dry_run:
                test_ok = self._run_tests_after_sync(changed_paths)
                if self.options.once and not test_ok:
                    raise SystemExit(1)

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
        needs = DEV_NEEDS
        if self.options.no_compile:
            needs = needs_no_mpy(DEV_NEEDS)
        prepared = prepare_device(
            self.mp,
            needs,
            target=self.options.target,
            feature=self.options.feature,
            no_feature=self.options.no_feature,
        )
        self._bytecode_ver, self._arch = prepared.bytecode_ver, prepared.arch
        self._active_tags = prepared.active_tags or set()
        self._device_context_ready = True

    def _return_to_repl(self) -> None:
        try:
            self.mp._exit_raw_repl()
        except Exception as exc:
            log.debug("return to repl skipped: %s", exc)

    def _traceback_output_mapper(self):
        if not (self.options.map_traceback or self.options.lens):
            return None
        from ..utils.traceback_map import create_traceback_output_mapper

        return create_traceback_output_mapper(
            local_dir=self.options.local_dir,
            remote_prefix=self.options.remote_path,
            manifest_path=self.options.manifest_path,
            active_tags=self._active_tags or set(),
            auto_compile=not self.options.no_compile,
            lens=self.options.lens,
            open_editor=self.options.open_editor,
        )

    def _tests_enabled(self) -> bool:
        return self.options.test_on_save != "off"

    def _run_tests_after_sync(self, changed_paths: Optional[Set[str]]) -> bool:
        try:
            plan = self._discover_test_plan(changed_paths)
        except FileNotFoundError as exc:
            self.stderr.write(f"[test] {exc}; skipping\n")
            self.stderr.flush()
            return True
        except ValueError as exc:
            self.stderr.write(f"[test] {exc}; skipping\n")
            self.stderr.flush()
            return True

        if not plan.files:
            self.stderr.write("[test] no matching device tests; skipping\n")
            self.stderr.flush()
            return True

        self.stderr.write(f"[test] running {len(plan.files)} device tests...\n")
        self.stderr.flush()
        try:
            session = self.test_runner(
                self.mp,
                plan,
                timeout=self.options.test_timeout,
                keep_files=False,
            )
        except Exception as exc:
            self.stderr.write(f"[test] ERROR device test run failed: {exc}\n")
            self.stderr.flush()
            return False

        mapper = self._traceback_output_mapper()
        by_path = {result.remote_path: result for result in session.results}
        for item in session.plan.files:
            result = by_path.get(item.remote_path)
            if result is None:
                self.stderr.write(f"[test] MISS {item.relative_path} (no result)\n")
                continue
            label = result.status.upper()
            self.stderr.write(
                f"[test] {label} {item.relative_path}  {result.duration_ms}ms\n"
            )
            if result.stdout:
                self._write_test_block(result.stdout, mapper)
            if result.error:
                self._write_test_block(result.error, mapper)
        self.stderr.flush()
        return session.ok

    def _discover_test_plan(
        self,
        changed_paths: Optional[Set[str]],
    ) -> DeviceTestPlan:
        selected_path = self.options.test_path
        plan = discover_device_tests(
            selected_path,
            cwd=self.options.local_dir,
        )
        if self.options.test_on_save != "changed":
            return plan

        selected = _filter_changed_tests(plan, changed_paths)
        if not selected:
            return DeviceTestPlan(files=[], remote_dir=plan.remote_dir)
        return DeviceTestPlan(files=selected, remote_dir=plan.remote_dir)

    def _write_test_block(self, text: str, mapper) -> None:
        if mapper is not None:
            text = mapper(text)
        for line in text.splitlines():
            self.stderr.write(f"    {line}\n")

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
    test_runner: Callable[..., DeviceTestSession] = run_device_test_plan,
    stderr=None,
) -> None:
    DevSession(
        options,
        mp_factory=mp_factory,
        manager_factory=manager_factory,
        test_runner=test_runner,
        stderr=stderr,
    ).run()


def _split_tags(value: Optional[str]) -> Set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def normalize_test_on_save(value: Optional[str | bool]) -> str:
    if value is None or value is False:
        return "off"
    if value is True:
        return "all"
    normalized = str(value).strip().lower()
    if normalized == "":
        return "all"
    if normalized in {"1", "true", "yes", "on"}:
        return "all"
    if normalized in {"0", "false", "no", "off"}:
        return "off"
    if normalized in {"all", "changed"}:
        return normalized
    raise ValueError("--test-on-save must be one of all, changed, or off")


def _filter_changed_tests(
    plan: DeviceTestPlan,
    changed_paths: Optional[Set[str]],
) -> list:
    if not changed_paths:
        return plan.files

    changed_names = {
        Path(raw).stem.removeprefix("test_")
        for raw in changed_paths
        if str(raw).endswith(".py")
    }
    if not changed_names:
        return plan.files

    selected = []
    for item in plan.files:
        test_stem = item.local_path.stem.removeprefix("test_")
        if test_stem in changed_names or any(
            name and name in test_stem for name in changed_names
        ):
            selected.append(item)
    return selected

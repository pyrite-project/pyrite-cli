import io
import json
from pathlib import Path
from unittest.mock import MagicMock

from cli.reg_commands.project import _resolve_dev_deep_options
from cli.project.dev import DevOptions, DevSession, ProjectWatcher, run_project_dev
from cli.project.sync import ProjectSyncManager, compute_file_hash
from cli.utils.flash import SET_EXECUTE


class _FakeConfig:
    auto_compile = True
    board_tags = {"ESP32_S3": ["ESP32", "wifi"]}


class _FakeMicroPython:
    def __init__(self):
        self.config = _FakeConfig()
        self.connected = False
        self.writes = []
        self.exits = 0
        self.disconnects = 0
        self.repl_kwargs = None

    @property
    def is_connected(self):
        return self.connected

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.disconnects += 1
        self.connected = False

    def get_mpy_version(self):
        return 6, "xtensa"

    def detect_tags(self):
        return {"ESP32"}

    def _write(self, data):
        self.writes.append(data)

    def _exit_raw_repl(self):
        self.exits += 1

    def repl_(self, **kwargs):
        self.repl_kwargs = kwargs


def test_project_watcher_ignores_hash_config_updates(tmp_path: Path):
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    watcher = ProjectWatcher(str(tmp_path))
    before = watcher.snapshot()

    (tmp_path / "pyrite_file_config.json").write_text("{}", encoding="utf-8")
    after_hash = watcher.snapshot()

    assert watcher.changed_paths(before, after_hash) == set()

    (tmp_path / "main.py").write_text("print('changed')\n", encoding="utf-8")
    after_source = watcher.snapshot()

    assert watcher.changed_paths(after_hash, after_source) == {
        str(tmp_path / "main.py")
    }


def test_project_flash_can_limit_work_to_changed_local_paths(tmp_path: Path):
    changed = tmp_path / "main.py"
    unchanged = tmp_path / "lib.py"
    changed.write_text("print('new')\n", encoding="utf-8")
    unchanged.write_text("print('same')\n", encoding="utf-8")

    hash_config = tmp_path / "pyrite_file_config.json"
    hash_config.write_text(
        json.dumps({
            "version": 1,
            "hash_algorithm": "sha256",
            "files": {
                "main.py": "old",
                "lib.py": compute_file_hash(str(unchanged)),
            },
        }),
        encoding="utf-8",
    )

    mp = MagicMock()
    mp.flash_entries.return_value = [(str(changed), "/app/main.py", True)]

    ProjectSyncManager(mp).flash(
        str(tmp_path),
        "/app",
        hash_config_path=str(hash_config),
        changed_paths={str(changed)},
    )

    mp.flash_entries.assert_called_once()
    assert mp.flash_entries.call_args.args[0] == [(str(changed), "/app/main.py")]

    saved = json.loads(hash_config.read_text(encoding="utf-8"))["files"]
    assert saved["main.py"] == compute_file_hash(str(changed))
    assert saved["lib.py"] == compute_file_hash(str(unchanged))


def test_dev_session_intercepts_run_as_soft_reboot(tmp_path: Path):
    mp = _FakeMicroPython()
    session = DevSession(
        DevOptions(port="COM99", local_dir=str(tmp_path)),
        mp_factory=lambda *_args, **_kwargs: mp,
        manager_factory=lambda _mp: MagicMock(),
    )

    assert session.handle_repl_command(b":run\r") is True
    assert mp.writes == [SET_EXECUTE]

    assert session.handle_repl_command(b"print(1)\r") is False


def test_run_project_dev_once_prints_busy_and_ready_status(tmp_path: Path):
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    mp = _FakeMicroPython()
    manager = MagicMock()
    manager.flash.return_value = [(str(tmp_path / "main.py"), "/main.py", True)]
    stderr = io.StringIO()

    run_project_dev(
        DevOptions(
            port="COM99",
            local_dir=str(tmp_path),
            no_repl=True,
            once=True,
            changed_paths={str(tmp_path / "main.py")},
        ),
        mp_factory=lambda *_args, **_kwargs: mp,
        manager_factory=lambda _mp: manager,
        stderr=stderr,
    )

    out = stderr.getvalue()
    assert "\033[33m" in out
    assert "REPL 暂不可用" in out
    assert "\033[32m" in out
    assert "REPL 可用" in out
    assert manager.flash.call_args.kwargs["changed_paths"] == {
        str(tmp_path / "main.py")
    }


def test_run_project_dev_once_exits_before_repl_by_default(tmp_path: Path):
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    mp = _FakeMicroPython()
    manager = MagicMock()
    manager.flash.return_value = [(str(tmp_path / "main.py"), "/main.py", True)]

    run_project_dev(
        DevOptions(
            port="COM99",
            local_dir=str(tmp_path),
            once=True,
        ),
        mp_factory=lambda *_args, **_kwargs: mp,
        manager_factory=lambda _mp: manager,
        stderr=io.StringIO(),
    )

    assert mp.repl_kwargs is None
    assert mp.disconnects == 1
    manager.flash.assert_called_once()


def test_deep_dev_defaults_to_run_and_traceback_mapping():
    assert _resolve_dev_deep_options(
        deep=False,
        auto_run=None,
        map_traceback=None,
    ) == (False, False)
    assert _resolve_dev_deep_options(
        deep=True,
        auto_run=None,
        map_traceback=None,
    ) == (True, True)


def test_deep_dev_respects_explicit_fine_grained_options():
    assert _resolve_dev_deep_options(
        deep=True,
        auto_run=False,
        map_traceback=False,
    ) == (False, False)
    assert _resolve_dev_deep_options(
        deep=False,
        auto_run=True,
        map_traceback=True,
    ) == (True, True)


def test_dev_session_passes_traceback_mapper_to_repl(tmp_path: Path):
    source = tmp_path / "lib" / "sensor.py"
    source.parent.mkdir()
    source.write_text("print('sensor')\n", encoding="utf-8")
    mp = _FakeMicroPython()
    manager = MagicMock()
    manager.flash.return_value = [(str(source), "/app/lib/sensor.mpy", True)]

    run_project_dev(
        DevOptions(
            port="COM99",
            local_dir=str(tmp_path),
            remote_path="/app",
            map_traceback=True,
        ),
        mp_factory=lambda *_args, **_kwargs: mp,
        manager_factory=lambda _mp: manager,
        stderr=io.StringIO(),
    )

    output_mapper = mp.repl_kwargs["output_mapper"]
    mapped = output_mapper('  File "/app/lib/sensor.mpy", line 3, in read\n')

    assert (
        "/app/lib/sensor.mpy:3 -> lib/sensor.py "
        "(.mpy bytecode; source line unavailable)"
    ) in mapped

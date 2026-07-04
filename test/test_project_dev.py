import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import click
from typer.testing import CliRunner

from cli.main import app, _normalize_optional_value_flags
from cli.reg_commands.project import _resolve_dev_deep_options
from cli.project.dev import (
    DevOptions,
    DevSession,
    ProjectWatcher,
    normalize_test_on_save,
    run_project_dev,
)
from cli.project.sync import ProjectSyncManager, compute_file_hash
from cli.utils.flash import SET_EXECUTE
from cli.utils.device_tests import (
    DeviceTestFile,
    DeviceTestPlan,
    DeviceTestResult,
    DeviceTestSession,
)


runner = CliRunner()


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

    (tmp_path / "config.json").write_text('{"mode":"dev"}\n', encoding="utf-8")
    after_data = watcher.snapshot()

    assert watcher.changed_paths(after_source, after_data) == {
        str(tmp_path / "config.json")
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


def test_dev_session_lens_expands_traceback_output(tmp_path: Path):
    source = tmp_path / "lib" / "sensor.py"
    source.parent.mkdir()
    source.write_text(
        "\n".join(
            [
                "def one():",
                "    return 1",
                "def read():",
                "    value = missing_name",
                "    return value",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    mp = _FakeMicroPython()
    manager = MagicMock()
    manager.flash.return_value = [(str(source), "/app/lib/sensor.py", True)]

    run_project_dev(
        DevOptions(
            port="COM99",
            local_dir=str(tmp_path),
            remote_path="/app",
            lens=True,
        ),
        mp_factory=lambda *_args, **_kwargs: mp,
        manager_factory=lambda _mp: manager,
        stderr=io.StringIO(),
    )

    output_mapper = mp.repl_kwargs["output_mapper"]
    mapped = output_mapper('  File "/app/lib/sensor.py", line 4, in read\n')

    assert "/app/lib/sensor.py:4 -> lib/sensor.py:4" in mapped
    assert "\nlib/sensor.py\n" in mapped
    assert "> 4 |     value = missing_name" in mapped


def test_dev_session_test_on_save_runs_device_tests_after_successful_sync(tmp_path: Path):
    source = tmp_path / "main.py"
    test_file = tmp_path / "test_device" / "test_main.py"
    source.write_text("print('hi')\n", encoding="utf-8")
    test_file.parent.mkdir()
    test_file.write_text("assert True\n", encoding="utf-8")
    mp = _FakeMicroPython()
    manager = MagicMock()
    manager.flash.return_value = [(str(source), "/app/main.py", True)]
    plan = DeviceTestPlan(
        files=[
            DeviceTestFile(
                local_path=test_file,
                relative_path="test_main.py",
                remote_path="/.pyrite_tests/test_main.py",
            )
        ],
        remote_dir="/.pyrite_tests",
    )
    session = DeviceTestSession(
        plan=plan,
        results=[
            DeviceTestResult(
                index=0,
                status="pass",
                remote_path="/.pyrite_tests/test_main.py",
                stdout="",
                error="",
                duration_ms=18,
            )
        ],
        raw_output="",
    )
    run_tests = MagicMock(return_value=session)
    stderr = io.StringIO()

    run_project_dev(
        DevOptions(
            port="COM99",
            local_dir=str(tmp_path),
            remote_path="/app",
            no_repl=True,
            once=True,
            test_on_save="all",
            test_path=str(test_file.parent),
        ),
        mp_factory=lambda *_args, **_kwargs: mp,
        manager_factory=lambda _mp: manager,
        test_runner=run_tests,
        stderr=stderr,
    )

    run_tests.assert_called_once()
    assert run_tests.call_args.args[0] is mp
    assert run_tests.call_args.args[1].files[0].relative_path == "test_main.py"
    assert "[test] running 1 device tests" in stderr.getvalue()
    assert "[test] PASS test_main.py" in stderr.getvalue()


def test_dev_session_once_test_failure_exits_for_ci(tmp_path: Path):
    source = tmp_path / "main.py"
    test_file = tmp_path / "test_device" / "test_main.py"
    source.write_text("print('hi')\n", encoding="utf-8")
    test_file.parent.mkdir()
    test_file.write_text("assert False\n", encoding="utf-8")
    mp = _FakeMicroPython()
    manager = MagicMock()
    manager.flash.return_value = [(str(source), "/app/main.py", True)]
    plan = DeviceTestPlan(
        files=[
            DeviceTestFile(
                local_path=test_file,
                relative_path="test_main.py",
                remote_path="/.pyrite_tests/test_main.py",
            )
        ],
        remote_dir="/.pyrite_tests",
    )
    session = DeviceTestSession(
        plan=plan,
        results=[
            DeviceTestResult(
                index=0,
                status="fail",
                remote_path="/.pyrite_tests/test_main.py",
                stdout="",
                error='Traceback\n  File "/app/main.py", line 1\nAssertionError\n',
                duration_ms=4,
            )
        ],
        raw_output="",
    )

    try:
        run_project_dev(
            DevOptions(
                port="COM99",
                local_dir=str(tmp_path),
                remote_path="/app",
                no_repl=True,
                once=True,
                map_traceback=True,
                test_on_save="all",
                test_path=str(test_file.parent),
            ),
            mp_factory=lambda *_args, **_kwargs: mp,
            manager_factory=lambda _mp: manager,
            test_runner=MagicMock(return_value=session),
            stderr=io.StringIO(),
        )
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("expected failed --once --test-on-save to exit with 1")


def test_project_dev_help_exposes_lens_and_test_on_save_options():
    result = runner.invoke(app, ["project", "dev", "--help"])
    help_text = click.utils.strip_ansi(result.stdout)

    assert result.exit_code == 0
    assert "--lens" in help_text
    assert "--open-editor" in help_text
    assert "--test-on-save" in help_text
    assert "--test-path" in help_text


def test_project_dev_normalizes_bare_test_on_save_flag():
    argv = ["pyrcli", "project", "dev", "COM3", ".", "/app", "--test-on-save"]

    _normalize_optional_value_flags(argv)

    assert argv == [
        "pyrcli",
        "project",
        "dev",
        "COM3",
        ".",
        "/app",
        "--test-on-save=all",
    ]
    assert normalize_test_on_save("changed") == "changed"

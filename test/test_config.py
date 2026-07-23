import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cli.utils import config as config_module
from cli.utils.config import (
    _load_config,
    create_default_config,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_BAUDRATE,
    CONFIG_FILE,
)
from cli.utils.config.types import PyriteConfig


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── _load_config ────────────────────────────────────────────────────


class TestLoadConfig:
    def test_non_object_config_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / CONFIG_FILE).write_text("[1, 2, 3]", encoding="utf-8")

        cfg = _load_config()

        assert cfg.chunk_size == DEFAULT_CHUNK_SIZE
        assert cfg.baudrate == DEFAULT_BAUDRATE

    def test_defaults_when_no_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        cfg = _load_config()
        assert cfg.chunk_size == DEFAULT_CHUNK_SIZE
        assert cfg.download_threads == 4
        assert cfg.auto_compile is True
        assert cfg.verify == "size"
        assert cfg.max_retries == 2
        assert cfg.baudrate == DEFAULT_BAUDRATE
        assert cfg.timeout == 10
        assert not hasattr(cfg, "delta_min_size")
        assert cfg.precheck == "basic"
        assert cfg.precheck_compat == "warn"
        assert cfg.precheck_mp_version == ""
        assert "ESP32" in cfg.board_tags

    def test_custom_chunk_size(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"chunk_size": 8192})
        assert _load_config().chunk_size == 8192

    def test_invalid_chunk_size_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"chunk_size": -1})
        assert _load_config().chunk_size == DEFAULT_CHUNK_SIZE

    def test_zero_chunk_size_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"chunk_size": 0})
        assert _load_config().chunk_size == DEFAULT_CHUNK_SIZE

    def test_string_chunk_size_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"chunk_size": "1024"})
        assert _load_config().chunk_size == DEFAULT_CHUNK_SIZE

    def test_download_threads_clamped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"download_threads": 100})
        assert _load_config().download_threads == 12

    def test_download_threads_zero_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"download_threads": 0})
        assert _load_config().download_threads == 4

    def test_auto_compile_off(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"auto_compile": False})
        assert _load_config().auto_compile is False

    def test_verify_modes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        for mode in ("off", "size", "crc32"):
            _write_json(tmp_path / CONFIG_FILE, {"verify": mode})
            assert _load_config().verify == mode

    def test_delta_flash_modes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        for mode in ("off", "auto", "on"):
            _write_json(tmp_path / CONFIG_FILE, {"delta_flash": mode})
            assert _load_config().delta_flash == mode

    def test_invalid_delta_flash_mode_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"delta_flash": "always"})
        assert _load_config().delta_flash == "auto"

    def test_delta_min_size_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"delta_min_size": 2048})
        assert not hasattr(_load_config(), "delta_min_size")

    def test_invalid_verify_mode_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"verify": "sha256"})
        assert _load_config().verify == "size"

    def test_precheck_modes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        for mode in ("off", "basic", "strict"):
            _write_json(tmp_path / CONFIG_FILE, {"precheck": mode})
            assert _load_config().precheck == mode

    def test_precheck_compat_modes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        for mode in ("warn", "error", "off"):
            _write_json(tmp_path / CONFIG_FILE, {"precheck_compat": mode})
            assert _load_config().precheck_compat == mode

    def test_precheck_mp_version_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"precheck_mp_version": " 1.20.0 "})
        assert _load_config().precheck_mp_version == "1.20.0"

    def test_invalid_precheck_config_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {
            "precheck": "always",
            "precheck_compat": "fail",
        })
        cfg = _load_config()
        assert cfg.precheck == "basic"
        assert cfg.precheck_compat == "warn"

    def test_max_retries_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"max_retries": 0})
        assert _load_config().max_retries == 0

    def test_custom_baudrate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"baudrate": 1500000})
        assert _load_config().baudrate == 1500000

    def test_custom_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"timeout": 25})
        assert _load_config().timeout == 25

    @pytest.mark.parametrize("value", [0, -1, "25", True])
    def test_invalid_timeout_ignored(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        value,
    ):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"timeout": value})
        assert _load_config().timeout == 10

    @pytest.mark.parametrize("value", [True, 1.5])
    def test_invalid_integer_config_values_are_ignored(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        value,
    ):
        monkeypatch.chdir(tmp_path)
        _write_json(
            tmp_path / CONFIG_FILE,
            {
                "chunk_size": value,
                "download_threads": value,
                "max_retries": value,
                "baudrate": value,
            },
        )

        cfg = _load_config()

        assert cfg.chunk_size == DEFAULT_CHUNK_SIZE
        assert cfg.download_threads == 4
        assert cfg.max_retries == 2
        assert cfg.baudrate == DEFAULT_BAUDRATE

    @pytest.mark.parametrize(
        "legacy",
        [
            {"profile": "fast"},
            {"profiles": {"fast": {"baudrate": 460800, "timeout": 30}}},
            {
                "profile": "fast",
                "profiles": {"fast": {"baudrate": 460800, "timeout": 30}},
            },
        ],
    )
    def test_legacy_profile_keys_warn_once_and_are_ignored(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        legacy: dict,
    ):
        monkeypatch.chdir(tmp_path)
        _write_json(
            tmp_path / CONFIG_FILE,
            {"baudrate": 115200, "timeout": 7, **legacy},
        )
        warning = MagicMock()
        monkeypatch.setattr(config_module.log, "warning", warning)

        cfg = _load_config()

        assert cfg.baudrate == 115200
        assert cfg.timeout == 7
        warning.assert_called_once()
        assert "profile" in warning.call_args.args[0]

    def test_legacy_profile_warning_is_deduplicated_for_same_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"profile": "fast"})
        warning = MagicMock()
        monkeypatch.setattr(config_module.log, "warning", warning)

        _load_config()
        _load_config()

        warning.assert_called_once()

    def test_negative_max_retries_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"max_retries": -1})
        assert _load_config().max_retries == 2

    def test_corrupted_json_falls_back(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / CONFIG_FILE).write_text("{broken", encoding="utf-8")
        cfg = _load_config()
        assert cfg.chunk_size == DEFAULT_CHUNK_SIZE

    def test_parent_directory_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        sub = tmp_path / "sub" / "dir"
        sub.mkdir(parents=True)
        _write_json(tmp_path / CONFIG_FILE, {"chunk_size": 2048})
        monkeypatch.chdir(sub)
        assert _load_config().chunk_size == 2048

    def test_pyproject_board_tags(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[tool.pyrite.board_tags]\nCUSTOM_BOARD = ["custom", "v2"]\n',
            encoding="utf-8",
        )
        cfg = _load_config()
        assert "CUSTOM_BOARD" in cfg.board_tags
        assert cfg.board_tags["CUSTOM_BOARD"] == ["custom", "v2"]
        assert "ESP32" in cfg.board_tags  # default still present

    def test_empty_json_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {})
        cfg = _load_config()
        assert cfg.chunk_size == DEFAULT_CHUNK_SIZE


# ── create_default_config ───────────────────────────────────────────


class TestCreateDefaultConfig:
    def test_creates_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        result = create_default_config()
        assert Path(result).exists()
        assert Path(result).name == CONFIG_FILE

    def test_valid_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        create_default_config()
        data = json.loads((tmp_path / CONFIG_FILE).read_text(encoding="utf-8"))
        assert data["chunk_size"] == DEFAULT_CHUNK_SIZE
        assert data["max_retries"] == 2
        assert data["verify"] == "size"
        assert data["download_threads"] == 4
        assert data["auto_compile"] is True
        assert data["baudrate"] == DEFAULT_BAUDRATE
        assert data["timeout"] == 10
        assert data["delta_flash"] == "auto"
        assert "delta_min_size" not in data
        assert data["precheck"] == "basic"
        assert data["precheck_compat"] == "warn"
        assert data["precheck_mp_version"] == ""

    def test_default_config_is_loadable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        create_default_config()
        cfg = _load_config()
        assert cfg.chunk_size == DEFAULT_CHUNK_SIZE
        assert cfg.verify == "size"

    def test_prints_default_timeout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        monkeypatch.chdir(tmp_path)
        create_default_config()
        assert "timeout = 10" in capsys.readouterr().out


class TestResolveConnectionSettings:
    def test_explicit_values_take_precedence(self):
        cfg = PyriteConfig(baudrate=460800, timeout=20)

        assert config_module.resolve_connection_settings(115200, 5, cfg) == (
            115200,
            5,
        )

    def test_config_values_are_used_when_explicit_values_are_missing(self):
        cfg = PyriteConfig(baudrate=460800, timeout=20)

        assert config_module.resolve_connection_settings(None, None, cfg) == (
            460800,
            20,
        )

    def test_invalid_values_fall_back_to_defaults(self):
        cfg = PyriteConfig(baudrate=0, timeout=0)

        assert config_module.resolve_connection_settings(0, -1, cfg) == (
            DEFAULT_BAUDRATE,
            10,
        )

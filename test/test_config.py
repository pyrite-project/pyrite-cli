import json
import os
from pathlib import Path

import pytest

from cli.utils.config import (
    _load_config,
    create_default_config,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_BAUDRATE,
    CONFIG_FILE,
)


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── _load_config ────────────────────────────────────────────────────


class TestLoadConfig:
    def test_defaults_when_no_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        cfg = _load_config()
        assert cfg.chunk_size == DEFAULT_CHUNK_SIZE
        assert cfg.download_threads == 4
        assert cfg.auto_compile is True
        assert cfg.verify == "size"
        assert cfg.max_retries == 2
        assert cfg.baudrate == DEFAULT_BAUDRATE
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

    def test_custom_delta_min_size(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"delta_min_size": 2048})
        assert _load_config().delta_min_size == 2048

    def test_invalid_delta_min_size_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"delta_min_size": -1})
        assert _load_config().delta_min_size == 10240

    def test_invalid_verify_mode_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"verify": "sha256"})
        assert _load_config().verify == "size"

    def test_max_retries_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"max_retries": 0})
        assert _load_config().max_retries == 0

    def test_custom_baudrate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / CONFIG_FILE, {"baudrate": 1500000})
        assert _load_config().baudrate == 1500000

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
        assert data["delta_flash"] == "auto"

    def test_default_config_is_loadable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        create_default_config()
        cfg = _load_config()
        assert cfg.chunk_size == DEFAULT_CHUNK_SIZE
        assert cfg.verify == "size"

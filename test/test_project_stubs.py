import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cli.project.project import detect_device_info
from cli.project import stubs
from cli.utils.device_context import DeviceContext


class _FakeResponse:
    def __init__(self, *, payload=None, text: str = "") -> None:
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def test_download_stubs_writes_under_global_cache_and_config_matches(
    tmp_path: Path,
    monkeypatch,
):
    stub_dir = "micropython-v1_20_0-esp32"
    pyi_url = "https://example.invalid/machine.pyi"
    cache_root = tmp_path / "home" / ".pyrcli" / "stubs"

    def fake_request(url: str, **kwargs):
        if url.endswith(f"/contents/stubs/{stub_dir}"):
            return _FakeResponse(
                payload=[
                    {
                        "type": "file",
                        "name": "machine.pyi",
                        "download_url": pyi_url,
                    },
                    {
                        "type": "file",
                        "name": "README.md",
                        "download_url": "https://example.invalid/README.md",
                    },
                ],
            )
        if url == pyi_url:
            return _FakeResponse(text="class Pin: ...\n")
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(stubs, "STUB_CACHE_ROOT", cache_root)
    monkeypatch.setattr(stubs, "_request_with_retry", fake_request)
    monkeypatch.setattr(stubs, "tqdm", None)

    count, stub_path = stubs.download_stubs(stub_dir, "")
    feature_stub_path = stubs.ensure_feature_stub()
    settings_file = stubs.create_vscode_config(
        stub_path,
        "esp32",
        "1.20.0",
        extra_paths=[feature_stub_path],
    )

    assert count == 1
    assert stub_path == cache_root.resolve() / stub_dir
    assert (cache_root / stub_dir / "machine.pyi").read_text(
        encoding="utf-8",
    ) == "class Pin: ...\n"
    assert (cache_root / "pyrite" / "feature_stub.pyi").exists()
    assert not (tmp_path / ".stubs").exists()

    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    assert settings["python.analysis.extraPaths"] == [
        (cache_root / stub_dir).resolve().as_posix(),
        (cache_root / "pyrite").resolve().as_posix(),
    ]
    assert settings["python.analysis.stubPath"] == cache_root.resolve().as_posix()


def test_download_stubs_reuses_cached_pyi_without_file_download(
    tmp_path: Path,
    monkeypatch,
):
    stub_dir = "micropython-v1_20_0-esp32"
    cache_root = tmp_path / "home" / ".pyrcli" / "stubs"
    cached_dir = cache_root / stub_dir
    cached_dir.mkdir(parents=True)
    (cached_dir / "machine.pyi").write_text("class Pin: ...\n", encoding="utf-8")

    def fake_request(url: str, **kwargs):
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(stubs, "STUB_CACHE_ROOT", cache_root)
    monkeypatch.setattr(stubs, "_request_with_retry", fake_request)

    count, stub_path = stubs.download_stubs(stub_dir, "")

    assert count == 1
    assert stub_path == cached_dir.resolve()


def test_write_project_stub_config_preserves_existing_fields(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    config_file = tmp_path / ".pyrite_config.json"
    config_file.write_text(
        json.dumps({"chunk_size": 2048, "download_threads": 2}),
        encoding="utf-8",
    )
    stub_path = tmp_path / "home" / ".pyrcli" / "stubs" / "micropython-v1_20_0-esp32"

    written = stubs.write_project_stub_config(
        hardware="esp32",
        version="1.20.0",
        variant=None,
        stub_dir="micropython-v1_20_0-esp32",
        stub_path=stub_path,
    )

    data = json.loads(written.read_text(encoding="utf-8"))
    assert data["chunk_size"] == 2048
    assert data["download_threads"] == 2
    assert data["stubs"] == {
        "hardware": "esp32",
        "version": "1.20.0",
        "variant": None,
        "stub_dir": "micropython-v1_20_0-esp32",
        "path": stub_path.resolve().as_posix(),
    }


def test_warn_legacy_project_stubs_does_not_remove_directory(
    tmp_path: Path,
    monkeypatch,
):
    legacy = tmp_path / ".stubs"
    legacy.mkdir()
    monkeypatch.setattr(stubs, "STUB_CACHE_ROOT", tmp_path / "home" / ".pyrcli" / "stubs")

    stubs.warn_legacy_project_stubs(tmp_path)

    assert legacy.exists()


def test_detect_device_info_uses_shared_device_context():
    mp = MagicMock()
    mp.ensure_device_context.return_value = DeviceContext(
        version="1.22.0",
        platform="esp32",
    )

    with patch("cli.utils.flash.MicroPython", return_value=mp):
        hardware, version = detect_device_info("COM3")

    assert (hardware, version) == ("esp32", "1.22.0")
    mp.connect.assert_called_once()
    mp.ensure_device_context.assert_called_once()
    mp.run.assert_not_called()
    mp.disconnect.assert_called_once()

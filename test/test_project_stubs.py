import json
from pathlib import Path

from cli.project import stubs


class _FakeResponse:
    def __init__(self, *, payload=None, text: str = "") -> None:
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def test_download_stubs_writes_under_project_stubs_dir_and_config_matches(
    tmp_path: Path,
    monkeypatch,
):
    stub_dir = "micropython-v1_20_0-esp32"
    pyi_url = "https://example.invalid/machine.pyi"

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
    monkeypatch.setattr(stubs, "_request_with_retry", fake_request)
    monkeypatch.setattr(stubs, "tqdm", None)

    count, stub_path = stubs.download_stubs(stub_dir, "")
    settings_file = stubs.create_vscode_config(stub_path, "esp32", "1.20.0")

    assert count == 1
    assert stub_path == Path(".stubs") / stub_dir
    assert (tmp_path / ".stubs" / stub_dir / "machine.pyi").read_text(
        encoding="utf-8",
    ) == "class Pin: ...\n"
    assert not (tmp_path / stub_dir).exists()

    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    assert settings["python.analysis.extraPaths"] == [".stubs/micropython-v1_20_0-esp32"]
    assert settings["python.analysis.stubPath"] == ".stubs"

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli.reg_commands.board import board_app
from cli.utils.board_alias import (
    AliasNotFoundError,
    BoardAlias,
    BoardAliasStore,
    DuplicateAliasError,
    InvalidAliasError,
    InvalidAliasFileError,
    resolve_port_alias,
)


runner = CliRunner()


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _legacy_data(*, name: str = "bench", port: str = "COM7") -> dict:
    return {
        "version": 1,
        "profiles": {
            name: {
                "name": name,
                "port": port,
                "tags": ["ESP32", "wifi"],
                "last_firmware": "MicroPython v1.22",
                "recommended": {"chunk_size": 4096, "verify": "crc32"},
            }
        },
    }


def test_register_list_show_and_remove_aliases(tmp_path: Path):
    path = tmp_path / "aliases.json"
    store = BoardAliasStore(path)

    alias = store.register("COM3", name="lab-esp32")

    assert alias.name == "lab-esp32"
    assert alias.port == "COM3"
    assert alias.to_dict() == {"name": "lab-esp32", "port": "COM3"}
    assert [item.name for item in store.list()] == ["lab-esp32"]
    assert store.show("lab-esp32").port == "COM3"
    assert store.show("@lab-esp32").port == "COM3"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "aliases": {"lab-esp32": "COM3"},
    }

    removed = store.remove("@lab-esp32")
    assert removed.name == "lab-esp32"
    assert store.list() == []
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "aliases": {},
    }


def test_resolve_port_alias_returns_plain_ports_and_alias_ports(tmp_path: Path):
    store = BoardAliasStore(tmp_path / "aliases.json")
    store.register("COM7", name="bench")

    assert resolve_port_alias("COM9", store=store) == "COM9"
    assert resolve_port_alias("@bench", store=store) == "COM7"


def test_duplicate_and_invalid_aliases_are_rejected(tmp_path: Path):
    store = BoardAliasStore(tmp_path / "aliases.json")
    store.register("COM3", name="lab-esp32")

    with pytest.raises(DuplicateAliasError):
        store.register("COM4", name="lab-esp32")
    assert store.register("COM4", name="lab-esp32", overwrite=True).port == "COM4"

    with pytest.raises(InvalidAliasError):
        resolve_port_alias("@", store=store)
    with pytest.raises(InvalidAliasError):
        resolve_port_alias("@bad/name", store=store)
    with pytest.raises(InvalidAliasError):
        store.register("COM3", name="@bad")
    with pytest.raises(InvalidAliasError):
        store.register("", name="empty-port")
    with pytest.raises(AliasNotFoundError):
        resolve_port_alias("@missing", store=store)

    with pytest.raises(InvalidAliasError):
        BoardAlias(name="bad/name", port="COM3")
    with pytest.raises(InvalidAliasError):
        BoardAlias(name="valid", port=" ")


def test_default_store_reads_legacy_file_and_mutation_migrates_without_deleting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    legacy_path = tmp_path / ".pyrite_board_profiles.json"
    canonical_path = tmp_path / ".pyrite_board_aliases.json"
    legacy_data = _legacy_data()
    _write_json(legacy_path, legacy_data)

    store = BoardAliasStore()
    assert store.show("@bench").port == "COM7"

    store.register("COM8", name="second")

    assert json.loads(canonical_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "aliases": {"bench": "COM7", "second": "COM8"},
    }
    assert json.loads(legacy_path.read_text(encoding="utf-8")) == legacy_data


def test_legacy_metadata_is_ignored_during_alias_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    legacy_data = _legacy_data()
    legacy_data["profiles"]["bench"]["tags"] = {"not": "a list"}
    legacy_data["profiles"]["bench"]["recommended"] = ["not", "an", "object"]
    _write_json(tmp_path / ".pyrite_board_profiles.json", legacy_data)

    alias = BoardAliasStore().show("bench")

    assert alias.to_dict() == {"name": "bench", "port": "COM7"}


def test_canonical_file_wins_over_legacy_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    _write_json(tmp_path / ".pyrite_board_profiles.json", _legacy_data())
    _write_json(
        tmp_path / ".pyrite_board_aliases.json",
        {"version": 1, "aliases": {"bench": "COM10", "canonical": "COM11"}},
    )

    store = BoardAliasStore()

    assert store.resolve("@bench") == "COM10"
    assert [item.name for item in store.list()] == ["bench", "canonical"]


def test_legacy_environment_variable_is_a_read_fallback_and_migrates_to_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    legacy_path = tmp_path / "custom-profiles.json"
    _write_json(legacy_path, _legacy_data(port="COM12"))
    monkeypatch.setenv("PYRITE_BOARD_PROFILE_FILE", str(legacy_path))

    store = BoardAliasStore()
    assert store.resolve("@bench") == "COM12"
    store.remove("bench")

    assert json.loads((tmp_path / ".pyrite_board_aliases.json").read_text()) == {
        "version": 1,
        "aliases": {},
    }
    assert json.loads(legacy_path.read_text()) == _legacy_data(port="COM12")


def test_new_environment_variable_wins_over_legacy_environment_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    canonical_path = tmp_path / "custom-aliases.json"
    legacy_path = tmp_path / "custom-profiles.json"
    _write_json(canonical_path, {"version": 1, "aliases": {"bench": "COM13"}})
    _write_json(legacy_path, _legacy_data(port="COM14"))
    monkeypatch.setenv("PYRITE_BOARD_ALIAS_FILE", str(canonical_path))
    monkeypatch.setenv("PYRITE_BOARD_PROFILE_FILE", str(legacy_path))

    store = BoardAliasStore()

    assert store.resolve("@bench") == "COM13"
    store.register("COM15", name="second")
    assert json.loads(canonical_path.read_text()) == {
        "version": 1,
        "aliases": {"bench": "COM13", "second": "COM15"},
    }
    assert json.loads(legacy_path.read_text()) == _legacy_data(port="COM14")


def test_new_environment_path_falls_back_to_legacy_until_canonical_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    canonical_path = tmp_path / "custom-aliases.json"
    legacy_path = tmp_path / "custom-profiles.json"
    legacy_data = _legacy_data(port="COM16")
    _write_json(legacy_path, legacy_data)
    monkeypatch.setenv("PYRITE_BOARD_ALIAS_FILE", str(canonical_path))
    monkeypatch.setenv("PYRITE_BOARD_PROFILE_FILE", str(legacy_path))

    store = BoardAliasStore()

    assert store.resolve("@bench") == "COM16"
    store.register("COM17", name="second")
    assert json.loads(canonical_path.read_text()) == {
        "version": 1,
        "aliases": {"bench": "COM16", "second": "COM17"},
    }
    assert json.loads(legacy_path.read_text()) == legacy_data


@pytest.mark.parametrize(
    "data",
    [
        [],
        {},
        {"version": 1},
        {"version": 1, "aliases": []},
        {"version": 1, "aliases": {"bench": 3}},
        {"version": 1, "aliases": {"bad/name": "COM3"}},
    ],
)
def test_invalid_alias_files_are_rejected(tmp_path: Path, data: object):
    path = tmp_path / "aliases.json"
    _write_json(path, data)

    with pytest.raises(InvalidAliasFileError):
        BoardAliasStore(path).list()


def test_board_cli_uses_alias_terms_and_compact_json_schema(tmp_path: Path):
    path = tmp_path / "aliases.json"

    result = runner.invoke(
        board_app,
        ["register", "COM3", "--name", "lab", "--alias-file", str(path), "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"name": "lab", "port": "COM3"}

    result = runner.invoke(board_app, ["list", "--alias-file", str(path), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "aliases": [{"name": "lab", "port": "COM3"}],
        "count": 1,
    }

    result = runner.invoke(board_app, ["show", "@lab", "--alias-file", str(path), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"name": "lab", "port": "COM3"}

    result = runner.invoke(board_app, ["remove", "lab", "--alias-file", str(path), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"removed": {"name": "lab", "port": "COM3"}}


def test_board_cli_keeps_profile_file_as_read_only_migration_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "profiles.json"
    legacy_data = _legacy_data(name="compat", port="COM5")
    _write_json(path, legacy_data)

    result = runner.invoke(
        board_app,
        ["register", "COM6", "--name", "second", "--profile-file", str(path), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"name": "second", "port": "COM6"}
    assert json.loads((tmp_path / ".pyrite_board_aliases.json").read_text()) == {
        "version": 1,
        "aliases": {"compat": "COM5", "second": "COM6"},
    }
    assert json.loads(path.read_text()) == legacy_data


def test_board_cli_rejects_both_alias_and_profile_files(tmp_path: Path):
    result = runner.invoke(
        board_app,
        [
            "list",
            "--alias-file",
            str(tmp_path / "aliases.json"),
            "--profile-file",
            str(tmp_path / "profiles.json"),
        ],
    )

    assert result.exit_code == 2
    assert "不能同时使用" in result.output


def test_board_register_help_drops_unused_metadata_options():
    result = runner.invoke(board_app, ["register", "--help"])

    assert result.exit_code == 0, result.output
    assert "--alias-file" in result.stdout
    assert "--profile-file" in result.stdout
    assert "--tag" not in result.stdout
    assert "--tags" not in result.stdout
    assert "--firmware" not in result.stdout
    assert "--recommended" not in result.stdout

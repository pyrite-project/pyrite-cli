import json
from pathlib import Path

import pytest

from cli.utils.board_profile import (
    BoardProfileStore,
    DuplicateProfileError,
    InvalidAliasError,
    InvalidProfileError,
    ProfileNotFoundError,
    parse_recommended_items,
    resolve_port_alias,
)


def test_register_list_show_and_remove_profiles(tmp_path: Path):
    store = BoardProfileStore(tmp_path / "profiles.json")

    profile = store.register(
        "COM3",
        name="lab-esp32",
        tags=["ESP32", "wifi"],
        firmware="MicroPython v1.22",
        recommended={"verify": "size", "chunk_size": 4096},
    )

    assert profile.name == "lab-esp32"
    assert profile.port == "COM3"
    assert profile.tags == ["ESP32", "wifi"]
    assert profile.last_firmware == "MicroPython v1.22"
    assert profile.recommended == {"verify": "size", "chunk_size": 4096}
    assert [p.name for p in store.list()] == ["lab-esp32"]
    assert store.show("lab-esp32").port == "COM3"

    saved = json.loads((tmp_path / "profiles.json").read_text(encoding="utf-8"))
    assert saved == {
        "version": 1,
        "profiles": {
            "lab-esp32": {
                "name": "lab-esp32",
                "port": "COM3",
                "tags": ["ESP32", "wifi"],
                "last_firmware": "MicroPython v1.22",
                "recommended": {"chunk_size": 4096, "verify": "size"},
            }
        },
    }

    removed = store.remove("lab-esp32")
    assert removed.name == "lab-esp32"
    assert store.list() == []


def test_resolve_port_alias_returns_plain_ports_and_alias_ports(tmp_path: Path):
    store = BoardProfileStore(tmp_path / "profiles.json")
    store.register("COM7", name="bench")

    assert resolve_port_alias("COM9", store=store) == "COM9"
    assert resolve_port_alias("@bench", store=store) == "COM7"


def test_duplicate_profile_name_is_rejected(tmp_path: Path):
    store = BoardProfileStore(tmp_path / "profiles.json")
    store.register("COM3", name="lab-esp32")

    with pytest.raises(DuplicateProfileError):
        store.register("COM4", name="lab-esp32")

    assert store.register("COM4", name="lab-esp32", overwrite=True).port == "COM4"


def test_invalid_alias_and_profile_names_are_rejected(tmp_path: Path):
    store = BoardProfileStore(tmp_path / "profiles.json")

    with pytest.raises(InvalidAliasError):
        resolve_port_alias("@", store=store)
    with pytest.raises(InvalidAliasError):
        resolve_port_alias("@bad/name", store=store)
    with pytest.raises(InvalidProfileError):
        store.register("COM3", name="@bad")
    with pytest.raises(ProfileNotFoundError):
        resolve_port_alias("@missing", store=store)


def test_recommended_items_keep_json_scalar_types():
    recommended = parse_recommended_items([
        "verify=size",
        "chunk_size=4096",
        "auto_compile=false",
        "delta_flash=\"auto\"",
    ])

    assert recommended == {
        "verify": "size",
        "chunk_size": 4096,
        "auto_compile": False,
        "delta_flash": "auto",
    }

    with pytest.raises(InvalidProfileError):
        parse_recommended_items(["broken"])


def test_recommended_structure_rejects_unknown_keys_and_invalid_values(tmp_path: Path):
    store = BoardProfileStore(tmp_path / "profiles.json")

    with pytest.raises(InvalidProfileError):
        parse_recommended_items(["label=lab"])
    with pytest.raises(InvalidProfileError):
        parse_recommended_items(["chunk_size=-1"])
    with pytest.raises(InvalidProfileError):
        store.register("COM3", name="lab", recommended={"verify": {"mode": "size"}})

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import click
import pytest
from typer.testing import CliRunner

from cli.main import app
from cli.utils.firmware.catalog import get_recipe, list_recipes, resolve
from cli.utils.firmware.cmodules import module_payload, validate_module_cmake
from cli.utils.firmware.config import load_project_data, update_firmware, update_lock
from cli.utils.firmware.frozen import frozen_payload, validate_manifest
from cli.utils.firmware.models import Partition, Pin
from cli.utils.firmware.partitions import (
    layout_fingerprint,
    parse_partitions,
    render_partitions,
    validate_partitions,
)
from cli.utils.firmware.pins import parse_pins, render_pins, validate_pins
from cli.utils.firmware.variants import normalize_variant, validate_variants


runner = CliRunner()


class _PilotButton:
    def __init__(self, identifier: str):
        self.id = identifier


class _PilotEvent:
    def __init__(self, identifier: str):
        self.button = _PilotButton(identifier)


def _install_fake_textual(monkeypatch: pytest.MonkeyPatch):
    """Install a small Textual contract double and return the captured app class."""
    import types

    captured: dict[str, object] = {}

    class Widget:
        def __init__(self, *children, id=None, value=None, text=None, **kwargs):
            self.children = list(children)
            self.id = id
            self.value = value if value is not None else ""
            self.text = text if text is not None else ""

    class Container(Widget):
        async def remove_children(self):
            self.children.clear()

        async def mount(self, *children):
            self.children.extend(children)

    class App:
        BINDINGS = []

        @classmethod
        def __class_getitem__(cls, _item):
            return cls

        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__(**kwargs)
            captured["app_class"] = cls

        def __init__(self):
            self.widgets = {"#content": Container(id="content")}
            self.exited = False

        def query_one(self, selector, _widget_type=None):
            if selector not in self.widgets:
                raise LookupError(selector)
            return self.widgets[selector]

        def call_after_refresh(self, callback):
            result = callback()
            if hasattr(result, "__await__"):
                asyncio.run(result)

        def exit(self):
            self.exited = True

        def run(self):
            captured["app"] = self

    textual = types.ModuleType("textual")
    app_module = types.ModuleType("textual.app")
    containers = types.ModuleType("textual.containers")
    widgets = types.ModuleType("textual.widgets")
    app_module.App = App
    app_module.ComposeResult = list
    containers.Vertical = Container
    for name in ("Button", "Footer", "Header", "Input", "Label", "Static", "TextArea", "Checkbox"):
        setattr(widgets, name, type(name, (Widget,), {}))
    monkeypatch.setitem(sys.modules, "textual", textual)
    monkeypatch.setitem(sys.modules, "textual.app", app_module)
    monkeypatch.setitem(sys.modules, "textual.containers", containers)
    monkeypatch.setitem(sys.modules, "textual.widgets", widgets)
    return captured


def test_catalog_lists_and_resolves_dependency_closure():
    recipes = list_recipes()
    assert {recipe.id for recipe in recipes} >= {"network.core", "bluetooth.nimble"}
    assert get_recipe("network.core").title == "Network"
    resolution = resolve({"bluetooth.nimble": True})
    assert resolution.final == {"network.core": True, "bluetooth.nimble": True}
    assert resolution.derived == {"network.core": "required by bluetooth.nimble"}
    assert resolution.macros["MICROPY_PY_NETWORK"] == "1"
    assert resolution.config["CONFIG_BT_NIMBLE_ENABLED"] == "y"


def test_catalog_reports_unknown_platform_and_recipe():
    with pytest.raises(ValueError, match="unknown firmware recipe"):
        get_recipe("missing")
    result = resolve({"usb.device.cdc": True}, platform="rp2")
    assert result.errors == ("usb.device.cdc is not supported on rp2",)
    assert result.final == {}
    with pytest.raises(TypeError):
        resolve([])  # type: ignore[arg-type]


def test_pins_parse_validate_and_render():
    pins = parse_pins("name,gpio,function\nled,0,out\nfree,-,\n")
    assert pins[0] == Pin("led", 0, "", "manual", "out")
    assert pins[1].gpio is None
    assert validate_pins(pins, gpio_count=2) == []
    assert "led,0" in render_pins(pins)
    with pytest.raises(ValueError, match="duplicate pin alias"):
        parse_pins("alias,gpio\na,1\na,2\n")
    with pytest.raises(ValueError, match="invalid pin alias"):
        parse_pins("alias,gpio\nnot-ok,1\n")
    assert any("out of range" in error for error in validate_pins([Pin("x", 4)], 4))


def test_partitions_parse_validate_render_and_fingerprint():
    parts = parse_partitions("# comment\nnvs,data,nvs,,0x6000\napp0,app,ota_0,0x10000,0x100000\n")
    assert parts[0].offset is None
    assert validate_partitions(parts, flash_size=2 * 1024 * 1024) == []
    rendered = render_partitions(parts)
    assert rendered.startswith("# Name,Type,SubType")
    assert layout_fingerprint(parts) == layout_fingerprint(parts)
    assert validate_partitions([Partition("a", "data", "x", 0x9000, 1), Partition("a", "data", "x", 0xA000, 1)])
    assert any("overlaps" in e for e in validate_partitions([Partition("a", "data", "x", 0x9000, 0x1000), Partition("b", "data", "x", 0x9000, 1)]))
    with pytest.raises(ValueError, match="requires"):
        parse_partitions("bad,line")


def test_frozen_manifest_validation_and_digest(tmp_path: Path):
    manifest = tmp_path / "manifest.py"
    manifest.write_text("module('foo')\ninclude('bar')\n", encoding="utf8")
    assert validate_manifest(manifest) == []
    payload = frozen_payload(manifest)
    assert payload["sha256"] and payload["errors"] == []
    manifest.write_text("import os\n", encoding="utf8")
    assert any("unsupported statement" in e for e in validate_manifest(manifest))
    manifest.write_text("module(foo)\n", encoding="utf8")
    assert any("must be literals" in e for e in validate_manifest(manifest))


def test_cmodule_validation_and_payload(tmp_path: Path):
    module = tmp_path / "CMakeLists.txt"
    module.write_text("add_library(foo STATIC foo.c)\n", encoding="utf8")
    assert validate_module_cmake(module) == []
    assert module_payload(module)["sha256"]
    module.write_text("add_custom_command(COMMAND sh x)\n", encoding="utf8")
    assert validate_module_cmake(module) == ["shell or custom command execution is not allowed"]
    assert validate_module_cmake(tmp_path / "missing") == ["module cmake does not exist"]


def test_variants_normalize_and_detect_duplicates():
    assert normalize_variant("esp32-s3/pro") == "ESP32_S3_PRO"
    assert validate_variants(["pro", "safe"]) == []
    assert any("duplicate" in e for e in validate_variants(["pro", "PRO"]))
    with pytest.raises(ValueError):
        normalize_variant("---")


def test_config_updates_firmware_and_lock_atomically(tmp_path: Path):
    config = tmp_path / ".pyrite_config.json"
    config.write_text(json.dumps({"project": {"name": "demo"}}), encoding="utf8")
    update_firmware(config, {"board": "CUSTOM"})
    update_lock(config, {"commit": "abc", "contract_hash": "def"})
    data = load_project_data(config)
    assert data["firmware"] == {"board": "CUSTOM"}
    lock = load_project_data(tmp_path / "pyrite.lock")
    assert lock["build"]["firmware"]["commit"] == "abc"
    with pytest.raises(ValueError, match="top level"):
        bad = tmp_path / "bad.json"
        bad.write_text("[]", encoding="utf8")
        load_project_data(bad)


def test_top_level_help_includes_firmware_and_strips_ansi():
    result = runner.invoke(app, ["--help"])
    help_text = click.utils.strip_ansi(result.stdout)
    assert result.exit_code == 0
    assert "firmware" in help_text


def test_firmware_commands_from_child_directory_resolve_extensions_at_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checkout = tmp_path / "micropython"
    board = checkout / "ports" / "esp32" / "boards" / "GENERIC"
    board.mkdir(parents=True)
    (board / "mpconfigboard.h").write_text("#define BASE 1\n", encoding="utf8")
    (board / "mpconfigboard.cmake").write_text("set(IDF_TARGET esp32)\n", encoding="utf8")
    subprocess.run(["git", "-C", str(checkout), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Firmware Test"], check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "fixture"], check=True)
    (tmp_path / "pins.csv").write_text("name,gpio,function\nled,0,out\n", encoding="utf8")
    (tmp_path / ".pyrite_config.json").write_text(
        json.dumps({"firmware": {"micropython": "micropython", "pins": "pins.csv"}}),
        encoding="utf8",
    )
    child = tmp_path / "nested" / "commands"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)

    plan_result = runner.invoke(app, ["firmware", "plan", "--format", "json"])
    assert plan_result.exit_code == 0, plan_result.output
    assert json.loads(plan_result.output)["extensions"]["pins"] == str(tmp_path / "pins.csv")

    generate_result = runner.invoke(app, ["firmware", "generate"])
    assert generate_result.exit_code == 0, generate_result.output
    build_result = runner.invoke(app, ["firmware", "build", "--dry-run"])
    assert build_result.exit_code == 0, build_result.output


def test_firmware_config_non_tty_does_not_import_textual(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["firmware", "config", "--set", "network.core=true"])
    assert result.exit_code == 0, result.output
    assert json.loads((tmp_path / ".pyrite_config.json").read_text())["firmware"]["selections"]["network.core"] is True


def test_firmware_tui_rejects_non_tty_without_importing_textual(monkeypatch):
    monkeypatch.setattr("cli.reg_commands.firmware._interactive_terminal", lambda: False)
    result = runner.invoke(app, ["firmware", "config", "--tui"])
    assert result.exit_code == 2
    assert "interactive terminal" in result.output


def test_tui_module_import_is_lazy_and_missing_dependency_is_clear(monkeypatch):
    from cli.utils.firmware.tui import app as tui_app

    assert "textual.app" not in tui_app.__dict__
    monkeypatch.setitem(sys.modules, "textual", None)
    with pytest.raises(ImportError, match="textual is required"):
        tui_app.run_tui()


def test_tui_pilot_contract_reviews_then_saves(tmp_path: Path, monkeypatch):
    from cli.utils.firmware.tui.app import run_tui

    captured = _install_fake_textual(monkeypatch)
    project = tmp_path / ".pyrite_config.json"
    run_tui(keys="vim", mode="config", project_path=project)
    app_instance = captured["app"]
    app_class = captured["app_class"]
    assert ("h", "previous", "Previous") in app_instance.BINDINGS
    assert app_instance.advanced is True

    app_instance._read = lambda: None
    asyncio.run(app_class.on_button_pressed(app_instance, _PilotEvent("review-action")))
    assert app_instance.advanced is False
    assert app_instance.page == 3
    app_instance.page = 5
    asyncio.run(app_class.on_button_pressed(app_instance, _PilotEvent("save")))
    assert app_instance.exited is True
    assert load_project_data(project)["firmware"]["port"] == "esp32"

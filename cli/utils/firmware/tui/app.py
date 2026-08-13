from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


def _terminal_size() -> tuple[int, int]:
    """Return terminal dimensions, falling back only when none are available."""
    size = shutil.get_terminal_size(fallback=(80, 24))
    return size.columns, size.lines


def run_tui(
    *,
    keys: str = "arrows",
    mode: str = "setup",
    project_path: Path | None = None,
) -> None:
    """Run the optional firmware editor.

    Textual is deliberately imported here, rather than at module import time, so
    JSON and non-interactive CLI commands remain usable without the extra
    dependency.  ``mode=config`` opens the compact advanced editor directly.
    """
    try:
        from textual.app import App, ComposeResult
        from textual.containers import Vertical
        from textual.widgets import Button, Footer, Header, Input, Label, Static, TextArea
    except ImportError as exc:
        raise ImportError("textual is required for firmware TUI") from exc

    from ..catalog import list_recipes, resolve
    from ..config import load_project_data, update_firmware

    path = project_path or (Path.cwd() / ".pyrite_config.json")
    try:
        existing = load_project_data(path)
    except ValueError as exc:
        raise ValueError(f"cannot start firmware TUI: {exc}") from exc
    firmware_data = existing.get("firmware")
    initial: dict[str, Any] = dict(firmware_data) if isinstance(firmware_data, dict) else {}
    initial.setdefault("port", "esp32")
    initial.setdefault("board", "")
    initial.setdefault("base_board", "")
    initial.setdefault("selections", {})
    initial.setdefault("pins", [])
    initial.setdefault("partition", [])
    initial.setdefault("frozen", [])
    initial.setdefault("c_modules", [])
    initial.setdefault("variants", [])

    class FirmwareApp(App[None]):
        TITLE = "Pyrite Firmware Builder"
        CSS = """
        Screen { align: center top; }
        #content { width: 1fr; max-width: 110; padding: 1 2; }
        .narrow { width: 1fr; }
        .wide { width: 1fr; }
        Button { margin: 1 1 0 0; }
        Input, TextArea { margin: 0 0 1 0; }
        .hidden { display: none; }
        """
        BINDINGS = [("left", "previous", "Previous"), ("right", "next", "Next"), ("escape", "quit", "Quit")]

        def __init__(self) -> None:
            super().__init__()
            self.keys_mode = keys
            self.page = 0
            self.advanced = mode == "config"
            self.data: dict[str, Any] = initial
            self.invalid_json = False
            if keys == "vim":
                self.BINDINGS = [*self.BINDINGS, ("h", "previous", "Previous"), ("l", "next", "Next")]

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static("Firmware setup", id="title")
            yield Vertical(id="content")
            yield Footer()

        async def on_mount(self) -> None:
            await self.render_page()

        def action_previous(self) -> None:
            if self.page > 0:
                self.page -= 1
                self.call_after_refresh(self.render_page)

        def action_next(self) -> None:
            if self.page < 5:
                self.page += 1
                self.call_after_refresh(self.render_page)

        def _read(self) -> None:
            def value(identifier: str, default: str = "") -> str:
                try:
                    return self.query_one(identifier, Input).value
                except Exception:
                    return default

            self.data["port"] = value("#port", self.data.get("port", "esp32"))
            self.data["board"] = value("#board", self.data.get("board", ""))
            self.data["base_board"] = value("#base-board", self.data.get("base_board", ""))
            selections = dict(self.data.get("selections") or {})
            for recipe in list_recipes():
                try:
                    selections[recipe.id] = self.query_one(f"#recipe-{recipe.id.replace('.', '-')}").value
                except Exception:
                    pass
            self.data["selections"] = selections
            for key in ("pins", "partition", "frozen", "c_modules", "variants"):
                try:
                    raw = self.query_one(f"#{key}", TextArea).text
                    parsed = json.loads(raw) if raw.strip() else []
                    if isinstance(parsed, list):
                        self.data[key] = parsed
                except (ValueError, json.JSONDecodeError):
                    self.invalid_json = True

        async def render_page(self) -> None:
            self._read()
            content = self.query_one("#content", Vertical)
            await content.remove_children()
            if self.advanced:
                await content.mount(Label("Advanced configuration"), Label("Edit JSON arrays, then press Review."), self._area("pins", "Pins", self.data.get("pins", [])), self._area("partition", "Partition", self.data.get("partition", [])), self._area("frozen", "Frozen modules", self.data.get("frozen", [])), self._area("c_modules", "C modules", self.data.get("c_modules", [])), self._area("variants", "Variants", self.data.get("variants", [])), Static(self._review_text(), id="review"), Button("Review", id="review-action", variant="primary"))
                return
            if self.page == 0:
                await content.mount(Label("1 / 6  Board"), Label("Select the target board."), Input(value=str(self.data.get("port", "esp32")), placeholder="Target (esp32)", id="port"), Input(value=str(self.data.get("board", "")), placeholder="Board name", id="board"), Input(value=str(self.data.get("base_board", "")), placeholder="Base board", id="base-board"))
            elif self.page == 1:
                await content.mount(Label("2 / 6  Capabilities"), Label("Enable capabilities; dependencies are resolved by the resolver."), *[self._recipe_widget(r) for r in list_recipes()])
            elif self.page == 2:
                await content.mount(Label("3 / 6  Extensions"), Label("Optional extension inputs (JSON arrays)."), self._area("pins", "Pins", self.data.get("pins", [])), self._area("partition", "Partition", self.data.get("partition", [])))
            elif self.page == 3:
                await content.mount(Label("4 / 6  Review"), Static(self._review_text(), id="review"))
            elif self.page == 4:
                await content.mount(Label("5 / 6  Review complete"), Static(self._review_text(), id="review"))
            else:
                await content.mount(Label("6 / 6  Actions"), Static("Configuration is ready."), Button("Save", id="save", variant="success"), Button("Back", id="back"))
            if self.page not in (4, 5):
                await content.mount(Button("Back", id="back"), Button("Next", id="next", variant="primary"))

        def _recipe_widget(self, recipe: Any) -> Any:
            from textual.widgets import Checkbox
            selected = bool((self.data.get("selections") or {}).get(recipe.id, False))
            return Checkbox(recipe.title, value=selected, id=f"recipe-{recipe.id.replace('.', '-')}")

        def _area(self, name: str, title: str, value: Any) -> Any:
            return Vertical(Label(title), TextArea(json.dumps(value, ensure_ascii=False, indent=2), id=name), classes="wide")

        def _review_text(self) -> str:
            resolution = resolve(self.data.get("selections", {}), str(self.data.get("port", "esp32")))
            return "Review\n" + json.dumps({"board": self.data.get("board"), "base_board": self.data.get("base_board"), "resolution": resolution.to_dict(), "options": self.data.get("options", {}), "pins": self.data.get("pins", []), "partition": self.data.get("partition", []), "frozen": self.data.get("frozen", []), "c_modules": self.data.get("c_modules", []), "variants": self.data.get("variants", []), "errors": list(resolution.errors)}, ensure_ascii=False, indent=2)

        async def on_button_pressed(self, event: Button.Pressed) -> None:
            self._read()
            if event.button.id == "save":
                self._read()
                if self.invalid_json:
                    return
                update_firmware(path, self.data)
                self.exit()
            elif event.button.id == "review-action":
                self.advanced = False
                self.page = 3
                await self.render_page()
            elif event.button.id == "back":
                self.action_previous()
            elif event.button.id == "next":
                self.action_next()

    FirmwareApp().run()

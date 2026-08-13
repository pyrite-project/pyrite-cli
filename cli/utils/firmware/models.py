from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class BoardContract:
    name: str
    base_board: str
    target: str = "esp32"
    micropython_commit: str = "unknown"
    complete: bool = False
    sources: tuple[dict[str, str], ...] = ()
    contract_hash: str = ""
    port: str = "esp32"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("board contract name is required")
        if not self.base_board:
            raise ValueError("base board is required")
        if not self.target:
            raise ValueError("board target is required")
        if self.complete:
            if self.micropython_commit == "unknown" or not self.micropython_commit:
                raise ValueError("complete board contract must pin a MicroPython commit")
            paths = {source.get("path") for source in self.sources}
            required = {
                f"ports/{self.target}/boards/{self.base_board}/mpconfigboard.h",
                f"ports/{self.target}/boards/{self.base_board}/mpconfigboard.cmake",
            }
            if not required.issubset(paths):
                raise ValueError("complete board contract must include both base board files")
            if not self.contract_hash:
                raise ValueError("complete board contract must include a contract hash")


@dataclass(frozen=True)
class Recipe:
    id: str
    title: str
    requires: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ("esp32",)
    macros: tuple[tuple[str, str], ...] = ()
    config: tuple[tuple[str, str], ...] = ()
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Resolution:
    requested: dict[str, bool]
    resolved: dict[str, bool]
    derived: dict[str, str] = field(default_factory=dict)
    macros: dict[str, str] = field(default_factory=dict)
    config: dict[str, str] = field(default_factory=dict)
    errors: tuple[str, ...] = ()

    @property
    def final(self) -> dict[str, bool]:
        """Return the complete selection after dependencies are derived."""
        return dict(self.resolved)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": dict(self.requested),
            "resolved": dict(self.resolved),
            "final": self.final,
            "derived": dict(self.derived),
            "macros": dict(self.macros),
            "config": dict(self.config),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class Pin:
    alias: str
    gpio: int | None
    board_pin: str = ""
    source: str = "manual"
    function: str = ""


@dataclass(frozen=True)
class Partition:
    name: str
    type: str
    subtype: str
    offset: int | None
    size: int
    flags: str = ""


@dataclass
class FirmwarePlan:
    board: BoardContract
    resolution: Resolution
    extensions: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: str = "unknown"

    def __post_init__(self) -> None:
        for error in self.resolution.errors:
            if error not in self.errors:
                self.errors.append(error)
        if self.status == "unknown":
            self.status = "blocked" if self.errors else "ready"
        elif self.status == "ready" and self.errors:
            self.status = "blocked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "board": asdict(self.board),
            "resolution": self.resolution.to_dict(),
            "extensions": self.extensions,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "status": self.status,
        }

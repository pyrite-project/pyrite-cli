"""Persistent local serial-port aliases for MicroPython boards.

The current on-disk format deliberately stores only the data that is used by
the CLI: an alias name and its serial port. The previous board-profile file
is accepted as a read-only migration source, while its unused metadata remains
untouched in the legacy file.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping


ALIAS_SCHEMA_VERSION = 1
DEFAULT_ALIAS_FILE = ".pyrite_board_aliases.json"
LEGACY_PROFILE_FILE = ".pyrite_board_profiles.json"
ALIAS_FILE_ENV = "PYRITE_BOARD_ALIAS_FILE"
LEGACY_PROFILE_FILE_ENV = "PYRITE_BOARD_PROFILE_FILE"

_ALIAS_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class BoardAliasError(ValueError):
    """Base error for board-alias operations."""


class DuplicateAliasError(BoardAliasError):
    """Raised when registering an existing alias without overwrite."""


class InvalidAliasError(BoardAliasError):
    """Raised when an alias or port value is malformed."""


class InvalidAliasFileError(BoardAliasError):
    """Raised when an alias file cannot be parsed or validated."""


class AliasNotFoundError(BoardAliasError):
    """Raised when a requested alias does not exist."""


@dataclass(frozen=True)
class BoardAlias:
    """A named serial port stored on the local machine."""

    name: str
    port: str

    def __post_init__(self) -> None:
        _validate_name(self.name)
        object.__setattr__(self, "port", _validate_port(self.port))

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "port": self.port}


class BoardAliasStore:
    """Read and persist board aliases, with legacy profile-file fallback."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        legacy_path: str | os.PathLike[str] | None = None,
    ) -> None:
        if path is None:
            alias_env = os.environ.get(ALIAS_FILE_ENV)
            self.path = Path(alias_env) if alias_env else Path.cwd() / DEFAULT_ALIAS_FILE
            old_env = os.environ.get(LEGACY_PROFILE_FILE_ENV)
            self.legacy_path = (
                Path(old_env) if old_env else Path.cwd() / LEGACY_PROFILE_FILE
            )
        else:
            self.path = Path(path)
            self.legacy_path = Path(legacy_path) if legacy_path is not None else None

    def list(self) -> list[BoardAlias]:
        aliases = self._load_aliases()
        return [aliases[name] for name in sorted(aliases)]

    def show(self, name_or_alias: str) -> BoardAlias:
        name = (
            alias_name(name_or_alias)
            if is_alias(name_or_alias)
            else _validate_name(name_or_alias)
        )
        aliases = self._load_aliases()
        try:
            return aliases[name]
        except KeyError as exc:
            raise AliasNotFoundError(f"board alias '{name}' was not found") from exc

    def register(self, port: str, *, name: str, overwrite: bool = False) -> BoardAlias:
        name = _validate_name(name)
        port = _validate_port(port)
        alias = BoardAlias(name=name, port=port)
        aliases = self._load_aliases()
        if name in aliases and not overwrite:
            raise DuplicateAliasError(f"board alias '{name}' already exists")
        aliases[name] = alias
        self._save_aliases(aliases)
        return alias

    def remove(self, name_or_alias: str) -> BoardAlias:
        name = (
            alias_name(name_or_alias)
            if is_alias(name_or_alias)
            else _validate_name(name_or_alias)
        )
        aliases = self._load_aliases()
        try:
            removed = aliases.pop(name)
        except KeyError as exc:
            raise AliasNotFoundError(f"board alias '{name}' was not found") from exc
        self._save_aliases(aliases)
        return removed

    def resolve(self, value: str) -> str:
        if not is_alias(value):
            return value
        return self.show(alias_name(value)).port

    def _source_path(self) -> Path | None:
        if self.path.exists():
            return self.path
        if self.legacy_path is not None and self.legacy_path.exists():
            return self.legacy_path
        return None

    def _load_aliases(self) -> dict[str, BoardAlias]:
        source = self._source_path()
        if source is None:
            return {}
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InvalidAliasFileError(f"board alias file is not valid JSON: {source}") from exc
        except OSError as exc:
            raise InvalidAliasFileError(f"cannot read board alias file: {source}") from exc

        try:
            if isinstance(data, dict) and "aliases" in data:
                return _parse_aliases(data)
            if isinstance(data, dict) and (
                "profiles" in data
                or (isinstance(data.get("name"), str) and "port" in data)
            ):
                return _parse_legacy_profiles(data)
            raise InvalidAliasError("alias file must contain an aliases object")
        except InvalidAliasError as exc:
            raise InvalidAliasFileError(f"invalid board alias file: {source}: {exc}") from exc

    def _save_aliases(self, aliases: Mapping[str, BoardAlias]) -> None:
        data = {
            "version": ALIAS_SCHEMA_VERSION,
            "aliases": {
                name: aliases[name].port
                for name in sorted(aliases)
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise InvalidAliasFileError(f"cannot write board alias file: {self.path}") from exc


def default_alias_path() -> Path:
    """Return the canonical alias path selected by the current environment."""

    env_path = os.environ.get(ALIAS_FILE_ENV)
    return Path(env_path) if env_path else Path.cwd() / DEFAULT_ALIAS_FILE


def default_legacy_profile_path() -> Path:
    """Return the compatibility profile path used as a read fallback."""

    env_path = os.environ.get(LEGACY_PROFILE_FILE_ENV)
    return Path(env_path) if env_path else Path.cwd() / LEGACY_PROFILE_FILE


def is_alias(value: str) -> bool:
    return isinstance(value, str) and value.startswith("@")


def alias_name(value: str) -> str:
    if not is_alias(value):
        raise InvalidAliasError("board alias must start with '@'")
    try:
        return _validate_name(value[1:])
    except InvalidAliasError as exc:
        raise InvalidAliasError(f"invalid board alias: {value}") from exc


def resolve_port_alias(
    value: str,
    *,
    store: BoardAliasStore | None = None,
    alias_file: str | os.PathLike[str] | None = None,
    profile_file: str | os.PathLike[str] | None = None,
) -> str:
    """Resolve ``@alias`` while leaving ordinary port values unchanged."""

    if not is_alias(value):
        return value
    if store is None:
        selected_path = alias_file if alias_file is not None else profile_file
        store = BoardAliasStore(selected_path)
    return store.resolve(value)


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not _ALIAS_NAME_RE.fullmatch(name):
        raise InvalidAliasError(
            "alias name must start with a letter or number and contain only letters, "
            "numbers, dots, underscores, or hyphens"
        )
    return name


def _validate_port(port: object) -> str:
    if not isinstance(port, str) or not port.strip():
        raise InvalidAliasError("alias port must be a non-empty string")
    return port.strip()


def _parse_aliases(data: Any) -> dict[str, BoardAlias]:
    if not isinstance(data, dict):
        raise InvalidAliasError("alias file must contain an object")
    if data.get("version") != ALIAS_SCHEMA_VERSION:
        raise InvalidAliasError(f"unsupported alias schema version: {data.get('version')!r}")
    raw_aliases = data.get("aliases")
    if not isinstance(raw_aliases, dict):
        raise InvalidAliasError("aliases must be an object")

    aliases: dict[str, BoardAlias] = {}
    for raw_name, raw_port in raw_aliases.items():
        name = _validate_name(raw_name)
        aliases[name] = BoardAlias(name=name, port=_validate_port(raw_port))
    return aliases


def _parse_legacy_profiles(data: Any) -> dict[str, BoardAlias]:
    """Extract only name/port from the pre-alias profile format."""

    if not isinstance(data, dict):
        raise InvalidAliasError("legacy profile file must contain an object")

    if "profiles" in data:
        raw_profiles = data["profiles"]
        if not isinstance(raw_profiles, dict):
            raise InvalidAliasError("profiles must be an object")
    elif isinstance(data.get("name"), str) and "port" in data:
        raw_profiles = {data["name"]: data}
    else:
        raise InvalidAliasError("legacy profile file must contain profiles")

    aliases: dict[str, BoardAlias] = {}
    for raw_name, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, Mapping):
            raise InvalidAliasError("each legacy profile must be an object")
        name = _validate_name(raw_name)
        embedded_name = raw_profile.get("name", raw_name)
        if embedded_name != raw_name:
            raise InvalidAliasError("profile key and name must match")
        aliases[name] = BoardAlias(name=name, port=_validate_port(raw_profile.get("port")))
    return aliases

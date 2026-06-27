from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


PROFILE_SCHEMA_VERSION = 1
DEFAULT_PROFILE_FILE = ".pyrite_board_profiles.json"
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_RECOMMENDED_KEYS = {
    "auto_compile",
    "baudrate",
    "chunk_size",
    "delta_flash",
    "delta_min_size",
    "download_threads",
    "max_retries",
    "timeout",
    "verify",
}


class BoardProfileError(ValueError):
    """Base error for board profile operations."""


class DuplicateProfileError(BoardProfileError):
    """Raised when registering an existing profile without overwrite."""


class InvalidAliasError(BoardProfileError):
    """Raised when an alias string is malformed."""


class InvalidProfileError(BoardProfileError):
    """Raised when a profile field is invalid."""


class ProfileNotFoundError(BoardProfileError):
    """Raised when a requested profile does not exist."""


@dataclass(frozen=True)
class BoardProfile:
    name: str
    port: str
    tags: list[str] = field(default_factory=list)
    last_firmware: str | None = None
    recommended: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "port": self.port,
            "tags": list(self.tags),
            "last_firmware": self.last_firmware,
            "recommended": dict(sorted(self.recommended.items())),
        }
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BoardProfile":
        name = data.get("name")
        port = data.get("port")
        if not isinstance(name, str):
            raise InvalidProfileError("profile name must be a string")
        if not isinstance(port, str) or not port.strip():
            raise InvalidProfileError("profile port must be a non-empty string")
        _validate_profile_name(name)

        tags_raw = data.get("tags", [])
        if tags_raw is None:
            tags_raw = []
        if not isinstance(tags_raw, list) or not all(isinstance(t, str) for t in tags_raw):
            raise InvalidProfileError("profile tags must be a list of strings")

        firmware = data.get("last_firmware", data.get("firmware"))
        if firmware is not None and not isinstance(firmware, str):
            raise InvalidProfileError("profile firmware must be a string")

        recommended = data.get("recommended", {})
        if recommended is None:
            recommended = {}
        if not isinstance(recommended, dict):
            raise InvalidProfileError("profile recommended must be an object")

        return cls(
            name=name,
            port=port.strip(),
            tags=_unique_nonempty(tags_raw),
            last_firmware=firmware,
            recommended=_validate_recommended(recommended),
        )


class BoardProfileStore:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_profile_path()

    def list(self) -> list[BoardProfile]:
        profiles = self._load_profiles()
        return [profiles[name] for name in sorted(profiles)]

    def show(self, name_or_alias: str) -> BoardProfile:
        name = alias_name(name_or_alias) if is_alias(name_or_alias) else name_or_alias
        _validate_profile_name(name)
        profiles = self._load_profiles()
        try:
            return profiles[name]
        except KeyError as exc:
            raise ProfileNotFoundError(f"board profile '{name}' was not found") from exc

    def register(
        self,
        port: str,
        *,
        name: str,
        tags: Iterable[str] | None = None,
        firmware: str | None = None,
        recommended: Mapping[str, Any] | None = None,
        overwrite: bool = False,
    ) -> BoardProfile:
        _validate_profile_name(name)
        if not isinstance(port, str) or not port.strip():
            raise InvalidProfileError("profile port must be a non-empty string")
        profile = BoardProfile(
            name=name,
            port=port.strip(),
            tags=_unique_nonempty(tags or []),
            last_firmware=firmware.strip() if isinstance(firmware, str) and firmware.strip() else None,
            recommended=_validate_recommended(recommended or {}),
        )
        profiles = self._load_profiles()
        if name in profiles and not overwrite:
            raise DuplicateProfileError(f"board profile '{name}' already exists")
        profiles[name] = profile
        self._save_profiles(profiles)
        return profile

    def remove(self, name_or_alias: str) -> BoardProfile:
        name = alias_name(name_or_alias) if is_alias(name_or_alias) else name_or_alias
        _validate_profile_name(name)
        profiles = self._load_profiles()
        try:
            removed = profiles.pop(name)
        except KeyError as exc:
            raise ProfileNotFoundError(f"board profile '{name}' was not found") from exc
        self._save_profiles(profiles)
        return removed

    def resolve(self, value: str) -> str:
        if not is_alias(value):
            return value
        return self.show(alias_name(value)).port

    def _load_profiles(self) -> dict[str, BoardProfile]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InvalidProfileError(f"board profile file is not valid JSON: {self.path}") from exc
        except OSError as exc:
            raise InvalidProfileError(f"cannot read board profile file: {self.path}") from exc

        profiles_raw = _extract_profiles(data)
        profiles: dict[str, BoardProfile] = {}
        for key, value in profiles_raw.items():
            if not isinstance(value, Mapping):
                raise InvalidProfileError("each profile must be an object")
            item = dict(value)
            item.setdefault("name", key)
            profile = BoardProfile.from_dict(item)
            if profile.name != key:
                raise InvalidProfileError("profile key and name must match")
            profiles[profile.name] = profile
        return profiles

    def _save_profiles(self, profiles: Mapping[str, BoardProfile]) -> None:
        data = {
            "version": PROFILE_SCHEMA_VERSION,
            "profiles": {
                name: profiles[name].to_dict()
                for name in sorted(profiles)
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def default_profile_path() -> Path:
    env_path = os.environ.get("PYRITE_BOARD_PROFILE_FILE")
    if env_path:
        return Path(env_path)
    return Path.cwd() / DEFAULT_PROFILE_FILE


def is_alias(value: str) -> bool:
    return isinstance(value, str) and value.startswith("@")


def alias_name(value: str) -> str:
    if not is_alias(value):
        raise InvalidAliasError("board alias must start with '@'")
    name = value[1:]
    try:
        _validate_profile_name(name)
    except InvalidProfileError as exc:
        raise InvalidAliasError(f"invalid board alias: {value}") from exc
    return name


def resolve_port_alias(
    value: str,
    *,
    store: BoardProfileStore | None = None,
    profile_file: str | os.PathLike[str] | None = None,
) -> str:
    if not is_alias(value):
        return value
    return (store or BoardProfileStore(profile_file)).resolve(value)


def parse_recommended_items(items: Iterable[str] | None) -> dict[str, Any]:
    recommended: dict[str, Any] = {}
    for item in items or []:
        key, sep, raw_value = item.partition("=")
        key = key.strip()
        if sep != "=" or not key:
            raise InvalidProfileError("recommended entries must use KEY=VALUE")
        if key not in _RECOMMENDED_KEYS:
            raise InvalidProfileError(f"unsupported recommended key: {key}")
        recommended[key] = _parse_scalar(raw_value.strip())
    return recommended


def _extract_profiles(data: Any) -> Mapping[str, Any]:
    if not isinstance(data, dict):
        raise InvalidProfileError("board profile file must contain an object")

    if "profiles" in data:
        profiles = data["profiles"]
        if not isinstance(profiles, dict):
            raise InvalidProfileError("profiles must be an object")
        return profiles

    if isinstance(data.get("name"), str) and isinstance(data.get("port"), str):
        return {data["name"]: data}

    return {}


def _validate_profile_name(name: str) -> None:
    if not isinstance(name, str) or not _PROFILE_NAME_RE.fullmatch(name):
        raise InvalidProfileError(
            "profile name must start with a letter or number and contain only letters, "
            "numbers, dots, underscores, or hyphens"
        )


def _unique_nonempty(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _validate_recommended(data: Mapping[str, Any]) -> dict[str, Any]:
    recommended: dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(key, str) or key not in _RECOMMENDED_KEYS:
            raise InvalidProfileError(f"unsupported recommended key: {key}")
        if not _is_json_scalar(value):
            raise InvalidProfileError("recommended values must be JSON scalar values")
        _validate_recommended_value(key, value)
        recommended[key] = value
    return recommended


def _validate_recommended_value(key: str, value: Any) -> None:
    if key == "auto_compile":
        if not isinstance(value, bool):
            raise InvalidProfileError("recommended auto_compile must be true or false")
        return

    if key in {"baudrate", "chunk_size", "download_threads", "timeout"}:
        if type(value) is not int or value <= 0:
            raise InvalidProfileError(f"recommended {key} must be a positive integer")
        return

    if key in {"delta_min_size", "max_retries"}:
        if type(value) is not int or value < 0:
            raise InvalidProfileError(f"recommended {key} must be a non-negative integer")
        return

    if key == "delta_flash":
        if value not in {"off", "auto", "on"}:
            raise InvalidProfileError("recommended delta_flash must be off, auto, or on")
        return

    if key == "verify":
        if value not in {"off", "size", "crc32"}:
            raise InvalidProfileError("recommended verify must be off, size, or crc32")


def _parse_scalar(value: str) -> Any:
    if value == "":
        return ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if not _is_json_scalar(parsed):
        raise InvalidProfileError("recommended values must be JSON scalar values")
    return parsed


def _is_json_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))

from __future__ import annotations

import re


_VARIANT_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def normalize_variant(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("variant name must be a string")
    value = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").upper()
    if not value or not _VARIANT_RE.fullmatch(value):
        raise ValueError("invalid variant name")
    return value


def validate_variants(names: list[str]) -> list[str]:
    errors: list[str] = []
    normalized: list[str] = []
    for name in names:
        try:
            normalized.append(normalize_variant(name))
        except ValueError as exc:
            errors.append(f"invalid variant {name!r}: {exc}")
    if len(set(normalized)) != len(normalized):
        errors.append("duplicate variant name")
    return errors

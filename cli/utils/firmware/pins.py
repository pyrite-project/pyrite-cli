from __future__ import annotations

import csv
import io
import re

from .models import Pin


_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_pins(text: str) -> list[Pin]:
    rows = csv.DictReader(io.StringIO(text))
    if rows.fieldnames is None:
        return []
    headings = {heading.strip().lower() for heading in rows.fieldnames if heading}
    if not ({"alias", "name"} & headings) or "gpio" not in headings:
        raise ValueError("pins CSV requires alias (or name) and gpio columns")

    result: list[Pin] = []
    seen_alias: set[str] = set()
    seen_gpio: set[int] = set()
    for line_number, row in enumerate(rows, start=2):
        normalized = {
            (key or "").strip().lower(): (value or "").strip()
            for key, value in row.items()
            if key is not None
        }
        alias = normalized.get("alias") or normalized.get("name") or ""
        raw = normalized.get("gpio", "")
        if not alias:
            raise ValueError(f"line {line_number}: pin alias is required")
        if not _ALIAS_RE.fullmatch(alias):
            raise ValueError(f"line {line_number}: invalid pin alias: {alias}")
        if alias in seen_alias:
            raise ValueError(f"duplicate pin alias: {alias}")
        try:
            gpio = None if raw in ("", "-") else int(raw, 0)
        except ValueError as exc:
            raise ValueError(f"line {line_number}: invalid GPIO: {raw}") from exc
        if gpio is not None and gpio in seen_gpio:
            raise ValueError(f"duplicate GPIO: {gpio}")
        seen_alias.add(alias)
        if gpio is not None:
            seen_gpio.add(gpio)
        result.append(
            Pin(
                alias,
                gpio,
                normalized.get("board_pin", ""),
                normalized.get("source", "manual") or "manual",
                normalized.get("function", ""),
            )
        )
    return result


def validate_pins(
    pins: list[Pin], gpio_count: int = 40, unavailable: set[int] | None = None
) -> list[str]:
    unavailable = unavailable or set()
    errors: list[str] = []
    aliases: set[str] = set()
    gpios: set[int] = set()
    if gpio_count <= 0:
        return ["GPIO count must be positive"]
    for pin in pins:
        if not _ALIAS_RE.fullmatch(pin.alias):
            errors.append(f"{pin.alias}: invalid alias")
        elif pin.alias in aliases:
            errors.append(f"duplicate pin alias: {pin.alias}")
        aliases.add(pin.alias)
        if pin.gpio is not None:
            if pin.gpio in gpios:
                errors.append(f"duplicate GPIO: {pin.gpio}")
            gpios.add(pin.gpio)
            if pin.gpio < 0 or pin.gpio >= gpio_count:
                errors.append(f"{pin.alias}: GPIO out of range")
            if pin.gpio in unavailable:
                errors.append(f"{pin.alias}: GPIO unavailable")
        if pin.source not in {"base", "schematic", "import", "manual"}:
            errors.append(f"{pin.alias}: unknown source")
    return errors


def render_pins(pins: list[Pin]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["alias", "gpio", "board_pin", "source", "function"])
    for pin in sorted(pins, key=lambda item: item.alias):
        writer.writerow(
            [
                pin.alias,
                "" if pin.gpio is None else pin.gpio,
                pin.board_pin,
                pin.source,
                pin.function,
            ]
        )
    return out.getvalue()

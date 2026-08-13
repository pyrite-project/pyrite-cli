from __future__ import annotations

import csv
import hashlib
import io

from .models import Partition


def parse_partitions(text: str) -> list[Partition]:
    out: list[Partition] = []
    for line_number, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        if not row or not any(cell.strip() for cell in row):
            continue
        if row[0].strip().startswith("#"):
            continue
        if len(row) < 5:
            raise ValueError(
                f"line {line_number}: partition requires name,type,subtype,offset,size"
            )
        name, kind, subtype = (cell.strip() for cell in row[:3])
        if not name or not kind or not subtype:
            raise ValueError(f"line {line_number}: partition name/type/subtype are required")
        try:
            offset = None if not row[3].strip() else int(row[3].strip(), 0)
            size = int(row[4].strip(), 0)
        except ValueError as exc:
            raise ValueError(f"line {line_number}: offset and size must be integers") from exc
        flags = row[5].strip() if len(row) > 5 else ""
        out.append(Partition(name, kind, subtype, offset, size, flags))
    return out


def _align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def validate_partitions(parts: list[Partition], flash_size: int = 4 * 1024 * 1024) -> list[str]:
    errors: list[str] = []
    if flash_size <= 0:
        return ["flash size must be positive"]
    used: list[tuple[int, int, str]] = []
    names: set[str] = set()
    cursor = 0x9000
    for partition in parts:
        if not partition.name:
            errors.append("partition name is required")
            continue
        if partition.name in names:
            errors.append(f"duplicate partition: {partition.name}")
        names.add(partition.name)
        if partition.size <= 0:
            errors.append(f"{partition.name}: size must be positive")
            continue
        if partition.offset is not None and partition.offset < 0:
            errors.append(f"{partition.name}: offset must not be negative")
            continue

        alignment = 0x10000 if partition.type.strip().lower() == "app" else 0x1000
        start = (
            partition.offset
            if partition.offset is not None
            else _align_up(cursor, alignment)
        )
        end = start + partition.size
        if start % alignment:
            errors.append(f"{partition.name}: invalid alignment")
        if start >= flash_size or end > flash_size:
            errors.append(f"{partition.name}: exceeds flash")
        for previous_start, previous_end, previous_name in used:
            if start < previous_end and end > previous_start:
                errors.append(f"{partition.name}: overlaps {previous_name}")
        used.append((start, end, partition.name))
        # Explicit offsets may move forward or backward, but an implicit next
        # partition must never begin before any partition already encountered.
        cursor = max(cursor, end)

    if any(partition.name == "otadata" for partition in parts) and not all(
        any(partition.name == name for partition in parts)
        for name in ("ota_0", "ota_1")
    ):
        errors.append("OTA requires otadata, ota_0 and ota_1")
    return errors


def render_partitions(parts: list[Partition]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["# Name", "Type", "SubType", "Offset", "Size", "Flags"])
    for partition in parts:
        writer.writerow(
            [
                partition.name,
                partition.type,
                partition.subtype,
                "" if partition.offset is None else hex(partition.offset),
                hex(partition.size),
                partition.flags,
            ]
        )
    return out.getvalue()


def layout_fingerprint(parts: list[Partition]) -> str:
    return hashlib.sha256(render_partitions(parts).encode()).hexdigest()

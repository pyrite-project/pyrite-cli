from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import FirmwarePlan
from .cmodules import validate_module_cmake
from .frozen import validate_manifest
from .partitions import parse_partitions, render_partitions, validate_partitions
from .pins import parse_pins, render_pins, validate_pins
from .variants import normalize_variant, validate_variants

_BOARD_FILES = ("mpconfigboard.h", "mpconfigboard.cmake")


def _macro_header(macros: dict[str, str]) -> str:
    return "".join(f"#define {key} {macros[key]}\n" for key in sorted(macros))


def _sdkconfig(config: dict[str, str]) -> str:
    return "".join(f"{key}={config[key]}\n" for key in sorted(config))


def _extension_values(value: object) -> list[Path]:
    if isinstance(value, (str, Path)):
        return [Path(value)]
    if isinstance(value, list):
        return [Path(str(item)) for item in value]
    return []


def _external_destination(destination: Path, checkout: Path | None) -> Path:
    result = destination.expanduser().resolve()
    if checkout is None:
        return result
    source = checkout.expanduser().resolve()
    try:
        result.relative_to(source)
    except ValueError:
        return result
    raise ValueError(
        f"generated BOARD_DIR must be outside the MicroPython checkout: {result}"
    )


def _read_base_board(plan: FirmwarePlan, checkout: Path) -> dict[str, str]:
    source = checkout.expanduser().resolve(strict=True)
    board_root = source / "ports" / plan.board.port / "boards" / plan.board.base_board
    files: dict[str, str] = {}
    for filename in _BOARD_FILES:
        path = board_root / filename
        if not path.is_file():
            raise ValueError(
                f"base board contract is incomplete; missing {path.relative_to(source)}"
            )
        files[filename] = path.read_text(encoding="utf8")
    return files


def generate_board(
    plan: FirmwarePlan,
    destination: Path,
    *,
    checkout: Path | None = None,
    base_header: str | None = None,
    base_cmake: str | None = None,
) -> dict[str, str]:
    """Generate a standalone ``BOARD_DIR`` without writing to MicroPython.

    A generated board must inherit both required files from its selected base
    board.  Callers should pass ``checkout`` so those files are copied from the
    verified contract.  Explicit contents remain available for callers that
    already loaded and verified the base board themselves; empty defaults are
    deliberately rejected.
    """
    if not plan.board.complete:
        raise ValueError("board contract is incomplete")
    destination = _external_destination(destination, checkout)
    if checkout is not None:
        from .boards import checkout_revision
        revision = checkout_revision(checkout)
        if revision != plan.board.micropython_commit:
            raise ValueError("MicroPython checkout revision does not match board contract")
        base = _read_base_board(plan, checkout)
        expected = {Path(item["path"]).name: item["sha256"] for item in plan.board.sources}
        for name, text in base.items():
            if expected.get(name) != hashlib.sha256(text.encode()).hexdigest():
                raise ValueError(f"base board source hash does not match contract: {name}")
        base_header = base["mpconfigboard.h"]
        base_cmake = base["mpconfigboard.cmake"]
    if base_header is None or base_cmake is None:
        raise ValueError("base board contract requires mpconfigboard.h and mpconfigboard.cmake")

    destination.mkdir(parents=True, exist_ok=True)
    # Keep inherited board files intact, then append deterministic generated
    # settings.  SDKCONFIG_DEFAULTS is consumed by the ESP-IDF build.
    header = base_header.rstrip() + "\n" + _macro_header(plan.resolution.macros)
    sdkconfig = dict(plan.resolution.config)
    sdkconfig_text = _sdkconfig(sdkconfig)
    sdkconfig_path = destination / "sdkconfig.pyrite"
    sdkconfig_path.write_text(sdkconfig_text, encoding="utf8")
    cmake = base_cmake.rstrip() + '\nlist(APPEND SDKCONFIG_DEFAULTS "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.pyrite")\n'
    files = {
        "board.json": json.dumps(plan.to_dict(), sort_keys=True, indent=2) + "\n",
        "mpconfigboard.h": header,
        "mpconfigboard.cmake": cmake,
        "sdkconfig.pyrite": sdkconfig_text,
    }
    # Materialize every configured extension as a build input, never merely as
    # metadata.  Files are copied into stable subdirectories and referenced by
    # the generated CMake board file.
    extension_lines: list[str] = []
    generated_pin_file: str | None = None
    generated_partition_file: str | None = None
    generated_manifest: str | None = None
    generated_modules: str | None = None
    generated_variants: list[str] = []
    for kind in ("pins", "partition", "frozen", "c_modules", "variants"):
        values = _extension_values(plan.extensions.get(kind))
        if not values:
            continue
        out_dir = destination / "extensions" / kind
        out_dir.mkdir(parents=True, exist_ok=True)
        for index, source_path in enumerate(values):
            if not source_path.is_file():
                raise ValueError(f"{kind} extension does not exist: {source_path}")
            if kind == "pins":
                parsed = parse_pins(source_path.read_text(encoding="utf8"))
                errors = validate_pins(parsed)
                if errors:
                    raise ValueError("pins: " + "; ".join(errors))
                # MicroPython's boardgen consumes board-pin,cpu-pin pairs,
                # rather than Pyrite's richer provenance CSV.
                content = "".join(
                    f"{pin.board_pin or pin.alias},GPIO{pin.gpio}\n"
                    for pin in sorted(parsed, key=lambda item: item.alias)
                    if pin.gpio is not None
                )
                relative = Path("pins.csv")
                generated_pin_file = relative.as_posix()
            elif kind == "partition":
                parsed_parts = parse_partitions(source_path.read_text(encoding="utf8"))
                errors = validate_partitions(parsed_parts)
                if errors:
                    raise ValueError("partition: " + "; ".join(errors))
                content = render_partitions(parsed_parts)
                relative = Path("partitions.csv")
                generated_partition_file = relative.as_posix()
            elif kind == "frozen":
                errors = validate_manifest(source_path)
                if errors:
                    raise ValueError("frozen: " + "; ".join(errors))
                content = source_path.read_text(encoding="utf8")
                relative = Path("extensions") / kind / f"{index}.py"
                generated_manifest = "manifest.py"
            elif kind == "c_modules":
                errors = validate_module_cmake(source_path)
                if errors:
                    raise ValueError("c_modules: " + "; ".join(errors))
                content = source_path.read_text(encoding="utf8")
                relative = Path("extensions") / kind / f"{index}.cmake"
                generated_modules = "micropython.cmake"
            else:
                variants = [
                    normalize_variant(line.strip())
                    for line in source_path.read_text(encoding="utf8").splitlines()
                    if line.strip()
                ]
                errors = validate_variants(variants)
                if errors:
                    raise ValueError("variants: " + "; ".join(errors))
                content = "\n".join(sorted(variants)) + "\n"
                relative = Path("extensions") / kind / f"{index}.txt"
                generated_variants.extend(sorted(variants))
            (destination / relative).write_text(content, encoding="utf8")
            files[relative.as_posix()] = content

    if generated_pin_file:
        extension_lines.append(
            f"set(MICROPY_BOARD_PINS_CSV ${{CMAKE_CURRENT_LIST_DIR}}/{generated_pin_file})\n"
        )
    if generated_partition_file:
        extension_lines.append("set(CONFIG_PARTITION_TABLE_CUSTOM y)\n")
        extension_lines.append(
            'set(CONFIG_PARTITION_TABLE_CUSTOM_FILENAME "partitions.csv")\n'
        )
    if generated_manifest:
        manifest = "".join(
            f'include("extensions/frozen/{index}.py")\n'
            for index, value in enumerate(_extension_values(plan.extensions.get("frozen")))
        )
        (destination / generated_manifest).write_text(manifest, encoding="utf8")
        files[generated_manifest] = manifest
        extension_lines.append(
            f"set(MICROPY_FROZEN_MANIFEST ${{CMAKE_CURRENT_LIST_DIR}}/{generated_manifest})\n"
        )
    if generated_modules:
        modules = "".join(
            f"include(${{CMAKE_CURRENT_LIST_DIR}}/extensions/c_modules/{index}.cmake)\n"
            for index, value in enumerate(_extension_values(plan.extensions.get("c_modules")))
        )
        (destination / generated_modules).write_text(modules, encoding="utf8")
        files[generated_modules] = modules
        extension_lines.append(
            f"set(USER_C_MODULES ${{CMAKE_CURRENT_LIST_DIR}}/{generated_modules})\n"
        )
    if generated_variants:
        for variant in sorted(set(generated_variants)):
            variant_file = destination / f"mpconfigvariant_{variant}.cmake"
            variant_file.write_text("", encoding="utf8")
            files[variant_file.name] = ""
    files["mpconfigboard.h"] = header
    files["mpconfigboard.cmake"] = cmake + "".join(extension_lines)
    (destination / "mpconfigboard.h").write_text(files["mpconfigboard.h"], encoding="utf8")
    (destination / "mpconfigboard.cmake").write_text(files["mpconfigboard.cmake"], encoding="utf8")
    (destination / "board.json").write_text(files["board.json"], encoding="utf8")
    return {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}

from __future__ import annotations

from collections.abc import Mapping

from .models import Recipe, Resolution


CATALOG = {
    "network.core": Recipe(
        "network.core", "Network", macros=(("MICROPY_PY_NETWORK", "1"),)
    ),
    "bluetooth.nimble": Recipe(
        "bluetooth.nimble",
        "Bluetooth with NimBLE",
        requires=("network.core",),
        macros=(("MICROPY_PY_BLUETOOTH", "1"),),
        config=(("CONFIG_BT_ENABLED", "y"), ("CONFIG_BT_NIMBLE_ENABLED", "y")),
    ),
    "network.wlan_csi": Recipe(
        "network.wlan_csi",
        "WLAN CSI",
        requires=("network.core",),
        macros=(("MICROPY_PY_NETWORK_WLAN_CSI", "1"),),
    ),
    "usb.device.cdc": Recipe(
        "usb.device.cdc",
        "USB CDC",
        platforms=("esp32", "esp32s2", "esp32s3"),
        macros=(("MICROPY_HW_USB_CDC", "1"),),
    ),
}


def list_recipes() -> list[Recipe]:
    return list(CATALOG.values())


def get_recipe(recipe_id: str) -> Recipe:
    try:
        return CATALOG[recipe_id]
    except KeyError:
        raise ValueError(f"unknown firmware recipe: {recipe_id}") from None


def resolve(selections: Mapping[str, bool], platform: str = "esp32") -> Resolution:
    """Resolve enabled recipes, including their complete dependency closure.

    Dependencies are part of the final build even when they were not explicitly
    requested.  Invalid requests are retained in ``errors`` so callers can show
    a blocked plan without accidentally treating a partial resolution as valid.
    """
    if not isinstance(selections, Mapping):
        raise TypeError("firmware selections must be a mapping")

    requested = dict(selections)
    platform_name = platform.strip().lower()
    resolved: dict[str, bool] = {}
    derived: dict[str, str] = {}
    macros: dict[str, str] = {}
    config: dict[str, str] = {}
    errors: list[str] = []
    visiting: list[str] = []
    reported: set[str] = set()

    def error(message: str) -> None:
        if message not in reported:
            reported.add(message)
            errors.append(message)

    def visit(recipe_id: str, parent: str | None = None) -> bool:
        if recipe_id in resolved:
            return True
        if recipe_id in visiting:
            cycle = " -> ".join([*visiting, recipe_id])
            error(f"cyclic firmware dependency: {cycle}")
            return False
        try:
            recipe = get_recipe(recipe_id)
        except ValueError as exc:
            error(str(exc))
            return False

        supported = {item.strip().lower() for item in recipe.platforms}
        if platform_name not in supported:
            error(f"{recipe_id} is not supported on {platform}")
            return False

        visiting.append(recipe_id)
        valid = True
        for dependency in recipe.requires:
            if not visit(dependency, recipe_id):
                valid = False
        visiting.pop()

        for conflict in recipe.conflicts:
            if conflict in resolved or bool(selections.get(conflict, False)):
                error(f"{recipe_id} conflicts with {conflict}")
                valid = False

        # Keep a failed recipe out of the final state; a blocked plan must never
        # be mistaken for a buildable plan by downstream generators.
        if not valid:
            return False
        output_valid = True
        for kind, target, values in (
            ("macro", macros, recipe.macros),
            ("config", config, recipe.config),
        ):
            for key, value in values:
                previous = target.get(key)
                if previous is not None and previous != value:
                    error(
                        f"conflicting {kind} value for {key}: "
                        f"{previous!r} vs {value!r}"
                    )
                    output_valid = False
        if not output_valid:
            return False
        for key, value in recipe.macros:
            macros[key] = value
        for key, value in recipe.config:
            config[key] = value
        resolved[recipe_id] = True
        if parent is not None and not bool(selections.get(recipe_id, False)):
            derived[recipe_id] = f"required by {parent}"
        return True

    for recipe_id, enabled in selections.items():
        if not enabled:
            continue
        if not isinstance(recipe_id, str) or not recipe_id:
            error(f"invalid firmware recipe id: {recipe_id!r}")
            continue
        visit(recipe_id)

    return Resolution(requested, resolved, derived, macros, config, tuple(errors))

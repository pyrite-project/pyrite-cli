"""Public facade for MicroPython flash operations."""

from . import core as _core

_export_names = [
    _name
    for _name in dir(_core)
    if not (_name.startswith("__") and _name.endswith("__"))
]

for _name in _export_names:
    globals()[_name] = getattr(_core, _name)

__all__ = _export_names

del _export_names, _name

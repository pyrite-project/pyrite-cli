"""Public import surface for flash utilities."""

import sys

from . import facade as _flash

_export_names = list(_flash.__all__)

for _name in _export_names:
    globals()[_name] = getattr(_flash, _name)

__all__ = _export_names

MicroPython = _flash.MicroPython
flash = _flash
sys.modules.setdefault(__name__ + ".flash", _flash)

del _export_names, _name, sys

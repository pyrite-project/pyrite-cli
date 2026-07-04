from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap_checkout_venv() -> None:
    """Expose the checkout-local venv for bare python smoke checks."""
    root = Path(__file__).resolve().parent.parent
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site_packages = root / ".venv" / "lib" / version / "site-packages"
    if not site_packages.is_dir():
        return
    path = str(site_packages)
    if path not in sys.path:
        sys.path.append(path)


_bootstrap_checkout_venv()

__version__ = "0.0.3"

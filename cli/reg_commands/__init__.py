"""Command-group registration for pyrite-cli."""

from __future__ import annotations

import typer


def register_command_groups(app: typer.Typer) -> None:
    from . import debug, device, fs, pkg, project

    debug.register(app)
    pkg.register(app)
    project.register(app)
    device.register(app)
    fs.register(app)

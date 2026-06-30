"""Command-group registration for pyrite-cli."""

from __future__ import annotations

import typer


def register_command_groups(app: typer.Typer) -> None:
    from . import (
        board,
        debug,
        device,
        device_test,
        fs,
        manifest,
        pkg,
        project,
        snapshot,
        trace,
        tunnel,
    )

    board.register(app)
    debug.register(app)
    pkg.register(app)
    manifest.register(app)
    project.register(app)
    device.register(app)
    fs.register(app)
    device_test.register(app)
    snapshot.register(app)
    trace.register(app)
    tunnel.register(app)

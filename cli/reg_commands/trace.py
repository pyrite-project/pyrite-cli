"""Trace file inspection commands."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ..utils.trace import (
    format_trace_summary,
    format_trace_view,
    load_trace,
    summarize_trace,
)
from ..utils.ui import print_json
from .common import _FORMAT_OPTION, _JSON_OPTION, _resolve_format


trace_app = typer.Typer(help="Flight Recorder trace tools", add_completion=False)


def register(app: typer.Typer) -> None:
    app.add_typer(trace_app, name="trace")


@trace_app.command("view")
def trace_view(
    path: Path = typer.Argument(..., help="Trace file path"),
    tail: Optional[int] = typer.Option(None, "--tail", help="Only show the last N events"),
) -> None:
    """Render a trace file as readable event lines."""
    records = load_trace(path)
    typer.echo(format_trace_view(records, limit=tail))


@trace_app.command("summarize")
def trace_summarize(
    path: Path = typer.Argument(..., help="Trace file path"),
    tail: int = typer.Option(10, "--tail", help="Include last N compact events"),
    fmt: str = _FORMAT_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Summarize traffic, phases, and failures in a trace file."""
    summary = summarize_trace(path, tail=tail)
    fmt = _resolve_format(fmt, json_output)
    if fmt == "json":
        print_json(summary)
        return
    typer.echo(format_trace_summary(summary))

"""Map MicroPython traceback device paths back to local source files."""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Set, Tuple

from .build import load_manifest


_FRAME_RE = re.compile(
    r'^(?P<indent>\s*)File "(?P<path>[^"]+)", line (?P<line>\d+)'
    r"(?:, in (?P<function>.*))?\s*$"
)

_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".pyrite_cache",
    ".venv",
    "venv",
    "env",
    "build",
    "dist",
}


@dataclass(frozen=True)
class TracebackFrame:
    raw: str
    path: str
    line: int
    function: Optional[str] = None


@dataclass(frozen=True)
class RemoteSourceMapping:
    remote_path: str
    local_path: str
    is_bytecode: bool = False
    source_path: Optional[str] = None


@dataclass(frozen=True)
class TracebackPathMatch:
    remote_path: str
    remote_line: int
    local_path: str
    local_line: Optional[int]
    is_bytecode: bool = False
    source_path: Optional[str] = None

    def format(self) -> str:
        remote_ref = f"{self.remote_path}:{self.remote_line}"
        if self.is_bytecode:
            return (
                f"{remote_ref} -> {self.local_path} "
                "(.mpy bytecode; source line unavailable)"
            )
        local_ref = self.local_path
        if self.local_line is not None:
            local_ref = f"{local_ref}:{self.local_line}"
        return f"{remote_ref} -> {local_ref}"


def parse_traceback_frame(line: str) -> Optional[TracebackFrame]:
    """Parse one MicroPython traceback frame line."""
    match = _FRAME_RE.match(line.rstrip("\r\n"))
    if match is None:
        return None
    return TracebackFrame(
        raw=line,
        path=match.group("path"),
        line=int(match.group("line")),
        function=match.group("function"),
    )


def parse_traceback_frames(text: str) -> list[TracebackFrame]:
    """Return structured traceback frames found in ``text``."""
    frames: list[TracebackFrame] = []
    for line in text.splitlines():
        frame = parse_traceback_frame(line)
        if frame is not None:
            frames.append(frame)
    return frames


class TracebackMapper:
    """Map traceback frame paths with exact and conservative suffix rules."""

    def __init__(
        self,
        mappings: Iterable[RemoteSourceMapping],
        *,
        local_base: Optional[str | Path] = None,
    ) -> None:
        self.local_base = Path(local_base).resolve() if local_base else None
        self._mappings: list[RemoteSourceMapping] = []
        self._exact: dict[str, RemoteSourceMapping] = {}
        for mapping in mappings:
            remote_key = _normalise_remote_path(mapping.remote_path)
            if not remote_key or remote_key.startswith("<"):
                continue
            normalised = RemoteSourceMapping(
                remote_path=remote_key,
                local_path=_local_display_path(
                    mapping.source_path or mapping.local_path,
                    self.local_base,
                ),
                is_bytecode=mapping.is_bytecode,
                source_path=_local_source_path(
                    mapping.source_path or mapping.local_path,
                    self.local_base,
                ),
            )
            self._mappings.append(normalised)
            self._exact.setdefault(remote_key, normalised)

    @classmethod
    def from_entries(
        cls,
        entries: Sequence[Tuple[str, str]],
        *,
        remote_prefix: str = "/",
        local_base: Optional[str | Path] = None,
        auto_compile: bool = True,
    ) -> "TracebackMapper":
        mappings: list[RemoteSourceMapping] = []
        for local_path, remote_part in entries:
            remote_path = _join_remote_path(remote_prefix, remote_part)
            mappings.append(RemoteSourceMapping(remote_path, local_path))
            if auto_compile and _compiles_to_mpy(local_path, remote_path):
                mappings.append(
                    RemoteSourceMapping(
                        _replace_suffix(remote_path, ".mpy"),
                        local_path,
                        is_bytecode=True,
                    )
                )
        return cls(mappings, local_base=local_base)

    def map_frame(self, frame: TracebackFrame) -> Optional[TracebackPathMatch]:
        remote_key = _normalise_remote_path(frame.path)
        mapping = self._exact.get(remote_key)
        if mapping is None:
            mapping = self._suffix_match(remote_key)
        if mapping is None:
            return None

        is_bytecode = mapping.is_bytecode or remote_key.endswith(".mpy")
        return TracebackPathMatch(
            remote_path=frame.path,
            remote_line=frame.line,
            local_path=mapping.local_path,
            local_line=None if is_bytecode else frame.line,
            is_bytecode=is_bytecode,
            source_path=mapping.source_path,
        )

    def map_text(self, text: str) -> str:
        """Insert mapping lines below matched traceback frames."""
        out: list[str] = []
        changed = False
        for line in text.splitlines(keepends=True):
            out.append(line)
            frame = parse_traceback_frame(line)
            if frame is None:
                continue
            match = self.map_frame(frame)
            if match is None:
                continue
            out.append(match.format() + _line_ending(line))
            changed = True
        return "".join(out) if changed else text

    def map_text_with_lens(self, text: str, *, context_lines: int = 3) -> str:
        """Insert path mappings and local source context below matched frames."""
        out: list[str] = []
        changed = False
        for line in text.splitlines(keepends=True):
            out.append(line)
            frame = parse_traceback_frame(line)
            if frame is None:
                continue
            match = self.map_frame(frame)
            if match is None:
                continue
            ending = _line_ending(line)
            out.append(match.format() + ending)
            lens = _format_source_lens(match, context_lines=context_lines)
            if lens:
                out.append(ending + lens + ending)
            changed = True
        return "".join(out) if changed else text

    def _suffix_match(self, remote_key: str) -> Optional[RemoteSourceMapping]:
        suffixes = _remote_suffixes(remote_key)
        if not suffixes:
            return None

        matches: list[RemoteSourceMapping] = []
        for mapping in self._mappings:
            local = mapping.local_path.replace("\\", "/")
            if any(local == suffix or local.endswith("/" + suffix) for suffix in suffixes):
                matches.append(mapping)

        wants_bytecode = remote_key.endswith(".mpy")
        preferred = [
            mapping for mapping in matches
            if mapping.is_bytecode == wants_bytecode
        ]
        if preferred:
            matches = preferred

        deduped: dict[tuple[str, bool], RemoteSourceMapping] = {}
        for mapping in matches:
            deduped.setdefault((mapping.local_path, mapping.is_bytecode), mapping)
        matches = list(deduped.values())

        if len(matches) == 1:
            return matches[0]
        return None


def build_project_traceback_mapper(
    local_dir: str = ".",
    remote_prefix: str = "/",
    *,
    manifest_path: Optional[str] = None,
    active_tags: Optional[Set[str]] = None,
    auto_compile: bool = True,
    auto_manifest: bool = False,
) -> TracebackMapper:
    """Build a mapper from a project directory and optional manifest."""
    base = Path(local_dir).resolve()
    manifest = _resolve_manifest_path(base, manifest_path, auto_manifest)
    if manifest is not None:
        entries = load_manifest(str(manifest), active_tags or set(), base_dir=str(base))
    else:
        entries = _collect_source_entries(base)
    return TracebackMapper.from_entries(
        entries,
        remote_prefix=remote_prefix,
        local_base=base,
        auto_compile=auto_compile,
    )


def create_traceback_output_mapper(
    local_dir: str = ".",
    remote_prefix: str = "/",
    *,
    manifest_path: Optional[str] = None,
    active_tags: Optional[Set[str]] = None,
    auto_compile: bool = True,
    auto_manifest: bool = False,
    lens: bool = False,
    context_lines: int = 3,
    open_editor: bool = False,
):
    mapper = build_project_traceback_mapper(
        local_dir,
        remote_prefix,
        manifest_path=manifest_path,
        active_tags=active_tags,
        auto_compile=auto_compile,
        auto_manifest=auto_manifest,
    )
    if lens:
        opened: set[tuple[str, int]] = set()

        def map_with_lens(text: str) -> str:
            mapped = mapper.map_text_with_lens(
                text,
                context_lines=context_lines,
            )
            if open_editor:
                notice = _open_editor_for_first_match(mapper, text, opened)
                if notice:
                    mapped += _line_ending(mapped) + notice + _line_ending(mapped)
            return mapped

        return map_with_lens
    return mapper.map_text


def _resolve_manifest_path(
    base: Path,
    manifest_path: Optional[str],
    auto_manifest: bool,
) -> Optional[Path]:
    if manifest_path:
        return Path(manifest_path).resolve()
    if auto_manifest:
        candidate = base / "manifest.py"
        if candidate.exists():
            return candidate
    return None


def _collect_source_entries(base: Path) -> list[Tuple[str, str]]:
    entries: list[Tuple[str, str]] = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [name for name in dirs if name not in _IGNORED_DIRS]
        for name in files:
            if not name.endswith(".py") or name == "manifest.py":
                continue
            local_path = Path(root) / name
            remote_part = local_path.relative_to(base).as_posix()
            entries.append((str(local_path), remote_part))
    return entries


def _join_remote_path(remote_prefix: str, remote_part: str) -> str:
    prefix = str(remote_prefix or "/").replace("\\", "/")
    part = str(remote_part or "").replace("\\", "/")
    if part.startswith("/"):
        return _normalise_remote_path(part)
    return _normalise_remote_path(posixpath.join(prefix, part))


def _normalise_remote_path(path: str) -> str:
    value = str(path).strip().replace("\\", "/")
    if not value:
        return ""
    if value.startswith("<"):
        return value
    value = re.sub(r"/+", "/", value)
    value = posixpath.normpath(value)
    if value == ".":
        value = ""
    if value.startswith("./"):
        value = value[2:]
    if value and not value.startswith("/"):
        value = "/" + value
    return value


def _local_display_path(local_path: str, base: Optional[Path]) -> str:
    path = Path(local_path)
    if base is not None:
        try:
            return path.resolve().relative_to(base).as_posix()
        except ValueError:
            pass
    return str(local_path).replace("\\", "/")


def _local_source_path(local_path: str, base: Optional[Path]) -> str:
    path = Path(local_path)
    if not path.is_absolute() and base is not None:
        path = base / path
    return str(path.resolve())


def _format_source_lens(
    match: TracebackPathMatch,
    *,
    context_lines: int = 3,
) -> str:
    if match.is_bytecode or match.local_line is None or not match.source_path:
        return ""

    try:
        lines = Path(match.source_path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ""
    if not lines:
        return ""

    line_no = max(1, match.local_line)
    if line_no > len(lines):
        return ""
    radius = max(0, int(context_lines))
    start = max(1, line_no - radius)
    end = min(len(lines), line_no + radius)
    width = len(str(end))
    out = [match.local_path]
    for current in range(start, end + 1):
        marker = ">" if current == line_no else " "
        out.append(f"{marker} {current:>{width}} | {lines[current - 1]}")
    return "\n".join(out)


def _open_editor_for_first_match(
    mapper: TracebackMapper,
    text: str,
    opened: set[tuple[str, int]],
) -> str:
    for frame in parse_traceback_frames(text):
        match = mapper.map_frame(frame)
        if (
            match is None
            or match.is_bytecode
            or match.local_line is None
            or not match.source_path
        ):
            continue
        key = (match.source_path, match.local_line)
        if key in opened:
            continue
        opened.add(key)
        error = _open_editor(match.source_path, match.local_line)
        if error:
            return f"[lens] open-editor failed: {error}"
        return ""
    return ""


def _open_editor(path: str, line: int) -> str:
    try:
        code = shutil.which("code")
        if code:
            subprocess.Popen(
                [code, "-g", f"{path}:{line}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return ""
        editor = os.environ.get("EDITOR")
        if not editor:
            return "$EDITOR is not set and VS Code 'code' was not found"
        command = shlex.split(editor)
        if not command:
            return "$EDITOR is empty"
        subprocess.Popen(
            command + [path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return ""
    except Exception as exc:
        return str(exc)


def _compiles_to_mpy(local_path: str, remote_path: str) -> bool:
    if not str(local_path).endswith(".py") or not remote_path.endswith(".py"):
        return False
    basename = posixpath.basename(remote_path)
    return basename not in {"main.py", "boot.py"}


def _replace_suffix(path: str, suffix: str) -> str:
    base, _old_suffix = posixpath.splitext(path)
    return base + suffix


def _remote_suffixes(remote_key: str) -> list[str]:
    if not remote_key or remote_key.startswith("<"):
        return []
    remote_rel = remote_key.lstrip("/")
    suffixes = [remote_rel]
    if remote_rel.endswith(".mpy"):
        suffixes.append(remote_rel[:-4] + ".py")
    return suffixes


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return "\n"

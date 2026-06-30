"""Pure helpers for local device filesystem snapshots."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import posixpath
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence


SNAPSHOT_DIR = ".pyrite_snapshots"
MANIFEST_FILE = "manifest.json"
DEFAULT_EXCLUDES = (
    "/log/**",
    "/logs/**",
    "/.pyrite_cache/**",
    "/.pyrite_tests/**",
    "*.tmp",
    "*.log",
)
DEFAULT_MAX_FILE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class SnapshotEntry:
    path: str
    local_path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class SnapshotManifest:
    name: str
    created_at: str
    device: str
    include: list[str]
    exclude: list[str]
    files: list[SnapshotEntry]


@dataclass(frozen=True)
class SnapshotPlanItem:
    path: str
    size: int = 0
    sha256: str = ""
    local_path: str = ""


@dataclass(frozen=True)
class SnapshotDiffPlan:
    add: list[SnapshotPlanItem]
    overwrite: list[SnapshotPlanItem]
    delete: list[SnapshotPlanItem]
    unchanged: list[SnapshotPlanItem]


@dataclass(frozen=True)
class SnapshotRestorePlan(SnapshotDiffPlan):
    dry_run: bool = True


def safe_snapshot_name(name: str) -> str:
    value = str(name).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise ValueError(
            "snapshot name must use letters, numbers, dots, underscores or dashes"
        )
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("snapshot name must not contain path separators")
    return value


def snapshot_path(name: str, *, root: str | Path = SNAPSHOT_DIR) -> Path:
    return Path(root) / safe_snapshot_name(name)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_device_path(path: str) -> str:
    value = str(path).replace("\\", "/").strip()
    if not value:
        return "/"
    value = posixpath.normpath(value)
    if not value.startswith("/"):
        value = "/" + value
    return value


def local_relpath_for_device_path(path: str) -> str:
    normalized = normalize_device_path(path)
    parts = [part for part in normalized.strip("/").split("/") if part]
    if not parts:
        raise ValueError("snapshot file path cannot be device root")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe device path: {path}")
    return posixpath.join("files", *parts)


def save_snapshot_files(
    name: str,
    files: Mapping[str, bytes],
    *,
    root: str | Path = SNAPSHOT_DIR,
    device: str = "",
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    created_at: str | None = None,
) -> SnapshotManifest:
    safe_name = safe_snapshot_name(name)
    target = Path(root) / safe_name
    entries: list[SnapshotEntry] = []
    for raw_path, data in sorted(files.items()):
        remote_path = normalize_device_path(raw_path)
        rel = local_relpath_for_device_path(remote_path)
        local_path = target / rel
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)
        entries.append(
            SnapshotEntry(
                path=remote_path,
                local_path=rel,
                size=len(data),
                sha256=sha256_bytes(data),
            )
        )

    manifest = SnapshotManifest(
        name=safe_name,
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
        device=device,
        include=list(include),
        exclude=list(exclude),
        files=entries,
    )
    write_snapshot_manifest(target, manifest)
    return manifest


def write_snapshot_manifest(path: str | Path, manifest: SnapshotManifest) -> None:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    data = asdict(manifest)
    (target / MANIFEST_FILE).write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_snapshot_manifest(path: str | Path) -> SnapshotManifest:
    raw = json.loads((Path(path) / MANIFEST_FILE).read_text(encoding="utf-8"))
    return SnapshotManifest(
        name=str(raw["name"]),
        created_at=str(raw["created_at"]),
        device=str(raw.get("device", "")),
        include=[str(item) for item in raw.get("include", [])],
        exclude=[str(item) for item in raw.get("exclude", [])],
        files=[
            SnapshotEntry(
                path=normalize_device_path(item["path"]),
                local_path=str(item["local_path"]),
                size=int(item["size"]),
                sha256=str(item["sha256"]),
            )
            for item in raw.get("files", [])
        ],
    )


def filter_device_entries(
    entries: Iterable[Mapping[str, str]],
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> list[Mapping[str, str]]:
    selected: list[Mapping[str, str]] = []
    exclude_patterns = tuple(exclude) + DEFAULT_EXCLUDES
    for entry in entries:
        if entry.get("type") != "F":
            continue
        path = normalize_device_path(str(entry.get("name", "")))
        size = _entry_size(entry)
        if size is None or size > max_file_bytes:
            continue
        if include and not _matches_any(path, include):
            continue
        if _matches_any(path, exclude_patterns):
            continue
        selected.append(entry)
    return selected


def build_current_index(
    entries: Iterable[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    index: dict[str, dict[str, object]] = {}
    for entry in entries:
        path = normalize_device_path(str(entry.get("path") or entry.get("name") or ""))
        if path == "/":
            continue
        index[path] = {
            "size": int(entry.get("size") or 0),
            "sha256": str(entry.get("sha256") or ""),
        }
    return index


def build_diff_plan(
    manifest: SnapshotManifest,
    current: Mapping[str, Mapping[str, object]],
) -> SnapshotDiffPlan:
    snapshot_index = {entry.path: entry for entry in manifest.files}
    current_paths = set(current)
    snapshot_paths = set(snapshot_index)

    add = [_item_from_entry(snapshot_index[path]) for path in sorted(snapshot_paths - current_paths)]
    delete = [
        SnapshotPlanItem(
            path=path,
            size=int(current[path].get("size") or 0),
            sha256=str(current[path].get("sha256") or ""),
        )
        for path in sorted(current_paths - snapshot_paths)
    ]
    overwrite: list[SnapshotPlanItem] = []
    unchanged: list[SnapshotPlanItem] = []
    for path in sorted(snapshot_paths & current_paths):
        entry = snapshot_index[path]
        if str(current[path].get("sha256") or "") == entry.sha256:
            unchanged.append(_item_from_entry(entry))
        else:
            overwrite.append(_item_from_entry(entry))
    return SnapshotDiffPlan(
        add=add,
        overwrite=overwrite,
        delete=delete,
        unchanged=unchanged,
    )


def build_restore_plan(
    manifest: SnapshotManifest,
    current: Mapping[str, Mapping[str, object]],
    *,
    apply: bool = False,
) -> SnapshotRestorePlan:
    plan = build_diff_plan(manifest, current)
    return SnapshotRestorePlan(
        add=plan.add,
        overwrite=plan.overwrite,
        delete=plan.delete,
        unchanged=plan.unchanged,
        dry_run=not apply,
    )


def format_snapshot_plan(plan: SnapshotDiffPlan) -> str:
    lines: list[str] = []
    if isinstance(plan, SnapshotRestorePlan):
        lines.append("DRY-RUN restore plan" if plan.dry_run else "APPLY restore plan")
    lines.extend(_format_plan_group("ADD", plan.add))
    lines.extend(_format_plan_group("OVERWRITE", plan.overwrite))
    lines.extend(_format_plan_group("DELETE", plan.delete))
    lines.extend(_format_plan_group("UNCHANGED", plan.unchanged))
    return "\n".join(lines) if lines else "no changes"


def _format_plan_group(label: str, items: Sequence[SnapshotPlanItem]) -> list[str]:
    if not items:
        return []
    return [f"{label} {item.path} ({item.size} bytes)" for item in items]


def _item_from_entry(entry: SnapshotEntry) -> SnapshotPlanItem:
    return SnapshotPlanItem(
        path=entry.path,
        size=entry.size,
        sha256=entry.sha256,
        local_path=entry.local_path,
    )


def _entry_size(entry: Mapping[str, object]) -> int | None:
    try:
        return int(str(entry.get("size", "")))
    except (TypeError, ValueError):
        return None


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(_matches(path, pattern) for pattern in patterns)


def _matches(path: str, pattern: str) -> bool:
    normalized = normalize_device_path(pattern)
    return fnmatch.fnmatch(path, normalized) or fnmatch.fnmatch(path.lstrip("/"), pattern)

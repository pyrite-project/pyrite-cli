"""Build-time helpers: compilation, preprocessing, and manifests."""

from .compiler import _compile_files_parallel, _compile_to_mpy
from .manifest_loader import ManifestEntry, ManifestPlan, load_manifest, load_manifest_plan
from .manifest_lock import (
    LOCKFILE_NAME,
    ManifestFeatureSummary,
    ManifestLock,
    ManifestLockError,
    ManifestLockModule,
    build_manifest_lock,
    check_manifest_lock_current,
    load_manifest_lock,
    save_manifest_lock,
    write_manifest_lock,
)
from .preprocessor import preprocess

__all__ = [
    "LOCKFILE_NAME",
    "ManifestEntry",
    "ManifestFeatureSummary",
    "ManifestLock",
    "ManifestLockError",
    "ManifestLockModule",
    "ManifestPlan",
    "_compile_files_parallel",
    "_compile_to_mpy",
    "build_manifest_lock",
    "check_manifest_lock_current",
    "load_manifest",
    "load_manifest_lock",
    "load_manifest_plan",
    "preprocess",
    "save_manifest_lock",
    "write_manifest_lock",
]

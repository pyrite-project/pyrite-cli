"""Build-time helpers: compilation, preprocessing, and manifests."""

from .compiler import _compile_files_parallel, _compile_to_mpy
from .manifest_loader import load_manifest
from .preprocessor import preprocess

__all__ = [
    "_compile_files_parallel",
    "_compile_to_mpy",
    "load_manifest",
    "preprocess",
]

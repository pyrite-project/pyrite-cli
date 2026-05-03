from pathlib import Path
from typing import List, Set, Tuple


def load_manifest(
    manifest_path: str, active_tags: Set[str], base_dir: str = None
) -> List[Tuple[str, str]]:
    base = Path(base_dir or Path(manifest_path).parent)
    entries: List[Tuple[str, str]] = []

    def _match(features):
        return not features or bool(set(features) & active_tags)

    def module(filename, remote=None, features=None):
        if _match(features):
            entries.append((str(base / filename), remote or filename))

    def package(dirname, remote=None, features=None):
        if not _match(features):
            return
        for f in (base / dirname).rglob("*.py"):
            rel = str(f.relative_to(base)).replace("\\", "/")
            entries.append((str(f), rel))

    exec(Path(manifest_path).read_text(encoding="utf-8"), {"module": module, "package": package})
    return entries

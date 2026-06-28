from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class PyriteConfig:
    chunk_size: int = 4096
    download_threads: int = 4
    auto_compile: bool = True
    verify: str = "size"
    max_retries: int = 2
    board_tags: Dict[str, List[str]] = field(default_factory=dict)
    baudrate: int = 921600
    timeout: int = 0  # 0 means use CLI default
    delta_flash: str = "auto"
    delta_min_size: int = 10240
    precheck: str = "basic"
    precheck_compat: str = "warn"

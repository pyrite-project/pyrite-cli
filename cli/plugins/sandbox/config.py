"""
沙箱配置与核心数据类型。

定义 SandboxConfig（沙箱配置）、ScanResult（扫描结果）、
SandboxError（沙箱违规异常）以及相关常量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# ═══════════════════════════════════════════════════════════════════════
# 常量：导入分类
# ═══════════════════════════════════════════════════════════════════════

# 始终阻止的导入（任何模式下）
ALWAYS_BLOCKED_IMPORTS: set[str] = {
    "os",
    "subprocess",
    "shutil",
    "ctypes",
    "socket",
    "http.server",
    "smtplib",
    "ftplib",
    "telnetlib",
    "pickle",
    "shelve",
    "marshal",
    "multiprocessing",
    "signal",
    "syslog",
    "pdb",
    "code",
    "codeop",
}

# 危险导入 — strict 阻止，standard 警告，permissive 放行
DANGEROUS_IMPORTS: set[str] = {
    "requests",
    "urllib",
    "urllib2",
    "http",
    "ssl",
    "threading",
    "concurrent.futures",
    "traceback",
    "pyserial",
}

# 网络相关模块（需要 config.network == True 才能导入）
NETWORK_MODULES: set[str] = {
    "socket",
    "http",
    "urllib",
    "requests",
    "httpx",
    "aiohttp",
    "websocket",
    "websockets",
    "ssl",
    "ftplib",
    "smtplib",
    "poplib",
    "imaplib",
    "telnetlib",
    "asyncio",
}

# 子进程相关模块（需要 config.subprocess == True 才能导入）
SUBPROCESS_MODULES: set[str] = {
    "subprocess",
    "os",
    "signal",
    "multiprocessing",
}

# ═══════════════════════════════════════════════════════════════════════
# 常量：危险的调用名称
# ═══════════════════════════════════════════════════════════════════════

# 始终阻止的函数调用（注意：__import__ 不在此列表，
# 导入控制由 sys.meta_path 导入拦截器负责）
ALWAYS_BLOCKED_CALLS: set[str] = {
    "eval",
    "exec",
    "compile",
    "breakpoint",
}

# 危险调用 — strict/standard 阻止，permissive 警告
DANGEROUS_CALLS: set[str] = {
    "open",  # 在 strict/standard 中阻止写模式
}

# ═══════════════════════════════════════════════════════════════════════
# 常量：危险的属性链
# ═══════════════════════════════════════════════════════════════════════

# 始终阻止的属性访问链
BLOCKED_ATTR_CHAINS: set[str] = {
    "os.system",
    "os.popen",
    "os.execl",
    "os.execle",
    "os.execlp",
    "os.execv",
    "os.execve",
    "os.execvp",
    "os.spawnl",
    "os.spawnle",
    "os.spawnlp",
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "shutil.rmtree",
    "shutil.copy",
    "shutil.copy2",
    "shutil.copytree",
    "shutil.move",
    "ctypes.CDLL",
    "ctypes.WinDLL",
    "ctypes.pythonapi",
}

# ═══════════════════════════════════════════════════════════════════════
# 常量：安全默认值
# ═══════════════════════════════════════════════════════════════════════

# 默认允许的顶级模块（合法插件必需的框架和内部模块）
DEFAULT_ALLOWED_TOP_LEVEL: set[str] = {
    "typer",
    "click",
    "rich",
    "cli",
    "abc",
    "array",
    "ast",
    "base64",
    "binascii",
    "bisect",
    "calendar",
    "cmath",
    "collections",
    "copy",
    "csv",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "fnmatch",
    "fractions",
    "functools",
    "glob",
    "hashlib",
    "heapq",
    "importlib",
    "inspect",
    "io",
    "itertools",
    "json",
    "keyword",
    "linecache",
    "math",
    "operator",
    "pathlib",
    "pprint",
    "random",
    "re",
    "statistics",
    "string",
    "struct",
    "textwrap",
    "time",
    "types",
    "typing",
    "typing_extensions",
    "unicodedata",
    "uuid",
    "warnings",
    "weakref",
    "xml",
    "zipfile",
    "gzip",
    "tarfile",
    "argparse",
    "configparser",
    "getpass",
    "logging",
    "platform",
    "secrets",
    "tempfile",
}

# 沙箱最大 AST 深度
MAX_AST_DEPTH: int = 30

# 最大源码大小（字符数）
MAX_SOURCE_SIZE: int = 500_000

# 默认超时（秒）
DEFAULT_TIMEOUT_SEC: int = 30

# 默认递归深度上限
DEFAULT_RECURSION_LIMIT: int = 1000


# ═══════════════════════════════════════════════════════════════════════
# 数据类型
# ═══════════════════════════════════════════════════════════════════════


class SandboxError(Exception):
    """沙箱违规异常。

    当插件代码违反沙箱安全策略时抛出，包含插件名和防御层信息便于诊断。
    """

    def __init__(self, message: str, plugin_name: str = "", layer: str = "") -> None:
        full_msg = message
        if plugin_name:
            full_msg = f"[{plugin_name}] {full_msg}"
        if layer:
            full_msg = f"[{layer}] {full_msg}"
        super().__init__(full_msg)
        self.plugin_name = plugin_name
        self.layer = layer
        self.message = message


@dataclass
class SandboxConfig:
    """插件沙箱配置。

    从 plugin.json 加载并与默认值合并。若无 plugin.json，所有字段使用默认值，
    对应 standard 模式 — 对合法插件零影响。
    """

    mode: str = "standard"  # strict | standard | permissive
    network: bool = False
    filesystem_read: list[str] = field(default_factory=list)
    filesystem_write: bool = False
    subprocess: bool = False
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    recursion_limit: int = DEFAULT_RECURSION_LIMIT
    allowed_imports: list[str] = field(default_factory=list)
    blocked_imports: list[str] = field(default_factory=list)
    allow_builtins: list[str] = field(default_factory=list)

    # ── 派生集合（便于快速查找） ──

    @property
    def allowed_import_set(self) -> set[str]:
        """允许的导入集合（默认安全模块 + 用户声明额外允许）。"""
        return DEFAULT_ALLOWED_TOP_LEVEL | set(self.allowed_imports)

    @property
    def blocked_import_set(self) -> set[str]:
        """用户显式阻止的额外导入。"""
        return set(self.blocked_imports)

    @property
    def allow_builtins_set(self) -> set[str]:
        """用户显式放行的内置函数。"""
        return set(self.allow_builtins)


@dataclass
class ScanResult:
    """AST 预扫描结果。

    包含扫描发现的所有问题，以及对插件是否可以通过的判断。
    """

    passed: bool = True
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    dangerous_imports: list[str] = field(default_factory=list)
    suspicious_calls: list[str] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def warning_count(self) -> int:
        return len(self.warnings)

    def add_error(self, line: int, col: int, message: str, code: str = "") -> None:
        """添加一个阻止性错误。"""
        self.errors.append({
            "line": line,
            "col": col,
            "message": message,
            "code": code,
        })
        self.passed = False

    def add_warning(self, line: int, col: int, message: str, code: str = "") -> None:
        """添加一个警告（不阻止加载）。"""
        self.warnings.append({
            "line": line,
            "col": col,
            "message": message,
            "code": code,
        })

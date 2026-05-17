"""扫描项目 imports 和 pyproject.toml，自动生成 requirements.txt。"""
import ast
import subprocess
import sys
from pathlib import Path
from typing import Dict, Set

ROOT = Path(__file__).resolve().parent.parent

# 标准库模块（Python 3.10+），用于过滤掉非三方依赖
STDLIB = {
    "abc", "ast", "binascii", "concurrent", "contextlib", "dataclasses",
    "enum", "gc", "getpass", "hashlib", "inspect", "io", "json", "math",
    "msvcrt", "os", "pathlib", "platform", "re", "select", "shutil",
    "subprocess", "sys", "tempfile", "termios", "textwrap", "time", "tty",
    "typing", "unicodedata", "uuid",
}

# 已知由其他包提供的隐式依赖（不需单独列出）
TRANSIENT = {"typer_slim", "mypy_extensions", "setuptools"}

# MicroPython 设备端模块（测试脚本用，非主机依赖）
MICROPYTHON = {"machine", "network", "ubinascii"}

# 本地项目模块（排除误检）
LOCAL_MODULES = {
    "cli", "utils", "project", "test",
    "flash", "ansi", "types", "config", "transport",
    "serial_transport", "webrepl_transport",
    "compiler", "preprocessor", "manifest_loader",
    "selector", "firmware", "webrepl_micropython",
    "stubs", "project", "sync",
}

# import 名 → PyPI 包名映射
IMPORT_TO_PACKAGE = {
    "serial": "pyserial",
    "websocket": "websocket-client",
    "mpy_cross": "mpy-cross",
    "tomllib": "tomli",
    "cst": "libcst",
    "yaml": "pyyaml",
}


def get_pip_freeze() -> Dict[str, str]:
    result = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--format=freeze"],
        capture_output=True, text=True
    )
    versions = {}
    for line in result.stdout.strip().splitlines():
        if "==" in line:
            name, ver = line.split("==", 1)
            versions[name.lower()] = ver
    return versions


def get_declared_deps(pyproject: Path) -> Dict[str, str]:
    """从 pyproject.toml 读取 dependencies 和 optional-dependencies。"""
    import tomli
    raw = pyproject.read_text(encoding="utf-8")
    data = tomli.loads(raw)
    deps: Dict[str, str] = {}
    for dep in data["project"].get("dependencies", []):
        # 处理带环境标记的依赖：`tomli>=2.0.0; python_version < '3.11'`
        parts = dep.split(";", 1)
        name_ver = parts[0].strip()
        marker = parts[1].strip() if len(parts) > 1 else ""
        name = name_ver.split(">=")[0].split("==")[0].split("~=")[0].split("!=")[0].strip()
        deps[name.lower()] = marker
    # 可选依赖
    for group, items in data["project"].get("optional-dependencies", {}).items():
        for dep in items:
            parts = dep.split(";", 1)
            name_ver = parts[0].strip()
            name = name_ver.split(">=")[0].split("==")[0].strip()
            deps[name.lower()] = f"# optional / {group}"
    return deps


def find_third_party_imports(src_dirs) -> Set[str]:
    """遍历源码目录收集所有第三方 import，映射到 PyPI 包名。"""
    imports: Set[str] = set()
    for src in src_dirs:
        for py in src.rglob("*.py"):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0].lower()
                        if top in STDLIB | MICROPYTHON | LOCAL_MODULES:
                            continue
                        imports.add(IMPORT_TO_PACKAGE.get(top, top))
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        top = node.module.split(".")[0].lower()
                        if top in STDLIB | MICROPYTHON | LOCAL_MODULES:
                            continue
                        imports.add(IMPORT_TO_PACKAGE.get(top, top))
    return imports


def main():
    pip_versions = get_pip_freeze()
    declared = get_declared_deps(ROOT / "pyproject.toml")
    actual_imports = find_third_party_imports([ROOT / "cli", ROOT / "test"])

    print("=== pyproject.toml 声明的依赖 ===")
    for name, marker in sorted(declared.items()):
        ver = pip_versions.get(name, "?")
        print(f"  {name} (installed={ver}) marker={marker}")

    print("\n=== 源码中检测到的第三方 import ===")
    for name in sorted(actual_imports):
        ver = pip_versions.get(name, "?")
        flag = "  DECLARED" if name in declared else "  NOT IN pyproject"
        print(f"  {name} (installed={ver}){flag}")

    # 生成 requirements.txt
    lines = [
        "# ============================================================",
        "# requirements.txt — pyrite-cli 依赖锁定",
        "# 由 scripts/gen_requirements.py 自动生成，手动修改将被覆盖",
        "# 用法：pip install -r requirements.txt",
        "# ============================================================",
        "",
    ]

    # 运行时核心依赖（按 pyproject.toml 顺序 + 检测到的补充）
    order = [
        "typer", "click",         # CLI
        "pyserial",               # 串口
        "requests",               # HTTP
        "tqdm",                   # 进度条
        "libcst",                 # CST 条件编译
        "websocket-client",       # WebREPL
        "esptool",                # 固件烧录
        "mpy-cross",              # .py → .mpy 编译
    ]
    written = set()

    # pip freeze 对包名做了规范化（- → _），需要双向查找
    def pip_ver(pkg: str) -> str:
        v = pip_versions.get(pkg)
        if v:
            return v
        return pip_versions.get(pkg.replace("-", "_"), "")

    lines.append("# ----- 运行时核心依赖 -----")
    for name in order:
        ver = pip_ver(name)
        if ver:
            lines.append(f"{name}=={ver}")
            written.add(name.lower())

    # tomli：条件依赖（Python < 3.11）
    if "tomli" in declared:
        ver = pip_ver("tomli")
        lines.append("")
        lines.append("# ----- 条件依赖 -----")
        lines.append(f"tomli=={ver}; python_version < '3.11'")
        written.add("tomli")

    # 开发依赖
    lines.append("")
    lines.append("# ----- 开发依赖 -----")
    for name in ["pytest", "mypy"]:
        ver = pip_ver(name)
        tag = f"=={ver}" if ver else ""
        lines.append(f"# {name}{tag}")

    # 构建依赖
    lines.append("")
    lines.append("# ----- 构建依赖 -----")
    lines.append("# build")
    lines.append("# setuptools>=64.0")

    # 检测到但未归类的新依赖（警告）
    all_mentioned = written | {"pytest", "mypy", "build", "setuptools"}
    extra = actual_imports - all_mentioned - TRANSIENT
    if extra:
        lines.append("")
        lines.append("# ⚠️  以下依赖出现在 import 中但未归类，请人工确认：")
        for name in sorted(extra):
            lines.append(f"# {name} (imported but uncategorized)")

    output = ROOT / "requirements.txt"
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nDone. Written to {output}")


if __name__ == "__main__":
    main()

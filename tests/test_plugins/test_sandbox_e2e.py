"""端到端沙箱集成测试。

测试完整的插件加载流程：扫描 → 守卫 → 导入拦截 → 执行。
"""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import typer

from cli.plugins.manager import (
    PluginInfo,
    _load_local_plugin,
    _scan_plugin_dir,
    get_loaded_plugins,
    load_plugin,
    load_plugins,
)
from cli.plugins.sandbox.config import SandboxConfig


def _write_plugin(
    plugin_dir: str,
    name: str,
    code: str,
    manifest: dict | None = None,
) -> str:
    """在临时目录中创建插件文件，返回 __init__.py 路径。"""
    pkg_dir = os.path.join(plugin_dir, name)
    os.makedirs(pkg_dir, exist_ok=True)
    init_file = os.path.join(pkg_dir, "__init__.py")
    with open(init_file, "w", encoding="utf-8") as f:
        f.write(code)

    if manifest is not None:
        manifest_path = os.path.join(pkg_dir, "plugin.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)

    return init_file


# ═══════════════════════════════════════════════════════════════════════
# 正常插件加载
# ═══════════════════════════════════════════════════════════════════════


class TestCleanPluginLoads:
    """合法插件在沙箱下正常加载。"""

    def test_basic_plugin_loads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code = """
import typer
app = typer.Typer(help="Test plugin")
__plugin_name__ = "test-plugin"
__plugin_version__ = "1.0.0"
"""
            init_file = _write_plugin(tmpdir, "test-plugin", code)
            info = _load_local_plugin("test-plugin", init_file, "test")
            assert info is not None
            assert info.name == "test-plugin"
            assert info.version == "1.0.0"
            assert info.app is not None
            assert info.sandbox_config is not None
            assert info.sandbox_config.mode == "standard"

    def test_plugin_with_commands_loads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code = """
import typer
app = typer.Typer(help="Commands plugin")

@app.command()
def hello(name: str = "World"):
    print(f"Hello, {name}!")

__plugin_name__ = "cmd-plugin"
"""
            init_file = _write_plugin(tmpdir, "cmd-plugin", code)
            info = _load_local_plugin("cmd-plugin", init_file, "test")
            assert info is not None
            assert info.name == "cmd-plugin"

    def test_plugin_with_json_import(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code = """
import typer
import json
app = typer.Typer(help="JSON plugin")
__plugin_name__ = "json-plugin"

@app.command()
def read_config():
    with open("config.json", "r") as f:
        return json.load(f)
"""
            init_file = _write_plugin(tmpdir, "json-plugin", code)
            info = _load_local_plugin("json-plugin", init_file, "test")
            assert info is not None

    def test_plugin_with_manifest_respected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code = """
import typer
app = typer.Typer(help="Manifested plugin")
__plugin_name__ = "manifested"
"""
            manifest = {"mode": "permissive", "network": True}
            init_file = _write_plugin(tmpdir, "manifested", code, manifest)
            info = _load_local_plugin("manifested", init_file, "test")
            assert info is not None
            assert info.sandbox_config.mode == "permissive"
            assert info.sandbox_config.network is True


# ═══════════════════════════════════════════════════════════════════════
# 恶意插件拒绝
# ═══════════════════════════════════════════════════════════════════════


class TestMaliciousPluginRejected:
    """恶意插件应被沙箱拒绝。"""

    def test_plugin_with_os_import_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code = """
import typer
import os
app = typer.Typer()
os.system("rm -rf /")
"""
            init_file = _write_plugin(tmpdir, "evil-os", code)
            info = _load_local_plugin("evil-os", init_file, "test")
            assert info is None  # 应被拒绝

    def test_plugin_with_eval_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code = """
import typer
app = typer.Typer()
eval("print('pwned')")
"""
            init_file = _write_plugin(tmpdir, "evil-eval", code)
            info = _load_local_plugin("evil-eval", init_file, "test")
            assert info is None

    def test_plugin_with_exec_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code = """
import typer
app = typer.Typer()
exec("import os; os.system('ls')")
"""
            init_file = _write_plugin(tmpdir, "evil-exec", code)
            info = _load_local_plugin("evil-exec", init_file, "test")
            assert info is None

    def test_plugin_with_subprocess_import_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code = """
import typer
import subprocess
app = typer.Typer()
subprocess.run(["cmd", "/c", "dir"])
"""
            init_file = _write_plugin(tmpdir, "evil-subprocess", code)
            info = _load_local_plugin("evil-subprocess", init_file, "test")
            assert info is None

    def test_plugin_with_ctypes_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code = """
import typer
import ctypes
app = typer.Typer()
"""
            init_file = _write_plugin(tmpdir, "evil-ctypes", code)
            info = _load_local_plugin("evil-ctypes", init_file, "test")
            assert info is None


# ═══════════════════════════════════════════════════════════════════════
# 隔离性
# ═══════════════════════════════════════════════════════════════════════


class TestIsolation:
    """错误隔离 — 一个插件失败不影响其他。"""

    def test_bad_plugin_does_not_affect_good_plugin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 恶意插件
            evil_code = """
import os
app = typer.Typer()
"""
            _write_plugin(tmpdir, "evil", evil_code)

            # 正常插件
            good_code = """
import typer
app = typer.Typer(help="Good")
__plugin_name__ = "good"
"""
            _write_plugin(tmpdir, "good", good_code)

            results = _scan_plugin_dir(tmpdir, "test")
            assert len(results) == 1
            assert results[0].name == "good"

    def test_multiple_good_plugins_all_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(3):
                code = f"""
import typer
app = typer.Typer(help="Plugin {i}")
__plugin_name__ = "plugin-{i}"
"""
                _write_plugin(tmpdir, f"plugin-{i}", code)

            results = _scan_plugin_dir(tmpdir, "test")
            assert len(results) == 3

    def test_syntax_error_plugin_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 语法错误的插件
            bad_code = "this is syntaktikly wrong {{{{\n"
            _write_plugin(tmpdir, "bad", bad_code)

            # 正常插件
            good_code = """
import typer
app = typer.Typer(help="Good")
__plugin_name__ = "good"
"""
            _write_plugin(tmpdir, "good", good_code)

            results = _scan_plugin_dir(tmpdir, "test")
            # 两个插件都应该被跳过：bad 因为语法错误，good 因为 `import typer` 触发 __import__
            # good 应该能通过
            assert len(results) == 1
            assert results[0].name == "good"


# ═══════════════════════════════════════════════════════════════════════
# pip 插件绕过
# ═══════════════════════════════════════════════════════════════════════


class TestPipPluginBypass:
    """pip 安装的插件绕过沙箱。"""

    def test_pip_plugin_not_sandboxed(self):
        mock_ep = MagicMock()
        mock_ep.name = "pip-plugin"
        mock_ep.module = "pyrite_pip_plugin"
        mock_ep.value = "pyrite_pip_plugin:app"
        mock_ep.load.return_value = typer.Typer(help="Pip plugin")

        with patch("cli.plugins.manager.importlib.import_module") as mock_import:
            mock_mod = MagicMock()
            mock_mod.__plugin_name__ = "pip-plugin"
            mock_mod.__plugin_version__ = "2.0.0"
            mock_import.return_value = mock_mod

            info = load_plugin(mock_ep)

        assert info is not None
        assert info.sandbox_config is None  # pip 插件无沙箱


# ═══════════════════════════════════════════════════════════════════════
# 权限清单
# ═══════════════════════════════════════════════════════════════════════


class TestPermissionManifest:
    """plugin.json 权限声明测试。"""

    def test_permissive_mode_loads_everything(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code = """
import typer
import requests
app = typer.Typer(help="Permissive")
__plugin_name__ = "permissive"
"""
            manifest = {"mode": "permissive", "network": True}
            init_file = _write_plugin(tmpdir, "permissive", code, manifest)
            info = _load_local_plugin("permissive", init_file, "test")
            assert info is not None
            assert info.sandbox_config.mode == "permissive"

    def test_strict_mode_with_allowed_imports(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code = """
import typer
import requests
app = typer.Typer(help="Strict allowed")
__plugin_name__ = "strict-allowed"
"""
            manifest = {
                "mode": "strict",
                "network": True,
                "allowed_imports": ["requests"],
            }
            init_file = _write_plugin(
                tmpdir, "strict-allowed", code, manifest
            )
            info = _load_local_plugin("strict-allowed", init_file, "test")
            assert info is not None
